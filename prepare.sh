#!/bin/zsh
# Mac side: export the Wyzant session and show what to paste into GitHub Secrets.
set -e
ROOT="$HOME/wyzant_monitor"; G="$ROOT/github"
"$ROOT/venv/bin/python" "$ROOT/cloud/export_session.py" "$G/auth_state.json" >/dev/null
echo "Session exported. Paste the contents of this file into the secret WYZANT_AUTH_STATE:"
echo "   $G/auth_state.json   (opens in TextEdit: open -e $G/auth_state.json)"
echo
echo "Secrets to create in the repo (Settings > Secrets and variables > Actions > New repository secret):"
echo "   WYZANT_AUTH_STATE   = contents of auth_state.json"
echo "   SMTP_USER           = philjoe@sas.upenn.edu"
echo "   SMTP_APP_PASSWORD   = your Google app password"
echo "   EMAIL_TO            = philjoe@sas.upenn.edu"
echo
echo "The whole auth_state.json is also on your clipboard now."
cat "$G/auth_state.json" | pbcopy
