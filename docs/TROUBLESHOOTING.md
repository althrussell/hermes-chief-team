# Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `hermes chat … --source X` fails with `invalid_params` | The gateway only accepts known session sources | Drop `--source` |
| Hermes.app stuck on "Let's get you setup… 100%" | Desktop opens the gateway's primary profile; it has no working provider | `hermes profile use chief`, quit and reopen the app |
| Desktop turn errors with model `nous/welcome` | The composer model picker is sticky per device and kept the free-trial model | Pick `Unity Gateway (Chief)` in the composer picker, then `/new` |
| Chief's replies arrive as one block | Model buffers on Unity Gateway (GPT-6.x Sol/Terra, measured) | Use a streaming model for Chief (`system.ai.gpt-5-5`), re-run `./install.sh` |
| `key_cmd` / `hct-token` fails | Databricks OAuth refresh token expired | `databricks auth login --profile <profile>` |
| OpenCode/Gemini 401 | `x-goog-api-key` sent instead of Bearer | Ensure `workers/opencode/plugin/hct-databricks-auth.js` exists (`teamctl doctor`) |
| Codex session shows `model_provider: Databricks`, not `hct-databricks` | Machine-managed `/etc/codex/managed_config.toml` outranks `CODEX_HOME` config | Expected; `teamctl doctor` verifies it targets the same workspace |
| `teamctl run` exits 3 | Duplicate `--dispatch-id`, same role already active, or the worktree pair is busy | `teamctl status`; wait, or `reconcile` if a runner died |
| Attempt stuck `running` after a crash | Runner was killed | `teamctl reconcile <task>`, inspect, `teamctl resolve …` |
| `HTTP 403 … Budget …` from the Gateway | Model service budget policy on your account | Choose a model within budget, or ask the workspace admin |

## Things to avoid

- `ug configure --dry-run` prints live bearer tokens inside the config it previews, and may upgrade
  tools. Don't paste its output anywhere. Use `ug status` to inspect instead.
- Don't copy tokens into `site.env`, `team.json` or briefs. Everything mints per request.
