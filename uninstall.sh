#!/usr/bin/env bash
# Roll back what install.sh created. Asks before each destructive step.
#   ./uninstall.sh            remove generated config, keep tasks/ and acceptance/ evidence
#   ./uninstall.sh --purge    also delete the Hermes profile and the whole Chief directory
set -uo pipefail
cd "$(dirname "$0")"
SITE="${HCT_SITE_FILE:-$(pwd)/site.env}"
[ -f "$SITE" ] || { echo "no site.env; nothing to do" >&2; exit 1; }
set -a; . "$SITE"; set +a
CHIEF_DIR="$(python3 -c 'import os,sys,string;print(string.Template(sys.argv[1]).safe_substitute(os.environ))' "$HCT_CHIEF_DIR")"
WT_DIR="$(python3 -c 'import os,sys,string;print(string.Template(sys.argv[1]).safe_substitute(os.environ))' "$HCT_WORKTREES_DIR")"
PURGE=0; [ "${1:-}" = "--purge" ] && PURGE=1
ask() { read -r -p "$1 [y/N] " a; [ "$a" = "y" ] || [ "$a" = "Y" ]; }

if hermes profile list 2>/dev/null | grep -q "◆${HCT_HERMES_PROFILE}"; then
  ask "Switch Hermes sticky default back to 'default'?" && hermes profile use default
fi
echo "Worktrees created by teamctl live under $WT_DIR:"
ls "$WT_DIR" 2>/dev/null || true
if ask "Remove clean feature worktrees (dirty ones are kept and listed)?"; then
  for manifest in "$WT_DIR"/*/manifest.json; do
    [ -f "$manifest" ] || continue
    python3 - "$manifest" <<'PY'
import json, subprocess, sys
m = json.load(open(sys.argv[1]))
for key, r in m["repos"].items():
    dirty = subprocess.run(["git", "-C", r["worktree"], "status", "--porcelain"], capture_output=True, text=True).stdout
    if dirty.strip():
        print(f"  kept (dirty): {r['worktree']}")
        continue
    subprocess.run(["git", "-C", r["source_root"], "worktree", "remove", r["worktree"]], check=False)
    print(f"  removed: {r['worktree']} (branch {r['branch']} kept in {r['source_root']})")
PY
  done
fi
if ask "Remove generated config in $CHIEF_DIR (bin, runner, workers, team/projects/AGENTS, roles)?"; then
  rm -rf "$CHIEF_DIR/bin" "$CHIEF_DIR/runner" "$CHIEF_DIR/workers" "$CHIEF_DIR/roles" \
         "$CHIEF_DIR/team.json" "$CHIEF_DIR/projects.json" "$CHIEF_DIR/AGENTS.md" "$CHIEF_DIR/site.env"
  echo "  removed; tasks/ and acceptance/ kept"
fi
if [ "$PURGE" = 1 ]; then
  ask "Delete Hermes profile '${HCT_HERMES_PROFILE}' (sessions, memory, config)?" && hermes profile delete "$HCT_HERMES_PROFILE"
  ask "Delete $CHIEF_DIR entirely (tasks and evidence included)?" && rm -rf "$CHIEF_DIR"
fi
echo "Backups of files install.sh replaced are in $(dirname "$CHIEF_DIR")/.setup-backups/"
