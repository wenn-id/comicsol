# Visual QA

Inspect every generated raw panel against its storyboard, character/scene references, and
declared invariants. Record non-empty evidence for exactly seven ordered checks:

1. `character-identity`: principal identity and all visible fingerprint invariants.
   Evidence: short phrase (e.g. `"match"`, `"hair ok"`, `"eyes correct"`).
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
- `accept_with_warnings`: readable warning-level impact remains and is named for the user.
- `regenerate`: an error-level failure needs a new attempt.

## Selective repair budgets

- Initial generation permits at most 2 regenerations per panel.
- Visual retries and transient repeats share 8 extra calls project-wide.
- A retry appends exactly one correction clause for observed failures while preserving
  every canonical anchor, reference, and other prompt content.
- Retain all attempt images. Do not touch passing panels or their hashes.
- Permit one immediate transient repeat; it consumes the global budget but not the
  per-panel visual retry budget.
- After exhaustion, an error-level panel is `BLOCKED` and cannot reach lettering/export.

An explicit user may override an error categorized `visual_qa` with a recorded reason
only when the image is readable and an error-level check has failed. The
override downgrades the failed error-level checks to warning severity, records
`override_reason`, and appends the reason to the panel and manifest warnings; the run
then continues and the final transition selects `COMPLETE_WITH_WARNINGS`. Never
override an unreadable/corrupt image, safety refusal, or non-visual failure.

## Composed-page QA

After composition, inspect numeric order, page continuity, borders/gutters, clipped or
overlapping text, bubble/caption readability, tail direction, covered faces/actions, and
consistent reading flow. For `bubble-tail-direction`, record exactly one bounded region per
dialogue with its `panel_id`, `text_id`, speaker, voice source, speaker anchor, resolved tip,
and result; generic `all-bubbles` evidence is rejected. Verify the tail points to the declared
voice source, stops before it, and has a continuous join, durable white core, consistent
outline, and clean tip. Any error-level panel or page keeps export blocked. Deterministic
hash/dimension/PDF checks bind evidence to current artifacts; they do not replace visual
inspection.
