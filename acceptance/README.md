# Acceptance exercise

Proves the whole setup end to end. Evidence goes in `$HCT_CHIEF_DIR/acceptance/` (never committed).

| # | Check | How |
|---|---|---|
| 1 | Chief makes a real tool call through the Gateway | In Hermes.app (chief): "Run pwd and tell me your model." Confirm `API call #… provider=custom model=<chief model>` in `hermes -p chief logs` |
| 2 | Each role reads WT and CT and reports roots/branches/commits | `teamctl workspace prepare acc-readonly --repo wt --repo ct --detach`, then ask Chief to dispatch a read-only brief to all four roles |
| 3 | Results return to Chief automatically, correlated by task/attempt | Watch Chief resume after each `TEAMCTL_RECEIPT` without you typing |
| 4 | Real Planner → critique → Coder → Tester → Reviewer on fixtures | `./make_fixtures.sh $HCT_CHIEF_DIR/acceptance/fixtures`; prepare with `--projects $HCT_CHIEF_DIR/acceptance/fixtures/projects.json --repo producer --repo consumer` |
| 5 | Two features run concurrently in different pairs | Two fixture features, dispatch both, compare branches/files |
| 6 | Fresh session reloads state; interrupted attempt is reconciled, not duplicated | `/new` in Chief, ask for status; kill a runner, `teamctl reconcile`, re-dispatch same id → refused |
| 7 | Product repos unchanged | Compare `git status --porcelain` / `git diff | shasum` before and after |

Fixture task used for #4 (deterministic, cross-repo): add a `locale` field to `producer/contract.json`
and `greeting()` (default `"en"`), bump the contract to version 2, and update the consumer to support
version 2 and render the new field. Tester runs both repos' unit tests; Reviewer inspects the diff.
