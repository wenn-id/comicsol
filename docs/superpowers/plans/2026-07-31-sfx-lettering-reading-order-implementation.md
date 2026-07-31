# SFX-Excluded Lettering Reading Order Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers/subagent-driven-development (recommended) or superpowers/executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure SFX never consumes deterministic lettering placement order.

**Architecture:** Keep the existing validated and sorted authored sequence for counts and provenance. Build one renderable-only sequence in `letter_panel` and enumerate only that sequence when placing dialogue and captions.

**Tech Stack:** Python 3.11, Pillow 12.3.0, `unittest`.

## Global Constraints

- Do not add fields or change the storyboard schema.
- Keep SFX validation and summary counts unchanged.
- Keep panel pixels byte-identical when an otherwise equivalent authored SFX is added.
- Modify only the lettering implementation, its regression test, and these design records.

---

### Task 1: Lock the failure with a public-behavior regression

**Files:**
- Modify: `tests/test_lettering.py`

**Interfaces:**
- Consumes: `letter_panel(output_path, panel_width, panel_height, text_items, character_bible) -> dict`
- Produces: a regression asserting placement IDs and contiguous `reading_order` values.

- [ ] **Step 1: Write the failing test**

Add a test that letters SFX priority 1, dialogue priority 2, and caption priority 3, then asserts placement IDs are dialogue/caption and reading orders are `[1, 2]`.

- [ ] **Step 2: Verify RED**

Run:

```bash
python -m unittest tests.test_lettering.LetteringTests.test_sfx_does_not_consume_rendered_reading_order -v
```

Expected: FAIL because the current values are `[2, 3]`.

### Task 2: Enumerate renderable items only

**Files:**
- Modify: `scripts/letter_panels.py:922-942`
- Test: `tests/test_lettering.py`

**Interfaces:**
- Consumes: validated `ordered: list[dict]`
- Produces: `renderable: list[dict]` containing only non-SFX items and contiguous placement order.

- [ ] **Step 1: Implement the minimal fix**

Create `renderable = [item for item in ordered if item.get("kind") != "sfx"]`, derive `rendered_text_count` from its length, and enumerate `renderable` directly.

- [ ] **Step 2: Verify GREEN and compatibility**

Run the focused regression and all lettering tests. Confirm the mixed-SFX byte-identity test still passes.

- [ ] **Step 3: Run full acceptance**

Run the complete unit suite, compile checks, package build/distribution validation, and diff hygiene. Deliver through a protected-branch pull request and verify the squash-merged `main` commit plus cross-platform CI.
