# Install guide

Target: macOS (Apple silicon or Intel). Linux should work for the CLI parts; Hermes Desktop is optional.
Time: about 20 minutes on a machine that already has the CLIs below.

## 1. Prerequisites

| Component | Check | Install |
|---|---|---|
| Python 3.9+ | `python3 --version` | python.org or pyenv |
| Git | `git --version` | Xcode CLT or Homebrew |
| Databricks CLI ≥ 0.250 | `databricks --version` | `brew install databricks` |
| Hermes Agent (and optionally Hermes.app) | `hermes --version` | `curl -fsSL https://hermes-agent.nousresearch.com/install.sh \| bash` |
| Claude Code | `claude --version` | https://docs.anthropic.com/claude-code |
| Codex CLI | `codex --version` | `npm install -g @openai/codex` |
| OpenCode ≥ 1.0 | `opencode --version` | `npm install -g opencode-ai@1` |
| Node.js (for Codex/OpenCode) | `node --version` | nodejs.org / fnm / nvm |

## 2. Databricks access

1. Log in once with an OAuth profile for the workspace that hosts Unity Gateway:
   ```sh
   databricks auth login --host https://<workspace>.cloud.databricks.com --profile <profile>
   ```
2. Confirm the exact model-service names you will use (they are not the marketing names):
   ```sh
   databricks ai-gateway list-model-services --parent schemas/system.ai --profile <profile> -o json \
     | python3 -c 'import json,sys;[print(s["name"]) for s in json.load(sys.stdin).get("model_services",[])]'
   ```
3. You need `USE CATALOG`, `USE SCHEMA` and `EXECUTE` on each model service (granted by default on `system.ai`).

## 3. Configure

```sh
git clone https://github.com/althrussell/hermes-chief-team && cd hermes-chief-team
./install.sh                  # creates site.env from site.env.example, then stops
$EDITOR site.env
```

Fill in `HCT_WORKSPACE_HOST`, `HCT_DATABRICKS_PROFILE`, the model ids, and the two repo roots. Leave
`*_BIN` empty to auto-detect (paths are resolved to stable real paths, which matters for fnm/nvm shims).

## 4. Prove the Gateway routes (recommended, 2 minutes)

```sh
python3 scripts/probe_gateway.py --host "$HCT_WORKSPACE_HOST" --profile <profile> --out /tmp/gateway.json
```

Every route should print `round_trip=True` except `gemini-native-goog-key` (Gemini on the Gateway
requires `Authorization: Bearer`; the OpenCode plugin handles this).

## 5. Install

```sh
./install.sh --dry-run        # what would change
./install.sh --set-default    # install and make Chief the sticky Hermes profile (Desktop opens it)
```

The installer:

- creates the Hermes profile (`HCT_HERMES_PROFILE`, default `chief`) if missing
- renders the templates into `~/.hermes/profiles/<profile>/` and `HCT_CHIEF_DIR` (default `~/Hermes/Chief`)
- backs up any file it replaces to `~/Hermes/.setup-backups/<timestamp>/`
- confirms the Claude Code, Codex and OpenCode guide skills are available to Chief
- runs `teamctl doctor`

## 6. Verify

```sh
~/Hermes/Chief/bin/teamctl doctor
hermes -p chief chat --oneshot -q "Run pwd with your terminal tool and tell me the result."
python3 -m unittest discover -s runner/tests -v
```

Then run the acceptance exercise in [`acceptance/README.md`](../acceptance/README.md).

## 7. Open Chief

- **Hermes.app**: profile rail at the bottom of the sidebar → `chief`. If the composer's model picker
  shows another model (it is sticky per device), pick `Unity Gateway (Chief)`.
- **CLI**: `chief chat` (wrapper created by Hermes) or `hermes -p chief chat`.

## Update / redeploy elsewhere

Pull, edit `site.env` if needed, and re-run `./install.sh`. Unchanged files are left alone.

## Rollback

```sh
./uninstall.sh            # removes generated config, keeps tasks/ and acceptance/ evidence
./uninstall.sh --purge    # also deletes the Hermes profile and Chief's directory (asks first)
```
