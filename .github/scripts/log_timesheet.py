import os
import re
import json
import math
import urllib.error
import urllib.request
import xmlrpc.client
from datetime import datetime

def format_duration(hours_float):
    """Formatte la durée en 00h00 (ex: 1.5 -> 1h30, 2.0 -> 2h00)."""
    h = int(hours_float)
    m = round((hours_float - h) * 60)
    return f"{h}h{m:02d}" if m else f"{h}h00"

def post_issue_comment(body):
    """Poste un commentaire sur l'issue (utilise GITHUB_TOKEN, pas le PAT)."""
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
gh_token      = os.environ["GITHUB_TOKEN"]   # token par défaut → pour poster le commentaire
gh_pat        = os.environ.get("GH_PAT") or gh_token  # PAT → pour GraphQL (custom fields Projects V2)
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
          id
          project {
            id
            fields(first: 30) {
              nodes {
                id
                name
              }
            }
          }
          taskIdField: fieldValueByName(name: "Tâche ID") {
            ... on ProjectV2ItemFieldNumberValue {
              number
            }
          }
          pointageTotalField: fieldValueByName(name: "Pointage total") {
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
        "Authorization": f"Bearer {gh_pat}",
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
item_id = None
project_id = None
pointage_total_minutes = 0
pointage_total_field_id = None

for item in items:
    task_id_val = item.get("taskIdField")
    if task_id_val and "number" in task_id_val:
        task_id = int(task_id_val["number"])
        item_id = item.get("id")
        project = item.get("project") or {}
        project_id = project.get("id")
        # Pointage total (en minutes) : vide → 0
        pt_field = item.get("pointageTotalField")
        if pt_field is not None and "number" in pt_field and pt_field["number"] is not None:
            try:
                pointage_total_minutes = int(float(pt_field["number"]))
            except (TypeError, ValueError):
                pointage_total_minutes = 0
        else:
            pointage_total_minutes = 0
        # Champ "Pointage total" du projet pour la mutation
        for f in (project.get("fields") or {}).get("nodes") or []:
            if (f.get("name") or "").strip() == "Pointage total":
                pointage_total_field_id = f.get("id")
                break
        break

if not task_id:
    msg = f"❌ Champ 'Tâche ID' non renseigné sur l'issue #{issue_number}"
    print(msg)
    post_issue_comment(msg)
    exit(1)

print(f"📋 Tâche ID (depuis GitHub) : {task_id}")
print(f"⏱️  Pointage total actuel : {pointage_total_minutes} min")

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

# Pointage arrondi en minutes, ajouté au total
duration_minutes = round(duration * 60)
new_total_minutes = pointage_total_minutes + duration_minutes

# Mise à jour du champ custom "Pointage total" sur l'item du projet
if item_id and project_id and pointage_total_field_id:
    mutation = """
    mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $value: Float!) {
      updateProjectV2ItemFieldValue(
        input: {
          projectId: $projectId
          itemId: $itemId
          fieldId: $fieldId
          value: { number: $value }
        }
      ) {
        projectV2Item { id }
      }
    }
    """
    payload_update = json.dumps({
        "query": mutation,
        "variables": {
            "projectId": project_id,
            "itemId": item_id,
            "fieldId": pointage_total_field_id,
            "value": float(new_total_minutes),
        },
    }).encode("utf-8")
    req_update = urllib.request.Request(
        "https://api.github.com/graphql",
        data=payload_update,
        headers={
            "Authorization": f"Bearer {gh_pat}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req_update) as resp_update:
            result_update = json.loads(resp_update.read().decode())
        if "errors" in result_update:
            print(f"⚠️ Champ 'Pointage total' non mis à jour : {result_update['errors']}")
        else:
            print(f"✅ Pointage total mis à jour : {new_total_minutes} min")
    except urllib.error.HTTPError as e:
        print(f"⚠️ Erreur lors de la mise à jour du pointage total : {e.code} {e.reason}")
else:
    if not pointage_total_field_id:
        print("⚠️ Champ custom 'Pointage total' introuvable sur le projet — total non persisté")

total_formatted = format_duration(new_total_minutes / 60.0)
print(f"✅ Timesheet créé ! ID Odoo : {timesheet_id} — {duration:.2f}h sur tâche {task_id}")
post_issue_comment(
    f"✅ Pointage pris en compte. Tâche ID : **{task_id}**, temps : **{format_duration(duration)}** — "
    f"**Pointage total : {total_formatted}**"
)