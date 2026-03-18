import os
import re
import json
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

# --- Parsing du commentaire ---
# Format : @pointage 1h30 #TASK-123
pattern = r"@pointage\s+(\d+)h(\d*)\s*(?:#([\w-]+))?"
match = re.search(pattern, comment, re.IGNORECASE)

if not match:
    print("❌ Format invalide. Attendu : @pointage 1h30 #TASK-123")
    exit(1)

hours_raw   = int(match.group(1))
minutes_raw = int(match.group(2)) if match.group(2) else 0
task_ref    = match.group(3) or f"ISSUE-{issue_number}"
duration    = hours_raw + minutes_raw / 60.0

print(f"⏱️  Durée     : {duration:.2f}h")
print(f"📋 Tâche     : {task_ref}")
print(f"👤 Auteur    : {gh_author}")

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

# --- Recherche de la tâche ---
task_ids = models.execute_kw(
    odoo_db, uid, odoo_password,
    "project.task", "search",
    [[["name", "ilike", task_ref]]]
)

if not task_ids:
    print(f"❌ Tâche '{task_ref}' introuvable dans Odoo")
    exit(1)

task_id = task_ids[0]
print(f"✅ Tâche Odoo ID : {task_id}")

# --- Création du timesheet ---
timesheet_id = models.execute_kw(
    odoo_db, uid, odoo_password,
    "account.analytic.line", "create",
    [{
        "task_id":     task_id,
        "unit_amount": duration,
        "name":        f"[GitHub Issue #{issue_number}] {task_ref}",
        "date":        datetime.today().strftime("%Y-%m-%d"),
        "employee_id": employee_id,
    }]
)

print(f"✅ Timesheet créé ! ID Odoo : {timesheet_id} — {duration:.2f}h sur '{task_ref}'")