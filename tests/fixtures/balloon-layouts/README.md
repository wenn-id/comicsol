# Balloon placement layout fixtures

Each JSON file in this directory is one named balloon layout applied on top of the
lettered `tests/fixtures/valid-one-page` project, so a placement defect can be
described as data instead of being hand-built inside a test body.

Rasters are deliberately not duplicated: only `panels/<panel-id>/lettering.json`
is rewritten, which is exactly the artifact a migration, a repair, or a manual
edit can leave behind in a real project.

## Fixture contract

| Field | Meaning |
| --- | --- |
| `description` | Why the layout is good or bad, in reviewer language. |
| `expected_check` | Page check ID this layout is designed to exercise, or `null` for the good baseline. |
| `expected_result` | Result that check must report: `pass`, `warning`, or `fail`. |
| `expected_decision` | Record decision the layout must produce. |
| `expected_reasons` | Optional tail reasons the failing regions must report. |
| `panels` | Per-panel edits: `replace` merges fields into an existing item by ID, `append` adds whole placement records. |

Coordinates are in each panel's own clean-raster pixel space, which is the space
lettering geometry is written in. For this project that is 736x588 for `p01-01`
and 720x1064 for `p01-02` and `p01-03`.

## Layouts

| File | Exercises |
| --- | --- |
| `good-baseline.json` | Unmodified engine output; every deterministic check passes. |
| `bad-out-of-bounds-clean-space.json` | A box inside the storyboard page rectangle but outside the clean raster. |
| `bad-subject-obstruction-dialogue.json` | A dialogue balloon centred on its own protected speaker anchor. |
| `bad-subject-obstruction-caption.json` | A caption dropped onto another line's protected speaker anchor. |
| `bad-tail-points-away.json` | A tail tip that no longer points at its authored speaker. |
| `bad-tail-voice-source.json` | Tail voice source that disagrees with the storyboard. |
| `bad-tail-detached.json` | An attachment inside the bounding box but off the drawn ellipse. |
| `bad-swapped-speaker-attribution.json` | Attribution naming a different character than the storyboard, as swapped speakers leave behind. |
| `bad-missing-speaker-attribution.json` | A balloon retained with no attribution to verify at all. |
| `bad-attribution-anchor-drift.json` | Attribution bound to a voice source the tail was not drawn toward. |
| `warn-crowded-coverage.json` | One balloon covering more of the panel than the reading budget allows. |
| `warn-crowded-separation.json` | Two balloons sitting closer than the readable separation. |
