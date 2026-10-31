#!/usr/bin/env python3
"""teamctl: launch and track Chief's external worker CLIs (Claude Code, OpenCode, Codex).

Every role is pinned in team.json to one harness, executable, model and config directory.
teamctl composes argv arrays (never shell strings), runs one attempt per invocation under
locks, and persists attempt state, logs, a structured result and a concise receipt.

Commands:
  doctor                              check executables, config refs, model bindings, auth
  run ROLE TASK BRIEF MANIFEST        launch one attempt of ROLE for TASK
  status [TASK] [--json]              show attempts, process identity, outcomes, artifacts
  cancel TASK [ATTEMPT]               stop the owned runner and its child process group
  reconcile [TASK]                    compare persisted state with live processes
  workspace prepare FEATURE ...       create the feature's worktree pair and manifest

Exit codes: 0 harness exited 0 | 1 harness exited non-zero | 2 usage/validation error |
3 conflict (duplicate dispatch, busy attempt or pair) | 4 launch failure | 5 timeout |
6 cancelled or interrupted.
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Optional

EXIT_OK, EXIT_HARNESS, EXIT_USAGE, EXIT_CONFLICT, EXIT_LAUNCH, EXIT_TIMEOUT, EXIT_STOPPED = 0, 1, 2, 3, 4, 5, 6
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
ACTIVE_STATES = {"launching", "running"}
UNRESOLVED_STATES = ACTIVE_STATES | {"uncertain"}
# Provider credentials and routing that a worker must never inherit from Chief's environment.
SCRUBBED_ENV = {
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
    "ANTHROPIC_CUSTOM_HEADERS", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "CODEX_HOME",
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY", "OPENROUTER_API_KEY",
    "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG_CONTENT", "DATABRICKS_TOKEN",
    "DATABRICKS_BEARER", "HERMES_HOME",
}
TERMINATE_GRACE_S = 10


class TeamctlError(Exception):
    def __init__(self, message: str, code: int = EXIT_USAGE):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------- paths and files


def chief_dir() -> Path:
    return Path(os.environ.get("HCT_CHIEF_DIR") or Path(__file__).resolve().parent.parent)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def write_json_atomic(path: Path, data: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def read_json(path: Path) -> Any:
    with open(path) as fh:
        return json.load(fh)


@contextlib.contextmanager
def flock(path: Path, blocking: bool = True):
    """Exclusive advisory lock; yields the open fd. Raises TeamctlError(EXIT_CONFLICT) if busy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                raise TeamctlError(f"lock busy: {path}", EXIT_CONFLICT) from None
            raise
        yield fd
    finally:
        os.close(fd)


def lock_is_held(path: Path) -> bool:
    if not path.exists():
        return False
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)
    return False


def validate_id(kind: str, value: str) -> str:
    if not ID_RE.match(value or ""):
        raise TeamctlError(f"invalid {kind} {value!r}: use lowercase letters, digits, '.', '_' or '-' (max 64)")
    return value


# ---------------------------------------------------------------------------- process identity


def process_start(pid: int) -> Optional[str]:
    """Kernel start time of ``pid`` as reported by ps, or None when the process is gone."""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = out.stdout.strip()
    return text or None


def same_process(pid: Optional[int], start: Optional[str]) -> bool:
    return bool(pid) and start is not None and process_start(pid) == start


def kill_group(pgid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


# ---------------------------------------------------------------------------- configuration


class Team:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "team.json"
        if not self.path.is_file():
            raise TeamctlError(f"team.json not found at {self.path}")
        self.data = read_json(self.path)
        self.roles: dict[str, dict] = self.data.get("roles") or {}

    def role(self, name: str) -> dict:
        if name not in self.roles:
            raise TeamctlError(f"unknown role {name!r}; allowed: {', '.join(sorted(self.roles))}")
        return self.roles[name]

    @property
    def tasks_dir(self) -> Path:
        return self.root / "tasks"

    @property
    def locks_dir(self) -> Path:
        return self.root / "runner" / "locks"


def load_manifest(path: Path) -> dict:
    if not path.is_file():
        raise TeamctlError(f"workspace manifest not found: {path}")
    manifest = read_json(path)
    repos = manifest.get("repos") or {}
    if not repos:
        raise TeamctlError(f"manifest {path} lists no repos")
    for key, repo in repos.items():
        wt = Path(repo.get("worktree", ""))
        if not wt.is_dir() or not (wt / ".git").exists():
            raise TeamctlError(f"manifest repo {key!r} worktree is not a git worktree: {wt}")
    return manifest


def git(cwd: Path, *args: str, check: bool = True) -> str:
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=120)
    if check and out.returncode != 0:
        raise TeamctlError(f"git {' '.join(args)} failed in {cwd}: {out.stderr.strip()[:300]}")
    return out.stdout.strip()


def repo_identity(worktree: Path) -> dict:
    return {
        "worktree": str(worktree),
        "branch": git(worktree, "branch", "--show-current", check=False),
        "head": git(worktree, "rev-parse", "HEAD", check=False),
        "dirty_paths": len([l for l in git(worktree, "status", "--porcelain", check=False).splitlines() if l]),
    }


# ---------------------------------------------------------------------------- harness adapters


def _role_paths(team: Team, role: dict) -> dict:
    return {k: str(Path(v).expanduser()) for k, v in (role.get("paths") or {}).items()}


def scoped_env(team: Team, role: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV and not k.startswith("HCT_TEST_")}
    for key, value in (role.get("env") or {}).items():
        env[key] = str(value)
    prepend = [str(Path(p).expanduser()) for p in role.get("path_prepend") or []]
    env["PATH"] = os.pathsep.join(prepend + [env.get("PATH", "/usr/bin:/bin")])
    env["HCT_SITE_ENV"] = str(team.root / "site.env")
    for key in ("HCT_TEST_CHILD_SLEEP", "HCT_TEST_CHILD_EXIT"):  # test fixtures only
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def build_claude(team, role, ctx) -> tuple[list[str], Optional[Path]]:
    paths = _role_paths(team, role)
    argv = [role["executable"], "-p", "--model", role["model"], "--output-format", "json",
            "--session-id", ctx["native_session_id"], "--settings", paths["settings"],
            "--setting-sources", "project,local", "--strict-mcp-config", "--mcp-config", paths["mcp_config"],
            "--permission-mode", role.get("permission_mode", "dontAsk")]
    for d in ctx["add_dirs"]:
        argv += ["--add-dir", d]
    allowed = [t.replace("{output_dir}", ctx["output_dir"]) for t in role.get("allowed_tools", [])]
    if allowed:
        argv += ["--allowedTools", *allowed]
    if role.get("disallowed_tools"):
        argv += ["--disallowedTools", *role["disallowed_tools"]]
    return argv, ctx["brief_copy"]  # prompt on stdin


def build_codex(team, role, ctx) -> tuple[list[str], Optional[Path]]:
    argv = [role["executable"], "exec", "--json", "--model", role["model"],
            "--sandbox", role.get("sandbox", "workspace-write"), "--cd", ctx["cwd"],
            "-o", str(ctx["attempt_dir"] / "last-message.md")]
    for d in ctx["add_dirs"]:
        argv += ["--add-dir", d]
    argv.append("-")  # prompt on stdin
    return argv, ctx["brief_copy"]


def build_opencode(team, role, ctx) -> tuple[list[str], Optional[Path]]:
    argv = [role["executable"], "run", "--format", "json", "--model", f"{role['provider_id']}/{role['model']}",
            "--dir", ctx["cwd"], "--title", f"{ctx['task']}:{ctx['attempt_id']}", "-f", str(ctx["brief_copy"]),
            "--", "Follow the attached brief exactly. It is your complete assignment."]
    return argv, None


ADAPTERS = {"claude-code": build_claude, "codex": build_codex, "opencode": build_opencode}


def parse_result(harness: str, stdout_path: Path, attempt_dir: Path) -> dict:
    text = stdout_path.read_text(errors="replace") if stdout_path.exists() else ""
    result: dict[str, Any] = {"harness": harness, "stdout_bytes": len(text.encode())}
    events = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            with contextlib.suppress(ValueError):
                events.append(json.loads(line))
    if harness == "claude-code":
        final = next((e for e in reversed(events) if e.get("type") == "result"), None)
        if final is None:
            with contextlib.suppress(ValueError):
                final = json.loads(text) if text.strip().startswith("{") else None
        if final:
            result.update({"final_text": final.get("result"), "is_error": final.get("is_error"),
                           "subtype": final.get("subtype"), "native_session_id": final.get("session_id"),
                           "models_observed": sorted((final.get("modelUsage") or {}).keys()),
                           "num_turns": final.get("num_turns"), "usage": final.get("usage")})
    elif harness == "codex":
        msgs = [e for e in events if (e.get("item") or {}).get("type") == "agent_message"]
        thread = next((e.get("thread_id") for e in events if e.get("type") == "thread.started"), None)
        last = attempt_dir / "last-message.md"
        result.update({"native_session_id": thread,
                       "final_text": last.read_text(errors="replace") if last.exists() else
                       (msgs[-1]["item"].get("text") if msgs else None),
                       "is_error": any(e.get("type") in ("turn.failed", "error") for e in events),
                       "errors": [e for e in events if e.get("type") in ("turn.failed", "error")][-3:],
                       "usage": next((e.get("usage") for e in reversed(events) if e.get("type") == "turn.completed"), None)})
    elif harness == "opencode":
        texts = [((e.get("part") or {}).get("text")) for e in events if e.get("type") == "text"]
        sessions = {e.get("sessionID") for e in events if e.get("sessionID")}
        models = sorted({f"{(e.get('part') or {}).get('providerID') or e.get('providerID')}/"
                         f"{(e.get('part') or {}).get('modelID') or e.get('modelID')}"
                         for e in events if (e.get("part") or {}).get("modelID") or e.get("modelID")})
        result.update({"native_session_id": next(iter(sessions), None), "final_text": (texts[-1] if texts else None),
                       "models_observed": models,
                       "is_error": any(e.get("type") == "error" for e in events),
                       "errors": [e for e in events if e.get("type") == "error"][-3:]})
    result["event_count"] = len(events)
    if result.get("final_text") and len(result["final_text"]) > 20000:
        result["final_text"] = result["final_text"][:20000] + "\n…[truncated; see stdout.log]"
    return result


def collect_native_evidence(team: Team, role: dict, result: dict, attempt_dir: Path) -> None:
    """Add runtime model evidence from the harness's own session store (not the worker's claims)."""
    session = result.get("native_session_id")
    if not session:
        return
    if role["harness"] == "opencode":
        try:
            out = subprocess.run([role["executable"], "export", session], capture_output=True, text=True,
                                 timeout=120, env=scoped_env(team, role))
            data = json.loads(out.stdout[out.stdout.find("{"):])
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            result["evidence_error"] = f"opencode export failed: {type(exc).__name__}"
            return
        write_json_atomic(attempt_dir / "native-session-export.json", data)
        infos = [m.get("info", {}) for m in data.get("messages", []) if m.get("info", {}).get("role") == "assistant"]
        result["models_observed"] = sorted({f"{i.get('providerID')}/{i.get('modelID')}" for i in infos})
        result["evidence_source"] = "opencode export (assistant message provider/model)"
    elif role["harness"] == "codex":
        home = Path((role.get("env") or {}).get("CODEX_HOME", Path.home() / ".codex")).expanduser()
        rollouts = sorted((home / "sessions").rglob(f"rollout-*{session}.jsonl"))
        if not rollouts:
            result["evidence_error"] = "codex rollout not found"
            return
        models, providers = set(), set()
        for line in rollouts[-1].read_text(errors="replace").splitlines():
            with contextlib.suppress(ValueError):
                payload = json.loads(line).get("payload") or {}
                if payload.get("model"):
                    models.add(payload["model"])
                if payload.get("model_provider"):
                    providers.add(payload["model_provider"])
        result.update({"models_observed": sorted(models), "model_providers_observed": sorted(providers),
                       "evidence_source": str(rollouts[-1])})
    elif role["harness"] == "claude-code":
        result["evidence_source"] = "claude -p result.modelUsage"


def managed_policies() -> dict:
    """Machine-level policies that outrank teamctl's scoped worker configs."""
    found = {}
    codex = Path("/etc/codex/managed_config.toml")
    if codex.exists():
        text = codex.read_text(errors="replace")
        found["codex"] = {"path": str(codex), "base_url": (re.findall(r'base_url\s*=\s*"([^"]+)"', text) or [None])[0],
                          "model_provider": (re.findall(r'^model_provider\s*=\s*"([^"]+)"', text, re.M) or [None])[0]}
    claude = Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    if claude.exists():
        with contextlib.suppress(ValueError):
            data = json.loads(claude.read_text())
            found["claude-code"] = {"path": str(claude), "base_url": (data.get("env") or {}).get("ANTHROPIC_BASE_URL"),
                                    "api_key_helper_set": bool(data.get("apiKeyHelper")),
                                    "hooks": sorted((data.get("hooks") or {}).keys())}
    return found


# ---------------------------------------------------------------------------- attempts


class Attempt:
    def __init__(self, team: Team, task: str, attempt_id: str):
        self.team, self.task, self.id = team, task, attempt_id
        self.dir = team.tasks_dir / task / "attempts" / attempt_id
        self.state_path = self.dir / "attempt.json"
        self.lock_path = self.dir / ".runner.lock"

    def load(self) -> dict:
        return read_json(self.state_path)

    def save(self, state: dict) -> None:
        state["updated"] = now_iso()
        write_json_atomic(self.state_path, state)


def list_attempts(team: Team, task: Optional[str] = None) -> list[Attempt]:
    base = team.tasks_dir
    tasks = [task] if task else sorted(p.name for p in base.iterdir() if p.is_dir()) if base.exists() else []
    found = []
    for t in tasks:
        adir = base / t / "attempts"
        if adir.exists():
            found += [Attempt(team, t, p.name) for p in sorted(adir.iterdir()) if (p / "attempt.json").exists()]
    return found


def pair_key(manifest: dict) -> str:
    roots = sorted(str(Path(r["worktree"]).resolve()) for r in manifest["repos"].values())
    return hashlib.sha256("\n".join(roots).encode()).hexdigest()[:16]


def allocate_attempt(team: Team, task: str, role_name: str, dispatch_id: Optional[str]) -> Attempt:
    """Create the next attempt for (task, role) under the task lock; refuse duplicates."""
    with flock(team.tasks_dir / task / ".task.lock"):
        existing = list_attempts(team, task)
        for att in existing:
            st = att.load()
            if dispatch_id and st.get("dispatch_id") == dispatch_id:
                raise TeamctlError(f"dispatch {dispatch_id!r} already exists as attempt {att.id} "
                                   f"(state {st.get('state')}); not launching again", EXIT_CONFLICT)
            if st.get("role") == role_name and st.get("state") in UNRESOLVED_STATES:
                raise TeamctlError(f"role {role_name!r} already has attempt {att.id} in state {st.get('state')} "
                                   f"for task {task!r}; run `teamctl reconcile {task}` first", EXIT_CONFLICT)
        number = 1 + max([int(a.id.split("-", 1)[0]) for a in existing if a.id[:3].isdigit()] or [0])
        att = Attempt(team, task, f"{number:03d}-{role_name}")
        att.dir.mkdir(parents=True, exist_ok=False)
        os.chmod(att.dir, 0o700)
        att.save({"task": task, "attempt": att.id, "role": role_name, "dispatch_id": dispatch_id,
                  "state": "launching", "created": now_iso()})
        return att


def run_attempt(team: Team, role_name: str, task: str, brief: Path, manifest_path: Path,
                dispatch_id: Optional[str], timeout_s: Optional[int]) -> int:
    validate_id("task", task)
    if dispatch_id:
        validate_id("dispatch id", dispatch_id)
    role = team.role(role_name)
    harness = role.get("harness")
    if harness not in ADAPTERS:
        raise TeamctlError(f"role {role_name!r} has unsupported harness {harness!r}")
    if not brief.is_file():
        raise TeamctlError(f"brief not found: {brief}")
    manifest = load_manifest(manifest_path)
    primary_key = role.get("primary_repo") if role.get("primary_repo") in manifest["repos"] else \
        manifest.get("primary") or sorted(manifest["repos"])[0]
    cwd = Path(manifest["repos"][primary_key]["worktree"]).resolve()

    pair_lock_path = team.locks_dir / f"pair-{pair_key(manifest)}.lock"
    att = allocate_attempt(team, task, role_name, dispatch_id)
    output_dir = att.dir / "output"
    output_dir.mkdir(mode=0o700)
    brief_copy = att.dir / "brief.md"
    shutil.copyfile(brief, brief_copy)
    os.chmod(brief_copy, 0o600)
    add_dirs = [str(Path(r["worktree"]).resolve()) for k, r in sorted(manifest["repos"].items()) if k != primary_key]
    add_dirs.append(str(output_dir))
    ctx = {"task": task, "attempt_id": att.id, "attempt_dir": att.dir, "output_dir": str(output_dir),
           "brief_copy": brief_copy, "cwd": str(cwd), "add_dirs": add_dirs,
           "native_session_id": str(uuid.uuid4())}
    state = att.load()
    state.update({"harness": harness, "model": role["model"], "executable": role["executable"],
                  "config_ref": role.get("config_ref"), "manifest": str(manifest_path.resolve()),
                  "cwd": str(cwd), "add_dirs": add_dirs, "output_dir": str(output_dir),
                  "brief": str(brief_copy), "brief_source": str(brief.resolve()),
                  "repos_before": {k: repo_identity(Path(r["worktree"])) for k, r in manifest["repos"].items()}})
    try:
        pair_lock = flock(pair_lock_path, blocking=False)
        pair_lock.__enter__()
    except TeamctlError as exc:
        state.update({"state": "rejected", "outcome": "conflicting_writer", "error": str(exc), "ended": now_iso()})
        att.save(state)
        raise TeamctlError(f"worktree pair is busy with another attempt ({pair_lock_path.name}); "
                           f"attempt {att.id} recorded as rejected", EXIT_CONFLICT) from None
    try:
        with flock(att.lock_path, blocking=False):
            return _launch_and_wait(team, role, att, state, ctx, timeout_s or role.get("timeout_s", 3600))
    finally:
        pair_lock.__exit__(None, None, None)


def _launch_and_wait(team, role, att: Attempt, state: dict, ctx: dict, timeout_s: int) -> int:
    argv, stdin_path = ADAPTERS[role["harness"]](team, role, ctx)
    write_json_atomic(att.dir / "argv.json", argv)
    stdout_path, stderr_path = att.dir / "stdout.log", att.dir / "stderr.log"
    stop = {"signal": None}

    def _on_signal(signum, _frame):
        stop["signal"] = signum

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _on_signal)
    state.update({"runner_pid": os.getpid(), "runner_start": process_start(os.getpid()),
                  "native_session_id": ctx["native_session_id"] if role["harness"] == "claude-code" else None,
                  "timeout_s": timeout_s, "stdout": str(stdout_path), "stderr": str(stderr_path)})
    try:
        with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err, \
                (open(stdin_path, "rb") if stdin_path else open(os.devnull, "rb")) as inp:
            os.chmod(stdout_path, 0o600)
            os.chmod(stderr_path, 0o600)
            proc = subprocess.Popen(argv, cwd=ctx["cwd"], env=scoped_env(team, role), stdin=inp,
                                    stdout=out, stderr=err, start_new_session=True)
    except OSError as exc:
        state.update({"state": "exited", "outcome": "launch_failed", "error": f"{type(exc).__name__}: {exc}",
                      "ended": now_iso()})
        att.save(state)
        return _finish(att, state, EXIT_LAUNCH)
    state.update({"state": "running", "started": now_iso(), "child_pid": proc.pid, "child_pgid": proc.pid,
                  "child_start": process_start(proc.pid)})
    att.save(state)
    deadline = time.monotonic() + timeout_s
    timed_out = False
    while True:
        try:
            code = proc.wait(timeout=1)
            break
        except subprocess.TimeoutExpired:
            pass
        if stop["signal"] or time.monotonic() > deadline:
            timed_out = not stop["signal"]
            kill_group(proc.pid, signal.SIGTERM)
            try:
                code = proc.wait(timeout=TERMINATE_GRACE_S)
            except subprocess.TimeoutExpired:
                kill_group(proc.pid, signal.SIGKILL)
                code = proc.wait()
            break
    kill_group(proc.pid, signal.SIGTERM)  # reap stragglers left in the owned group
    cancel_flag = att.dir / "cancel.requested"
    if timed_out:
        outcome, rc = "timeout", EXIT_TIMEOUT
    elif stop["signal"]:
        outcome = "cancelled" if cancel_flag.exists() else "interrupted"
        rc = EXIT_STOPPED
    elif code == 0:
        outcome, rc = "harness_exited_zero", EXIT_OK
    else:
        outcome, rc = "harness_failed", EXIT_HARNESS
    result = parse_result(role["harness"], stdout_path, att.dir)
    collect_native_evidence(team, role, result, att.dir)
    if rc == EXIT_OK and (result.get("is_error") or not result.get("final_text")):
        outcome = "harness_exited_zero_without_usable_result"
    write_json_atomic(att.dir / "result.json", result)
    manifest = read_json(Path(state["manifest"]))
    state.update({"state": "exited", "outcome": outcome, "exit_code": code, "ended": now_iso(),
                  "signal": stop["signal"], "native_session_id": result.get("native_session_id") or
                  state.get("native_session_id"), "models_observed": result.get("models_observed"),
                  "model_providers_observed": result.get("model_providers_observed"),
                  "managed_policies": managed_policies(),
                  "repos_after": {k: repo_identity(Path(r["worktree"])) for k, r in manifest["repos"].items()}})
    att.save(state)
    return _finish(att, state, rc)


def _finish(att: Attempt, state: dict, rc: int) -> int:
    outputs = sorted(str(p) for p in (att.dir / "output").rglob("*") if p.is_file()) \
        if (att.dir / "output").exists() else []
    receipt = {"task": att.task, "attempt": att.id, "role": state.get("role"), "harness": state.get("harness"),
               "model": state.get("model"), "outcome": state.get("outcome"), "exit_code": state.get("exit_code"),
               "process_note": "process outcome only; assess acceptance criteria from the artifacts",
               "attempt_dir": str(att.dir), "result": str(att.dir / "result.json"),
               "stdout": state.get("stdout"), "stderr": state.get("stderr"), "outputs": outputs,
               "native_session_id": state.get("native_session_id"), "models_observed": state.get("models_observed")}
    write_json_atomic(att.dir / "receipt.json", receipt)
    print("TEAMCTL_RECEIPT " + json.dumps(receipt, sort_keys=True), flush=True)
    return rc


# ---------------------------------------------------------------------------- cancel / reconcile / status


def cancel(team: Team, task: str, attempt_id: Optional[str]) -> int:
    candidates = [a for a in list_attempts(team, validate_id("task", task))
                  if (attempt_id is None or a.id == attempt_id) and a.load().get("state") in ACTIVE_STATES]
    if not candidates:
        raise TeamctlError(f"no active attempt for task {task!r}" + (f" / {attempt_id}" if attempt_id else ""))
    for att in candidates:
        st = att.load()
        (att.dir / "cancel.requested").write_text(now_iso() + "\n")
        if same_process(st.get("runner_pid"), st.get("runner_start")):
            os.kill(st["runner_pid"], signal.SIGTERM)  # the runner terminates its own child group
            print(f"cancel requested: {att.task}/{att.id} (runner {st['runner_pid']})")
        elif same_process(st.get("child_pid"), st.get("child_start")):
            kill_group(st["child_pgid"], signal.SIGTERM)
            st.update({"state": "uncertain", "outcome": "cancelled_orphan_child", "ended": now_iso()})
            att.save(st)
            print(f"runner gone; terminated orphan child group of {att.task}/{att.id}")
        else:
            print(f"{att.task}/{att.id}: no matching live process; run reconcile")
    return EXIT_OK


def reconcile(team: Team, task: Optional[str]) -> int:
    changed = 0
    for att in list_attempts(team, validate_id("task", task) if task else None):
        st = att.load()
        if st.get("state") not in ACTIVE_STATES:
            continue
        if lock_is_held(att.lock_path) and same_process(st.get("runner_pid"), st.get("runner_start")):
            continue  # healthy, still running
        child_alive = same_process(st.get("child_pid"), st.get("child_start"))
        writes = team.roles.get(st.get("role"), {}).get("writes_product_code", False)
        st.update({"state": "uncertain", "ended": st.get("ended") or now_iso(), "reconciled": now_iso(),
                   "outcome": "runner_lost_child_alive" if child_alive else "interrupted_unknown_completion",
                   "may_have_changed_files": bool(writes) or child_alive,
                   "repos_at_reconcile": {k: repo_identity(Path(r["worktree"]))
                                          for k, r in read_json(Path(st["manifest"]))["repos"].items()}
                   if st.get("manifest") and Path(st["manifest"]).exists() else None})
        att.save(st)
        changed += 1
        print(f"{att.task}/{att.id}: marked uncertain ({st['outcome']}); not relaunched. "
              f"Inspect {att.dir} and record a decision with `teamctl resolve`.")
    if not changed:
        print("reconcile: no interrupted attempts")
    return EXIT_OK


def resolve(team: Team, task: str, attempt_id: str, decision: str, note: str) -> int:
    att = Attempt(team, validate_id("task", task), attempt_id)
    st = att.load()
    if st.get("state") != "uncertain":
        raise TeamctlError(f"{task}/{attempt_id} is {st.get('state')!r}, not uncertain")
    if lock_is_held(att.lock_path):
        raise TeamctlError("runner lock still held; refusing to resolve a live attempt", EXIT_CONFLICT)
    st.update({"state": "resolved", "resolution": {"decision": decision, "note": note, "at": now_iso()}})
    att.save(st)
    print(f"{task}/{attempt_id}: resolved as {decision}")
    return EXIT_OK


def status(team: Team, task: Optional[str], as_json: bool) -> int:
    rows = []
    for att in list_attempts(team, validate_id("task", task) if task else None):
        st = att.load()
        live = same_process(st.get("child_pid"), st.get("child_start")) if st.get("state") in ACTIVE_STATES else False
        rows.append({"task": att.task, "attempt": att.id, "role": st.get("role"), "state": st.get("state"),
                     "outcome": st.get("outcome"), "exit_code": st.get("exit_code"), "child_pid": st.get("child_pid"),
                     "child_alive": live, "started": st.get("started"), "ended": st.get("ended"),
                     "native_session_id": st.get("native_session_id"), "attempt_dir": str(att.dir)})
    if as_json:
        print(json.dumps(rows, indent=2))
    elif not rows:
        print("no attempts")
    else:
        for r in rows:
            print(f"{r['task']:24} {r['attempt']:18} {r['state']:10} {str(r['outcome']):40} "
                  f"exit={r['exit_code']} pid={r['child_pid']} alive={r['child_alive']}")
            print(f"    {r['attempt_dir']}")
    return EXIT_OK


# ---------------------------------------------------------------------------- workspace prepare


def workspace_prepare(team: Team, feature: str, repo_specs: list[str], branch_prefix: str,
                      worktrees_root: Optional[str], projects_file: Optional[Path] = None,
                      detach: bool = False) -> int:
    validate_id("feature", feature)
    projects = read_json(projects_file or team.root / "projects.json")
    root = Path(worktrees_root or projects.get("worktrees_root") or "~/Hermes/Worktrees").expanduser() / feature
    manifest_path = root / "manifest.json"
    wanted = {}
    for spec in repo_specs:
        key, _, base = spec.partition("=")
        if key not in projects.get("repos", {}):
            raise TeamctlError(f"unknown repo key {key!r}; known: {', '.join(projects.get('repos', {}))}")
        wanted[key] = base or projects["repos"][key].get("default_base", "origin/main")
    with flock(team.locks_dir / f"workspace-{feature}.lock"):
        manifest = read_json(manifest_path) if manifest_path.exists() else {
            "feature": feature, "created": now_iso(), "repos": {}}
        for key, base in sorted(wanted.items()):
            source = Path(projects["repos"][key]["root"]).expanduser()
            worktree = root / key
            branch = None if detach else f"{branch_prefix}{feature}"
            if key in manifest["repos"]:
                if Path(manifest["repos"][key]["worktree"]).is_dir():
                    continue
                raise TeamctlError(f"manifest lists {key} but {worktree} is missing; inspect before re-creating")
            base_sha = git(source, "rev-parse", "--verify", f"{base}^{{commit}}")
            if worktree.exists():
                raise TeamctlError(f"{worktree} exists but is not recorded in {manifest_path}; refusing to reuse it")
            if branch and git(source, "branch", "--list", branch):
                raise TeamctlError(f"branch {branch} already exists in {source}; choose another feature id")
            if branch:
                git(source, "worktree", "add", "-b", branch, str(worktree), base_sha)
            else:  # read-only inspection: no branch is created in the source repo
                git(source, "worktree", "add", "--detach", str(worktree), base_sha)
            manifest["repos"][key] = {"source_root": str(source), "worktree": str(worktree), "branch": branch,
                                      "base_ref": base, "base_sha": base_sha}
        manifest["primary"] = manifest.get("primary") or sorted(manifest["repos"])[0]
        manifest["updated"] = now_iso()
        write_json_atomic(manifest_path, manifest, mode=0o644)
    task_dir = team.tasks_dir / feature
    task_dir.mkdir(parents=True, exist_ok=True)
    link = task_dir / "workspace.json"
    write_json_atomic(link, {"manifest": str(manifest_path)}, mode=0o644)
    print(json.dumps({"manifest": str(manifest_path), **manifest}, indent=2))
    return EXIT_OK


# ---------------------------------------------------------------------------- doctor


def doctor(team: Team) -> int:
    problems = 0

    def report(ok: bool, label: str, detail: str = "") -> None:
        nonlocal problems
        problems += 0 if ok else 1
        print(f"  {'ok ' if ok else 'ERR'} {label}{(' - ' + detail) if detail else ''}")

    print(f"chief dir: {team.root}")
    report(os.access(team.root, os.W_OK), "chief dir writable")
    report(not (team.root / ".git").exists(), "chief dir is not a git repo")
    token_helper = team.root / "bin" / "hct-token"
    try:
        out = subprocess.run([str(token_helper)], capture_output=True, text=True, timeout=60,
                             env={**os.environ, "HCT_SITE_ENV": str(team.root / "site.env")})
        report(out.returncode == 0 and len(out.stdout.strip()) > 40, "token helper mints a bearer",
               "token not shown" if out.returncode == 0 else out.stderr.strip()[:200])
    except (OSError, subprocess.TimeoutExpired) as exc:
        report(False, "token helper mints a bearer", type(exc).__name__)
    for name, role in sorted(team.roles.items()):
        print(f"role {name}: {role.get('harness')} {role.get('model')}")
        exe = Path(role.get("executable", ""))
        report(exe.is_file() and os.access(exe, os.X_OK), "executable", str(exe))
        if exe.is_file():
            try:
                ver = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=60,
                                     env=scoped_env(team, role))
                report(ver.returncode == 0, "version", (ver.stdout or ver.stderr).strip().splitlines()[0][:80]
                       if (ver.stdout or ver.stderr).strip() else "")
            except (OSError, subprocess.TimeoutExpired) as exc:
                report(False, "version", type(exc).__name__)
        for key, path in _role_paths(team, role).items():
            report(Path(path).exists(), f"config {key}", path)
        report(role.get("harness") in ADAPTERS, "harness supported")
    host = (team.data.get("gateway") or {}).get("workspace_host", "")
    for harness, policy in managed_policies().items():
        same = bool(policy.get("base_url")) and policy["base_url"].startswith(host)
        report(same, f"managed {harness} policy routes to this workspace",
               f"{policy['path']} -> {policy.get('base_url')} (outranks teamctl's scoped config)")
    projects_path = team.root / "projects.json"
    if projects_path.exists():
        for key, repo in read_json(projects_path).get("repos", {}).items():
            root = Path(repo["root"]).expanduser()
            ok = (root / ".git").exists()
            report(ok, f"project {key}", f"{root} @ {git(root, 'rev-parse', '--short', 'HEAD', check=False)}"
                   if ok else str(root))
    else:
        report(False, "projects.json present")
    print("doctor: " + ("all checks passed" if not problems else f"{problems} problem(s)"))
    return EXIT_OK if not problems else EXIT_USAGE


# ---------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="teamctl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="check executables, config refs, model bindings and auth")
    r = sub.add_parser("run", help="launch one attempt of ROLE for TASK")
    r.add_argument("role")
    r.add_argument("task")
    r.add_argument("brief", type=Path, help="markdown brief file (passed by file/stdin, never argv)")
    r.add_argument("manifest", type=Path, help="workspace manifest from `teamctl workspace prepare`")
    r.add_argument("--dispatch-id", help="idempotency key; a second run with the same id is refused")
    r.add_argument("--timeout", type=int, help="seconds before the child group is terminated")
    s = sub.add_parser("status", help="show attempts and their process/outcome state")
    s.add_argument("task", nargs="?")
    s.add_argument("--json", action="store_true")
    c = sub.add_parser("cancel", help="terminate the selected runner and its owned child group")
    c.add_argument("task")
    c.add_argument("attempt", nargs="?")
    rc = sub.add_parser("reconcile", help="mark attempts whose runner is gone as uncertain (never relaunches)")
    rc.add_argument("task", nargs="?")
    rs = sub.add_parser("resolve", help="record Chief's decision for an uncertain attempt")
    rs.add_argument("task")
    rs.add_argument("attempt")
    rs.add_argument("decision", choices=["accept-partial", "discard", "retry-new-attempt"])
    rs.add_argument("--note", default="")
    w = sub.add_parser("workspace", help="worktree management")
    wsub = w.add_subparsers(dest="wcmd", required=True)
    wp = wsub.add_parser("prepare", help="create FEATURE worktrees from verified source repos")
    wp.add_argument("feature")
    wp.add_argument("--repo", action="append", required=True, metavar="KEY[=BASE]",
                    help="repo key from projects.json, optional base ref (default from projects.json)")
    wp.add_argument("--branch-prefix", default="hct/")
    wp.add_argument("--root", help="override worktrees root (tests and fixtures)")
    wp.add_argument("--projects", type=Path, help="alternate projects.json (fixture repos)")
    wp.add_argument("--detach", action="store_true",
                    help="detached worktrees at the base commit, no branch (read-only tasks)")
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        team = Team(chief_dir())
        if args.cmd == "doctor":
            return doctor(team)
        if args.cmd == "run":
            return run_attempt(team, args.role, args.task, args.brief.expanduser(), args.manifest.expanduser(),
                               args.dispatch_id, args.timeout)
        if args.cmd == "status":
            return status(team, args.task, args.json)
        if args.cmd == "cancel":
            return cancel(team, args.task, args.attempt)
        if args.cmd == "reconcile":
            return reconcile(team, args.task)
        if args.cmd == "resolve":
            return resolve(team, args.task, args.attempt, args.decision, args.note)
        if args.cmd == "workspace" and args.wcmd == "prepare":
            return workspace_prepare(team, args.feature, args.repo, args.branch_prefix, args.root,
                                     args.projects, args.detach)
    except TeamctlError as exc:
        print(f"teamctl: {exc}", file=sys.stderr)
        return exc.code
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
