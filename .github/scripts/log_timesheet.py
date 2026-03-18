import os
import re
import json
import math
import urllib.request
import xmlrpc.client
from datetime import datetime

def format_duration(hours_float):
    """Formatte la durée en 00h00 (ex: 1.5 -> 1h30, 2.0 -> 2h00)."""
    h = int(hours_float)
    m = round((hours_float - h) * 60)
    return f"{h}h{m:02d}" if m else f"{h}h00"

def post_issue_comment(body):
    """Poste un commentaire sur l'issue GitHub."""
    owner, repo = gh_repo.split("/")
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments"
    payload = json.dumps({"body": body}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {gh_token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"⚠️ Impossible de poster le commentaire : {e.code} {e.reason}")

# --- Variables d'environnement ---
odoo_url      = os.environ["ODOO_URL"]
odoo_db       = os.environ["ODOO_DB"]
odoo_user     = os.environ["ODOO_USERNAME"]
odoo_password = os.environ["ODOO_PASSWORD"]
comment       = os.environ["COMMENT_BODY"]
gh_author     = os.environ["COMMENT_AUTHOR"]
issue_number  = os.environ["ISSUE_NUMBER"]
gh_token      = os.environ["GITHUB_TOKEN"]
gh_repo       = os.environ["GITHUB_REPOSITORY"]

# --- Parsing du commentaire ---
# Formats acceptés : @pointage 1h30, @pointage 2h (heures) ; @pointage 45, @pointage 90 (minutes, pas de h)
pattern_hm = r"@pointage\s+(\d+)h(\d+)"
pattern_h = r"@pointage\s+(\d+)h\b"
# Nombre seul (sans h après) = minutes
pattern_minutes = r"@pointage\s+(\d+(?:\.\d+)?)(?!h)"

match_hm = re.search(pattern_hm, comment, re.IGNORECASE)
match_h = re.search(pattern_h, comment, re.IGNORECASE)
match_min = re.search(pattern_minutes, comment, re.IGNORECASE)

if match_hm:
    duration_h = int(match_hm.group(1)) + int(match_hm.group(2)) / 60.0
elif match_h:
    duration_h = float(match_h.group(1))
elif match_min:
    duration_h = float(match_min.group(1)) / 60.0
else:
    msg = "❌ Format invalide. Attendu : @pointage 1h30, @pointage 2h ou @pointage 45 (minutes)"
    print(msg)
    post_issue_comment(msg)
    exit(1)

# Arrondi au 1/4 h supérieur
duration = math.ceil(duration_h / 0.25) * 0.25

print(f"⏱️  Durée  : {duration:.2f}h")
print(f"👤 Auteur : {gh_author}")

# --- Récupération du champ custom "Tâche ID" depuis GitHub Projects v2 ---
owner, repo = gh_repo.split("/")

graphql_query = """
query($owner: String!, $repo: String!, $issue_number: Int!) {
  repository(owner: $owner, name: $repo) {
    issue(number: $issue_number) {
      title
      projectItems(first: 10) {
        nodes {
          fieldValueByName(name: "Tâche ID") {
            ... on ProjectV2ItemFieldNumberValue {
              number
            }
          }
        }
      }
    }
  }
}
"""

payload = json.dumps({
    "query": graphql_query,
    "variables": {
        "owner": owner,
        "repo": repo,
        "issue_number": int(issue_number),
    },
}).encode("utf-8")

req = urllib.request.Request(
    "https://api.github.com/graphql",
    data=payload,
    headers={
        "Authorization": f"Bearer {gh_token}",
        "Content-Type": "application/json",
    },
    method="POST",
)

with urllib.request.urlopen(req) as resp:
    result = json.loads(resp.read().decode())

if "errors" in result:
    msg = f"❌ Erreur GraphQL GitHub : {result['errors']}"
    print(msg)
    post_issue_comment(msg)
    exit(1)

issue_data = result.get("data", {}).get("repository", {}).get("issue", {})
issue_title = (issue_data.get("title") or "").strip().replace("\n", " ")
items = issue_data.get("projectItems", {}).get("nodes", [])
task_id = None
for item in items:
    field_value = item.get("fieldValueByName")
    if field_value and "number" in field_value:
        task_id = int(field_value["number"])
        break

if not task_id:
    msg = f"❌ Champ 'Tâche ID' non renseigné sur l'issue #{issue_number}"
    print(msg)
    post_issue_comment(msg)
    exit(1)

print(f"📋 Tâche ID (depuis GitHub) : {task_id}")

# --- Chargement du mapping utilisateurs ---
mapping_path = os.path.join(os.path.dirname(__file__), "users_mapping.json")
with open(mapping_path, "r") as f:
    users_mapping = json.load(f)

employee_id = users_mapping.get(gh_author)

if not employee_id:
    msg = f"❌ Login GitHub '{gh_author}' absent du mapping users_mapping.json"
    print(msg)
    post_issue_comment(msg)
    exit(1)

print(f"✅ Employé Odoo ID : {employee_id}")

# --- Connexion Odoo XML-RPC ---
common = xmlrpc.client.ServerProxy(f"{odoo_url}/xmlrpc/2/common")
uid    = common.authenticate(odoo_db, odoo_user, odoo_password, {})

if not uid:
    msg = "❌ Authentification Odoo échouée"
    print(msg)
    post_issue_comment(msg)
    exit(1)

models = xmlrpc.client.ServerProxy(f"{odoo_url}/xmlrpc/2/object")

# --- Vérification de la tâche dans Odoo ---
task_exists = models.execute_kw(
    odoo_db, uid, odoo_password,
    "project.task", "search",
    [[["id", "=", task_id]]]
)

if not task_exists:
    msg = f"❌ Tâche ID {task_id} introuvable dans Odoo"
    print(msg)
    post_issue_comment(msg)
    exit(1)

print(f"✅ Tâche Odoo ID : {task_id}")

# --- Création du timesheet ---
try:
    timesheet_id = models.execute_kw(
        odoo_db, uid, odoo_password,
        "account.analytic.line", "create",
        [{
            "task_id":     task_id,
            "unit_amount": duration,
            "name":        f"[DEV] #{issue_number} {issue_title}",
            "date":        datetime.today().strftime("%Y-%m-%d"),
            "employee_id": employee_id,
        }]
    )
except Exception as e:
    msg = f"❌ Erreur Odoo lors de la création du timesheet : {e}"
    print(msg)
    post_issue_comment(msg)
    exit(1)

print(f"✅ Timesheet créé ! ID Odoo : {timesheet_id} — {duration:.2f}h sur tâche {task_id}")
post_issue_comment(f"✅ Pointage pris en compte. Tâche ID : **{task_id}**, temps : **{format_duration(duration)}**")