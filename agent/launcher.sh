#!/usr/bin/env bash
# lab-agent launcher: runs the current release in a loop and rolls back a
# release that keeps crashing before it registers. Installed as
# /usr/local/bin/lab-agent by scripts/setup-lab-node.sh; the Windows
# supervisor starts this, not run.sh.
set -u

ROOT="${LAB_AGENT_ROOT:-/opt/lab-agent}"
EXIT_UPDATED=75
MAX_FAILS="${LAB_AGENT_MAX_FAILS:-3}"
RESTART_DELAY="${LAB_AGENT_RESTART_DELAY:-5}"

switch_current() {
  ln -sfn "releases/$1" "$ROOT/current.new" &&
    python3 -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' "$ROOT/current.new" "$ROOT/current"
}

while true; do
  bash "$ROOT/current/run.sh" "$@"
  code=$?
  # The next agent reads this to tell the server why it restarted.
  echo "$code $(date +%s)" > "$ROOT/last_exit" 2>/dev/null || true
  if [ "$code" -eq "$EXIT_UPDATED" ]; then
    echo "lab-agent: updated to $(readlink "$ROOT/current"), restarting"
    continue
  fi
  if [ -f "$ROOT/pending" ]; then
    fails=$(( $(cat "$ROOT/pending.fails" 2>/dev/null || echo 0) + 1 ))
    echo "$fails" > "$ROOT/pending.fails"
    if [ "$fails" -ge "$MAX_FAILS" ]; then
      bad="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$ROOT/pending" 2>/dev/null)"
      prev="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["previous"])' "$ROOT/pending" 2>/dev/null)"
      if [ -n "$prev" ] && [ -d "$ROOT/releases/$prev" ]; then
        echo "lab-agent: release $bad failed $fails times before registering; rolling back to $prev"
        switch_current "$prev"
        # The agent will not take this version again until it is removed here.
        [ -n "$bad" ] && echo "$bad" >> "$ROOT/rejected"
      fi
      rm -f "$ROOT/pending" "$ROOT/pending.fails"
    fi
  fi
  echo "lab-agent: agent exited with $code; restarting in ${RESTART_DELAY}s"
  sleep "$RESTART_DELAY"
done
