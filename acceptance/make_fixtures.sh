#!/usr/bin/env bash
# Create two tiny interdependent git repos for the acceptance exercise (outside any product repo).
#   producer: publishes contract.json + greeting.py      consumer: renders the producer's contract
# Usage: make_fixtures.sh <dest-dir>   (refuses to overwrite an existing dest)
set -euo pipefail
DEST="$1"
[ -e "$DEST" ] && { echo "refusing: $DEST exists" >&2; exit 1; }
mkdir -p "$DEST/src"
g() { git -C "$1" -c user.email=fixtures@example.invalid -c user.name="hct fixtures" "${@:2}"; }

P="$DEST/src/producer"; mkdir -p "$P/tests"
cat > "$P/contract.json" <<'J'
{"name": "greeting", "version": 1, "fields": ["message", "recipient"]}
J
cat > "$P/greeting.py" <<'J'
"""Producer: builds greeting payloads that satisfy contract.json."""
import json
from pathlib import Path

CONTRACT = json.loads((Path(__file__).parent / "contract.json").read_text())


def greeting(recipient: str) -> dict:
    return {"message": f"Hello, {recipient}!", "recipient": recipient}
J
cat > "$P/tests/test_greeting.py" <<'J'
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from greeting import CONTRACT, greeting


class GreetingTest(unittest.TestCase):
    def test_payload_matches_contract(self):
        self.assertEqual(sorted(greeting("Ada")), sorted(CONTRACT["fields"]))
J
printf '# producer fixture\nRun tests: `python3 -m unittest discover -s tests`\n' > "$P/README.md"

C="$DEST/src/consumer"; mkdir -p "$C/tests"
cat > "$C/render.py" <<'J'
"""Consumer: renders producer payloads. PRODUCER_DIR points at the producer checkout."""
import json, os, sys
from pathlib import Path

PRODUCER_DIR = Path(os.environ.get("PRODUCER_DIR", Path(__file__).resolve().parent.parent / "producer"))
sys.path.insert(0, str(PRODUCER_DIR))
from greeting import CONTRACT, greeting  # noqa: E402

SUPPORTED_VERSION = 1


def render(recipient: str) -> str:
    if CONTRACT["version"] != SUPPORTED_VERSION:
        raise RuntimeError(f"unsupported contract version {CONTRACT['version']}")
    payload = greeting(recipient)
    return " | ".join(f"{k}={payload[k]}" for k in CONTRACT["fields"])
J
cat > "$C/tests/test_render.py" <<'J'
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from render import render


class RenderTest(unittest.TestCase):
    def test_render_lists_every_contract_field(self):
        self.assertEqual(render("Ada"), "message=Hello, Ada! | recipient=Ada")
J
printf '# consumer fixture\nRun tests: `PRODUCER_DIR=<producer worktree> python3 -m unittest discover -s tests`\n' > "$C/README.md"

for r in "$P" "$C"; do
  git -C "$r" init -q -b main
  g "$r" add .
  g "$r" commit -q -m "fixture: initial commit"
done
cat > "$DEST/projects.json" <<J
{"version": 1, "worktrees_root": "$DEST/worktrees",
 "repos": {"producer": {"name": "Producer fixture", "root": "$P", "default_base": "main"},
           "consumer": {"name": "Consumer fixture", "root": "$C", "default_base": "main"}},
 "relationship": "consumer renders payloads defined by producer/contract.json; contract changes must land in both"}
J
echo "fixtures ready: $DEST"
