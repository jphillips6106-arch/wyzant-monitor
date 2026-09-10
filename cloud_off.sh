#!/bin/zsh
# Turn the GitHub copy off: disable both workflows, cancel any live run,
# and let the Mac send emails again.
export PATH="$HOME/wyzant_monitor/bin:$PATH"
cd "$HOME/wyzant_monitor/github"
gh workflow disable monitor.yml; gh workflow disable watchdog.yml
for id in $(gh run list --status in_progress --json databaseId -q '.[].databaseId'; gh run list --status queued --json databaseId -q '.[].databaseId'); do gh run cancel "$id"; done
python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home()/"wyzant_monitor/config.json"; c = json.loads(p.read_text())
c["email_to"] = "philjoe@sas.upenn.edu"; c["watch_github"] = False
p.write_text(json.dumps(c, indent=2) + "\n")
PY
echo "GitHub copy OFF. Mac sends banner+Opera+email while awake."
