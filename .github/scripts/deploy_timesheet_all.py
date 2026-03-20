import requests
import base64
import time
import os
from requests.exceptions import RequestException

# --- Configuration ---
ORG_NAME     = os.environ["ORG_NAME"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
FILE_PATH    = ".github/workflows/timesheet.yml"

# --- Exclusions fixes (toujours ignorés) ---
EXCLUDE = {".github"}

# --- Exclusions supplémentaires passées depuis le formulaire ---
extra = os.environ.get("EXTRA_EXCLUDE", "")
if extra.strip():
    EXCLUDE.update([r.strip() for r in extra.split(",") if r.strip()])

print(f"🚫 Repos exclus : {', '.join(EXCLUDE)}\n")

WORKFLOW_CONTENT = """\
name: Timesheet
on:
  issue_comment:
    types: [created]
jobs:
  call-central:
    uses: {org}/.github/.github/workflows/timesheet.yml@main
    secrets: inherit
""".format(org=ORG_NAME)

headers = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json"
}

REQUEST_TIMEOUT = 20
MAX_RETRIES = 4
RETRY_DELAY_SECONDS = 2


def github_request(method, url, **kwargs):
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return requests.request(method, url, **kwargs)
        except RequestException as exc:
            if attempt == MAX_RETRIES:
                raise
            wait_time = RETRY_DELAY_SECONDS * attempt
            print(
                f"⚠️ Requête {method} {url} en échec "
                f"(tentative {attempt}/{MAX_RETRIES}) : {exc}. "
                f"Nouvel essai dans {wait_time}s."
            )
            time.sleep(wait_time)

# --- Récupération de tous les repos non archivés ---
repos = []
page = 1
while True:
    r = github_request(
        "GET",
        f"https://api.github.com/orgs/{ORG_NAME}/repos",
        headers=headers,
        params={"per_page": 100, "page": page}
    )
    data = r.json()
    if not data:
        break
    repos += [repo["name"] for repo in data if not repo["archived"]]
    page += 1

to_deploy = [r for r in repos if r not in EXCLUDE]
print(f"✅ {len(repos)} repos trouvés — {len(to_deploy)} à traiter\n")

content_b64 = base64.b64encode(WORKFLOW_CONTENT.encode()).decode()

# --- Déploiement ---
success, updated, failed = 0, 0, 0

for repo_name in to_deploy:
    repo_url = f"https://api.github.com/repos/{ORG_NAME}/{repo_name}"
    try:
        repo_info = github_request("GET", repo_url, headers=headers)
    except RequestException as exc:
        print(f"❌ {repo_name} — accès repo impossible (réseau) : {exc}")
        failed += 1
        continue

    if repo_info.status_code != 200:
        print(f"❌ {repo_name} — accès repo impossible : {repo_info.json().get('message')}")
        failed += 1
        continue

    branch = repo_info.json().get("default_branch", "main")
    url = f"{repo_url}/contents/{FILE_PATH}"
    try:
        existing = github_request("GET", url, headers=headers, params={"ref": branch})
    except RequestException as exc:
        print(f"❌ {repo_name} — lecture fichier impossible (réseau) : {exc}")
        failed += 1
        continue

    payload = {
        "message": "ci: add timesheet workflow",
        "content": content_b64,
        "branch": branch
    }

    if existing.status_code == 200:
        payload["sha"] = existing.json()["sha"]
        payload["message"] = "ci: update timesheet workflow"

    try:
        result = github_request("PUT", url, headers=headers, json=payload)
    except RequestException as exc:
        print(f"❌ {repo_name} — écriture fichier impossible (réseau) : {exc}")
        failed += 1
        continue

    if result.status_code in (200, 201):
        if existing.status_code == 200:
            print(f"🔄 {repo_name} — mis à jour (branche {branch})")
            updated += 1
        else:
            print(f"✅ {repo_name} — créé (branche {branch})")
            success += 1
    else:
        print(f"❌ {repo_name} — erreur : {result.json().get('message')}")
        failed += 1

    time.sleep(0.5)

print(f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎉 Déploiement terminé !
✅ Créés    : {success}
🔄 Mis à jour : {updated}
❌ Erreurs  : {failed}
━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")
