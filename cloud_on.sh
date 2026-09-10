#!/bin/zsh
# Turn the GitHub copy back on: enable both workflows, start a monitor run,
# and hand email duty back to GitHub (Mac keeps banner + Opera).
set -e
export PATH="$HOME/wyzant_monitor/bin:$PATH"
cd "$HOME/wyzant_monitor/github"
gh workflow enable monitor.yml && gh workflow enable watchdog.yml
gh workflow run monitor.yml
python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home()/"wyzant_monitor/config.json"; c = json.loads(p.read_text())
c["email_to"] = ""; c["watch_github"] = True
p.write_text(json.dumps(c, indent=2) + "\n")
PY
echo "GitHub copy ON. Mac now banner+Opera only; GitHub sends email."
