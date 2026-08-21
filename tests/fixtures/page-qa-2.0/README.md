# Pre-change page-QA record fixture

`page-001.json` is a page-QA record in the **schema-2.0** shape that `CS-023`
(#130, PR #176) superseded: seven checks in the old normative order and eleven
`bindings` without `normalization_sha256s`. It exists so the registered
`("2.0", "2.1")` migration in `scripts/page_quality.py` is exercised against a
committed record rather than one assembled inside a test body.

It is written against the lettered and composed `tests/fixtures/valid-one-page`
project, whose single dialogue is `p01-02-t01` spoken by `mira`.

## What the migration must do with it

| Part of the record | Expected outcome |
| --- | --- |
| The four old deterministic checks | Discarded and re-derived as seven current ones. Their evidence starts with `Superseded schema-2.0 engine measurement`, so a migration that copied instead of re-deriving is visible in the result. |
| `face-action-obstruction`, `bubble-tail-direction`, `accidental-text-watermark` | Carried across verbatim, but only while `bindings.page_sha256` still matches the page on disk. |
| `review` | Carried across verbatim. The original reviewer and timestamp are the record of who looked and when, so a migration never restamps them. |
| `bindings` | Re-derived in full, including the added `normalization_sha256s`. |
| `decision`, `unresolved_warnings` | Re-derived from the merged ten-check set, because a newly derived check can warn where the old record accepted. |

## Rebound values

Three values cannot be committed, because they are digests of artifacts that
`letter_project()` and `compose_all_pages()` render at test time, and the
balloon tail tip is measured from that rendered geometry. They are recorded as
`null` and resolved by `legacy_page_record()` in
`tests/test_page_qa_migration.py`, in the same spirit as
`tests/fixtures/balloon-layouts`, which stores placement as data and applies it
to a freshly lettered project instead of duplicating rasters.

| Field | Rebound from |
| --- | --- |
| `bindings.page_sha256` | `pages/page-001.png` as composed. This is the one legacy value the migration reads, so a test that needs a stale record mutates the page after rebinding. |
| `bindings.composition_cache_sha256` | `cache/composition.json` as written by composition. |
| `bindings.lettering_sha256s` | Ordered `panel-id:sha256` values for each `panels/{panel-id}/lettering.json`. |
| `checks[bubble-tail-direction].regions[].tip` | The tail tip in the rendered lettering geometry, matched by `panel_id` and `text_id`. Every other field of the region is committed, so the fixture and the storyboard must still agree. |
