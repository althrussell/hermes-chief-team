# hermes-chief-team

A local, multi-agent engineering team on one laptop: **Chief** (a [Hermes Agent](https://github.com/NousResearch/hermes-agent)
profile) scopes features across your repositories and hands the work to real external coding CLIs:
**Claude Code**, **OpenCode** and **Codex**. Every model call, including background inference, goes through
**Databricks Unity Gateway**.

```
                 you ──► Chief (Hermes, GPT-5.5)
                           │  writes briefs, critiques plans, assesses evidence
                           │  dispatches with:  teamctl run <role> <task> <brief> <manifest>
          ┌────────────────┼─────────────────┬──────────────────┐
          ▼                ▼                 ▼                  ▼
     Planner           Coder            Reviewer            Tester
  Claude Code -p    OpenCode run     Claude Code -p       Codex exec
  Opus 5.5          Gemini 4 Argon   Opus 5.5 (new conv)  GPT-6.1 Sol
          └────────────────┴───── worktree pair per feature ┴──────┘
                    ~/Hermes/Worktrees/<feature>/{wt,ct}
                                   │
                    Databricks Unity Gateway (one workspace)
     /ai-gateway/openai/v1 · /anthropic · /gemini/v1beta · /codex/v1
```

Workflow for a feature: **Planner → Chief's critique → Coder → Tester → Reviewer → Chief's assessment.**
Completion of each worker arrives back in Chief's session automatically (Hermes background process with
`notify_on_complete`), so you never relay output between chats.

## What's in the repo

| Path | What it is |
|---|---|
| `install.sh`, `uninstall.sh` | Idempotent install/update from `site.env`; scoped rollback |
| `site.env.example` | The only place your workspace, profile, model ids and repo paths live |
| `runner/teamctl.py` | The worker runner: `doctor`, `run`, `status`, `cancel`, `reconcile`, `resolve`, `workspace prepare` |
| `runner/tests/` | Wrapper behaviour tests (locks, duplicates, cancel, lost runner, hostile input) |
| `bin/hct-token` | Credential helper: prints a Databricks bearer via `databricks auth token` |
| `templates/hermes/` | Chief's Hermes profile: `config.yaml` (custom provider + `key_cmd`), `SOUL.md`, skin |
| `templates/chief/` | Chief's workspace: `AGENTS.md`, `team.json`, `projects.json`, role briefs |
| `templates/workers/` | Child-scoped configs: Claude Code (`CLAUDE_CONFIG_DIR`), Codex (`CODEX_HOME`), OpenCode (`OPENCODE_CONFIG_DIR` + auth plugin) |
| `scripts/probe_gateway.py` | Real tool-calling round trip per Gateway route |
| `scripts/probe_mcp.py` | MCP `initialize` + `tools/list` against Unity Gateway MCP services |
| `acceptance/` | Fixture repos and the acceptance checklist |
| `docs/` | [Install](docs/INSTALL.md) · [Architecture](docs/ARCHITECTURE.md) · [Operations](docs/OPERATIONS.md) · [Troubleshooting](docs/TROUBLESHOOTING.md) |

## Quick start

```sh
git clone https://github.com/althrussell/hermes-chief-team && cd hermes-chief-team
./install.sh            # first run creates site.env from the example and stops
$EDITOR site.env        # workspace URL, Databricks CLI profile, repo roots
./install.sh --set-default
```

Then open **Hermes.app** (profile rail → `chief`) or run `hermes -p chief chat`, and try:

> Plan a feature spanning Workshop Terminal and Control Tower. Have Planner inspect both repos, then challenge the plan before implementation.

Full prerequisites and verification steps: [docs/INSTALL.md](docs/INSTALL.md).

## Design rules

- **One gateway.** Chief uses a Hermes custom provider with a command-minted token (`key_cmd`); all 19
  Hermes auxiliary tasks are pinned to it; fallbacks are empty; toolsets backed by other AI services are disabled.
- **Real harnesses.** Roles run in the named CLIs, never as Hermes subagents. `teamctl` builds argv arrays,
  never shell strings; briefs go by file/stdin.
- **Evidence over claims.** Each attempt records process identity, repo commits before/after, exit code, logs,
  a structured result and the model observed in the harness's own session data.
- **Process outcome ≠ task outcome.** Exit 0 means the CLI finished; Chief still assesses acceptance criteria.
- **No secrets on disk or in argv.** Tokens are minted per request; `site.env` is git-ignored.

## Known limitations

- GPT-6.x (Sol/Terra) output arrives in one block on Unity Gateway on every route (measured server-side buffering), so Chief uses GPT-5.5, which streams. Tester keeps GPT-6.1 Sol (batch).
- Machine-managed policies (e.g. `/etc/codex/managed_config.toml`, Claude Code `managed-settings.json`)
  outrank the scoped worker configs. `teamctl doctor` reports them and checks they target the same workspace.
- Worktrees isolate files and branches only, not ports, databases or deployment targets.

License: MIT.
