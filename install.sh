#!/usr/bin/env bash
# Install or update the Chief team on this machine from site.env. Safe to re-run.
#   ./install.sh                 render + verify
#   ./install.sh --set-default   also make the Chief profile Hermes's sticky default (Desktop opens it)
#   ./install.sh --dry-run       show what would change
set -euo pipefail
cd "$(dirname "$0")"
REPO="$(pwd)"
SITE="${HCT_SITE_FILE:-$REPO/site.env}"
SET_DEFAULT=0; DRY=""
for arg in "$@"; do
  case "$arg" in
    --set-default) SET_DEFAULT=1 ;;
    --dry-run) DRY="--dry-run" ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [ ! -f "$SITE" ]; then
  cp site.env.example "$SITE"; chmod 600 "$SITE"
  echo "Created $SITE from the example. Fill in the <placeholders>, then re-run ./install.sh"; exit 1
fi
# shellcheck disable=SC1090
set -a; . "$SITE"; set +a
need() { command -v "$1" >/dev/null 2>&1 || { echo "missing required command: $1 ($2)" >&2; exit 1; }; }
need python3 "https://www.python.org/downloads/ (3.9+)"
need git "https://git-scm.com/"
need hermes "https://hermes-agent.nousresearch.com/docs/getting-started/installation"
need "${HCT_DATABRICKS_BIN:-databricks}" "https://docs.databricks.com/dev-tools/cli/install"
for tool in claude codex opencode; do need "$tool" "see docs/INSTALL.md"; done

echo "== Databricks auth (profile ${HCT_DATABRICKS_PROFILE})"
if ! "${HCT_DATABRICKS_BIN:-databricks}" auth token --profile "$HCT_DATABRICKS_PROFILE" -o json >/dev/null 2>&1; then
  echo "Token mint failed. Run: databricks auth login --profile $HCT_DATABRICKS_PROFILE --host $HCT_WORKSPACE_HOST" >&2
  exit 1
fi
echo "ok (token not shown)"

echo "== Hermes profile ${HCT_HERMES_PROFILE}"
if hermes profile list 2>/dev/null | grep -Eq "^[[:space:]]*[◆ ]*${HCT_HERMES_PROFILE}[[:space:]]"; then
  echo "exists"
elif [ -z "$DRY" ]; then
  hermes profile create "$HCT_HERMES_PROFILE" --description \
    "Chief: scopes multi-repo features and dispatches Planner/Coder/Reviewer/Tester work to external Claude Code, OpenCode and Codex CLIs via teamctl."
fi

echo "== Render templates"
python3 scripts/render.py "$SITE" $DRY
[ -n "$DRY" ] && exit 0

CHIEF_DIR="$(python3 -c 'import os,sys,string;print(string.Template(sys.argv[1]).safe_substitute(os.environ))' "$HCT_CHIEF_DIR")"
echo "== Hermes skills for Chief (claude-code, codex, opencode guides)"
for skill in claude-code codex opencode; do
  if hermes -p "$HCT_HERMES_PROFILE" skills list 2>/dev/null | grep -Fq " $skill "; then
    echo "  $skill: present"
  else
    echo "  $skill: missing; installing official/autonomous-ai-agents/$skill"
    hermes -p "$HCT_HERMES_PROFILE" skills install --yes "official/autonomous-ai-agents/$skill" || \
      echo "  (install failed; Chief still works, the guide is optional)" >&2
  fi
done

if [ "$SET_DEFAULT" = 1 ]; then
  hermes profile use "$HCT_HERMES_PROFILE"
fi

echo "== teamctl doctor"
"$CHIEF_DIR/bin/teamctl" doctor
echo
echo "Done. Open Chief: Hermes.app (profile rail → ${HCT_HERMES_PROFILE}) or: hermes -p ${HCT_HERMES_PROFILE} chat"
