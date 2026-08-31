# Verification commands and results

Every gate run for the WP17 work package, with the actual command and
its actual outcome, is recorded below. Incomplete, timed-out, skipped,
or unavailable gates are recorded as such; nothing is described as
passing without an explicit recorded result.

| Command | Result |
| --- | --- |
| `python -m unittest web.tests.test_web_docs` | pass — 69 tests, 0 failures |
| `python -m unittest web.tests.test_webmcp_contract` | pass — 17 tests (5 read + 9 write + 3 parity), 0 failures |
| `python -m unittest web.tests.test_app` | pass — 25 tests (static inventory, /healthz, registration), 0 failures |
| `python -m unittest web.tests.test_web_security` | pass — 39 tests, 0 failures |
| `python -m unittest web.tests.test_web_e2e` | pass — 14 tests, 0 failures |
| `python -m unittest discover -s web/tests -p "test_*.py"` (CI-identical, from repo root) | pass — 509 tests, 0 failures |
| `python -m unittest tests.test_user_docs` | pass — existing root docs contract, unchanged at 124 docs tests |
| `python -m unittest tests.test_release_docs` | pass — existing release docs contract |
| `python -m unittest tests.test_showcase_contract` | pass — existing showcase contract |
| Combined docs contract (the four rows above run together in one invocation) | pass — 0 failures; the per-module counts are recorded in their own rows above and are not restated as a separate aggregate total here |
| `python -m unittest tests.test_doctor_diagnostics tests.test_distribution tests.test_clean_install tests.test_lock_provenance tests.test_container_contract tests.test_command_service tests.test_manifest tests.test_installers tests.test_agent_constitution tests.test_dogfood_docs tests.test_client_setup` | pass — release-qualification suite, 0 failures (skips reflect platform skips) |
| `python -m coverage run -m unittest discover -s tests` then `python scripts/check_coverage.py` (CI-identical root coverage gate) | pass — 1919 tests, 0 failures; coverage line 83.41% (floor 82%), branch 73.89% (floor 72%) |
| Web distribution coverage | informational only — the Web distribution has no enforced coverage floor in CI; the discovery result is 509/509 passing |
| `python -m ruff check scripts comic_sol_product tests web/comic_sol_web web/tests` | pass |
| `python -m ruff format --check scripts comic_sol_product tests web/comic_sol_web web/tests` | pass |
| `python -m mypy` (root, CI form) | pass — 10 source files clean |
| `python -m mypy web` (from repo root) | pass — 65 source files clean |
| `python -m build --no-isolation` (root) | pass — `comic_sol-2.0.0rc6-py3-none-any.whl` and `comic_sol-2.0.0rc6.tar.gz` |
| `python -m build --no-isolation` (web) | pass — `comic_sol_web-0.1.0-py3-none-any.whl` and `comic_sol_web-0.1.0.tar.gz` |
| `python -m comic_sol_product.release dist/*.whl dist/*.tar.gz` | pass — `distribution-ok` for wheel and sdist |
| `pip install --force-reinstall --no-deps dist/comic_sol-2.0.0rc6-py3-none-any.whl` then `comic-sol --json doctor` | pass — `ok: true, ready: true, healthy: true` |
| `pip install --force-reinstall --no-deps web/dist/comic_sol_web-0.1.0-py3-none-any.whl` then, in a background process, `python -c "import os, uvicorn; from comic_sol_web.config import WebConfig; from comic_sol_web.app import create_app; uvicorn.run(create_app(WebConfig.from_env(os.environ)), host='127.0.0.1', port=8000)"` and, from a second process, `curl -s -o - -w '%{http_code}' <loopback>:8000/healthz` | pass — recorded output `200{"status":"ok"}` |
| `python scripts/clean_install_smoke.py --wheel dist/comic_sol-2.0.0rc6-py3-none-any.whl` | pass — `clean-install-ok: mcp=False` |
| `python scripts/sync_plugin_bundle.py --check` | pass |
| `git diff --check` | pass — no conflict markers or whitespace errors |
| `python -c "import pathlib, re; root=pathlib.Path('docs/web').resolve(); bad=0\nfor p in root.rglob('*.md'):\n    txt=p.read_text(encoding='utf-8')\n    for m in re.finditer(r'\\]\\(([^)]+\\.md)(#[^)]+)?\\)', txt):\n        target=m.group(1).split('#',1)[0]\n        if not (root/target).exists():\n            print('missing',p,m.group(0)); bad+=1\nprint('missing_targets=',bad)"` (link validator) | pass — `missing_targets= 0` |
| `python -c "import pathlib,json; bad=0\nfor p in pathlib.Path('submission/webmcp').rglob('*.json'):\n    try: json.loads(p.read_text(encoding='utf-8'))\n    except Exception as e: print('bad',p,e); bad+=1\nprint('bad_json=',bad)"` (JSON parse check) | pass — `bad_json= 0`; both fixture files parse |
| `python -m unittest web.tests.test_web_docs.WebDocumentationLinkTests.test_no_wp17_document_contains_a_credential_shaped_string` (the disclosure-class scan that runs `CREDENTIAL_PATTERNS` over every new docs and submission file) | pass — 0 hits across `docs/web/**` and `submission/webmcp/**` |
| Sabotage proof of the same scan: each disclosure class (vendor key prefix, `Authorization`/`Bearer` header, `Set-Cookie` value, `csrf_token` in all three quote forms, PEM private-key block, private `/home/<user>` and `C:\Users\<user>` path, loopback/metadata endpoint, JSON body carrying `api_key`/`access_token`) fed through `CREDENTIAL_PATTERNS` as a synthetic sample | pass — 16 of 16 samples reported `CAUGHT`; no class reported `MISS` |
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
