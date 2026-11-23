#!/usr/bin/env bash
# Bindet ein Produkt-Repo an die Software Factory an.
#
#   setup/attach-project.sh OWNER/REPO
#
# Legt die Labels an und erlaubt Actions, Pull Requests zu öffnen. Die Dateien aus
# templates/product/ (Workflow, Profil, Issue-Vorlage) kopierst du selbst ins Repo,
# die Secrets setzt du selbst.
set -euo pipefail

repo="${1:?Aufruf: setup/attach-project.sh OWNER/REPO}"

label() { gh label create "$1" --repo "$repo" --color "$2" --description "$3" --force >/dev/null; echo "Label $1"; }
label "factory:go"          "0e8a16" "Startet die Factory für dieses Issue"
label "factory:running"     "1d76db" "Die Factory arbeitet an diesem Issue"
label "factory:waiting"     "fbca04" "Die Factory wartet auf eine Antwort"
label "factory:review"      "5319e7" "Pull Request liegt zur Prüfung bereit"
label "factory:done"        "0e8a16" "Von der Factory automatisch gemergt"
label "factory:needs-human" "d93f0b" "Die Factory kommt allein nicht weiter"
label "factory:rejected"    "cccccc" "Kein Ticket für die Factory"

# Ohne diese Einstellung darf der Job-Token keinen Pull Request öffnen.
gh api -X PUT "repos/$repo/actions/permissions/workflow" \
  -f default_workflow_permissions=read -F can_approve_pull_request_reviews=true >/dev/null
echo "Actions dürfen Pull Requests öffnen"

cat <<EOF

Noch von Hand:
  1. templates/product/ ins Repo kopieren und .factory/profile.yaml anpassen
  2. Modell-Zugang als Secret setzen, eines von beiden:
       gh secret set ANTHROPIC_API_KEY --repo $repo
       gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo $repo     (aus: claude setup-token)
  3. Für den Richter: Deploy-Key des Szenarien-Repos als Secret SCENARIOS_DEPLOY_KEY
EOF
