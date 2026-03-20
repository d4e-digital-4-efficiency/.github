import requests
import base64
import time
import os

# --- Configuration ---
ORG_NAME     = os.environ["ORG_NAME"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
BRANCH       = "main"
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

# --- Récupération de tous les repos non archivés ---
repos = []
page = 1
while True:
    r = requests.get(
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
    url = f"https://api.github.com/repos/{ORG_NAME}/{repo_name}/contents/{FILE_PATH}"

    existing = requests.get(url, headers=headers, params={"ref": BRANCH})

    payload = {
        "message": "ci: add timesheet workflow",
        "content": content_b64,
        "branch": BRANCH
    }

    if existing.status_code == 200:
        payload["sha"] = existing.json()["sha"]
        payload["message"] = "ci: update timesheet workflow"

    result = requests.put(url, headers=headers, json=payload)

    if result.status_code in (200, 201):
        if existing.status_code == 200:
            print(f"🔄 {repo_name} — mis à jour")
            updated += 1
        else:
            print(f"✅ {repo_name} — créé")
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
