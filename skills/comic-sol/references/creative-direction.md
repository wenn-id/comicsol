# Creative direction

## Original story and style

Use original manga/anime direction described through line weight, contrast, palette,
texture, lens, camera, lighting, and pacing. Do not place living-artist, active-studio, or
franchise names in prompts. Keep the must-have comic to 1–4 pages and 1–12 panels.

## Story and character planning

- Build a beginning, turn, climax, and ending across 2–5 scenes.
- Give each scene a purpose, location, time, participating character IDs, and an exact
  continuity anchor covering architecture, palette, time, and light source.
- Give every recurring or speaking character a stable ID and visual fingerprint:
  silhouette, face, hair, wardrobe, palette, signature props, 2–5 visible invariants,
  and explicit avoid traits.
- Reuse fingerprint and scene-anchor strings exactly; do not paraphrase them per panel.

Generate one neutral canonical character reference: full body, front three-quarter pose,
readable wardrobe/props, plain background, and no lettering. A visual scene reference is
worth its cost only when the same location appears in 3 or more panels; otherwise repeat
the exact text anchor. Once a dependent panel is accepted, references are immutable.

## Panel scripting and layouts

Each panel specifies beat, scene, characters, shot, composition, action, expression,
lighting, continuity, negatives, text-safe anchor, and dialogue/caption/SFX payload. Keep
dialogue at 32 words, captions at 45, SFX at 3, and total items at 0–3 per panel.

Choose one deterministic preset for pacing:

- `full-page`
- `two-horizontal`
- `three-horizontal`
- `hero-top-two-bottom`
- `two-top-hero-bottom`

Let validation supply exact 1600 × 2400 geometry, 64 px margins, and 32 px gutters.

## Prompt construction

Write each preserved panel prompt in this exact order:

1. Project style anchor.
2. Exact scene continuity anchor.
3. Exact visual fingerprints for every on-panel character.
4. Panel action and expression.
5. Camera, composition, and lighting.
6. Reserved text-safe areas for deterministic dialogue and caption lettering.
7. Exact storyboard-authored SFX: when present, require the image model to draw every
   exact item once as dynamic motion/action typography integrated into the artwork.
   If no SFX is authored, prohibit generated SFX.
8. Negative constraints: no generated dialogue, captions, or speech bubbles; no
   blank/white placeholder rectangles of any kind; no empty balloon-like shapes;
   no logos, signatures, watermarks, unauthorized text/SFX, duplicated principal
   characters, or avoid-trait drift.

Request the storyboard rectangle's aspect ratio. Supply canonical references whenever the
capability supports them; otherwise strengthen the unchanged text anchors and disclose
degraded consistency in QA.
