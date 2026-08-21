# Comic Sol artifact schemas

This document is the normative schema contract for Comic Sol. Most project artifacts
remain schema version `1.0`; the panel QA record uses its documented schema version
`2.0` and the page QA record uses `2.1`. The JSON templates in `templates/` are starting shapes for the agent
and deterministic scripts. A template may be structurally incomplete for a later
pipeline stage; stage validation applies the cross-field rules in this document
before allowing a transition.

## Project-schema compatibility and migration policy

The current project manifest schema is `1.0`. The minimum reader and writer version is
`1.0`; the current reader accepts only explicitly supported versions from
`SUPPORTED_PROJECT_SCHEMA_VERSIONS` in `scripts/schema.py`.

- Writers emit the current `1.0` manifest and update tests when the version changes.
- Readers reject unsupported or future versions with `UnsupportedSchemaVersionError`; they
  never guess, downgrade, or rewrite a project.
- A migration is allowed only through an explicit `(source_version, target_version)` hook
  in `PROJECT_MIGRATIONS`. Adding a version requires a hook where applicable, a fixture,
  and compatibility tests in the same change.
- Migration stages the complete replacement through the journal-backed project transaction.
  If validation or migration fails, `project.json`, source files, logs, and user artifacts
  remain byte-for-byte unchanged.
- This release has no older manifest representation registered for automatic migration;
  an older artifact is rejected until a reviewed migration is added.

The project manifest version is independent from artifact-level versions such as panel QA
`2.0`, page QA `2.1`, and stage cache versions. Those artifacts retain their own validators.

An artifact-level version that has its own migrations follows the same rules through its own
registry. The page QA record registers `PAGE_QA_MIGRATIONS` in `scripts/page_quality.py`,
keyed by `(source_version, target_version)` exactly like `PROJECT_MIGRATIONS`: a record whose
version has no registered hook is rejected with `UnsupportedSchemaVersionError` rather than
being accepted, migration is published through the journal-backed project transaction, and a
refused or interrupted migration leaves the record byte-for-byte unchanged.

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

## Character identity pack: `plan/character-identity-pack.json`

The identity pack is an opt-in companion plan artifact that carries each character's stable
visual identity in one structured place, so panel prompts embed a single fixed copy of the
identity instead of a freshly reworded description per panel. Its `schema_version` is `1.0`
and is independent of the project manifest version; `SUPPORTED_IDENTITY_PACK_SCHEMA_VERSIONS`
in `scripts/character_identity.py` is its compatibility gate. Top-level fields are exactly
`schema_version` and `characters`, an array of unique entries in character-bible order.

The pack is derived from `plan/character-bible.json` rather than authored from scratch.
`character_identity.py PROJECT_DIR --derive` publishes it atomically through the project
transaction, and re-running it on an unchanged project rewrites byte-identical content. It
is not a manifest `artifacts` descriptor, so `project.json` keeps schema version `1.0`; the
pack is validated by its own gate instead.

### Character entry

| Field | Type | Rules |
|---|---|---|
| `id` | ID | A character ID present in the character bible |
| `immutable_traits` | object | Exactly `silhouette`, `face`, `hair`, and `invariants`, verbatim from `visual_fingerprint` |
| `wardrobe` | object | Exactly `base`, `accessories`, and `palette` |
| `proportions` | object | Exactly `build` and `notes` |
| `reference_views` | array[view] | One or more unique views; must include `canonical` |
| `avoid` | array[string] | Non-empty prohibition strings; may be empty |
| `source_fingerprint_sha256` | SHA-256 | Canonical digest of the `visual_fingerprint` the entry was derived from |

`immutable_traits.invariants` repeats the same 2–5 panel-checkable facts as the bible.
`wardrobe.base` is the fingerprint wardrobe, `wardrobe.accessories` the signature props, and
`wardrobe.palette` holds at least one palette entry. `proportions.build` defaults to the
fingerprint silhouette and may be overridden; `proportions.notes` carries additional
non-derivable proportion facts and may be empty.

A view contains exactly `view` and `path`. `view` is a unique ID, and the required
`canonical` view equals the character's bible `reference_path`. `path` is a POSIX-style
relative project path. Extra views such as `three-quarter` or `profile` are authored
additions and survive re-derivation, as does `proportions.notes`; every other field is
always rebuilt from the bible, so a pack can never quietly disagree with it.

Authored view names taken from the shot classes below — `close-up`, `profile`,
`three-quarter`, and `full-body` — are matched by the reference selection plan and attached
ahead of other supplementary views. Any other view name is treated as scene-specific.

### Validation and prompt use

`character_identity.py PROJECT_DIR --check` fails closed before generation when the pack is
missing, structurally invalid, inconsistent with the bible, stale relative to
`source_fingerprint_sha256`, or backed by a reference view whose file is missing or escapes
the project boundary. Every path resolves through `contained_project_path()`.

`character_identity.py PROJECT_DIR --panel PANEL_ID` renders the deterministic `IDENTITY
LOCK` block for the characters that storyboard panel uses, ordered by the pack rather than
the panel so one character's clause is byte-stable project-wide. That block and
`identity_reference_paths()` emit plain text and relative paths only; they name no provider,
model, endpoint, or credential, so an adapter decides how to transmit them.

## Reference selection plan: `logs/reference-selection.json`

The reference selection plan records which character references each panel receives, in
which order, and why. It exists because a single reference does not constrain a character
equally well across every camera setup, and because a drifted panel cannot be debugged
without knowing the references it was actually given. Its `schema_version` is `1.0` and is
independent of the project manifest version. Top-level fields are exactly `schema_version`
and `panels`, one entry per storyboard panel in page and reading order.

`reference_strategy.py PROJECT_DIR --plan [--budget COUNT]` derives the plan from
`plan/character-identity-pack.json` and `plan/storyboard.json` and publishes it atomically
through the project transaction. It refuses to publish while the identity pack gate reports
an issue, and re-running it on an unchanged project rewrites byte-identical content. The
plan is provenance rather than a manifest `artifacts` descriptor, so `project.json` keeps
schema version `1.0`.

### Panel entry

| Field | Type | Rules |
|---|---|---|
| `panel_id` | ID | A storyboard panel ID |
| `shot_class` | enum | `close-up`, `profile`, `three-quarter`, `full-body`, or `unclassified` |
| `shot_cue` | string \| null | The cue matched inside the panel `shot` text; `null` when unclassified |
| `reference_budget` | integer \| null | Maximum references one panel may carry; `null` when unlimited and `0` when the capability supports none |
| `characters` | array[ID] | The panel's characters in identity-pack order |
| `selected` | array[selection] | Attachments in attachment order |
| `omitted` | array[omission] | Views deliberately not attached |

A selection contains exactly `character_id`, `view`, `path`, `reason`, and `rank`, where
`rank` is the 1-based attachment order and `reason` is `canonical-anchor`, `shot-aligned`,
`identity-supplement`, or `scene-specific`. An omission contains exactly `character_id`,
`view`, `path`, and `reason`, where `reason` is `duplicate-path`, `reference-budget`, or
`references-unsupported`. Every view a panel's characters carry appears exactly once across
`selected` and `omitted`, so the record accounts for the whole decision rather than only
its outcome.

### Selection rules

`shot_class` is derived from the authored panel `shot` text by the cue that appears
earliest, so a description opening with its framing is not reclassified by a later
incidental word. A cue counts only where it stands as its own word, so `overhead shot` is
not a head shot and `profiled character` is not a profile, while hyphenated compounds and
plurals such as `medium-wide` and `close-ups` do count. A cue is also ignored when a
negation such as `no`, `not`, `without`, or `instead` appears within the three words before
it, so an explicitly ruled-out framing yields to the next declared cue. A description with
no surviving cue stays `unclassified` rather than being guessed into a class it never
declared.

The canonical view ranks first for every shot class, because it is the only view
cross-checked against the character bible. The view named by the panel's shot class comes
next, then the remaining identity views, then scene-specific views. A budget is spent
breadth-first across the panel's characters, so every character receives its canonical
anchor before any character receives a second view, and a limit below the cast size records
the dropped character rather than hiding it. One path is attached at most once per panel: a
repeated path is recorded as `duplicate-path` and does not consume the budget.

The plan covers every panel or none. A page that is not an object, a page with no panel
array, a panel that is not an object, a panel without a string ID, and a repeated panel ID
are each rejected instead of skipped, because a silently skipped panel is exactly the panel
whose references nobody could afterwards account for.

Classification, ranking, and allocation read no clock, locale, or random seed. The rendered
plan block emits plain text and relative paths only and names no provider, model, endpoint,
or credential, so the caller supplies the reference limit and decides how to transmit the
attachments.

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

`shot` is free text and stays authored prose; the reference selection plan classifies it
rather than constraining it, and reports `unclassified` when it recognizes no framing cue.

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

Anchors are `top-left`, `top-center`, `top-right`, `middle-left`, `middle-right`, `bottom-left`, `bottom-center`, and `bottom-right`. `anchor` places every text item, captions included. Human `speaker_anchor` identifies the visible mouth/face voice-source region; spoken devices use `voice_source: device` and anchor their visible audio source.

A panel that letters more than one balloon must stay attributable. Because a
`speaker_anchor` is the only machine-readable evidence tying a balloon to a
character, two rules decide attribution in normalized panel space and are applied
identically by storyboard validation and by lettering. Anchors closer than `0.04`
belonging to *different* speakers read as one voice source and fail as
`shared-anchor`; anchors farther apart than `0.25` claimed by the *same* speaker
place one character in two positions and fail as `split-anchor`. One speaker may
hold several balloons at the same anchor. Every spoken balloon also needs a text
ID, unique within its panel, because placements, page-QA regions, and reviewer
evidence all address a balloon by that ID. A violation is reported as
`dialogue-attribution-ambiguous` — or `dialogue-attribution-required` for a
missing ID — and is never resolved by authoring order. Non-spoken system status is a caption and has no tail. Captions and SFX omit `voice_source` and `speaker_anchor`. Legacy `tail_target` remains readable but produces `balloon-tail-migration-required` at lettering and later stages; it is never silently reinterpreted. Control characters other than newline are invalid. Explicit newlines are optional wrapping hints. Authored punctuation and words are not rewritten by deterministic scripts.

The word limits are a ceiling, not a guarantee of fit. Dialogue is inscribed in an oval, which holds roughly half the text of the rectangle bounding it, and an anchor area is about 42% of panel width by 30% of panel height. A 32-word line needs a panel of roughly 1000x1200 px or larger; a 720x1064 panel holds about 14 words. Lettering fails with `text item {id} does not fit inside the panel` rather than printing over the artwork, so size dialogue to the panel rectangle the storyboard assigns it.

Lettering geometry is schema `1.1`. Every retained placement carries an
`attribution` record: `null` for captions and SFX, which are not spoken, and for
dialogue exactly `authored_speaker` (the token the storyboard wrote), `speaker`
(the stable character-bible ID it resolved to), `resolution`, and `speaker_anchor`
(the voice source the attribution is bound to).

`resolution` records how identity was established rather than offering a second
way to author it. A storyboard `speaker` must be a character-bible ID — a display
name does not match the ID pattern and is rejected by storyboard validation — so
every validated project records `declared`. `inferred` exists only for callers
invoking `letter_panel()` directly with a display name, which the renderer
accepted silently before and now resolves to exactly one character or refuses: a
name shared by two characters resolves to no one. Authoring dialogue against
display names is not a supported storyboard contract. Because
geometry is fully derived from the clean raster, the storyboard, and the font
policy, a record written at schema `1.0` is reported as
`lettering-record-stale` and re-lettered rather than migrated in place.

Dialogue tails are stored in lettering geometry as `organic-cubic-v1` records containing `attachment`, `base`, `control`, `tip`, `speaker_anchor`, `voice_source`, `source_gap`, `length`, `width`, and `policy_version`. The body and cubic tail are supersampled into one mask before one outline is derived. Page-QA check `bubble-tail-direction` requires exactly one current `regions` entry per dialogue with `panel_id`, `text_id`, `speaker`, `voice_source`, `speaker_anchor`, `tip`, and `result`; generic or stale regions fail closed.

Dialogue and captions are deterministic lettering inputs. SFX is authored storyboard
content for generation prompts and visual QA, but Pillow neither draws SFX nor allocates a placement rectangle or overlap reservation. Lettering summaries retain `text_count`
for the total authored item count and additionally report `rendered_text_count` for
dialogue/caption items and `sfx_count` for authored SFX items.

## Panel QA record: `qa/panels/{panel-id}.json`

Schema 2.0 is the canonical record. Its required top-level fields are
`schema_version`, `kind`, `subject_id`, `bindings`, `checks`, `review`, `decision`,
and `unresolved_warnings`; the only optional top-level field is `override_reason`.
`kind` is `panel-qa`; `subject_id` is the storyboard panel ID; a record may not also
contain the legacy `panel_id` field.

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

The `character-identity` check may additionally carry `provenance`; its presence marks a
trait-level character review. Its `regions` then contains exactly seven ordered entries
for every character in identity-pack order: `face`, `hair`, `age-appearance`, `clothing`,
`accessories`, `proportions`, and `immutable-traits`. Each entry contains exactly
`character_id`, `trait`, `expected`, `result`, `severity`, `evidence`,
`repair_guidance`. String expectations are used for face, hair, age appearance, and
clothing; accessories and immutable traits retain their arrays; proportions retains the
identity pack's `build` and `notes` object. Evidence is non-empty and specific. A
non-passing or warning-severity entry has non-empty repair guidance naming the character,
trait, canonical expectation, and observed drift; a clean pass uses `null` guidance.

Trait outcomes roll up deterministically. Any `fail`/`error` entry makes the parent
`fail`/`error` and requires `regenerate`; otherwise any warning makes the parent
`warning`/`warning` and selects `accept-warning`; all passes make it `pass`/`error`.
An explicit user override retains parent `result: fail`, downgrades both the parent and
failed trait to warning severity, and follows the existing override contract.

Character provenance contains exactly `panel_id`, canonical identity-pack and reference-
plan paths plus their SHA-256 values, and one entry per reviewed character with its
`source_fingerprint_sha256` and selected reference records. Validation rebuilds the
expected traits from the current character bible and identity pack, rehashes both bound
documents, and compares the panel's current reference selection. A changed bible,
identity pack, selected reference, or reference plan makes the accepted panel stale and
forces review on resume; unchanged inputs remain reusable.

`character_quality.py PROJECT_DIR --context PANEL_ID` prints the provider-neutral review
input. To publish normalized assessment objects into the existing panel QA record, pipe a
JSON array on standard input to `character_quality.py PROJECT_DIR --record PANEL_ID
--method METHOD --reviewer REVIEWER`. Publication uses `ProjectTransaction`; raw provider
responses, credentials, endpoints, and vendor-specific model contracts are not stored.

### Bounded defect regions

`anatomy` and `text-free` may additionally carry bounded defect regions, which is the
evidence a selective repair needs to touch part of a panel instead of all of it. `regions`
stays `[]` when a reviewer records no bounded evidence. Each entry contains exactly `area`,
`character_id`, `evidence`, `repair_guidance`, `result`, and `severity`. Exactly one of
`area` and `character_id` is non-null: `character_id` names a reviewed character, and
`area` is one of the eight storyboard anchors, so a defect is located by the vocabulary the
storyboard already uses rather than by pixel rectangles. Evidence is non-empty and
specific, a non-passing or warning-severity entry carries non-empty repair guidance, and a
clean pass uses `null` guidance, exactly as trait regions do. One `(scope, target)` pair
appears at most once per check.

A `character_id` must name a character the panel's own trait review covered, so a subject
region requires a `character-identity` check carrying provenance; a region naming anyone
else, or naming a character when no trait review established a cast, is rejected rather
than repaired. An `area` region needs no reviewed cast.

`action`, `composition`, `continuity`, and `technical` must keep `regions: []`. Faulting
camera framing, the scripted beat, a cross-panel anchor, or a whole-raster property is a
statement about the panel rather than about a patch of it, so bounded regions on those
checks are rejected instead of inviting a localized repair that cannot work.

## Repair plan: `logs/repair-plan.json`

The repair plan records how each reviewed panel should be repaired, which accepted content
the repair must leave alone, and why a repair could not be narrowed when it could not. It
exists because regenerating a whole accepted panel for one localized defect spends a scarce
retry budget and re-rolls detail the review already accepted. Its `schema_version` is `1.0`
and is independent of the project manifest version. Top-level fields are exactly
`schema_version` and `panels`.

`repair_strategy.py PROJECT_DIR --plan [--localized-edit]` derives the plan from
`plan/storyboard.json` and the published panel QA records and publishes it atomically
through the project transaction. `--localized-edit` states that the detected capability can
edit a bounded part of an existing raster; omitting it plans a full regeneration for every
repair. `--panel PANEL_ID` prints the same decision for one panel as provider-neutral plain
text. Re-running `--plan` on an unchanged project rewrites byte-identical content. The plan
is provenance rather than a manifest `artifacts` descriptor, so `project.json` keeps schema
version `1.0`.

`panels` carries one entry per reviewed panel, in storyboard page and reading order. A
panel with no QA record has not been reviewed and therefore carries no entry; a record that
exists but cannot be classified fails closed rather than being skipped.

### Panel entry

| Field | Type | Rules |
|---|---|---|
| `panel_id` | ID | A storyboard panel ID |
| `decision` | enum | The QA decision the plan was derived from |
| `strategy` | enum | `no-repair`, `selective-repair`, or `full-regeneration` |
| `fallback_reason` | enum \| null | Non-null only for `full-regeneration` |
| `localized_edit_supported` | boolean | The capability flag the caller supplied |
| `accepted_raw_path` | path | The bound accepted raster a repair must archive |
| `accepted_raw_sha256` | SHA-256 | That raster's reviewed hash |
| `targets` | array[target] | Repair targets in repair order; empty unless `selective-repair` |
| `defects` | array[defect] | Every non-passing check, repairable or not |
| `unaffected` | object | Exactly `checks` and `subjects` that recorded a clean pass |

A target contains exactly `scope`, `target`, `guidance`, and `rank`, where `rank` is the
1-based repair order, `scope` is `subject` or `area`, and `guidance` is the ordered
correction text for that target. A defect contains exactly `check_id`, `scope`, `target`,
`result`, `severity`, `evidence`, `guidance`, and `fallback_reason`, where `scope` is
`subject`, `area`, or `panel`, and `target` is `null` for panel scope. Every non-passing
check appears in `defects`, and every clean check appears in `unaffected.checks`, so the
record accounts for the whole review rather than only the part it repairs.

### Repair rules

A panel whose decision is not `regenerate` selects `no-repair`: the review accepted it, so
its warnings are recorded but not repaired. Otherwise the scope of each defect decides the
strategy. `character-identity` is subject-scoped through its trait regions; `anatomy` and
`text-free` are localized through their bounded defect regions; the four panel-wide checks
are never localized. A failing check with no usable region evidence, including a
`character-identity` check with no trait regions and a failing parent whose every trait
passed, is `unlocalized-evidence`, because a defect nobody located is a defect nobody can
repair in place.

`selective-repair` is selected only when every defect is localized, the accepted artifacts
still hash to their bindings, and the caller reports localized-editing support. Anything
else selects `full-regeneration` and records exactly one reason: `stale-bindings` when a
bound raw, clean, or normalization artifact is missing, unreadable, or changed;
`editing-unsupported` when the capability cannot edit in place; `panel-wide-check` when a
panel-wide check failed; and `unlocalized-evidence` otherwise. That precedence is fixed, so
one record and one capability flag always report the same reason, and every defect also
keeps the reason it could not be localized.

Warnings are repairable defects, not decoration: a warning region contributes its guidance
to its target so a repair does not fix the failure and leave the warning behind.

Classification reads no clock, locale, or random seed, and the module plans repairs without
performing one: it edits no raster and names no provider, model, endpoint, or credential.

A subject-scoped repair is planned only from a trait review its own gate accepts. The
`character-identity` check is validated before classification, its provenance must name the
panel under review, and no region on any check may fault a character outside the reviewed
cast, so a malformed region cannot aim a localized edit at an arbitrary character.

Reading the reviews, hashing the bound artifacts, and publishing the plan share one project
transaction, so they hold the lock that serializes every other project operation. A plan
derived outside that critical section could claim to preserve bytes that a concurrent
promotion had already replaced.

Validation re-derives every published entry from the current QA record using the recorded
capability flag. A panel whose review still says `regenerate` must carry a matching entry,
or the plan reports `repair-plan-stale`; an entry for a panel the review has since accepted
describes a repair that already succeeded and is kept as history. Coverage is checked in
both directions: a reviewed panel that still requires regeneration but has no entry reports
`repair-plan-incomplete`, so a truncated plan cannot validate while omitting a repair. A
plan naming a panel the storyboard does not have, repeating a panel, departing from
storyboard order, referring to a missing record, describing a record that cannot be trusted
(`repair-plan-record-invalid`), or carrying an unknown schema version is reported instead of
read.

Planning distinguishes a review that was never written from one that cannot be trusted. A
panel with no record is skipped as unreviewed, while a refused path, a symlink, a directory,
or an unreadable entry fails closed, because treating an unreadable review as an absent one
would publish a plan that quietly omits a panel awaiting repair.

Decisions are `accept`, `accept-warning`, or `regenerate`. An error-level failed
check requires `regenerate`; a warning result or warning severity requires
`accept-warning` or `regenerate`. `accept-warning` records a non-empty
`unresolved_warnings` list, while `accept` records none. Accepted records are only
reused after structural validation and all bound files, hashes, and dimensions are
rechecked.

`override_reason` is a non-empty user-provided reason that distinguishes an explicit
override from an ordinary warning. When present, the decision is `accept-warning`,
the same reason appears in `unresolved_warnings`, and at least one check retains
`result: fail` with severity downgraded to `warning`. The override operation starts
only from `regenerate` plus an error-level failed check, revalidates every current
binding before mutation, appends the reason to manifest warnings, and records a
`panel.overridden` event with the panel ID and accepted action. Missing, stale,
non-canonical, unreadable, or corrupt bindings cannot be overridden.

### Legacy schema 1.0 migration

Schema-1.0 records with `panel_id`, generation metadata, `failure_category`, and
`accept_with_warnings` remain readable and retain their existing override path for
compatibility. That path only accepts `failure_category: visual_qa`; corrupt-image,
safety, and non-visual categories remain non-overridable. Schema 1.0 is not
canonical: migrate identity to `subject_id`, move artifact provenance into the
schema-2.0 `bindings` object, provide rich checks and review data, and use
`accept-warning`.

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

## Typography preflight: `panels/{panel-id}/typography.json`

Schema version `1.1`. Written by lettering before any panel raster is published, and
re-verified by `validate_lettering_provenance`. The record is derived entirely from the
storyboard text and the font policy, so an older record is re-lettered rather than
migrated.

| Field | Rules |
| --- | --- |
| `kind` | Always `typography-preflight`. |
| `schema_version` | Always `1.1`. Version `1.0` records predate the script coverage policy and are reported stale. |
| `status` | Always `pass`. Preflight raises instead of persisting a failing record. |
| `issues` | Always `[]`, for the same reason. |
| `checks` | One entry per performed check, currently `typography-shaping-policy` and `typography-glyph-coverage`. Each carries `id`, `result` (`pass`), `severity` (`error`), `method` (`font-cmap-policy-v1`), `reviewer`, and `evidence`. |
| `glyphs` | One entry per visible character: `character`, `codepoint` (`U+XXXX`), `coverage` (`supported`), `shaping` (`supported`), `script`, `style` (`regular` or `bold`), `font_id`, and `item_id`. `font_id` is a bare file name, never a path and never `.notdef`. |
| `non_glyphs` | One entry per character with no drawn glyph: `codepoint`, `item_id`, and `policy` (`line-break` or `normalized-space`). |
| `scripts` | Roll-up of which faces served which script: `script`, `codepoints`, and sorted `font_ids`. Its `codepoints` total equals the length of `glyphs`. |
| `font_policy` | Role-to-font-id map. Required roles are `regular`, `bold`, and `fallback`; an optional per-script face appears as `script:{script}`. |
| `font_policy_sha256` | SHA-256 over each role's font ID and file digest in sorted role order. A policy with no script extension hashes identically to one from before extensions existed. |
| `input_sha256` | SHA-256 over the canonical authored text items. |

Characters are checked as the renderer will draw them: normalized to NFC, and
uppercased for `dialogue`. Uppercasing can move a character into a different Unicode
block, so `script` reflects the displayed form.

Script names, block ranges, and shaping classification come from
`scripts/font_coverage.py`, whose `main()` prints the coverage inventory. A script is
admitted only when advance-only placement renders it faithfully; see
`docs/typography.md` for the supported set, the selected extension fonts, and the
fallback order.

## Generated project paths

The version 1.0 project boundary contains `project.json`; exact source/request copies; the three plan JSON files; character/scene reference PNGs; preserved reference/panel prompt text; raw `panels/raw/{panel-id}.png`, clean `panels/clean/{panel-id}.png`, and lettered `panels/{panel-id}/lettered.png` panel images; per-panel QA JSON; per-page QA JSON `qa/pages/page-{NNN}.json`; `qa/report.md`; zero-padded `pages/page-001.png` files; the composition cache `cache/composition.json`; `exports/{project-id}.pdf`; the resume cache `logs/stage-cache.json`; and append-only `logs/events.jsonl`.

The opt-in `plan/character-identity-pack.json` companion artifact also lives inside this boundary, as does the `logs/reference-selection.json` provenance record derived from it and the `logs/repair-plan.json` record derived from the panel QA records.

Failed image attempts are retained as `panels/raw/{panel-id}.attempt-{attempt-number}.png`; only the accepted attempt occupies `panels/raw/{panel-id}.png`. An accepted raster is replaced only while its QA record asks for a repair: promotion verifies the new raster, refuses to overwrite a panel the review still accepts, and archives the previous accepted bytes before publishing the replacement. Only a review that was never written permits replacement, which is what initial generation and transient repeats need; a record that exists but cannot be resolved, read, or understood withholds permission rather than granting it, because it is not evidence that anything faulted the panel. The record is read inside the promotion transaction, under the same lock that publishes the replacement. Generated images intentionally contain no dialogue, captions, speech bubbles, signatures, logos, or watermarks. Exact storyboard-authored SFX is instead allowed and required in generated artwork; generated SFX is forbidden when the storyboard has none.

## Page QA record: `qa/pages/page-{NNN}.json`

One schema-2.1 `page-qa` record per composed page is created from `templates/page-qa.json`
after bounded visual inspection. It contains `schema_version: "2.1"`, `kind: "page-qa"`,
and `subject_id: "page-{NNN}"`; ten checks in the normative page order; a review object
with the fixed method `deterministic-plus-bounded-visual-review`, a non-empty reviewer, and
an ISO-8601 UTC `reviewed_at`; `decision`; and `unresolved_warnings`.

Seven of those checks are deterministic and authored by the engine with
`method: deterministic-geometry-v1` and `reviewer: comic-sol`: `clipped-text`,
`text-overlap`, `reading-order`, `layout-border-integrity`,
`balloon-subject-obstruction`, `bubble-tail-geometry`, and `balloon-crowding`. The
remaining three — `face-action-obstruction`, `bubble-tail-direction`, and
`accidental-text-watermark` — are the bounded visual review the caller supplies.

Balloon geometry is audited in each panel's own clean-raster pixel space, taken from
`panels/{panel-id}/normalization.json` (`clean.size`), not in the storyboard page
rectangle the panel is later fitted into. `clipped-text` regions report the offending
`box`; `text-overlap` regions add `overlap_area` and `overlap_ratio` against the smaller
box; `balloon-subject-obstruction` regions report the measured `clearance` and the
`required_clearance` a balloon must keep from an authored `speaker_anchor`;
`bubble-tail-geometry` regions report a `reason` of `missing-tail`,
`missing-attribution`, `speaker-mismatch`, `speaker-anchor-mismatch`,
`voice-source-mismatch`, `placement-kind-mismatch`,
`speaker-anchor-out-of-range`, `detached-tail`,
`tail-does-not-point-at-speaker`, or `attribution-anchor-mismatch`.
`missing-attribution` and `speaker-mismatch` audit identity rather than shape:
they compare the placement's retained `attribution` against the speaker the
storyboard authored, which is how a swapped pair of speakers is caught when both
tails are drawn correctly. `speaker-mismatch` requires *both* `speaker` and
`authored_speaker` to equal the storyboard's `speaker`, because the canonical
`speaker` is the identity consumers read and a record agreeing on only the
authored echo would leave it wrong silently. This is exact for a validated
storyboard, which authors a character-bible ID; an `inferred` record therefore
fails closed here, and only a storyboard that failed validation by authoring a
display name could produce one. `attribution-anchor-mismatch` is checked last and
against the drawn tail, so a storyboard edit is still reported as an anchor
mismatch and what remains here is attribution naming a voice source the tail was
never aimed at. `detached-tail` is measured against the ellipse
actually drawn rather than its bounding box, so an attachment resting in the
balloon body is detached even though it is inside the box.
`speaker-anchor-out-of-range` fails an anchor outside normalized `[0,1]` even when
the retained tail agrees with it, because a self-consistent tail can still aim at
a voice source that is not in the panel. `balloon-crowding` is the one warning-severity
deterministic check: it reports `balloons`, `coverage_ratio`, `coverage_limit`,
`required_separation`, and `tight_pairs` per crowded panel, selects `accept-warning`,
and never blocks export on its own.

`bindings` contains exactly `composition_cache_path`, `composition_cache_sha256`,
`layout_name`, `layout_version`, ordered `lettering_sha256s` values (`panel-id:sha256`),
ordered `normalization_sha256s` values (`panel-id:sha256`), `page_height`, `page_path`,
`page_sha256`, `page_width`, `storyboard_path`, and `storyboard_sha256`. Every value is
bound to the artifacts inspected by the record. `normalization_sha256s` is bound because
`clean.size` defines the pixel space every balloon verdict is measured in, so
re-normalizing a panel makes the record stale even when the page image is unchanged.

An error-level failed check selects `regenerate`; otherwise any check whose result or severity
is `warning` selects `accept-warning` and places its evidence, in check order, in
`unresolved_warnings`; all passing checks select `accept`. A legacy five-field record
(`page`, `page_path`, `page_sha256`, `schema_version`, `status`) is schema-1.0 input only:
it remains readable for reporting but requires migration and cannot satisfy final validation.

### Version 2.1 and the 2.0 migration

Version `2.1` records the check set that grew from seven entries to ten and the added
`normalization_sha256s` binding. A `2.0` record is reported as `quality-migration-required`
rather than as a malformed record, because a version field exists precisely to distinguish
"this predates a check-set change" from "the reviewer supplied the wrong check IDs".

`migrate_page_quality_record()` runs the registered `("2.0", "2.1")` hook inside the project
transaction. The seven deterministic checks and all twelve bindings are re-derived from
current artifacts; nothing is copied and no check result is ever fabricated. The three
reviewer-supplied checks and the original `review` object — reviewer and `reviewed_at`
included — are carried across only while the record's bound `page_sha256` still matches the
page on disk, because that digest is the evidence the reviewer inspected those pixels. When
the page has changed, migration is refused as stale and the page must be reviewed again.
`decision` and `unresolved_warnings` are re-derived from the merged ten-check set, since a
newly derived check can warn where the `2.0` record accepted.
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
- The character identity pack is re-derived from the character bible, so changing a fingerprint or a canonical reference changes the pack, changes every panel prompt that embeds it, and invalidates generation and every downstream artifact.
- The reference selection plan is derived from the identity pack and the storyboard, so adding a reference view, changing a canonical reference, or rewriting a panel's `shot` changes which references that panel receives and is republished with the plan.
- Changing dialogue or captions alone invalidates lettered panels, pages, PDF, and the final report; raw and clean panels remain reusable. Changing authored SFX invalidates generation and every downstream artifact because SFX belongs to the artwork.
- Artifact reuse requires file existence, matching recorded hash, schema validity, valid dependencies, and a matching stage cache key.
- Deterministic writes use a sibling temporary file, flush and `fsync`, then `os.replace`; the manifest transition is last.
- A no-change resume performs validation but writes no artifact, timestamp, or event.
- A project with an unresolved error-level panel is `BLOCKED` and cannot be composed/exported unless an allowed explicit override converts it to a warning.
- Page PNGs are opaque RGB 1600 × 2400, use exact storyboard rectangles, LANCZOS resizing, inward 6 px black panel borders, white background, and numeric order.
- The PDF contains the same RGB pages in numeric order at 150 DPI metadata and no additional margins.
