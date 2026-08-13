---
name: comic-sol
description: Create, storyboard, render, resume, repair, and export finished original manga/anime comics from a short prompt, prose story, pasted narrative, or local .txt/.md source. Use when Codex should produce editable plans, consistent panel PNGs, composed page PNGs, a PDF, manifest, and transparent QA report without building a web app.
---

# Comic Sol

Turn one natural-language request into a local, editable comic project. Operate as an
agent workflow: reason about story and images, use an exposed image-generation
capability, and delegate deterministic validation, lettering, composition, export, and
reporting to the bundled Python scripts.

## Read progressively

- Read [workflow](references/workflow.md) for input detection, all ten stages, commands,
  state transitions, failures, resume, and completion.
- Read [creative direction](references/creative-direction.md) before authoring plans,
  character fingerprints, storyboards, references, or image prompts.
- Read [capability detection](references/capability-detection.md) immediately before
  selecting or invoking an image-generation tool.
- Read [visual QA](references/visual-qa.md) before accepting, retrying, overriding,
  composing, or exporting any generated panel.
- Read [safety and IP](references/safety-ip.md) before sending prompts externally and
  whenever people, minors, sensitive data, named styles, franchises, or refusals appear.
- Read [schemas](references/schemas.md) whenever writing or revising JSON artifacts.

## Core orchestration

1. Detect resume/source-file/pasted-story/short-prompt mode in the normative order.
2. Ask only a materially required question listed in the workflow reference; otherwise
   apply defaults and continue without confirmation.
3. Run the local doctor, initialize or inspect the project, then write and validate each
   semantic artifact before advancing its status. Persist each completed stage with
   `record-stage` so resume can reuse honest cache keys.
4. Detect image capability from tools exposed in the current agent session. Do not ask
   deterministic scripts to discover or call an image provider.
5. Generate canonical references and panels into attempt paths. Require the image model
   to draw each exact storyboard SFX in the artwork, inspect every result visually, record
   all seven QA checks, and selectively repair only failures within budget.
6. Promote accepted attempts; deterministically letter dialogue and captions while
   validating and counting exact storyboard SFX without drawing it in Pillow; compose
   pages, inspect every composed page and write its `qa/pages/page-{NNN}.json` record,
   export the PDF, render the QA report (which projects the terminal status the project
   is about to reach), validate final integrity, then transition to that terminal status.
7. Return status, counts, warnings, and clickable project output paths.

## Token budget rules

Apply these rules in every session to reduce token waste without reducing output quality.

## Fast Mode

Fast Mode keeps all quality gates while removing the specific waste that turns a
pilot into a 4+ hour run. Apply it to every new production by default.

### Never read engine source

Do not read, grep, or open any file under `scripts/`, `comic_sol_product/`, or
`tests/`. The deterministic engine is a black box invoked only via `init`,
`doctor`, `validate`, `status`, `resume-plan`, and `finalize`. Reading source
wastes context and risks editing the engine you should not touch.

If doctor reports missing Python/font/Pillow, stop and report the exact required
environment change. Obtain explicit user approval, then apply only that documented
fix; do not patch the engine to make the check pass. If a version pin looks wrong,
report it as a skill upgrade, not an ad-hoc edit.

### Use init, never hand-write setup scripts

Do not write `setup_batch_*.py`, `build_plans.py`, or equivalent helpers. The engine
owns project structure. Create a project with one command:

```text
python3.11 scripts/comic_sol.py init --output-root OUTPUT_ROOT --title TITLE --source SOURCE --request-json REQUEST_JSON
```

For setup, edit only the semantic artifacts the engine expects (`plan/*.json`) through
`transition` stages. Page QA later requires agent-authored
`qa/pages/page-{NNN}.json` records. Hand-written scaffolding is the single largest
hidden time sink.

### Right-size reasoning per stage

Plan, storyboard, QA-evidence, finalize, and report do not need maximum reasoning.
Reserve the highest reasoning only for the actual image-generation decision loop.
Statement: use a cheaper/mid reasoning preset for deterministic stages and the
strongest preset for image prompting and visual rejection decisions. Do not run the
entire run at `xhigh`.

### Parallel independent panels

Queue independent panel generations without waiting for each to complete before
starting the next. Panels with no dependency on each other's accepted output can be
generated in the same batch. Visual QA still inspects every accepted attempt before
promote.

### One resolution check per artifact

Check panel/source at original resolution during panel QA. Do not re-run a second
phone-scale (390px) check on every panel. Apply the 390px readability check once,
on the composed page, in page QA — that is where phone readability is actually
decided.

### Two-call finalization

After the last panel is promoted, run `finalize` once to letter and compose, inspect
the composed pages at full and phone scale, write each `qa/pages/page-*.json` from
`templates/page-qa.json`, then run `finalize` once more. Never re-run lettering,
composition, or export turn-by-turn.

### Lock the brief

Before generation, confirm the batch map once (e.g. Batch A pages 1-4, Batch B
pages 5-8), persist it in project artifacts, and reuse it unchanged when resuming.
The batch map takes precedence only over contradictory internal checklist counts;
do not invent a third project to satisfy a miscounted checklist. Page-count limits,
safety/IP rules, engine validation, visual QA, and final-acceptance gates remain
authoritative.

### Progressive loading

Do not read all references at once. Load only the files needed for the current stage:

- Read [workflow](references/workflow.md) immediately after input detection.
- Read [creative direction](references/creative-direction.md) before writing plans.
- Read [capability detection](references/capability-detection.md) just before generating.
- Read [visual QA](references/visual-qa.md) just before inspecting panels.
- Read [safety and IP](references/safety-ip.md) only when people, minors, or sensitive
  content appear in the source.
- Read [schemas](references/schemas.md) only when resolving a schema field ambiguity.

### No subagents

Do not spawn subagents, delegate, or fork for review, audit, or independent inspection.
Perform all visual QA, final validation, and completion checks in the main agent thread.

### Concise evidence

Record QA evidence as a short keyword phrase (e.g. `"match"`, `"PASS"`, `"pose ok"`,
`"hands visible"`, `"no text"`). Do not write long-form sentences such as
`"Live panel visually reviewed against canonical reference and storyboard."`

### Deterministic fallthrough

After the last panel passes QA and is promoted, run the full deterministic pipeline
with one combined command instead of per-stage turns:

```text
python3.11 scripts/comic_sol.py finalize PROJECT_DIR
```

When `finalize` is available, prefer it over stacking the deterministic stages
turn-by-turn. When `finalize` is not available, use the `comic_finalize` MCP tool or run
the stage-by-stage route in the workflow reference.

`finalize` fails closed with `page_qa_required` until every composed page has an
agent-authored `qa/pages/page-{NNN}.json` record matching that page's hash. Run
`finalize` once to letter and compose, inspect each page, write its record from
`templates/page-qa.json`, then run `finalize` again. Never fabricate that record.

### Evidence provenance

Label deterministic sample evidence as `mechanics-only`: it proves normalization,
layout, lettering, retry/resume, provenance, rollback, and export integrity, but does
not prove live visual quality. Use `scripts/quality_sample.py PROJECT_DIR --mode
deterministic` to write that disclosure into `qa/evidence.json`.

Live visual evidence is valid only for an already retained local attempt. Supply its
provider/model, attempt path and SHA-256, references, reviewer method, and known
limitations. The sample runner never invokes an image provider and refuses live mode
without a retained attempt. The QA report discloses these fields without inferring or
fabricating them.

### Completion response

Report final status, pages, panels, generation/retry count, and unresolved warnings. Give
clickable PDF path, page directory, manifest path, and QA report path. Do not spawn a
subagent to audit results.

## Deterministic command route

When installed as a package, use the stable `comic-sol` executable for `doctor`, `init`,
`status`, `validate`, `resume`, `finalize`, and `mcp`. Source checkouts retain the
script routes below for compatibility.

Use Python 3.11 from the skill root. Replace uppercase placeholders with resolved paths
or values; quote shell arguments safely.

```text
python3.11 scripts/comic_sol.py doctor --output-root OUTPUT_ROOT
python3.11 scripts/comic_sol.py init --output-root OUTPUT_ROOT --title TITLE --source SOURCE --request-json REQUEST_JSON
python3.11 scripts/comic_sol.py status PROJECT_DIR --json
python3.11 scripts/comic_sol.py transition PROJECT_DIR TARGET [--warning TEXT]
python3.11 scripts/validate_project.py PROJECT_DIR --stage plan|storyboard|panels|final [--json]

python3.11 scripts/comic_sol.py resume-plan PROJECT_DIR --json
python3.11 scripts/comic_sol.py resume PROJECT_DIR --json
python3.11 scripts/comic_sol.py invalidate PROJECT_DIR STAGE
python3.11 scripts/comic_sol.py record-stage PROJECT_DIR STAGE
python3.11 scripts/comic_sol.py record-attempt PROJECT_DIR PANEL_ID initial|visual_retry|transient_repeat PATH
python3.11 scripts/comic_sol.py promote-attempt PROJECT_DIR PANEL_ID PATH
python3.11 scripts/comic_sol.py override-panel PROJECT_DIR PANEL_ID --reason TEXT

python3.11 scripts/letter_panels.py PROJECT_DIR [--font PATH]
python3.11 scripts/compose_pages.py PROJECT_DIR --all
python3.11 scripts/compose_pages.py PROJECT_DIR --page N
python3.11 scripts/export_pdf.py PROJECT_DIR [--output PATH]
python3.11 scripts/render_report.py PROJECT_DIR [--output PATH]
```

Never fabricate successful artifacts, provider capability, visual evidence, or a terminal
success status. Preserve editable intermediates and stop at `BLOCKED` when safe completion
is impossible.
