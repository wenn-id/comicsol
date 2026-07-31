# SFX-Excluded Lettering Reading Order Design

## Problem

Comic Sol sorts every authored text item by `priority` and `id`, then enumerates that complete list before skipping SFX. Because SFX is generated inside the artwork and is not placed by deterministic lettering, an SFX with priority 1 incorrectly causes the first rendered dialogue or caption to receive `reading_order: 2`.

## Decision

Preserve the authored ordering used for validation, counts, and deterministic sorting. Derive a separate `renderable` sequence containing only dialogue and caption items, then enumerate that sequence from 1 when producing placements.

SFX remains validated, included in `text_count`, `sfx_count`, and `word_count`, and excluded from placement and rendering. No storyboard field, schema version, cache version, or migration is added.

## Required behavior

Given these authored items:

1. SFX, priority 1
2. Dialogue, priority 2
3. Caption, priority 3

lettering must produce exactly two placements:

- Dialogue with `reading_order: 1`
- Caption with `reading_order: 2`

The same renderable text must produce byte-identical panel output whether an earlier SFX item is present or absent.

## Validation

A regression test exercises the public `letter_panel` behavior and must fail on the existing implementation because its placement orders are `[2, 3]`. After the minimal engine fix, focused lettering tests, the complete suite, package/distribution checks, and cross-platform CI must pass.
