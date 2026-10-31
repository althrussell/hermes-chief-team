#!/usr/bin/env python3
"""Render this repo's templates into a Hermes profile and Chief's scratch directory.

Reads site.env, auto-detects executables left empty, validates the result, backs up any file it
is about to change, then writes the rendered files. Idempotent: unchanged files are left alone.
Usage: render.py <site.env> [--dry-run]
"""
import argparse
import os
import re
import shutil
import string
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REQUIRED = ["HCT_WORKSPACE_HOST", "HCT_DATABRICKS_PROFILE", "HCT_HERMES_PROFILE", "HCT_CHIEF_DIR",
            "HCT_WORKTREES_DIR", "HCT_PROJECT_WT_ROOT", "HCT_PROJECT_CT_ROOT", "HCT_MODEL_CHIEF",
            "HCT_MODEL_PLANNER", "HCT_MODEL_REVIEWER", "HCT_MODEL_CLAUDE", "HCT_MODEL_CODER",
            "HCT_MODEL_TESTER", "HCT_CHIEF_CONTEXT_LENGTH"]
DETECT = {"HCT_DATABRICKS_BIN": "databricks", "HCT_CLAUDE_BIN": "claude", "HCT_CODEX_BIN": "codex",
          "HCT_OPENCODE_BIN": "opencode"}
AUX_TASKS = ["vision", "compression", "skills_hub", "approval", "review", "mcp", "title_generation",
             "memory_query_rewrite", "tts_audio_tags", "voice_chat", "triage_specifier", "kanban_decomposer",
             "profile_describer", "goal_judge", "curator", "monitor", "background_review", "moa_reference",
             "moa_aggregator"]


def load_site(path: Path) -> dict:
    values = {"HOME": str(Path.home())}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = string.Template(value).safe_substitute(values)
    return values


def stable_which(name: str) -> str:
    found = shutil.which(name)
    return os.path.realpath(found) if found else ""


def resolve(values: dict) -> dict:
    for key, exe in DETECT.items():
        values[key] = values.get(key) or stable_which(exe)
    if not values.get("HCT_NODE_BIN_DIR"):
        node = stable_which("node")
        values["HCT_NODE_BIN_DIR"] = os.path.dirname(node) if node else ""
    values["HCT_PYTHON"] = values.get("HCT_PYTHON") or sys.executable
    values["HCT_WORKSPACE_HOST"] = values.get("HCT_WORKSPACE_HOST", "").rstrip("/")
    values["HCT_HERMES_PROFILE_DIR"] = str(Path.home() / ".hermes" / "profiles" / values["HCT_HERMES_PROFILE"])
    values["HCT_AUX_BLOCK"] = "".join(
        f"  {t}:\n    provider: unity-chief\n    model: {values['HCT_MODEL_CHIEF']}\n" for t in AUX_TASKS)
    return values


def validate(values: dict) -> list:
    problems = [f"{k} is not set" for k in REQUIRED if not values.get(k)]
    problems += [f"{k} still has a placeholder: {values[k]}" for k in values if re.search(r"<[^>]+>", values[k])]
    for key in list(DETECT) + ["HCT_PYTHON"]:
        if not values.get(key) or not os.access(values[key], os.X_OK):
            problems.append(f"{key} is not an executable: {values.get(key) or '(not found on PATH)'}")
    if not values.get("HCT_NODE_BIN_DIR") or not os.path.exists(os.path.join(values["HCT_NODE_BIN_DIR"], "node")):
        problems.append("HCT_NODE_BIN_DIR does not contain node (needed by codex/opencode)")
    for key in ("HCT_PROJECT_WT_ROOT", "HCT_PROJECT_CT_ROOT"):
        if values.get(key) and not (Path(values[key]) / ".git").exists():
            problems.append(f"{key} is not a git repository: {values[key]}")
    if not values.get("HCT_WORKSPACE_HOST", "").startswith("https://"):
        problems.append("HCT_WORKSPACE_HOST must start with https://")
    return problems


def plan(values: dict) -> list:
    p, c, t = Path(values["HCT_HERMES_PROFILE_DIR"]), Path(values["HCT_CHIEF_DIR"]), REPO / "templates"
    files = [
        (t / "hermes/config.yaml.tmpl", p / "config.yaml", 0o600),
        (t / "hermes/SOUL.md.tmpl", p / "SOUL.md", 0o644),
        (t / "hermes/skins/chief.yaml", p / "skins/chief.yaml", 0o644),
        (t / "chief/AGENTS.md.tmpl", c / "AGENTS.md", 0o644),
        (t / "chief/team.json.tmpl", c / "team.json", 0o644),
        (t / "chief/projects.json.tmpl", c / "projects.json", 0o644),
        (t / "workers/claude/settings.json.tmpl", c / "workers/claude-home/settings.json", 0o600),
        (t / "workers/claude/empty-mcp.json", c / "workers/claude-home/empty-mcp.json", 0o644),
        (t / "workers/codex/config.toml.tmpl", c / "workers/codex-home/config.toml", 0o600),
        (t / "workers/opencode/opencode.json.tmpl", c / "workers/opencode/opencode.json", 0o600),
        (t / "workers/opencode/plugin/hct-databricks-auth.js.tmpl",
         c / "workers/opencode/plugin/hct-databricks-auth.js", 0o644),
        (REPO / "bin/hct-token", c / "bin/hct-token", 0o755),
        (REPO / "runner/teamctl.py", c / "runner/teamctl.py", 0o755),
        (REPO / "runner/tests/test_teamctl.py", c / "runner/tests/test_teamctl.py", 0o644),
        (REPO / "runner/tests/fakes/fake-claude", c / "runner/tests/fakes/fake-claude", 0o755),
    ]
    files += [(f, c / "roles" / f.name, 0o644) for f in sorted((t / "chief/roles").glob("*.md"))]
    return files


def render_text(src: Path, values: dict) -> str:
    text = src.read_text()
    if src.suffix == ".tmpl":
        try:
            text = string.Template(text).substitute(values)
        except (KeyError, ValueError) as exc:
            sys.exit(f"render: {src.relative_to(REPO)} references an unknown value: {exc}")
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("site_env", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    values = resolve(load_site(args.site_env))
    problems = validate(values)
    if problems:
        print("render: site.env is not ready:\n  - " + "\n  - ".join(problems), file=sys.stderr)
        return 2
    chief = Path(values["HCT_CHIEF_DIR"])
    backup = Path(values["HCT_CHIEF_DIR"]).parent / ".setup-backups" / time.strftime("%Y%m%dT%H%M%S")
    outputs = [(dst, render_text(src, values), mode) for src, dst, mode in plan(values)]
    wrapper = (f"#!/bin/sh\n# Generated by hermes-chief-team install.sh\n"
               f"HCT_CHIEF_DIR='{chief}' exec '{values['HCT_PYTHON']}' '{chief}/runner/teamctl.py' \"$@\"\n")
    outputs.append((chief / "bin/teamctl", wrapper, 0o755))
    site_lines = [f"{k}={v}" for k, v in sorted(values.items()) if k.startswith("HCT_") and k != "HCT_AUX_BLOCK"]
    outputs.append((chief / "site.env", "# Resolved by install.sh; local only.\n" + "\n".join(site_lines) + "\n", 0o600))
    changed = 0
    for dst, text, mode in outputs:
        if dst.exists() and dst.read_text() == text:
            continue
        changed += 1
        print(f"{'would write' if args.dry_run else 'write'} {dst}")
        if args.dry_run:
            continue
        if dst.exists():
            target = backup / dst.relative_to(Path.home())
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, target)
            os.chmod(backup, 0o700)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text)
        os.chmod(dst, mode)
    if not args.dry_run:
        for d in ("tasks", "acceptance", "runner/locks"):
            (chief / d).mkdir(parents=True, exist_ok=True)
        Path(values["HCT_WORKTREES_DIR"]).mkdir(parents=True, exist_ok=True)
    print(f"render: {changed} file(s) {'to change' if args.dry_run else 'changed'}"
          + (f"; backups in {backup}" if changed and not args.dry_run and backup.exists() else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
