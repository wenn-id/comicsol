# Visual QA

Inspect every generated raw panel against its storyboard, character/scene references, and
declared invariants. Record non-empty evidence for exactly seven ordered checks:

1. `character-identity`: principal identity and all visible fingerprint invariants.
   For every on-panel character, inspect face, hair, age appearance, clothing,
   accessories, proportions, and immutable traits against the exact expectations emitted
   by `character_quality.py PROJECT_DIR --context PANEL_ID`. Record specific observed
   evidence for every trait; generic evidence such as `"ok"` or `"pass"` is invalid.
2. `anatomy`: readable pose, hands, limbs, face, and no beat-breaking defects.
   Evidence: short phrase (`"pose ok"`, `"hands visible"`, `"face correct"`).
3. `action`: the scripted action and important props are present and correct.
   Evidence: short phrase (`"present"`, `"props match"`, `"action correct"`).
4. `composition`: camera, subject placement, focus, and reserved text-safe area work.
   Evidence: short phrase (`"framing ok"`, `"text-safe clear"`).
5. `continuity`: exact character and scene anchors match adjacent panels.
   Evidence: short phrase (`"match prior"`, `"scene consistent"`).
6. `text-free`: no generated dialogue, caption, speech bubbles, logos, signatures,
   watermarks, blank/white placeholder rectangles, or empty balloon-like shapes.
   Exact storyboard-authored `generated-visual` SFX is allowed and required;
   missing, misspelled, duplicated, or unauthorized SFX fails this check, and generated
   SFX fails when none is authored `generated-visual` — including a panel whose effects
   are all `deterministic-lettering`, which asks the model for no SFX at all. Only the
   image model's SFX is reviewed here; effects Comic Sol letters are verified by their
   own provenance. Evidence: short phrase (`"no text"`, `"sfx correct"`,
   `"no placeholder"`).
7. `technical`: readable raster, minimum 512 px dimensions, aspect within ±2%, and no
   unintended alpha. Evidence: short phrase (`"512+ px"`, `"ratio ok"`, `"no alpha"`).

Results are `pass`, `warning`, or `fail`; severity is `warning` or `error`. Decisions are:

- `accept`: all required checks pass.
- `accept-warning`: readable warning-level impact remains and is named for the user.
- `regenerate`: an error-level failure needs a new attempt.

For trait-level identity review, pipe the seven normalized assessment records per
character to `character_quality.py PROJECT_DIR --record PANEL_ID --method METHOD
--reviewer REVIEWER`. The engine attaches the canonical expectation and reference
provenance, derives the panel decision, and generates subject-specific repair guidance for
every warning or failure. The review method may be human, model-assisted, or another
bounded visual process; core names no required provider or model.

To locate a defect that is not a character trait, record bounded defect regions on
`anatomy` or `text-free`: each entry names exactly one reviewed `character_id` or one of the
eight storyboard anchor `area` values, with specific evidence and repair guidance. Leave
`regions` empty when the evidence is not bounded, and never attach regions to `action`,
`composition`, `continuity`, or `technical`; those checks describe the whole panel.

## Selective repair budgets

- Initial generation permits at most 2 regenerations per panel.
- Visual retries and transient repeats share 8 extra calls project-wide.
- Plan every repair with `repair_strategy.py PROJECT_DIR --panel PANEL_ID
  [--localized-edit]`, then record the project-wide decision with `--plan`.
- A `selective-repair` edits only the listed subjects and areas, carries one correction
  clause per target, and leaves every other pixel of the accepted raster unchanged.
- A `full-regeneration` appends exactly one correction clause for observed failures while
  preserving every canonical anchor, reference, and other prompt content.
- Fall back to `full-regeneration` whenever the plan says so. It is chosen for
  `stale-bindings`, `editing-unsupported`, `panel-wide-check`, or `unlocalized-evidence`,
  and the reason is recorded rather than argued.
- Retain all attempt images. Do not touch passing panels or their hashes.
- Promotion replaces an accepted raster only while its QA record asks for a repair. Record
  the new review first; the previous accepted bytes are archived automatically. A record
  that cannot be read or whose decision is unrecognized refuses the replacement.
- A subject defect region names a character the panel's trait review covered. Use an
  `area` region, or record the trait review first, when it cannot.
- A faulty generated SFX has a cheaper remedy than re-rolling the panel and hoping.
  `python scripts/sfx_repair.py PROJECT_DIR --panel PANEL_ID --text-id TEXT_ID --reason
  "..."` routes that one effect to deterministic lettering, archives the rejected raw,
  clean, and lettered rasters under `panels/{panel-id}/sfx-audit/`, records the transition
  and the reason in `panels/{panel-id}/sfx-audit.json`, and adds the missing generated-SFX
  prohibition to the panel. It edits the plan, never a raster: planning and the
  storyboard stage stay cached, and generation and lettering re-derive the panel from
  the corrected storyboard.
- Whether that repair also needs a regeneration is a visual judgement the command cannot
  make, so it returns the answer as `next_action`. Re-lettering is the whole fix when the
  model omitted the effect. When the model drew a faulty one, that ink is still in the clean
  raster: re-review the panel to `regenerate` first, because promotion refuses to replace an
  accepted raster while its QA record still accepts the panel.
- Lettered SFX is excluded from `balloon-subject-obstruction` and `balloon-crowding`. Both
  encode rules about speech — a balloon must not cover the mouth it speaks from, a cluster
  must not crowd the reading path — and an effect is placed over the action deliberately.
  `clipped-text`, `text-overlap`, and `reading-order` still judge it.
- Permit one immediate transient repeat; it consumes the global budget but not the
  per-panel visual retry budget.
- After exhaustion, an error-level panel is `BLOCKED` and cannot reach lettering/export.

An explicit user may override a schema-2.0 panel only from `regenerate` with an
error-level failed visual check and current canonical `bindings`. The operation
revalidates all bound hashes and raster dimensions, downgrades the failed checks to
warning severity while retaining `result: fail`, selects `accept-warning`, records the
non-empty reason as `override_reason` and an `unresolved_warnings` entry, appends it to
manifest warnings, and appends a `panel.overridden` audit event. The run then continues
and the final transition selects `COMPLETE_WITH_WARNINGS`. Missing, stale, unreadable,
or corrupt artifacts cannot be overridden. Schema-1.0 compatibility additionally
requires `failure_category: visual_qa` and retains its legacy
`accept_with_warnings` spelling; safety refusal and non-visual categories remain
non-overridable.

## Composed-page QA

After composition, inspect numeric order, page continuity, borders/gutters, clipped or
overlapping text, bubble/caption readability, tail direction, covered faces/actions, and
consistent reading flow. For `bubble-tail-direction`, record exactly one bounded region per
dialogue with its `panel_id`, `text_id`, speaker, voice source, speaker anchor, resolved tip,
and result; generic `all-bubbles` evidence is rejected. Verify the tail points to the declared
voice source, stops before it, and has a continuous join, durable white core, consistent
outline, and clean tip. Any error-level panel or page keeps export blocked. Deterministic
hash/dimension/PDF checks bind evidence to current artifacts; they do not replace visual
inspection. A warning-level tail check must have at least one owned dialogue region marked
`fail`; a warning cannot hide all-passing regions. A page warning selects `accept-warning`
and records the check evidence in `unresolved_warnings`; an error-level failure selects
`regenerate`.

### Deterministic balloon placement audit

Balloon placement is audited by the engine before you inspect the page, so a review never
has to re-derive geometry by eye. All of it is measured in each panel's clean-raster pixel
space rather than the storyboard page rectangle, because that is the space lettering
geometry is written in.

- `clipped-text` — every box stays inside its clean raster.
- `text-overlap` — no two boxes in a panel intersect; regions report the shared area and
  its ratio against the smaller box so a hairline touch reads differently from a
  buried balloon.
- `balloon-subject-obstruction` — no balloon comes closer to an authored `speaker_anchor`
  than the gap the renderer reserves for a tail. Captions are measured exactly against
  their box; dialogue is measured radially against the ellipse actually drawn, the same way
  the renderer resolves a tail attachment. That radial measure is deliberately permissive
  for wide balloons, because measuring any tighter would reject balloons the renderer
  considers valid. A panel that authors no dialogue anchor has no protected subject and
  passes, and an anchor outside the raster is reported by `bubble-tail-geometry` rather
  than trusted as a keep-out region. This is the check that catches a caption dropped onto
  a speaking face, which placement never guards against because it resolves a box from its
  anchor keyword without consulting any other line's speaker.
- `bubble-tail-geometry` — every dialogue tail attaches to its balloon, stops short of its
  voice source, points at the authored anchor, and keeps its tip inside the panel; the
  retained tail must still agree with the storyboard's `speaker_anchor` and `voice_source`.
  Attachment is checked against the ellipse the renderer actually draws, not its bounding
  box, so a tail whose attachment has drifted into the balloon body is reported as
  `detached-tail` rather than accepted for being inside the box. An anchor outside
  normalized `[0,1]` fails as `speaker-anchor-out-of-range` even when the retained tail
  agrees with it, because a self-consistent tail can still aim outside the panel.
- `balloon-crowding` — warns when balloon coverage passes 30% of a panel or two balloons
  sit closer than the readable separation. It names the crowded panels and suggests
  shortening the line, moving it to another panel, or re-anchoring, then selects
  `accept-warning` rather than blocking the page.

These checks bound the geometry only. `face-action-obstruction` still requires a human to
judge whether a balloon covers something that matters artistically, and a passing
`balloon-subject-obstruction` never implies that review happened.
