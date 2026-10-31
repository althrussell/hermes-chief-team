"""Behaviour tests for teamctl's wrapper logic.

These use a fake harness (tests/fakes/fake-claude) and throwaway git repos, so they validate the
runner only: locking, state, receipts, cancellation, reconciliation and argument safety. Real
harness and Unity Gateway behaviour is covered by the acceptance exercise, not here.

Run: python3 -m unittest discover -s runner/tests -v
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEAMCTL = HERE.parent / "teamctl.py"
FAKE_CLAUDE = HERE / "fakes" / "fake-claude"


def sh(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


class TeamctlTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="teamctl-test-"))
        self.chief = self.tmp / "chief"
        (self.chief / "bin").mkdir(parents=True)
        (self.chief / "tasks").mkdir()
        cfg = self.chief / "workers"
        cfg.mkdir()
        (cfg / "settings.json").write_text("{}")
        (cfg / "mcp.json").write_text('{"mcpServers": {}}')
        role = {"harness": "claude-code", "executable": str(FAKE_CLAUDE), "model": "system.ai.test-model",
                "paths": {"settings": str(cfg / "settings.json"), "mcp_config": str(cfg / "mcp.json")},
                "allowed_tools": ["Read", "Write(/{output_dir}/**)"], "timeout_s": 60}
        (self.chief / "team.json").write_text(json.dumps({"roles": {
            "planner": role, "reviewer": {**role}, "coder": {**role, "writes_product_code": True}}}))
        self.repos = {}
        for key in ("wt", "ct"):
            src = self.tmp / "src" / key
            src.mkdir(parents=True)
            sh("git", "init", "-q", "-b", "main", cwd=src)
            (src / "README.md").write_text(f"{key} fixture\n")
            sh("git", "add", ".", cwd=src)
            sh("git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "init", cwd=src)
            self.repos[key] = {"root": str(src), "default_base": "main"}
        (self.chief / "projects.json").write_text(json.dumps(
            {"worktrees_root": str(self.tmp / "worktrees"), "repos": self.repos}))
        self.brief = self.tmp / "brief.md"
        self.brief.write_text("Read README.md in both repos.\n")
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                with_suppress(lambda: os.killpg(p.pid, signal.SIGKILL))
                p.wait()
            p.stdout.close()
            p.stderr.close()

    # ------------------------------------------------------------------ helpers

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith("HCT_")}
        env.update({"HCT_CHIEF_DIR": str(self.chief), **extra})
        return env

    def teamctl(self, *args, **extra_env):
        return subprocess.run([sys.executable, str(TEAMCTL), *args], capture_output=True, text=True,
                              env=self.env(**extra_env), timeout=120)

    def teamctl_bg(self, *args, **extra_env):
        p = subprocess.Popen([sys.executable, str(TEAMCTL), *args], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=self.env(**extra_env), start_new_session=True)
        self.procs.append(p)
        return p

    def prepare(self, feature):
        out = self.teamctl("workspace", "prepare", feature, "--repo", "wt", "--repo", "ct")
        self.assertEqual(out.returncode, 0, out.stderr)
        return self.tmp / "worktrees" / feature / "manifest.json"

    def wait_for_state(self, task, state, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            rows = json.loads(self.teamctl("status", task, "--json").stdout or "[]")
            if rows and rows[-1]["state"] == state:
                return rows[-1]
            time.sleep(0.2)
        self.fail(f"{task} never reached {state}")

    def receipt(self, stdout):
        line = next(l for l in stdout.splitlines() if l.startswith("TEAMCTL_RECEIPT "))
        return json.loads(line.split(" ", 1)[1])

    # ------------------------------------------------------------------ tests

    def test_invalid_role_is_rejected(self):
        m = self.prepare("feat-a")
        out = self.teamctl("run", "deployer", "feat-a", str(self.brief), str(m))
        self.assertEqual(out.returncode, 2)
        self.assertIn("unknown role", out.stderr)

    def test_invalid_task_id_is_rejected(self):
        m = self.prepare("feat-a")
        out = self.teamctl("run", "planner", "feat;rm -rf", str(self.brief), str(m))
        self.assertEqual(out.returncode, 2)
        self.assertIn("invalid task", out.stderr)

    def test_success_records_receipt_and_passes_hostile_brief_verbatim(self):
        m = self.prepare("feat-a")
        hostile = 'Plan it. $(touch PWNED) `touch PWNED2`; rm -rf "$HOME" && echo \'x\'\nline2 "quoted"\n'
        brief = self.tmp / "brief with spaces.md"
        brief.write_text(hostile)
        out = self.teamctl("run", "planner", "feat-a", str(brief), str(m))
        self.assertEqual(out.returncode, 0, out.stderr)
        rec = self.receipt(out.stdout)
        self.assertEqual(rec["outcome"], "harness_exited_zero")
        self.assertEqual(rec["models_observed"], ["system.ai.test-model"])
        result = json.loads(Path(rec["result"]).read_text())
        self.assertEqual(result["final_text"], hostile)
        argv = json.loads((Path(rec["attempt_dir"]) / "argv.json").read_text())
        self.assertNotIn(hostile.strip(), " ".join(argv))
        self.assertIn("--session-id", argv)
        for wt in ("wt", "ct"):
            self.assertFalse((self.tmp / "worktrees" / "feat-a" / wt / "PWNED").exists())
        state = json.loads((Path(rec["attempt_dir"]) / "attempt.json").read_text())
        self.assertEqual(state["repos_before"]["ct"]["head"], state["repos_after"]["ct"]["head"])
        self.assertEqual(oct(Path(rec["stdout"]).stat().st_mode & 0o777), "0o600")

    def test_failed_child_is_visible(self):
        m = self.prepare("feat-a")
        out = self.teamctl("run", "planner", "feat-a", str(self.brief), str(m), HCT_TEST_CHILD_EXIT="3")
        self.assertEqual(out.returncode, 1)
        rec = self.receipt(out.stdout)
        self.assertEqual((rec["outcome"], rec["exit_code"]), ("harness_failed", 3))

    def test_duplicate_dispatch_id_is_refused(self):
        m = self.prepare("feat-a")
        first = self.teamctl("run", "planner", "feat-a", str(self.brief), str(m), "--dispatch-id", "plan-1")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.teamctl("run", "planner", "feat-a", str(self.brief), str(m), "--dispatch-id", "plan-1")
        self.assertEqual(second.returncode, 3)
        self.assertIn("already exists", second.stderr)

    def test_same_role_same_task_cannot_run_twice_concurrently(self):
        m = self.prepare("feat-a")
        self.teamctl_bg("run", "planner", "feat-a", str(self.brief), str(m), HCT_TEST_CHILD_SLEEP="6")
        self.wait_for_state("feat-a", "running")
        out = self.teamctl("run", "planner", "feat-a", str(self.brief), str(m))
        self.assertEqual(out.returncode, 3)
        self.assertIn("already has attempt", out.stderr)

    def test_conflicting_writer_in_same_pair_is_rejected(self):
        m = self.prepare("feat-a")
        self.teamctl_bg("run", "coder", "feat-a", str(self.brief), str(m), HCT_TEST_CHILD_SLEEP="6")
        self.wait_for_state("feat-a", "running")
        out = self.teamctl("run", "reviewer", "feat-a-review", str(self.brief), str(m))
        self.assertEqual(out.returncode, 3)
        rows = json.loads(self.teamctl("status", "feat-a-review", "--json").stdout)
        self.assertEqual(rows[-1]["outcome"], "conflicting_writer")

    def test_different_pairs_run_concurrently(self):
        ma, mb = self.prepare("feat-a"), self.prepare("feat-b")
        pa = self.teamctl_bg("run", "coder", "feat-a", str(self.brief), str(ma), HCT_TEST_CHILD_SLEEP="2")
        pb = self.teamctl_bg("run", "coder", "feat-b", str(self.brief), str(mb), HCT_TEST_CHILD_SLEEP="2")
        start = time.time()
        self.assertEqual((pa.wait(30), pb.wait(30)), (0, 0))
        self.assertLess(time.time() - start, 7, "pairs were serialized")
        a = self.receipt(pa.stdout.read())
        b = self.receipt(pb.stdout.read())
        self.assertEqual((a["task"], b["task"]), ("feat-a", "feat-b"))

    def test_cancel_terminates_owned_group_and_keeps_evidence(self):
        m = self.prepare("feat-a")
        p = self.teamctl_bg("run", "planner", "feat-a", str(self.brief), str(m), HCT_TEST_CHILD_SLEEP="30")
        row = self.wait_for_state("feat-a", "running")
        out = self.teamctl("cancel", "feat-a")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(p.wait(30), 6)
        state = json.loads((Path(row["attempt_dir"]) / "attempt.json").read_text())
        self.assertEqual(state["outcome"], "cancelled")
        self.assertTrue((Path(row["attempt_dir"]) / "receipt.json").exists())
        self.assertTrue((Path(row["attempt_dir"]) / "stdout.log").exists())
        self.assertIsNone(subprocess.run(["ps", "-o", "pid=", "-p", str(row["child_pid"])],
                                         capture_output=True, text=True).stdout.strip() or None)

    def test_lost_runner_is_marked_uncertain_and_never_relaunched(self):
        m = self.prepare("feat-a")
        p = self.teamctl_bg("run", "coder", "feat-a", str(self.brief), str(m), "--dispatch-id", "code-1",
                            HCT_TEST_CHILD_SLEEP="30")
        row = self.wait_for_state("feat-a", "running")
        os.kill(p.pid, signal.SIGKILL)  # runner dies; its child keeps running in its own group
        p.wait()
        out = self.teamctl("reconcile", "feat-a")
        self.assertIn("marked uncertain", out.stdout)
        state = json.loads((Path(row["attempt_dir"]) / "attempt.json").read_text())
        self.assertEqual((state["state"], state["outcome"]), ("uncertain", "runner_lost_child_alive"))
        self.assertTrue(state["may_have_changed_files"])
        again = self.teamctl("run", "coder", "feat-a", str(self.brief), str(m), "--dispatch-id", "code-1")
        self.assertEqual(again.returncode, 3)
        busy = self.teamctl("run", "coder", "feat-a", str(self.brief), str(m))
        self.assertEqual(busy.returncode, 3)
        with_suppress(lambda: os.killpg(state["child_pgid"], signal.SIGKILL))

    def test_workspace_prepare_is_idempotent_and_validates_repos(self):
        m = self.prepare("feat-a")
        manifest = json.loads(m.read_text())
        self.assertEqual(manifest["repos"]["wt"]["branch"], "hct/feat-a")
        again = self.teamctl("workspace", "prepare", "feat-a", "--repo", "wt", "--repo", "ct")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(json.loads(m.read_text())["repos"], manifest["repos"])
        bad = self.teamctl("workspace", "prepare", "feat-c", "--repo", "nope")
        self.assertEqual(bad.returncode, 2)


def with_suppress(fn):
    try:
        fn()
    except (ProcessLookupError, PermissionError):
        pass


if __name__ == "__main__":
    unittest.main()
