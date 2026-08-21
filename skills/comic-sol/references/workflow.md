# Comic Sol workflow

## Input detection and project boundary

Choose mode in this order:

1. `resume` takes precedence when the request names an existing directory containing
   `project.json` and says resume, continue, retry, or finish.
2. `source_file` applies to an existing named `.txt` or `.md` file. Require readable
   UTF-8, at most 200 KiB, and preserve its exact bytes.
3. `pasted_story` applies to narrative prose of at least 120 characters or two paragraph
   breaks.
4. Otherwise use `short_prompt`.

Reject a missing file, invalid UTF-8, unsupported extension, or oversized source before
initialization. Generated files stay below the chosen project directory: `project.json`,
`source/`, `plan/`, `references/`, `prompts/`, `panels/`, `qa/`, `pages/`, `exports/`, and
`logs/`. Never overwrite an unrelated directory.

## Materially missing questions

Ask only when one of exactly four conditions holds:

1. The source is unreadable or the intended source among multiple named files is ambiguous.
2. The requested page count exceeds 4 or panel count exceeds 12; offer truncation.
3. The content's audience rating is materially ambiguous because it contains explicit
   sexual content, graphic gore, or an apparently real minor.
4. The output directory exists but is not a valid Comic Sol project and writing could
   overwrite unrelated files.

Otherwise continue with these defaults:

- Pages: 2
- Panels: 4–8, at most 4 per page
- Reading direction: Left-to-right
- Page: 1600 × 2400 px portrait
- Geometry: 32 px gutter and 64 px outer margin
- Direction: original high-contrast manga/anime with expressive ink-like linework and
  restrained color accents
- Rating: Teen, without explicit sex or graphic gore
- Language: source language
- Output root: `./comic-sol-output/`
- Retry: 2 regenerations per panel and 8 extra calls project-wide

Give a short interpretation and announce page/panel count before generation, but do not
pause unless a material condition applies.

## Ten stages

After a stage's required inputs and outputs validate, persist its resume cache with
`comic_sol.py record-stage PROJECT_DIR
planning|storyboard|generation|lettering|composition|export`. Record before advancing
to the next production stage; record `export` after the terminal QA report is rendered.
Without a valid recorded cache entry, `resume-plan` honestly marks the affected stage
and its downstream stages for rerun instead of guessing.

### 1. Detect and initialize

Run `comic_sol.py doctor`, then `comic_sol.py init` with exact source/request files. For
resume, run `comic_sol.py status` and `comic_sol.py resume-plan`, then recover by status:

- A `BLOCKED` project recovers with `comic_sol.py resume PROJECT_DIR`. Only `resume`
  clears `blocked_from` and `blocked_reason`, and it invalidates the stale stages itself.
  Running `invalidate` on a blocked project leaves those fields set and every later
  validation rejects the manifest, so `invalidate` refuses while a project is `BLOCKED`.
- A non-blocked project with a stale stage uses `comic_sol.py invalidate PROJECT_DIR
  STAGE` from the earliest stale stage only.

Initialization creates the generated directory boundary and `INIT` manifest.

### 2. Plan story and characters

Write canonical `plan/story-plan.json` and `plan/character-bible.json` using the schemas
and creative reference. Run `validate_project.py PROJECT_DIR --stage plan`, revise invalid
semantic content, then `comic_sol.py transition PROJECT_DIR PLANNED`.

Then publish the character identity pack with `character_identity.py PROJECT_DIR --derive`.
The pack is the identity context every panel prompt embeds later, so deriving it once here
keeps each panel pointing at one structured copy of the traits instead of a fresh
paraphrase. Re-run `--derive` after any character-bible fingerprint edit.

### 3. Script and storyboard

Write dialogue, captions, exact SFX, pacing, camera, light, continuity, fixed layouts,
and absolute rectangles to `plan/storyboard.json`. Every dialogue identifies its
character-bible `speaker`, sets `voice_source` to `human` or `device`, and places a
normalized `speaker_anchor` on the visible mouth/face or device audio-source region.
Non-spoken system status is a caption with no tail; captions and SFX omit dialogue-only
fields. Legacy `tail_target` is readable but blocks lettering with
`balloon-tail-migration-required` and must be migrated explicitly. Dialogue and captions
are always deterministic lettering content. SFX chooses its producer with `render_mode`:
`generated-visual` (the default) is artwork content the image model bakes in, and
`deterministic-lettering` is drawn by Comic Sol as outlined display type. Prefer
`deterministic-lettering` when the effect must be legible verbatim — non-Latin scripts
especially — and `generated-visual` when integration with the artwork matters more than
exactness. Transition through `SCRIPTED`, validate with
`validate_project.py PROJECT_DIR --stage storyboard`, then transition to `STORYBOARDED`.

### 4. Detect image capability

Follow the capability reference. Record neutral feature flags in `project.json`. If none
is available, transition to `BLOCKED` with the exact preservation error. Do not create
empty image files.

Note the capability's documented maximum number of reference images per request. That
count is the `--budget` value the reference plan spends in stage 5; the engine never
infers it.

### 5. Generate canonical references

Generate and inspect one canonical reference for each recurring character. Generate a
scene reference only at the creative threshold. Preserve prompts and transition to
`REFERENCES_READY` only when references are usable.

Record any additional usable view beyond the bible reference in the identity pack's
`reference_views`, then re-run `character_identity.py PROJECT_DIR --derive`; authored views
and proportion notes survive re-derivation while derived identity fields are rebuilt. Name
a view `close-up`, `profile`, `three-quarter`, or `full-body` when it is that framing of
the character, so panels of that shot receive it; any other name is treated as
scene-specific and ranked after the identity views.

Then publish the reference plan with `reference_strategy.py PROJECT_DIR --plan [--budget
COUNT]`. Pass the capability's reference limit from stage 4 as `--budget`, omit it when
there is no limit, and pass `0` when `supports_reference_images` is false. The plan is
recorded at `logs/reference-selection.json`, so a panel that drifts can be reviewed
against the references it was actually given. Re-run `--plan` after changing a reference
view or a panel's `shot`.

### 6. Generate panels

Before the first panel, run `character_identity.py PROJECT_DIR --check`. It fails closed on
a missing, invalid, stale, or unbacked identity pack rather than letting identity drift
start.

Write each ordered prompt, requiring the image model to integrate every exact authored
SFX once and prohibiting generated dialogue, captions, speech bubbles, logos,
signatures, watermarks, or un-authored SFX. Embed the `character_identity.py PROJECT_DIR
--panel PANEL_ID` block verbatim in the prompt, so a retry or a resume reuses the same
identity context instead of rewording it.

Attach exactly the references named by `reference_strategy.py PROJECT_DIR --panel PANEL_ID
[--budget COUNT]`, in the order it lists: the canonical view leads, the view matching the
panel's shot follows, and scene-specific views come last. Do not re-add a view the block
records as omitted; each omission states its reason, and a reference path shared by two
characters is deliberately attached once rather than spending the limit twice.

Invoke the selected agent tool into an attempt file, then run `comic_sol.py
record-attempt`. Confirm readable raster output and at least 512 px in both dimensions.
Never promote before visual QA.

### 7. Visual QA and selective repair

Apply all seven checks from the QA reference with evidence, including exact SFX spelling,
count, and authorization. Retry only failed panels and retain every attempt. Use
`comic_sol.py promote-attempt` for accepted images; it refuses to overwrite a panel whose
record still accepts it, so record the new review before promoting a repair. Use
`comic_sol.py override-panel` only for an explicit allowed user override: it downgrades
the failed error-level checks to warning severity, records the reason on the panel and
manifest, and the run continues toward `COMPLETE_WITH_WARNINGS` at the final transition.
Validate with `validate_project.py PROJECT_DIR --stage panels`, then transition through
`PANELS_READY` and `QA_READY`.

Before judging `character-identity`, run `character_quality.py PROJECT_DIR --context
PANEL_ID`. Assess all seven emitted traits for every listed character, then pipe the
normalized assessment array to `character_quality.py PROJECT_DIR --record PANEL_ID
--method METHOD --reviewer REVIEWER`. The command replaces only the panel's existing
`character-identity` check, preserves the other six checks, publishes atomically, and
derives `accept`, `accept-warning`, or `regenerate`. Do not persist a raw vision-provider
response; keep only the bounded evidence accepted by this command.

When a panel fails `character-identity`, read its entry in
`logs/reference-selection.json` before rewriting the prompt: a panel whose omissions name
`reference-budget` or `references-unsupported` was constrained by fewer references than
the pack carries, which is a different repair from a prompt that contradicted them. Use
the failed trait's `repair_guidance` as the single correction clause; it names the subject
and canonical expectation without requiring a provider-specific repair API.

Before repairing any faulted panel, run `repair_strategy.py PROJECT_DIR --panel PANEL_ID`,
adding `--localized-edit` only when the detected capability can edit a bounded part of an
existing raster. A `selective-repair` block lists the subjects and areas you may touch and
the accepted content you must leave alone; a `full-regeneration` block states which of
`stale-bindings`, `editing-unsupported`, `panel-wide-check`, or `unlocalized-evidence`
withdrew the narrower option. To locate a non-identity defect for the planner, record
bounded defect regions on `anatomy` or `text-free` when the review can name a character or
an anchor area. Publish the project-wide record with `repair_strategy.py PROJECT_DIR --plan
[--localized-edit]` after each review so `logs/repair-plan.json` matches the current
records; validation reports a plan that no longer does.

### 8–10. Deterministic finalization (letter, compose, export, complete)

After the last panel is accepted and promoted, prefer one combined command:

```text
PYTHON scripts/comic_sol.py finalize PROJECT_DIR
```

`finalize` runs lettering, composition, PDF export, report rendering, final validation,
and the terminal transition without extra agent turns. Do not spawn subagents to
re-audit results.

`finalize` fails closed with `page_qa_required` until every composed page has an
agent-authored `qa/pages/page-{NNN}.json` record whose `page_sha256` matches the page on
disk. That record is visual evidence and cannot be fabricated or generated by a script,
so the normal sequence is: run `finalize` once to letter and compose, inspect each
composed page, write its record from `templates/page-qa.json`, then run `finalize` again
to complete. Recomposing a page invalidates its record.

If `finalize` is unavailable, run the stages individually in this order:

1. `letter_panels.py PROJECT_DIR [--font PATH]`, `comic_sol.py record-stage PROJECT_DIR
   lettering`, then transition to `LETTERED`. `text_count` is the authored total;
   `rendered_text_count` counts every item drawn; `sfx_count` counts validated authored
   SFX; `lettered_sfx_count` counts the SFX subset Pillow drew. Pillow must not draw
   `generated-visual` SFX. Each panel's `sfx` provenance block names every effect's
   `origin` as `image-model` or `comic-sol-lettering`; replace a faulty generated effect
   with `sfx_repair.py PROJECT_DIR --panel PANEL_ID --text-id TEXT_ID --reason "..."`,
   which archives the rejected rasters and re-letters from the corrected storyboard.
2. `compose_pages.py PROJECT_DIR --all`, `comic_sol.py record-stage PROJECT_DIR
   composition`, then transition to `COMPOSED`. Composition also writes
   `cache/composition.json` and its manifest descriptor.
3. Inspect each composed page and write its `qa/pages/page-{NNN}.json` record. Confirm
   with `validate_project.py PROJECT_DIR --stage export-ready`.
4. `export_pdf.py PROJECT_DIR` full-content verifies every decoded PDF page and
   transactionally records `pdf_verification` at `exports/pdf-verification.json`
   alongside the `pdf` descriptor, then transition to `EXPORTED`.
5. `render_report.py PROJECT_DIR`, which records the `qa_report` descriptor. The report
   must exist before the terminal transition because final validation requires it, and
   it projects the terminal status the project is about to reach. Do not re-render it
   afterwards; that would invalidate the recorded hash.
6. `validate_project.py PROJECT_DIR --stage final`, then transition to `COMPLETE`,
   `COMPLETE_WITH_WARNINGS`, or `BLOCKED`, and record the `export` cache with
   `comic_sol.py record-stage PROJECT_DIR export`.

The success path is:

`INIT → PLANNED → SCRIPTED → STORYBOARDED → REFERENCES_READY → PANELS_READY → QA_READY → LETTERED → COMPOSED → EXPORTED → COMPLETE`

## Evidence provenance

Deterministic quality fixtures and `quality_sample.py --mode deterministic` prove
mechanics only: normalization, layout, typography policy, retry/resume, provenance,
rollback, and artifact integrity. They do not prove live visual quality.

For live visual evidence, retain the actual attempt first, then run
`quality_sample.py --mode live-visual` with its relative path, provider/model,
references, reviewer method, and known limitations. The runner hashes the retained
local file and never calls a provider. Missing retained attempts or provenance fail
closed. The QA report reads `qa/evidence.json` and displays the claim boundary.

## Failure taxonomy

- Invalid input: stop before initialization and name the path, encoding, or size issue.
- Invalid semantic artifact: retain earlier stages, identify file/field, and revise it.
- Capability unavailable: preserve plans, transition `BLOCKED`, and give enable/resume
  instructions.
- Safety refusal: do not evade; record only a sanitized category and transition `BLOCKED`.
- Quota/transient failure: permit one bounded repeat, then preserve and block.
- Invalid image: retain the attempt and selectively retry within budget.
- Visual QA failure, including missing, misspelled, duplicated, or unauthorized SFX:
  repair only the failed panel; passing hashes remain unchanged. Prefer the narrowest
  repair the plan allows, and fall back to full regeneration with the recorded reason.
- Lettering/glyph overflow: preserve images and revise supported text downstream only.
- Missing/stale/corrupt artifact: invalidate its earliest owning stage and downstream.
- Composition/PDF failure: retain lettered panels and rerun only deterministic outputs.

## Completion response

Report final status, pages, panels, generation/retry count, and unresolved warnings. Give
clickable PDF path, page directory, manifest path, and QA report path. State
`COMPLETE_WITH_WARNINGS` plainly; never present `BLOCKED` as partial success.
