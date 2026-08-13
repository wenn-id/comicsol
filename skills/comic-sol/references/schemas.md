# Comic Sol artifact schemas

This document is the normative schema contract for Comic Sol. Most project artifacts
remain schema version `1.0`; panel and page QA records use their documented schema
version `2.0`. The JSON templates in `templates/` are starting shapes for the agent
and deterministic scripts. A template may be structurally incomplete for a later
pipeline stage; stage validation applies the cross-field rules in this document
before allowing a transition.

## Common JSON rules

- Encoding is UTF-8 without a byte-order mark.
- Writers use two-space indentation, lexicographically sorted object keys, and one newline at end of file.
- Each artifact's `schema_version` is the exact version stated in its section.
- Validators reject unknown fields for their declared schema version.
- IDs match `^[a-z][a-z0-9-]{0,47}$`.
- Timestamps are strings in ISO 8601 UTC form `YYYY-MM-DDTHH:MM:SSZ`.
- SHA-256 values are 64-character lowercase hexadecimal strings once their referenced artifact exists. A template may use `null` before the artifact exists; persisted stage output may not use an empty string or sentinel hash.
- Project paths are POSIX-style relative paths rooted at the generated project directory. Absolute paths and `..` components are invalid.
- JSON numbers used for pixels, page numbers, order, priority, dimensions, and attempts are integers.

## Project manifest: `project.json`

The manifest is created from `templates/manifest.json` and is the authoritative project state.

### Top-level fields

| Field | Type | Required | Rules |
|---|---|---:|---|
| `schema_version` | string | yes | Exactly `"1.0"` |
| `project_id` | string | yes | Valid ID; also the base output/PDF name |
| `title` | string | yes | Non-empty after initialization |
| `created_at` | timestamp | yes | Set once at initialization |
| `updated_at` | timestamp | yes | Changed only by a non-no-op atomic transition |
| `status` | enum | yes | One manifest status listed below |
| `input` | object | yes | Exact input provenance |
| `settings` | object | yes | Fixed production settings and chosen scope |
| `capability` | object | yes | Feature-based image capability record |
| `artifacts` | object | yes | Produced named artifact descriptors; initially empty |
| `stage_versions` | object | yes | Deterministic cache-version strings |
| `panels` | array[string] | yes | Unique panel IDs in page/reading order |
| `warnings` | array[string] | yes | Unresolved project-level warning messages |
| `blocked_from` | enum \| null | yes | Linear status held when blocked; `null` otherwise |
| `blocked_reason` | string \| null | yes | Stable sanitized category when blocked; `null` otherwise |

### Manifest statuses

The linear success path is:

`INIT → PLANNED → SCRIPTED → STORYBOARDED → REFERENCES_READY → PANELS_READY → QA_READY → LETTERED → COMPOSED → EXPORTED → COMPLETE`

`BLOCKED` is permitted from any nonterminal state when the run cannot safely continue. `COMPLETE_WITH_WARNINGS` is a terminal alternative to `COMPLETE`. Skipping a linear state is invalid.

While `status` is `BLOCKED`, `blocked_from` and `blocked_reason` are both set; in every other status both are `null`. Only `comic_sol.py resume` clears them, so recovering a blocked project always goes through `resume` rather than `invalidate`.

### `input`

| Field | Type | Rules |
|---|---|---|
| `mode` | enum | `short_prompt`, `pasted_story`, `source_file`, or `resume` |
| `source_path` | relative path | Exactly `source/input.txt` in version 1.0 |
| `source_sha256` | SHA-256 or null | Null only in the untouched template; required after initialization |
| `request_path` | relative path | Exactly `source/request.json` |
| `language` | string | Non-empty BCP-47-like language tag chosen from the input |

Only UTF-8 `.txt` and `.md` source files are accepted. A source file must be at most 200 KiB. Resume mode takes precedence, then an existing named `.txt`/`.md` file, pasted narrative of at least 120 characters or two paragraph breaks, then short prompt.

### `settings`

| Field | Type | Rules/default |
|---|---|---|
| `page_width` | integer | Exactly `1600` |
| `page_height` | integer | Exactly `2400` |
| `reading_direction` | enum | `ltr` only in must-have version |
| `page_count` | integer | 1–4; default 2 |
| `panel_count` | integer | 0 only before storyboarding; otherwise 1–12 |
| `style_anchor` | string | Non-empty original visual description |
| `max_panel_retries` | integer | Exactly 2 |

There are at most four panels per page. The outer margin is 64 px and the gutter is 32 px.

### `capability`

| Field | Type | Rules |
|---|---|---|
| `status` | enum | `not_checked`, `available`, or `unavailable` |
| `name` | string or null | Neutral capability name; null only when not checked/unavailable |
| `supports_reference_images` | boolean | Feature flag only |
| `supports_dimensions` | boolean | Feature flag only |
| `detected_at` | timestamp or null | Required when status is available/unavailable |

The manifest never stores credentials, tokens, environment values, provider request payloads, or raw provider errors.

### `artifacts`

Allowed keys are `story_plan`, `character_bible`, `storyboard`, `composition_cache`, `qa_report`, and `pdf`. Each present value is an object containing exactly `path` (relative path) and `sha256` (valid SHA-256). Entries are absent until their file has been produced and verified; empty descriptor values are invalid.

Three descriptors are written by the deterministic scripts, not the agent: `compose_pages.py` records `composition_cache`, `export_pdf.py` records `pdf`, and `render_report.py` records `qa_report`. Final validation requires `character_bible`, `story_plan`, `storyboard`, and `composition_cache`, plus `qa_report` and `pdf` at the terminal stages.

### `stage_versions`

Contains exactly `planning`, `storyboard`, `generation`, `lettering`, `composition`, and `export`. Each value is a non-empty decimal version string. Schema version 1.0 templates use lettering stage version `"2"` and `"1"` for every other stage; the lettering bump invalidates cached output from the earlier renderer without changing the artifact schema version.

## Character bible: `plan/character-bible.json`

Top-level fields are `schema_version` and `characters`. `characters` is an array of unique character objects. It may be empty in the initial template. Every speaking or recurring character must be present before `STORYBOARDED`; one-off background figures need no record.

### Character object

| Field | Type | Rules |
|---|---|---|
| `id` | ID | Unique character ID |
| `name` | string | Non-empty display name |
| `role` | string | Narrative function |
| `age_band` | string | Non-empty, unambiguous age description |
| `pronouns` | string | Non-empty |
| `visual_fingerprint` | object | Exact stable visible-trait object below |
| `personality` | array[string] | One or more concise traits |
| `motivation` | string | Non-empty story motivation |
| `speech` | string | Stable speech-pattern direction |
| `reference_path` | relative path | `references/characters/{character-id}.png` |

### `visual_fingerprint`

Contains exactly `silhouette`, `face`, `hair`, `wardrobe`, `palette`, `signature_props`, `invariants`, and `avoid`. The first four are concrete non-empty visible descriptions. `palette`, `signature_props`, and `avoid` are arrays of non-empty strings. `invariants` contains 2–5 panel-checkable facts. Fingerprint strings are reused verbatim in dependent prompts.

## Story plan: `plan/story-plan.json`

| Field | Type | Rules |
|---|---|---|
| `schema_version` | string | Exactly `"1.0"` |
| `title` | string | Non-empty before `PLANNED` |
| `logline` | string | One concise story sentence |
| `theme` | string | Non-empty thematic statement |
| `tone` | array[string] | One or more tone descriptors |
| `rating` | enum | `teen` in must-have version |
| `setting` | string | Non-empty setting summary |
| `beginning` | string | Complete opening beat |
| `turn` | string | Complete reversal/complication |
| `climax` | string | Complete climactic beat |
| `ending` | string | Complete resolution |
| `scenes` | array[scene] | 2–5 scenes before `PLANNED` |

A scene contains exactly `id`, `purpose`, `location`, `time`, `characters`, and `continuity_anchor`. `id` is unique. The four descriptive strings are non-empty. `characters` contains unique character IDs present in the character bible. `continuity_anchor` fixes visible architecture, palette, time, and light-source facts reused across panels.

## Storyboard: `plan/storyboard.json`

Top-level fields are `schema_version` and `pages`. `pages` may be empty in the initial template; before `STORYBOARDED` it contains 1–4 pages numbered contiguously from 1. Pages and panels are stored in left-to-right reading order.

### Page object

| Field | Type | Rules |
|---|---|---|
| `number` | integer | Contiguous, 1–4 |
| `layout` | enum | One fixed layout below |
| `panels` | array[panel] | 1–4 panels; count matches layout |

Layout enums are `full-page`, `two-horizontal`, `three-horizontal`, `hero-top-two-bottom`, and `two-top-hero-bottom`. Rectangles are calculated for 1600 × 2400 px pages with 64 px margin and 32 px gutters. Panels remain inside the margin and do not overlap, rotate, bleed, or inset.

### Panel object

| Field | Type | Rules |
|---|---|---|
| `id` | ID | Format `pNN-NN`; encodes page and reading order |
| `order` | integer | Contiguous reading order within page |
| `scene_id` | ID | References a story-plan scene |
| `rect` | object | Integer `x`, `y`, `width`, `height`; exact layout rectangle |
| `beat` | string | Single story beat |
| `characters` | array[ID] | Unique character-bible references |
| `shot` | string | Shot size and camera angle |
| `composition` | string | Subject placement and text-safe space |
| `action` | string | Visible action |
| `expression` | string | Visible emotional expression |
| `lighting` | string | Key/fill/environment light direction |
| `continuity` | array[string] | Exact character/scene invariants checked in QA, each written `owner-id:fact` |
| `negative` | array[string] | Prohibits generated dialogue, captions, speech bubbles, logos, signatures, watermarks, unauthorized text/SFX, and panel-specific failures; exact authored SFX remains allowed |
| `text` | array[text item] | 0–3 items; at most 45 words total |

There are at most 12 panels project-wide. Every panel scene and character must exist. Dialogue speakers must both exist in the character bible and appear in the panel.

Each `continuity` entry is `<owner-id>:<fact>`, where `owner-id` is a character or scene ID used by the panel and `fact` string-equals one of that character's `visual_fingerprint.invariants` entries or that scene's `continuity_anchor`, verbatim. Write `mira:amber scarf`, not `amber scarf`.

### Text item

| Field | Type | Rules |
|---|---|---|
| `id` | ID | Unique project-wide text ID |
| `kind` | enum | `dialogue`, `caption`, or `sfx` |
| `speaker` | ID or null | Required for dialogue and resolves to an entity present in the character bible and panel; null for caption/SFX |
| `voice_source` | enum or omitted | Dialogue only: exactly `human` or `device` |
| `speaker_anchor` | array[number] or omitted | Dialogue only: finite normalized `[x,y]` coordinates for the visible voice-source region |
| `content` | string | NFC-normalized; dialogue ≤32 words, caption ≤45, SFX ≤3 |
| `anchor` | enum | One of eight anchors below |
| `priority` | integer | Positive placement order; ties break by item ID |

Anchors are `top-left`, `top-center`, `top-right`, `middle-left`, `middle-right`, `bottom-left`, `bottom-center`, and `bottom-right`. `anchor` places every text item, captions included. Human `speaker_anchor` identifies the visible mouth/face voice-source region; spoken devices use `voice_source: device` and anchor their visible audio source. Non-spoken system status is a caption and has no tail. Captions and SFX omit `voice_source` and `speaker_anchor`. Legacy `tail_target` remains readable but produces `balloon-tail-migration-required` at lettering and later stages; it is never silently reinterpreted. Control characters other than newline are invalid. Explicit newlines are optional wrapping hints. Authored punctuation and words are not rewritten by deterministic scripts.

The word limits are a ceiling, not a guarantee of fit. Dialogue is inscribed in an oval, which holds roughly half the text of the rectangle bounding it, and an anchor area is about 42% of panel width by 30% of panel height. A 32-word line needs a panel of roughly 1000x1200 px or larger; a 720x1064 panel holds about 14 words. Lettering fails with `text item {id} does not fit inside the panel` rather than printing over the artwork, so size dialogue to the panel rectangle the storyboard assigns it.

Dialogue tails are stored in lettering geometry as `organic-cubic-v1` records containing `attachment`, `base`, `control`, `tip`, `speaker_anchor`, `voice_source`, `source_gap`, `length`, `width`, and `policy_version`. The body and cubic tail are supersampled into one mask before one outline is derived. Page-QA check `bubble-tail-direction` requires exactly one current `regions` entry per dialogue with `panel_id`, `text_id`, `speaker`, `voice_source`, `speaker_anchor`, `tip`, and `result`; generic or stale regions fail closed.

Dialogue and captions are deterministic lettering inputs. SFX is authored storyboard
content for generation prompts and visual QA, but Pillow neither draws SFX nor allocates a placement rectangle or overlap reservation. Lettering summaries retain `text_count`
for the total authored item count and additionally report `rendered_text_count` for
dialogue/caption items and `sfx_count` for authored SFX items.

## Panel QA record: `qa/panels/{panel-id}.json`

Schema 2.0 is the canonical record. Its exact top-level fields are `schema_version`,
`kind`, `subject_id`, `bindings`, `checks`, `review`, `decision`, and
`unresolved_warnings`. `kind` is `panel-qa`; `subject_id` is the storyboard panel
ID; a record may not also contain the legacy `panel_id` field.

`bindings` contains exactly `raw_path`, `raw_sha256`, `raw_width`, `raw_height`,
`clean_path`, `clean_sha256`, `clean_width`, `clean_height`,
`normalization_path`, and `normalization_sha256`. They bind the reviewed artifacts
to `panels/raw/{panel-id}.png`, `panels/{panel-id}/clean.png`, and
`panels/{panel-id}/normalization.json`, including current hashes and raster
dimensions. Resume and validation reject missing, non-canonical, stale, or
unreadable bindings.

`checks` contains exactly seven rich records in this order: `character-identity`,
`anatomy`, `action`, `composition`, `continuity`, `text-free`, and `technical`.
Every check has `id`, `result`, `severity`, `evidence`, `method`, `reviewer`, and
`regions`; results are `pass`, `fail`, or `warning`, and severities are `error` or
`warning`. `review` contains a non-empty `method` and `reviewer` plus UTC
`reviewed_at`.

Decisions are `accept`, `accept-warning`, or `regenerate`. An error-level failed
check requires `regenerate`; a warning result or warning severity requires
`accept-warning` or `regenerate`. `accept-warning` records a non-empty
`unresolved_warnings` list, while `accept` records none. Accepted records are only
reused after structural validation and all bound files, hashes, and dimensions are
rechecked.

### Legacy schema 1.0 migration

Schema-1.0 records with `panel_id`, generation metadata, and
`accept_with_warnings` remain readable for compatibility. They are not canonical:
migrate their identity to `subject_id`, move artifact provenance into the schema-2.0
`bindings` object, provide rich checks and review data, and use `accept-warning`.

## Human-readable QA report: `qa/report.md`

`templates/qa-report.md.tmpl` is a Markdown projection, not JSON. Its tokens occur exactly once and in this order:

1. `{{PROJECT_SUMMARY}}`
2. `{{CAPABILITY}}`
3. `{{COUNTS}}`
4. `{{PANEL_TABLE}}`
5. `{{WARNINGS}}`
6. `{{INTEGRITY}}`
7. `{{RESUME}}`

The rendered report contains project/final status; capability and reference support; page/panel/attempt/regeneration/warning/failure counts; a panel result table; unresolved warnings or an explicit none statement; artifact dimensions/hashes/references/page/PDF integrity; and reused versus regenerated resume artifacts. It discloses degraded consistency when references are unsupported and states that the selected external image capability's policies govern transmitted prompts and references.

Report aggregation is deterministic. Panel records are sorted by `panel_id`; attempt,
regeneration, warning, and hard-failure counts are calculated from those records rather
than copied from manifest summaries. A hard failure is an error-level failed check whose
retry budget is exhausted, or a corrupt/safety category that cannot be overridden.
Markdown table evidence escapes `|` as `\|` and renders embedded newlines as `<br>`.
Manifest-only project/page warnings are merged with panel warnings and deduplicated with
their source identified. When neither the manifest nor a panel has user-visible
unresolved impact, the warnings section is exactly `No unresolved warnings.` The integrity section lists recorded relative paths and
SHA-256 values, page dimensions/order, reference existence, and the PDF readability
result. Rendering fails before publication if any `{{TOKEN}}` remains, and successful
output is UTF-8 with exactly one trailing newline.

## Generated project paths

The version 1.0 project boundary contains `project.json`; exact source/request copies; the three plan JSON files; character/scene reference PNGs; preserved reference/panel prompt text; raw `panels/raw/{panel-id}.png`, clean `panels/clean/{panel-id}.png`, and lettered `panels/{panel-id}/lettered.png` panel images; per-panel QA JSON; per-page QA JSON `qa/pages/page-{NNN}.json`; `qa/report.md`; zero-padded `pages/page-001.png` files; the composition cache `cache/composition.json`; `exports/{project-id}.pdf`; the resume cache `logs/stage-cache.json`; and append-only `logs/events.jsonl`.

Failed image attempts are retained as `panels/raw/{panel-id}.attempt-{attempt-number}.png`; only the accepted attempt occupies `panels/raw/{panel-id}.png`. Generated images intentionally contain no dialogue, captions, speech bubbles, signatures, logos, or watermarks. Exact storyboard-authored SFX is instead allowed and required in generated artwork; generated SFX is forbidden when the storyboard has none.

## Page QA record: `qa/pages/page-{NNN}.json`

One schema-2.0 `page-qa` record per composed page is created from `templates/page-qa.json`
after bounded visual inspection. It contains `schema_version: "2.0"`, `kind: "page-qa"`,
and `subject_id: "page-{NNN}"`; seven checks in the normative page order; a review object
with the fixed method `deterministic-plus-bounded-visual-review`, a non-empty reviewer, and
an ISO-8601 UTC `reviewed_at`; `decision`; and `unresolved_warnings`.

`bindings` contains exactly `composition_cache_path`, `composition_cache_sha256`,
`layout_name`, `layout_version`, ordered `lettering_sha256s` values (`panel-id:sha256`),
`page_height`, `page_path`, `page_sha256`, `page_width`, `storyboard_path`, and
`storyboard_sha256`. Every value is bound to the artifacts inspected by the record.

An error-level failed check selects `regenerate`; otherwise any check whose result or severity
is `warning` selects `accept-warning` and places its evidence, in check order, in
`unresolved_warnings`; all passing checks select `accept`. A legacy five-field record
(`page`, `page_path`, `page_sha256`, `schema_version`, `status`) is schema-1.0 input only:
it remains readable for reporting but requires migration and cannot satisfy final validation.
These records are the integrity gate on finalization. `comic_sol.py finalize` and `validate_project.py --stage final|export-ready` fail closed when a record is missing or when `page_sha256` no longer matches the page, because a terminal status must not claim visual review that did not happen. Recompose a page and its record goes stale; write it again after re-inspecting.

## Composition cache: `cache/composition.json`

`compose_pages.py PROJECT_DIR --all` writes this file and its `composition_cache` manifest descriptor in the same transaction as the page PNGs. It is a canonical JSON object containing exactly `schema_version` and `stages`, where `stages.composition.artifacts` maps each `pages/page-{NNN}.png` to its SHA-256. Final and export-ready validation require both the file and the descriptor.

## Stage cache: `logs/stage-cache.json`

`comic_sol.py record-stage PROJECT_DIR STAGE` persists one completed stage's resume cache entry atomically. The file is a canonical JSON object containing exactly `schema_version` and `stages`. During a run, `stages` contains zero or more entries keyed only by the six resume stages (`planning`, `storyboard`, `generation`, `lettering`, `composition`, `export`); a fully recorded project contains all six. Each entry contains exactly:

- `key`: the SHA-256 stage cache key over timestamp-free semantic inputs, required material file hashes, and the manifest `stage_versions` value. Generation material includes each panel's recorded source prompt and actual character/scene reference paths.
- `artifacts`: an object mapping every required stage output file (relative path) to its SHA-256 value.

`record-stage` refuses to record a stage when any required input or output is missing. If an existing cache is structurally invalid, the command starts a new canonical cache with only the newly recorded entry rather than preserving untrusted entries. `resume-plan` compares every recorded generated-artifact hash and freshly computed material; panel reuse additionally requires a valid accepted QA record and a reusable generation-stage cache. A missing cache entry or invalid cache file produces an honest rerun plan rather than a traceback. `invalidate` drops the affected stage and every downstream entry while preserving upstream entries and artifact files; it publishes the pruned cache before the rewound manifest.

## Cross-artifact and stage rules

- The agent writes and validates an artifact before advancing the manifest.
- Storyboard scene, character, speaker, and continuity references resolve to the story plan and character bible.
- Character fingerprints and canonical reference images are immutable after the first dependent panel is accepted.
- Changing a fingerprint or reference invalidates dependent panels, pages, PDF, and QA outputs.
- Changing dialogue or captions alone invalidates lettered panels, pages, PDF, and the final report; raw and clean panels remain reusable. Changing authored SFX invalidates generation and every downstream artifact because SFX belongs to the artwork.
- Artifact reuse requires file existence, matching recorded hash, schema validity, valid dependencies, and a matching stage cache key.
- Deterministic writes use a sibling temporary file, flush and `fsync`, then `os.replace`; the manifest transition is last.
- A no-change resume performs validation but writes no artifact, timestamp, or event.
- A project with an unresolved error-level panel is `BLOCKED` and cannot be composed/exported unless an allowed explicit override converts it to a warning.
- Page PNGs are opaque RGB 1600 × 2400, use exact storyboard rectangles, LANCZOS resizing, inward 6 px black panel borders, white background, and numeric order.
- The PDF contains the same RGB pages in numeric order at 150 DPI metadata and no additional margins.
