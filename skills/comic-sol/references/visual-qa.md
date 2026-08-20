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
   Exact storyboard-authored SFX is allowed and required when authored;
   missing, misspelled, duplicated, or unauthorized SFX fails this check, and generated
   SFX fails when none is authored. Evidence: short phrase (`"no text"`, `"sfx correct"`,
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

## Selective repair budgets

- Initial generation permits at most 2 regenerations per panel.
- Visual retries and transient repeats share 8 extra calls project-wide.
- A retry appends exactly one correction clause for observed failures while preserving
  every canonical anchor, reference, and other prompt content.
- Retain all attempt images. Do not touch passing panels or their hashes.
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
