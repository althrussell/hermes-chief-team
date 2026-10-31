# Operations

All commands below use the default Chief directory `~/Hermes/Chief`; substitute yours.

## Daily use

Talk to Chief in Hermes.app or `chief chat`. Typical requests:

- "Plan a feature spanning WT and CT: <goal>. Have Planner inspect both repos, then challenge the plan before implementation."
- "Review the current diff on feature <id>."
- "Run the tests for feature <id> and tell me what fails."

Chief runs only the stages the request needs and reports after each one.

## teamctl reference

```sh
T=~/Hermes/Chief/bin/teamctl
$T doctor                                              # executables, configs, auth, managed policy, repos
$T workspace prepare <feature> --repo wt --repo ct     # paired worktrees on hct/<feature>
$T workspace prepare <feature> --repo ct=origin/release-2   # one repo, explicit base
$T workspace prepare <feature> --repo wt --repo ct --detach # read-only inspection, no branches
$T run <role> <task> <brief.md> <manifest.json> --dispatch-id <stage-n> [--timeout S]
$T status [<task>] [--json]
$T cancel <task> [<attempt>]
$T reconcile [<task>]
$T resolve <task> <attempt> {accept-partial|discard|retry-new-attempt} --note "why"
```

Roles: `planner`, `coder`, `reviewer`, `tester`. Ids: lowercase letters, digits, `.`, `_`, `-` (≤ 64).

Exit codes: 0 finished · 1 harness non-zero · 2 bad input · 3 conflict · 4 launch failure · 5 timeout · 6 cancelled/interrupted.

## Recovering from interruptions

1. `teamctl reconcile <task>` marks any attempt whose runner is gone as `uncertain` (never relaunches).
2. Inspect the worktrees: `git -C <worktree> status`, `git diff`.
3. `teamctl resolve <task> <attempt> discard|accept-partial|retry-new-attempt --note …`.
4. Dispatch a new attempt with a new `--dispatch-id`.

## Where to look

| Question | Where |
|---|---|
| What is running? | `teamctl status` |
| What did a worker produce? | `tasks/<task>/attempts/<id>/output/` and `result.json` |
| Full worker transcript | `tasks/<task>/attempts/<id>/stdout.log` |
| Which model actually answered? | `result.json` → `models_observed`, `evidence_source` |
| Chief's model calls | `hermes -p chief logs` (look for `API call #… model=… provider=custom`) |
| Gateway spend | `ug usage` or the workspace's Unity Gateway usage tables (tag `source=hermes-chief-team`) |

## Updating

`git pull && ./install.sh`. Templates are the source of truth; local edits to rendered files are backed up and replaced.

## Adding a repository

Add `HCT_PROJECT_<KEY>_ROOT/BASE` to `site.env`, add the repo to `templates/chief/projects.json.tmpl`,
re-run `./install.sh`. `teamctl workspace prepare <feature> --repo <key>` then works for it.
