# Architecture

## Roles and routing

| Role | Harness | Model (UC model service) | Unity Gateway route | Auth | Config scope |
|---|---|---|---|---|---|
| Chief | Hermes Agent profile | `system.ai.gpt-5-5` | `/ai-gateway/openai/v1` (Responses; Hermes `codex_responses` transport) | Hermes `key_cmd` → `databricks auth token` | `~/.hermes/profiles/<profile>/config.yaml` |
| Planner | Claude Code `-p` | `system.ai.claude-opus-5-5` | `/ai-gateway/anthropic` | `apiKeyHelper` → `hct-token` | `CLAUDE_CONFIG_DIR=<chief>/workers/claude-home` + `--settings` |
| Coder | OpenCode `run` | `system.ai.gemini-4-argon` | `/ai-gateway/gemini/v1beta` (`@ai-sdk/google`) | auth plugin → `hct-token`, `Authorization: Bearer` | `OPENCODE_CONFIG_DIR=<chief>/workers/opencode` |
| Reviewer | Claude Code `-p`, new session | `system.ai.claude-opus-5-5` | `/ai-gateway/anthropic` | as Planner | as Planner |
| Tester | Codex `exec` | `system.ai.gpt-6-1-sol` | `/ai-gateway/codex/v1` | provider `auth.command` → `hct-token` | `CODEX_HOME=<chief>/workers/codex-home` |

Why these routes: each was proven with a two-step tool-calling round trip (`scripts/probe_gateway.py`).
Gemini on the Gateway accepts `Authorization: Bearer` but rejects `x-goog-api-key` (401), so the OpenCode
plugin replaces the SDK's key header on every request.

### Streaming

Measured on 2026-10-31 (`acceptance` evidence): GPT-6.x models (Sol, Terra) return the full reply as one text
event on every route and request shape, including Codex's own. GPT-5.5, Gemini 4 Argon, Opus 5.5 and Fable 5.1
stream. Chief is interactive, so it uses GPT-5.5; the Tester runs in batch, so Sol is fine there.

## Precedence and machine policy

Each worker gets a child-scoped config directory, so it cannot inherit your personal login, MCP servers,
plugins or another model. Two things still outrank it, by design of the harnesses:

1. **CLI flags set by teamctl**: `--model` is always explicit.
2. **Machine-managed policy**: e.g. `/etc/codex/managed_config.toml` and
   `/Library/Application Support/ClaudeCode/managed-settings.json` (written by enterprise tooling).
   These can force the provider/base URL and add hooks. `teamctl doctor` reports any managed policy and
   fails if it points at a different workspace than `site.env`.

The model actually used is recorded per attempt from the harness's own data: Claude `modelUsage`,
the Codex session rollout (`model`, `model_provider`), and `opencode export` (assistant `providerID/modelID`).

## Background inference

- Hermes: all 19 `auxiliary.*` tasks are pinned to Chief's provider; `fallback_providers: []` at top level
  and for delegation; TTS/STT, image/video generation, X search and computer-use toolsets are disabled
  (their backends are other AI services); model-catalog fetch and update checks are off.
- Claude Code: every model slot (`ANTHROPIC_DEFAULT_*`, small/fast, subagent) is set to the role model.
- OpenCode: `small_model` (titles) is the role model; `enabled_providers` lists only the Gateway provider.
- Codex: a single provider; compaction uses the same model.

## teamctl state model

```
tasks/<task>/
  feature.json                    Chief's feature state (Chief writes)
  briefs/<stage>-<n>.md           Chief's briefs
  workspace.json                  → worktree manifest
  attempts/<NNN>-<role>/          teamctl writes, nobody edits
    attempt.json                  state machine + process identity + repo commits before/after
    argv.json  brief.md           exactly what was launched
    stdout.log stderr.log         0600, complete
    result.json                   parsed harness result + observed model
    receipt.json                  concise handoff (also printed as TEAMCTL_RECEIPT {...})
    output/                       the role's only write location (plan.md, review.md, test-report.md...)
    native-session-export.json    OpenCode session export (evidence)
```

States: `launching → running → exited` (outcome `harness_exited_zero | harness_failed | timeout | cancelled |
interrupted | launch_failed | harness_exited_zero_without_usable_result`), or `rejected`
(`conflicting_writer`), or `uncertain` (runner lost; set by `reconcile`) → `resolved` (Chief's decision).

Locks (`fcntl`): a task lock serialises attempt allocation (duplicate dispatch ids and a second active attempt
for the same role are refused); a pair lock allows one active attempt per worktree pair (writers, tests and
final review never overlap on the same code); the runner holds an attempt lock for its lifetime, which is how
`reconcile` tells a live runner from a lost one. Child processes run in their own process group;
`cancel` and timeouts terminate only that group.

## Completion delivery

Chief launches `teamctl run …` with Hermes's terminal tool as a background process with
`notify_on_complete=true`. When it exits, Hermes injects the completion (including the `TEAMCTL_RECEIPT`
line) into Chief's live session, which starts his next turn. This needs a live interactive session
(Desktop or `hermes chat`). A one-shot `-q` session ends before the notification arrives.
