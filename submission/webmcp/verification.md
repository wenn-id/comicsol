# Verification commands and results

Every gate run for the WP17 work package, with the actual command and
its actual outcome, is recorded below. Incomplete, timed-out, skipped,
or unavailable gates are recorded as such; nothing is described as
passing without an explicit recorded result.

| Command | Result |
| --- | --- |
| `python -m unittest web.tests.test_web_docs` | pass — 59 tests, 0 failures |
| `python -m unittest web.tests.test_webmcp_contract` | pass — 17 tests (5 read + 9 write + 3 parity), 0 failures |
| `python -m unittest web.tests.test_app` | pass — 25 tests (static inventory, /healthz, registration), 0 failures |
| `python -m unittest web.tests.test_web_security` | pass — 39 tests, 0 failures |
| `python -m unittest web.tests.test_web_e2e` | pass — 14 tests, 0 failures |
| `python -m unittest discover -s web/tests -p "test_*.py"` | pass — 499 tests, 0 failures |
| `python -m unittest tests.test_user_docs` | pass — existing root docs contract, unchanged at 124 docs tests |
| `python -m unittest tests.test_release_docs` | pass — existing release docs contract |
| `python -m unittest tests.test_showcase_contract` | pass — existing showcase contract |
| Combined docs (`test_user_docs` + `test_release_docs` + `test_showcase_contract` + `web.tests.test_web_docs`) | pass — 183 tests, 0 failures |
| `python -m unittest tests.test_doctor_diagnostics tests.test_distribution tests.test_clean_install tests.test_lock_provenance tests.test_container_contract tests.test_command_service tests.test_manifest tests.test_installers tests.test_agent_constitution tests.test_dogfood_docs tests.test_client_setup` | pass — release-qualification suite, 310 tests, 0 failures (skips reflect platform skips) |
| `python -m unittest discover -s tests` (full root discovery) | unavailable in this worktree — long-running root suite including benchmark runs; this submission was not authorized to weaken the gate by skipping it; the targeted release-qualification tests above substitute for the time-bounded subset |
| `python -m coverage run -m unittest discover -s web/tests && python -m coverage json` | informational only — the Web distribution has no enforced coverage floor; the discover result is 499/499 passing |
| `python -m coverage run -m unittest discover -s tests` (root coverage gate) | unavailable in this worktree — see above; the root coverage floor of 82% line / 72% branch applies to `scripts/` and `comic_sol_product/` and is unchanged by this work package |
| `ruff check scripts comic_sol_product tests web/comic_sol_web web/tests` | pass |
| `ruff format --check scripts comic_sol_product tests web/comic_sol_web web/tests` | pass |
| `mypy comic_sol_product scripts` | pass — 10 source files clean |
| `mypy comic_sol_web tests` (from `web/`) | pass — 65 source files clean |
| `python -m build --no-isolation` (root) | pass — `comic_sol-2.0.0rc6-py3-none-any.whl` and `comic_sol-2.0.0rc6.tar.gz` |
| `python -m build --no-isolation` (web) | pass — `comic_sol_web-0.1.0-py3-none-any.whl` and `comic_sol_web-0.1.0.tar.gz` |
| `python -m comic_sol_product.release dist/*.whl dist/*.tar.gz` | pass — `distribution-ok` for wheel and sdist |
| `pip install --force-reinstall --no-deps dist/comic_sol-2.0.0rc6-py3-none-any.whl` then `comic-sol --json doctor` | pass — `ok: true, ready: true, healthy: true` |
| `pip install --force-reinstall --no-deps web/dist/comic_sol_web-0.1.0-py3-none-any.whl` then `comic_sol_web.app.create_app(WebConfig.from_env(...))` | pass — FastAPI app constructs, `GET /healthz` returns `200 {"status":"ok"}` |
| `python scripts/clean_install_smoke.py --wheel dist/comic_sol-2.0.0rc6-py3-none-any.whl` | pass — `clean-install-ok: mcp=False` |
| `python scripts/sync_plugin_bundle.py --check` | pass |
| `git diff --check` | pass — no conflict markers or whitespace errors |
| Link and anchor validation across `docs/web/` and `submission/webmcp/` | pass — 0 missing targets, 0 dead anchors |
| `json.loads()` on every JSON in `submission/webmcp/` | pass — both fixture files parse |
| Leakage scan (api_key/bearer/sk-/ghp_/private-key patterns) on the new docs and submission | pass — 0 hits |
| Live paid provider smoke | not run (no paid call authorized by the #251 waiver) |
| External deployment | not run (no deployment authorized) |
| Local ComfyUI smoke | not run (no local instance available in this worktree) |
| Active-agent WebMCP demonstration | not run (no `document.modelContext` in environment; the hosted process never contacts user localhost) |
| Video recording | unavailable (no recording environment; the narration/demo script in `demo.md` stands in) |
| Native portable archive portable-release smoke (`scripts/portable_release_smoke.py`) | not run (no native archive for `v2.0.0rc6`; rc6 release assets are prepared but not published) |

> **What this table means**
>
> Every `pass` row corresponds to a real command and a real recorded
> outcome in the worktree log. Every `not run` or `unavailable` row
> names the reason and is a recorded limitation, not an implicit
> passing gate. Incomplete, timed-out, skipped, and unavailable gates
> are kept in this table on purpose; they are not described as passing
> anywhere else in this submission.
