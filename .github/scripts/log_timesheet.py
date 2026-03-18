import os
import re
import json
import urllib.request
import xmlrpc.client
from datetime import datetime

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
# Formats acceptés : @pointage 1h30, @pointage 2h, @pointage 1.5, @pointage 10
pattern_hm = r"@pointage\s+(\d+)h(\d+)"
pattern_h = r"@pointage\s+(\d+)h\b"
pattern_dec = r"@pointage\s+(\d+(?:\.\d+)?)\b"

match_hm = re.search(pattern_hm, comment, re.IGNORECASE)
match_h = re.search(pattern_h, comment, re.IGNORECASE)
match_dec = re.search(pattern_dec, comment, re.IGNORECASE)

if match_hm:
    duration = int(match_hm.group(1)) + int(match_hm.group(2)) / 60.0
elif match_h:
    duration = float(match_h.group(1))
elif match_dec:
    duration = float(match_dec.group(1))
else:
    print("❌ Format invalide. Attendu : @pointage 1h30, @pointage 2h, @pointage 1.5 ou @pointage 10")
    exit(1)

print(f"⏱️  Durée  : {duration:.2f}h")
print(f"👤 Auteur : {gh_author}")

# --- Récupération du champ custom "Tâche ID" depuis GitHub Projects v2 ---
owner, repo = gh_repo.split("/")

graphql_query = """
query($owner: String!, $repo: String!, $issue_number: Int!) {
  repository(owner: $owner, name: $repo) {
    issue(number: $issue_number) {
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
    print(f"❌ Erreur GraphQL GitHub : {result['errors']}")
    exit(1)

task_id = None
items = (result.get("data", {}).get("repository", {})
         .get("issue", {}).get("projectItems", {}).get("nodes", []))
for item in items:
    field_value = item.get("fieldValueByName")
    if field_value and "number" in field_value:
        task_id = int(field_value["number"])
        break

if not task_id:
    print(f"❌ Champ 'Tâche ID' non renseigné sur l'issue #{issue_number}")
    exit(1)

print(f"📋 Tâche ID (depuis GitHub) : {task_id}")

# --- Chargement du mapping utilisateurs ---
mapping_path = os.path.join(os.path.dirname(__file__), "users_mapping.json")
with open(mapping_path, "r") as f:
    users_mapping = json.load(f)

employee_id = users_mapping.get(gh_author)

if not employee_id:
    print(f"❌ Login GitHub '{gh_author}' absent du mapping users_mapping.json")
    exit(1)

print(f"✅ Employé Odoo ID : {employee_id}")

# --- Connexion Odoo XML-RPC ---
common = xmlrpc.client.ServerProxy(f"{odoo_url}/xmlrpc/2/common")
uid    = common.authenticate(odoo_db, odoo_user, odoo_password, {})

if not uid:
    print("❌ Authentification Odoo échouée")
    exit(1)

models = xmlrpc.client.ServerProxy(f"{odoo_url}/xmlrpc/2/object")

# --- Vérification de la tâche dans Odoo ---
task_exists = models.execute_kw(
    odoo_db, uid, odoo_password,
    "project.task", "search",
    [[["id", "=", task_id]]]
)

if not task_exists:
    print(f"❌ Tâche ID {task_id} introuvable dans Odoo")
    exit(1)

print(f"✅ Tâche Odoo ID : {task_id}")

# --- Création du timesheet ---
timesheet_id = models.execute_kw(
    odoo_db, uid, odoo_password,
    "account.analytic.line", "create",
    [{
        "task_id":     task_id,
        "unit_amount": duration,
        "name":        f"[GitHub Issue #{issue_number}]",
        "date":        datetime.today().strftime("%Y-%m-%d"),
        "employee_id": employee_id,
    }]
)

print(f"✅ Timesheet créé ! ID Odoo : {timesheet_id} — {duration:.2f}h sur tâche {task_id}")