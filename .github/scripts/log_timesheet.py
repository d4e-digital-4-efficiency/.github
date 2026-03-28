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


def normalize_comment_body_for_pointage(text):
    """
    Retire les caractères invisibles (copier-coller) et unifie les espaces,
    pour que la durée après @pointage soit reconnue.
    """
    if not text:
        return text
    for ch in ("\u200b", "\u200c", "\u200d", "\ufeff", "\u2060"):
        text = text.replace(ch, "")
    return text.replace("\xa0", " ").replace("\u202f", " ")


# Préfixe : @pointage ou lien Markdown [@pointage](https://...)
POINTAGE_PREFIX = r"(?:@pointage|\[@pointage\]\([^)]*\))"

# Journée ouvrée pour les labels "Durée : < Xj" / "jours"
PLANNED_DURATION_HOURS_PER_DAY = 8.0


def parse_planned_duration_from_label(label_name):
    """
    Extrait la durée prévue depuis un label de type "Durée : <2h", "Durée <4h",
    "Durée : < 30 min", "Durée : < 2j", "Durée : <1,5jours".
    Retourne un float en heures, ou None si non exploitable (ex: ">16h").
    """
    if not label_name:
        return None

    normalized = label_name.strip().lower()
    if not (normalized.startswith("durée") or normalized.startswith("duree")):
        return None

    # Accepte les variantes avec/sans ":" : "Durée : <2h", "Durée <4h", "Duree<30min"
    value_part = re.sub(r"^dur[ée]e\s*:?\s*", "", normalized, count=1).strip()
    compact = re.sub(r"\s+", "", value_part)

    # Cas non borné supérieur (ex: >16h) => pas de "dépassement prévu" pertinent
    if compact.startswith(">"):
        return None

    # Exemples gérés : "<30min", "<2h", "<2j", "<1,5jours", "<12h(1,5j)"
    match = re.match(r"^<(\d+(?:[.,]\d+)?)(min|h|j(?:ours)?)\b", compact)
    if not match:
        return None

    amount = float(match.group(1).replace(",", "."))
    unit = match.group(2)
    if unit == "min":
        return amount / 60.0
    if unit.startswith("j"):
        return amount * PLANNED_DURATION_HOURS_PER_DAY
    return amount

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
comment       = normalize_comment_body_for_pointage(os.environ["COMMENT_BODY"])
gh_author     = os.environ["COMMENT_AUTHOR"]
issue_number  = os.environ["ISSUE_NUMBER"]
gh_token      = os.environ["GITHUB_TOKEN"]   # token par défaut → pour poster le commentaire
gh_pat        = os.environ.get("GH_PAT") or gh_token  # PAT → pour GraphQL (custom fields Projects V2)
gh_repo       = os.environ["GITHUB_REPOSITORY"]

# --- Parsing du commentaire ---
# Formats : @pointage … ou [@pointage](url) … puis 1h30, 1 h 30, 1:30, 2h, ou 45 (minutes)
pattern_hm = POINTAGE_PREFIX + r"\s+(\d+)\s*h\s*(\d+)"
pattern_colon = POINTAGE_PREFIX + r"\s+(\d+)\s*:\s*(\d+)"
pattern_h = POINTAGE_PREFIX + r"\s+(\d+)\s*h\b"
pattern_minutes = POINTAGE_PREFIX + r"\s+(\d+(?:\.\d+)?)(?!h)"

match_hm = re.search(pattern_hm, comment, re.IGNORECASE)
match_colon = re.search(pattern_colon, comment, re.IGNORECASE)
match_h = re.search(pattern_h, comment, re.IGNORECASE)
match_min = re.search(pattern_minutes, comment, re.IGNORECASE)

if match_hm:
    duration_h = int(match_hm.group(1)) + int(match_hm.group(2)) / 60.0
elif match_colon:
    duration_h = int(match_colon.group(1)) + int(match_colon.group(2)) / 60.0
elif match_h:
    duration_h = float(match_h.group(1))
elif match_min:
    duration_h = float(match_min.group(1)) / 60.0
else:
    msg = (
        "❌ Format invalide. Attendu : @pointage 1h30, @pointage 2h ou @pointage 45"
    )
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
      author {
        login
      }
      labels(first: 50) {
        nodes {
          name
        }
      }
      projectItems(first: 10) {
        nodes {
          id
          project {
            id
            fields(first: 50) {
              nodes {
                ... on ProjectV2Field {
                  id
                  name
                }
                ... on ProjectV2SingleSelectField {
                  id
                  name
                }
                ... on ProjectV2IterationField {
                  id
                  name
                }
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
issue_author_login = (issue_data.get("author") or {}).get("login")
labels = issue_data.get("labels", {}).get("nodes", [])
items = issue_data.get("projectItems", {}).get("nodes", [])
task_id = None
item_id = None
project_id = None
pointage_total_minutes = 0
pointage_total_field_id = None
planned_duration_h = None
planned_duration_label = None

for label in labels:
    label_name = (label.get("name") or "").strip()
    parsed = parse_planned_duration_from_label(label_name)
    if parsed is not None:
        planned_duration_h = parsed
        planned_duration_label = label_name
        break

if planned_duration_h is not None:
    print(
        f"🎯 Durée prévue détectée : {format_duration(planned_duration_h)} "
        f"(label: {planned_duration_label})"
    )
else:
    print("ℹ️ Aucune durée max exploitable détectée dans les labels")

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
        # fieldId depuis la liste des champs du projet (nom "Pointage total")
        for node in (project.get("fields") or {}).get("nodes") or []:
            if (node.get("name") or "").strip() == "Pointage total" and node.get("id"):
                pointage_total_field_id = node["id"]
                break
        break

if not task_id:
    msg = f"❌ Champ 'Tâche ID' non renseigné sur l'issue #{issue_number}"
    if issue_author_login:
        msg += (
            f"\n\n@{issue_author_login} Merci de renseigner le champ **Tâche ID** "
            "dans le projet GitHub pour que le pointage puisse être enregistré."
        )
    print(msg)
    post_issue_comment(msg)
    exit(1)

print(f"📋 Tâche ID (depuis GitHub) : {task_id}")
print(f"⏱️  Pointage total actuel : {pointage_total_minutes} min")

# --- Chargement du mapping utilisateurs ---
mapping_path = os.path.join(os.path.dirname(__file__), "users_mapping.json")
with open(mapping_path, "r") as f:
    users_mapping = json.load(f)

user_entry = users_mapping.get(gh_author)

first_name = None

if isinstance(user_entry, dict):
    employee_id = user_entry.get("employee_id")
    first_name = user_entry.get("first_name")
    exclude_from_total = user_entry.get("exclude_from_total", False)
else:
    employee_id = user_entry
    exclude_from_total = False

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

# Pointage arrondi en minutes, ajouté au total (sauf si l'utilisateur est exclu)
duration_minutes = round(duration * 60)
if exclude_from_total:
    new_total_minutes = pointage_total_minutes
    print(f"ℹ️ Utilisateur '{gh_author}' exclu du pointage total (exclude_from_total=true)")
else:
    new_total_minutes = pointage_total_minutes + duration_minutes

# Mise à jour du champ custom "Pointage total" sur l'item du projet (seulement si le total a changé)
if not exclude_from_total and item_id and project_id and pointage_total_field_id:
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
            err_msg = str(result_update["errors"])
            print(f"⚠️ Champ 'Pointage total' non mis à jour : {result_update['errors']}")
            if "resource" in err_msg.lower() or "access" in err_msg.lower() or "scope" in err_msg.lower() or "permission" in err_msg.lower():
                print("💡 Vérifiez que le secret GH_PAT a le scope « project » (Settings → Developer settings → Personal access tokens).")
        else:
            print(f"✅ Pointage total mis à jour : {new_total_minutes} min")
    except urllib.error.HTTPError as e:
        print(f"⚠️ Erreur lors de la mise à jour du pointage total : {e.code} {e.reason}")
        if e.code == 401 or e.code == 403:
            print("💡 Vérifiez que le secret GH_PAT a le scope « project » (écriture Projects v2).")
else:
    if not pointage_total_field_id:
        print("⚠️ Champ custom 'Pointage total' introuvable sur le projet — total non persisté")

total_formatted = format_duration(new_total_minutes / 60.0)
print(f"✅ Timesheet créé ! ID Odoo : {timesheet_id} — {duration:.2f}h sur tâche {task_id}")

exclude_note = ""
if exclude_from_total:
    exclude_note = "\n\n> ℹ️ Ce pointage n'est **pas comptabilisé** dans le total de l'issue (utilisateur exclu du total)."

if first_name:
    merci_suffix = f"\nMerci {first_name} !"
else:
    merci_suffix = ""

if planned_duration_h is not None:
    duree_suffix = (
        f"\n\n**Durée max :** {format_duration(planned_duration_h)}"
    )
else:
    duree_suffix = (
        "\n\n> ⚠️ **Aucun label « Durée » exploitable** sur cette issue "
        "(ex. `Durée <4h`, `Durée : < 2h`, `Durée : < 2j`, `Durée : < 30 min`). "
        "Sans ce label, la comparaison au prévisionnel et l’alerte de dépassement ne s’appliquent pas."
    )

warning_suffix = ""
if planned_duration_h and (new_total_minutes / 60.0) > planned_duration_h:
    overrun_ratio = ((new_total_minutes / 60.0) - planned_duration_h) / planned_duration_h
    overrun_percent = round(overrun_ratio * 100)
    warning_suffix = (
        "\n\n> ⚠️ **Warning**\n"
        f"> Le temps max pour cette issue était de **{format_duration(planned_duration_h)}**, "
        f"il a été dépassé de **{overrun_percent}%**."
    )
    print(
        f"⚠️ Dépassement durée max ({planned_duration_label}) : "
        f"+{overrun_percent}% vs {format_duration(planned_duration_h)}"
    )

post_issue_comment(
    f"✅ Pointage pris en compte. Tâche ID : **{task_id}**, temps : **{format_duration(duration)}** — "
    f"**Pointage total : {total_formatted}**{merci_suffix}{exclude_note}{duree_suffix}{warning_suffix}"
)