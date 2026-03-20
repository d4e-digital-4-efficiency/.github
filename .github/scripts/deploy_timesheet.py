import requests
import base64
import time
import os
from requests.exceptions import RequestException

# --- Configuration ---
ORG_NAME     = os.environ["ORG_NAME"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
FILE_PATH    = ".github/workflows/timesheet.yml"

# --- Repos ciblés (passés depuis le formulaire GitHub Actions) ---
INCLUDE = [r.strip() for r in os.environ["REPOS"].split(",") if r.strip()]

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

content_b64 = base64.b64encode(WORKFLOW_CONTENT.encode()).decode()

print(f"🎯 Déploiement sur {len(INCLUDE)} repo(s) : {', '.join(INCLUDE)}\n")

for repo_name in INCLUDE:
    repo_name = repo_name.strip()
    repo_url = f"https://api.github.com/repos/{ORG_NAME}/{repo_name}"
    try:
        repo_info = github_request("GET", repo_url, headers=headers)
    except RequestException as exc:
        print(f"❌ {repo_name} — accès repo impossible (réseau) : {exc}")
        continue

    if repo_info.status_code != 200:
        print(f"❌ {repo_name} — accès repo impossible : {repo_info.json().get('message')}")
        continue

    branch = repo_info.json().get("default_branch", "main")
    url = f"{repo_url}/contents/{FILE_PATH}"
    try:
        existing = github_request("GET", url, headers=headers, params={"ref": branch})
    except RequestException as exc:
        print(f"❌ {repo_name} — lecture fichier impossible (réseau) : {exc}")
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
        continue

    if result.status_code in (200, 201):
        action = "mis à jour" if existing.status_code == 200 else "créé"
        print(f"✅ {repo_name} — fichier {action} (branche {branch})")
    else:
        print(f"❌ {repo_name} — erreur : {result.json().get('message')}")

    time.sleep(0.5)

print("\n🎉 Déploiement terminé !")
