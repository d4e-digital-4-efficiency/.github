# .github


```

---

## Résumé de la structure finale
```
TON_ORG/.github/                          ← repo central
├── .github/
│   └── workflows/
│       └── timesheet.yml                 ← workflow central
└── scripts/
    ├── log_timesheet.py                  ← toute la logique
    └── users_mapping.json                ← mapping GitHub → Odoo

TON_ORG/repo-projet-A/
└── .github/workflows/timesheet.yml      ← 5 lignes seulement

TON_ORG/repo-projet-B/
└── .github/workflows/timesheet.yml      ← 5 lignes seulement


Les 4 secrets Odoo (ODOO_URL, ODOO_DB, etc.) se mettent ici :
github.com/TON_ORG
  → Settings
    → Secrets and variables
      → Actions
        → New organization secret
⚠️ Pour chaque secret, choisis "All repositories" dans le champ Repository access pour qu'ils soient accessibles partout.

2. Secrets de repo (si besoin spécifique)
Si un repo a une instance Odoo différente, tu peux surcharger au niveau repo :
github.com/TON_ORG/mon-repo
  → Settings
    → Secrets and variables
      → Actions
        → New repository secret

Pourquoi ça fonctionne avec secrets: inherit
C'est la ligne clé dans le workflow de chaque repo :
yamljobs:
  call-central:
    uses: TON_ORG/.github/.github/workflows/timesheet.yml@main
    secrets: inherit   # ← transmet tous les secrets org au workflow central
Sans secrets: inherit, le workflow central n'aurait pas accès aux secrets. ✅