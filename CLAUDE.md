# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

This is the **organization-level `.github` repo** for **D4E (Digital4Efficiency)**, an Odoo Silver Partner in Switzerland. It serves as the central hub for shared GitHub Actions workflows, issue templates, and automation scripts used across all repos in the `d4e-digital-4-efficiency` GitHub organization.

The primary language is **French** (code comments, commit messages, issue templates, user-facing messages). Follow this convention.

## Architecture

### Timesheet system (`@pointage`)

The core feature: developers log time to Odoo by commenting `@pointage 1h30` (or `2h`, `45`, `1:30`) on any GitHub issue across the org.

Flow:
1. **Client repos** have a thin workflow (`.github/workflows/timesheet.yml`) that calls the central reusable workflow via `workflow_call` + `secrets: inherit`
2. **Central workflow** (`.github/workflows/timesheet.yml`) checks out this repo, runs `log_timesheet.py`
3. **`log_timesheet.py`** parses the comment, resolves the GitHub user to an Odoo employee via `users_mapping.json`, reads the "Tache ID" custom field from GitHub Projects v2 (GraphQL), creates a timesheet entry in Odoo via XML-RPC, updates the "Pointage total" project field, and posts a confirmation comment on the issue

Key files:
- `.github/scripts/log_timesheet.py` — all timesheet logic (parsing, Odoo XML-RPC, GitHub GraphQL, comment posting)
- `.github/scripts/users_mapping.json` — GitHub login to Odoo employee_id mapping. Some users have `exclude_from_total: true` (their time is logged to Odoo but doesn't count toward the issue's total)
- `.github/workflows/timesheet.yml` — central reusable workflow
- `.github/REPO_CLIENT/workflows/timesheet.yml` — template for client repos

Duration parsing accepts: `1h30`, `1 h 30`, `1:30`, `2h`, `45` (minutes). Durations are rounded up to the nearest 15 minutes. The script also reads "Duree" labels on issues to detect time overruns.

### Deploy scripts

- `.github/scripts/deploy_timesheet.py` — deploy the timesheet workflow to specific repos (comma-separated input)
- `.github/scripts/deploy_timesheet_all.py` — deploy to all non-archived org repos (with exclusion list)

### Translation workflow

`.github/workflows/translate-mistral.yml` — uses Mistral AI to translate issues/comments between French and English when the `en` label is present.

### Issue templates

Three templates in `.github/ISSUE_TEMPLATE/`: Bug, Feature ("demande"), and Technical Task ("tache-technique"). All auto-assign to project `d4e-digital-4-efficiency/5`.

## Environment & secrets

Required org-level secrets: `ODOO_URL`, `ODOO_DB`, `ODOO_USERNAME`, `ODOO_PASSWORD`, `GH_PAT` (needs `project` scope for Projects v2 fields), `MISTRAL_API_KEY`.

## Development

- Python 3.11, only stdlib + `requests` (deploy scripts only; `log_timesheet.py` uses only stdlib)
- No test suite or linter configured
- To test `log_timesheet.py` locally, set the required env vars (see the workflow file for the full list)
