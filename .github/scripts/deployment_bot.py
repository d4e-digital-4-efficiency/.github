"""
Bot de déploiement automatique du module d4e_construction.

Surveille les releases du repo source (d4e-common-def), puis crée/met à jour
des PRs sur tous les repos ElvyBat de l'organisation pour upgrader le module.
Met à jour un tableau de bord (issue GitHub) avec l'état de chaque projet.

Reproduit le fonctionnement du projet C# D4E.Deployment.Bot (AWS Lambda).

Équivalences C# → Python :
┌──────────────────────────────────────────┬──────────────────────────────────────────────────┐
│ C# (D4E.Deployment.Bot)                 │ Python (deployment_bot.py)                       │
├──────────────────────────────────────────┼──────────────────────────────────────────────────┤
│ DeploymentService.ProcessAsync()         │ main()                                           │
│ DeploymentService.HandleRepositoryAsync()│ handle_repository()                              │
│ RepositoryService  (Octokit)             │ gh_api(), gh_api_paginated() — API REST urllib   │
│ GitService         (LibGit2Sharp)        │ clone_repo(), checkout_branch(), … — git CLI     │
│ UnzipSourcesHelper                       │ replicate_with_source_stream()                   │
│ ModuleVersionHelper                      │ get_version_from_manifest()                      │
│ DashboardService                         │ generate_dashboard()                             │
│ IssueService                             │ update_dashboard_issue()                         │
│ RepositoryBranchExtensions               │ find_production_branch(), find_staging_branch()  │
│ GitReleaseExtensions                     │ latest_release_for_major(), new_releases_from()  │
│ SemanticVersionExtensions                │ parse_version(), module_version()                │
│ ReleaseNotesHelper                       │ compile_release_notes()                          │
│ OdooManifestVersion                      │ tuple (odoo_maj, odoo_min, mod_maj, mod_min, p)  │
│ AppOptions / GitHubOptions               │ Variables d'environnement (voir ci-dessous)      │
│ AWS Lambda scheduled trigger             │ workflow_dispatch (déclenchement manuel)          │
│ GitHubCredentialStore                    │ Token injecté dans l'URL de clone                │
│ DashboardDetail                          │ dict {name, url, manager, prod_version, …}       │
│ Custom property "elvybat-project"        │ is_elvybat_project() — GET /properties/values    │
│ Custom property "project-manager"        │ get_project_manager() — GET /properties/values   │
└──────────────────────────────────────────┴──────────────────────────────────────────────────┘

Variables d'environnement requises :
  GH_TOKEN         — PAT GitHub (scope repo + project)
  SOURCE_OWNER     — Organisation (défaut : d4e-digital-4-efficiency)
  SOURCE_REPO      — Repo source des releases (défaut : d4e-common-def)
  BOT_BRANCH       — Nom de la branche créée par le bot (défaut : bot/update-d4e_construction)
  DASHBOARD_ISSUE  — Numéro de l'issue dashboard (défaut : 1)
  GIT_USERNAME     — Auteur des commits (défaut : marc-d4e)
  GIT_EMAIL        — Email des commits (défaut : marc@digital4efficiency.ch)
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

# ── Configuration ──────────────────────────────────────────────────────────────

GH_TOKEN = os.environ["GH_TOKEN"]
SOURCE_OWNER = os.environ.get("SOURCE_OWNER", "d4e-digital-4-efficiency")
SOURCE_REPO = os.environ.get("SOURCE_REPO", "d4e-common-def")
BOT_BRANCH = os.environ.get("BOT_BRANCH", "bot/update-d4e_construction")
DASHBOARD_ISSUE = int(os.environ.get("DASHBOARD_ISSUE", "1"))
GIT_USERNAME = os.environ.get("GIT_USERNAME", "marc-d4e")
GIT_EMAIL = os.environ.get("GIT_EMAIL", "marc@digital4efficiency.ch")

API = "https://api.github.com"
HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "d4e-deployment-bot",
}

# ── Helpers HTTP ───────────────────────────────────────────────────────────────

def gh_api(path, method="GET", body=None, extra_headers=None):
    """Appel générique à l'API GitHub REST."""
    url = path if path.startswith("http") else f"{API}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {**HEADERS, **(extra_headers or {})}
    if body:
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        print(f"  ⚠ API {method} {url} → {e.code}: {error_body[:300]}")
        raise


def gh_api_paginated(path):
    """Récupère toutes les pages d'un endpoint paginé."""
    results = []
    page = 1
    while True:
        sep = "&" if "?" in path else "?"
        data = gh_api(f"{path}{sep}per_page=100&page={page}")
        if not data:
            break
        results.extend(data)
        if len(data) < 100:
            break
        page += 1
    return results


def gh_download_stream(url):
    """Télécharge un flux brut (zipball) depuis GitHub."""
    req = Request(url, headers=HEADERS)
    with urlopen(req) as resp:
        return io.BytesIO(resp.read())


# ── Parsing de version ─────────────────────────────────────────────────────────

VERSION_RE = re.compile(
    r"(?P<odoo_major>\d+)\.(?P<odoo_minor>\d+)\."
    r"(?P<mod_major>\d+)\.(?P<mod_minor>\d+)\.(?P<mod_patch>\d+)"
)

MANIFEST_VERSION_RE = re.compile(
    r"'version'\s*:\s*'"
    r"((?:\d+)\.(?:\d+)\.(?:\d+)\.(?:\d+)\.(?:\d+))"
    r"'",
    re.IGNORECASE,
)

MANIFEST_LEGACY_VERSION_RE = re.compile(
    r"'version'\s*:\s*'"
    r"(\d+)\.(?:\d+)\.(?:\d+)"
    r"'",
    re.IGNORECASE,
)


def parse_version(version_str):
    """Parse une chaîne de version en tuple (odoo_major, odoo_minor, mod_major, mod_minor, mod_patch)."""
    if not version_str:
        return None
    cleaned = version_str.replace("construction-v", "")
    m = VERSION_RE.search(cleaned)
    if not m:
        return None
    return (
        int(m.group("odoo_major")),
        int(m.group("odoo_minor")),
        int(m.group("mod_major")),
        int(m.group("mod_minor")),
        int(m.group("mod_patch")),
    )


def module_version(v):
    """Retourne le tuple (mod_major, mod_minor, mod_patch) pour comparaison."""
    return (v[2], v[3], v[4])


def version_str(v):
    """Formate un tuple de version en chaîne."""
    return f"{v[0]}.{v[1]}.{v[2]}.{v[3]}.{v[4]}"


def get_version_from_manifest(content):
    """Extrait la version depuis le contenu d'un __manifest__.py."""
    if not content:
        return None
    m = MANIFEST_VERSION_RE.search(content)
    if m:
        return m.group(1)
    # Format legacy : 14.0.0 → 14.0.0.0.0
    m = MANIFEST_LEGACY_VERSION_RE.search(content)
    if m:
        return f"{m.group(1)}.0.0.0.0"
    return None


# ── Branches ───────────────────────────────────────────────────────────────────

def find_production_branch(branches):
    """Trouve la branche de production (commence par 'prod')."""
    for b in branches:
        if b["name"].lower().startswith("prod"):
            return b["name"]
    return None


def _parse_rec_date(name):
    """Parse la date d'une branche rec-YYMMDD."""
    pos = name.index("-") + 1
    date_str = name[pos:pos + 6]
    try:
        return datetime.strptime(date_str, "%y%m%d")
    except ValueError:
        return None


def find_staging_branch(branches):
    """Trouve la branche de recette la plus récente (rec-YYMMDD)."""
    candidates = []
    for b in branches:
        if b["name"].startswith("rec-"):
            d = _parse_rec_date(b["name"])
            if d:
                candidates.append((d, b["name"]))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


# ── Custom properties ──────────────────────────────────────────────────────────

def get_custom_properties(owner, repo_name):
    """Récupère les custom properties d'un repo."""
    try:
        return gh_api(f"/repos/{owner}/{repo_name}/properties/values")
    except HTTPError:
        return []


def is_elvybat_project(owner, repo_name):
    """Vérifie si le repo est un projet ElvyBat."""
    props = get_custom_properties(owner, repo_name)
    for p in props:
        if p.get("property_name") == "elvybat-project" and p.get("value") == "true":
            return True
    return False


def get_project_manager(owner, repo_name):
    """Récupère le chef de projet depuis les custom properties."""
    props = get_custom_properties(owner, repo_name)
    for p in props:
        if p.get("property_name") == "project-manager":
            return p.get("value")
    return None


# ── Manifest ───────────────────────────────────────────────────────────────────

def get_manifest_content(owner, repo_name, branch, module="d4e_construction"):
    """Récupère le contenu du __manifest__.py d'un module sur une branche."""
    try:
        data = gh_api(f"/repos/{owner}/{repo_name}/contents/{module}/__manifest__.py?ref={branch}")
        if data and data.get("encoding") == "base64":
            import base64
            return base64.b64decode(data["content"]).decode()
        return data.get("content", "")
    except HTTPError:
        return None


# ── Releases ───────────────────────────────────────────────────────────────────

def get_construction_releases(owner, repo_name):
    """Récupère toutes les releases construction-v* non-prerelease."""
    releases = gh_api_paginated(f"/repos/{owner}/{repo_name}/releases")
    result = []
    for r in releases:
        tag = r.get("tag_name", "")
        if tag.startswith("construction-v") and not r.get("prerelease", False):
            v = parse_version(tag)
            if v:
                result.append((v, r))
    return result


def latest_release_for_major(releases, odoo_major):
    """Retourne la release la plus récente pour une version majeure Odoo donnée."""
    candidates = [(v, r) for v, r in releases if v[0] == odoo_major]
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0][1], module_version(x[0])), reverse=True)
    return candidates[0]


def new_releases_from(releases, from_version):
    """Retourne les releases plus récentes que from_version (même major Odoo)."""
    result = []
    for v, r in releases:
        if v[0] == from_version[0] and module_version(v) > module_version(from_version):
            result.append((v, r))
    result.sort(key=lambda x: (x[0][1], module_version(x[0])), reverse=True)
    return result


# ── Opérations Git (CLI) ──────────────────────────────────────────────────────

def _git(repo_path, *args):
    """Exécute une commande git dans le répertoire donné."""
    result = subprocess.run(
        ["git"] + list(args),
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  git {' '.join(args)} → code {result.returncode}: {result.stderr.strip()}")
    return result


def clone_repo(clone_url, dest):
    """Clone un repo avec authentification par token."""
    auth_url = clone_url.replace("https://", f"https://x-access-token:{GH_TOKEN}@")
    subprocess.run(["git", "clone", auth_url, dest], capture_output=True, text=True, check=True)


def checkout_branch(repo_path, branch):
    """Checkout et pull d'une branche existante."""
    _git(repo_path, "fetch", "origin", "--prune")
    _git(repo_path, "checkout", branch)
    _git(repo_path, "reset", "--hard", f"origin/{branch}")
    _git(repo_path, "pull", "origin", branch)


def create_branch(repo_path, branch):
    """Crée une branche locale depuis la branche courante, push et pull."""
    # Supprimer la branche locale si elle existe déjà
    _git(repo_path, "branch", "-D", branch)
    _git(repo_path, "checkout", "-b", branch)
    # Push (peut échouer si la branche existe déjà sur remote, c'est OK)
    _git(repo_path, "push", "-u", "origin", branch)
    _git(repo_path, "pull", "origin", branch)


def stage_all(repo_path):
    """Stage tous les changements."""
    _git(repo_path, "add", "--all")


def is_dirty(repo_path):
    """Vérifie s'il y a des changements non commités."""
    result = _git(repo_path, "status", "--porcelain")
    return bool(result.stdout.strip())


def commit_and_push(repo_path, branch, message):
    """Commit et push les changements."""
    _git(repo_path, "-c", f"user.name={GIT_USERNAME}", "-c", f"user.email={GIT_EMAIL}",
         "commit", "-m", message)
    _git(repo_path, "push", "origin", branch)


# ── Extraction du zipball ──────────────────────────────────────────────────────

def replicate_with_source_stream(repo_path, zipball_stream):
    """
    Remplace les modules d4e_construction* dans le repo par ceux du zipball.
    Reproduit exactement la logique C# UnzipSourcesHelper.
    """
    # Identifier les modules à mettre à jour
    modules_to_update = []
    for entry in os.listdir(repo_path):
        full_path = os.path.join(repo_path, entry)
        if os.path.isdir(full_path) and entry.startswith("d4e_construction"):
            modules_to_update.append(entry)
            shutil.rmtree(full_path)

    if not modules_to_update:
        print("  Aucun module d4e_construction trouvé dans le repo")
        return

    print(f"  Modules à mettre à jour : {modules_to_update}")

    with zipfile.ZipFile(zipball_stream) as zf:
        # Le zipball GitHub contient un dossier racine (owner-repo-sha/)
        entries = zf.namelist()
        if not entries:
            return
        root_prefix = entries[0]  # ex: "d4e-digital-4-efficiency-d4e-common-def-abc1234/"

        for entry in entries:
            if entry.endswith("/"):
                continue

            # Chemin relatif sans le dossier racine
            relative = entry[len(root_prefix):]
            if not relative:
                continue

            # Extraire le dossier racine du chemin relatif
            root_folder = relative.split("/")[0]

            if root_folder in modules_to_update:
                dest_path = os.path.join(repo_path, relative)
                dest_dir = os.path.dirname(dest_path)
                os.makedirs(dest_dir, exist_ok=True)
                with zf.open(entry) as src, open(dest_path, "wb") as dst:
                    dst.write(src.read())


# ── Pull Requests ──────────────────────────────────────────────────────────────

def find_existing_pr(owner, repo_name, head_branch):
    """Cherche une PR ouverte existante pour la branche donnée."""
    prs = gh_api_paginated(f"/repos/{owner}/{repo_name}/pulls?state=open")
    for pr in prs:
        if pr["head"]["ref"] == head_branch:
            return pr
    return None


def branch_exists_on_remote(owner, repo_name, branch):
    """Vérifie si une branche existe sur le remote."""
    try:
        gh_api(f"/repos/{owner}/{repo_name}/branches/{branch}")
        return True
    except HTTPError:
        return False


def create_pull_request(owner, repo_name, head, base, title, body, reviewer=None):
    """Crée une PR et ajoute un reviewer si défini."""
    pr = gh_api(f"/repos/{owner}/{repo_name}/pulls", method="POST", body={
        "title": title,
        "head": head,
        "base": base,
        "body": body,
    })
    if reviewer and pr:
        add_reviewer(owner, repo_name, pr["number"], reviewer)
    return pr


def update_pull_request(owner, repo_name, pr_number, title, body, reviewer=None):
    """Met à jour le titre et le body d'une PR existante."""
    gh_api(f"/repos/{owner}/{repo_name}/pulls/{pr_number}", method="PATCH", body={
        "title": title,
        "body": body,
    })
    if reviewer:
        add_reviewer(owner, repo_name, pr_number, reviewer)


def add_reviewer(owner, repo_name, pr_number, reviewer):
    """Ajoute un reviewer à une PR."""
    try:
        gh_api(f"/repos/{owner}/{repo_name}/pulls/{pr_number}/requested_reviewers",
               method="POST", body={"reviewers": [reviewer]})
    except HTTPError:
        print(f"  ⚠ Impossible d'ajouter le reviewer {reviewer}")


# ── Release notes ──────────────────────────────────────────────────────────────

def compile_release_notes(releases):
    """Compile les notes de release en markdown."""
    lines = []
    for _, r in releases:
        lines.append(f"# {r['tag_name']}")
        lines.append(r.get("body", "") or "")
        lines.append("")
    return "\n".join(lines)


# ── Dashboard ──────────────────────────────────────────────────────────────────

def generate_dashboard(releases_dict, repo_details):
    """Génère le contenu markdown du tableau de bord."""
    lines = []

    # Section releases
    lines.append("# Latest releases")
    lines.append("| Release | Date |")
    lines.append("| :------ | ---: |")
    for tag in sorted(releases_dict.keys()):
        r = releases_dict[tag]
        created = r.get("created_at", "")[:10]
        lines.append(f"| [{tag}]({r['html_url']}) | {created} |")

    lines.append("")

    # Section projets
    lines.append("# Project status")
    lines.append("| Repository | Manager | Production | Staging |")
    lines.append("| :--------- | :------ | ---------: | ------: |")

    # Trier par version prod desc, puis par nom asc pour les ex-aequo
    sorted_details = sorted(repo_details, key=lambda d: d["name"])
    sorted_details = sorted(sorted_details, key=lambda d: d["prod_version"] or "", reverse=True)

    for d in sorted_details:
        fire = ""
        construction_tag = f"construction-v{d['prod_version']}"
        if construction_tag not in releases_dict:
            fire = ":fire: "
        manager = d.get("manager") or ""
        lines.append(
            f"| [{d['name']}]({d['url']}) | {manager} | {fire}{d['prod_version']} | {d['staging_version']} |"
        )

    lines.append("")
    lines.append(f"_Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC_")

    return "\n".join(lines)


def update_dashboard_issue(owner, repo_name, issue_number, body):
    """Met à jour le body de l'issue dashboard."""
    gh_api(f"/repos/{owner}/{repo_name}/issues/{issue_number}", method="PATCH", body={"body": body})


# ── Traitement principal ───────────────────────────────────────────────────────

def handle_repository(owner, repo_name, repo_html_url, clone_url, source_releases, releases_dict, repo_details):
    """Traite un repo ElvyBat : détecte les versions, met à jour le module, crée/met à jour la PR."""

    # Branches
    branches = gh_api_paginated(f"/repos/{owner}/{repo_name}/branches")
    prod_branch = find_production_branch(branches)
    if not prod_branch:
        print(f"  Pas de branche de production trouvée")
        return
    print(f"  Branche production : {prod_branch}")

    staging_branch = find_staging_branch(branches)
    if not staging_branch:
        print(f"  Pas de branche de recette trouvée")
        return
    print(f"  Branche recette : {staging_branch}")

    # Version production
    prod_manifest = get_manifest_content(owner, repo_name, prod_branch)
    prod_version_str = get_version_from_manifest(prod_manifest)
    if not prod_version_str:
        print(f"  Manifest introuvable sur {prod_branch}")
        return
    prod_version = parse_version(prod_version_str)
    print(f"  Version production : {prod_version_str}")

    # Version recette
    staging_manifest = get_manifest_content(owner, repo_name, staging_branch)
    staging_version_str = get_version_from_manifest(staging_manifest)
    if not staging_version_str:
        print(f"  Manifest introuvable sur {staging_branch}")
        return
    staging_version = parse_version(staging_version_str)
    print(f"  Version recette : {staging_version_str}")

    # Dernière release pour cette version Odoo majeure
    latest = latest_release_for_major(source_releases, staging_version[0])
    if not latest:
        print(f"  Aucune release pour la version Odoo {staging_version[0]}")
        return

    latest_version, latest_release = latest
    print(f"  Dernière release : {latest_release['tag_name']}")

    # Ajouter au dashboard
    releases_dict[latest_release["tag_name"]] = latest_release

    # Clone et mise à jour
    temp_dir = os.path.join(tempfile.gettempdir(), f"deploy-bot-{repo_name}")
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)

    try:
        print(f"  Clonage du repo...")
        clone_repo(clone_url, temp_dir)

        print(f"  Checkout de {staging_branch}...")
        checkout_branch(temp_dir, staging_branch)

        print(f"  Création de la branche {BOT_BRANCH}...")
        create_branch(temp_dir, BOT_BRANCH)

        print(f"  Téléchargement du zipball...")
        zipball = gh_download_stream(latest_release["zipball_url"])

        print(f"  Extraction et remplacement des modules...")
        replicate_with_source_stream(temp_dir, zipball)

        print(f"  Staging des changements...")
        stage_all(temp_dir)

        # Vérifier PR existante et branche remote
        existing_pr = find_existing_pr(owner, repo_name, BOT_BRANCH)
        remote_branch_exists = branch_exists_on_remote(owner, repo_name, BOT_BRANCH)

        # Notes de release
        new_rels = new_releases_from(source_releases, staging_version)
        commit_message = f"Update to {latest_release['tag_name']}"
        pr_body = compile_release_notes(new_rels)

        # Chef de projet
        project_manager = get_project_manager(owner, repo_name)
        if project_manager:
            print(f"  Chef de projet : {project_manager}")

        if is_dirty(temp_dir):
            print(f"  Changements détectés, commit et push...")
            commit_and_push(temp_dir, BOT_BRANCH, commit_message)

            print(f"  {len(new_rels)} nouvelle(s) release(s) à appliquer")

            if existing_pr is None and remote_branch_exists:
                print(f"  Création d'une nouvelle PR...")
                create_pull_request(owner, repo_name, BOT_BRANCH, staging_branch,
                                    commit_message, pr_body, project_manager)
        else:
            print(f"  Aucun changement détecté")

        # Mise à jour de la PR existante (même sans nouveaux changements)
        if remote_branch_exists and existing_pr is not None:
            print(f"  Mise à jour de la PR #{existing_pr['number']}...")
            update_pull_request(owner, repo_name, existing_pr["number"],
                                commit_message, pr_body, project_manager)

    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)

    # Ajout au dashboard
    repo_details.append({
        "name": repo_name,
        "url": repo_html_url,
        "manager": project_manager,
        "prod_version": prod_version_str,
        "staging_version": staging_version_str,
    })


def main():
    print(f"=== D4E Deployment Bot ===")
    print(f"Source : {SOURCE_OWNER}/{SOURCE_REPO}")
    print(f"Branche bot : {BOT_BRANCH}")
    print(f"Dashboard issue : #{DASHBOARD_ISSUE}")
    print()

    # Récupérer les releases du repo source
    source_releases = get_construction_releases(SOURCE_OWNER, SOURCE_REPO)
    print(f"{len(source_releases)} release(s) construction trouvée(s)")
    print()

    # Récupérer tous les repos privés non archivés de l'organisation
    all_repos = gh_api_paginated(f"/orgs/{SOURCE_OWNER}/repos?type=private")
    repos = [r for r in all_repos if not r.get("archived", False)]
    print(f"{len(repos)} repo(s) non archivé(s) trouvé(s)")
    print()

    releases_dict = {}
    repo_details = []

    for repo in repos:
        repo_name = repo["name"]
        repo_owner = repo["owner"]["login"]

        # Ignorer le repo source lui-même
        if repo_name == SOURCE_REPO:
            print(f"[{repo_name}] Repo source, ignoré")
            print()
            continue

        print(f"[{repo_name}] Traitement...")

        # Vérifier si c'est un projet ElvyBat
        if not is_elvybat_project(repo_owner, repo_name):
            print(f"  Pas un projet ElvyBat, ignoré")
            print()
            continue

        try:
            handle_repository(
                repo_owner, repo_name, repo["html_url"], repo["clone_url"],
                source_releases, releases_dict, repo_details,
            )
            print(f"  ✓ Terminé")
        except Exception as e:
            print(f"  ✗ Erreur : {e}")

        print()

    # Mise à jour du dashboard
    print("Mise à jour du tableau de bord...")
    dashboard_body = generate_dashboard(releases_dict, repo_details)
    update_dashboard_issue(SOURCE_OWNER, SOURCE_REPO, DASHBOARD_ISSUE, dashboard_body)
    print("✓ Dashboard mis à jour")
    print()
    print("=== Terminé ===")


if __name__ == "__main__":
    main()
