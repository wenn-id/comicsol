# Typography and font coverage

This document states which scripts Comic Sol letters, why the rest are refused, and
how to extend coverage for one project without changing the install.

The supported set is not a taste judgement. It follows from one implementation fact:
lettering draws text with Pillow's advance-only text placement, putting each glyph at
the pen position its predecessor's nominal advance produced. That is faithful for a
script whose glyphs do not change shape or order in context, and silently wrong for
one that does.

Pillow can be built against Raqm, which would shape text properly, but Raqm is an
optional native dependency. Deriving the supported set from whichever Pillow build a
host happens to have would make the same storyboard letter differently on different
machines, so the policy is declared as data in `scripts/font_coverage.py` instead.
Determinism is the constraint that decides this, not glyph availability.

## Inventory

`scripts/font_coverage.py` reads the bundled cmap tables and reports coverage per
declared Unicode block. Take the inventory yourself:

```
PYTHON scripts/font_coverage.py
PYTHON scripts/font_coverage.py --font regular=PATH --font fallback=PATH
```

`doctor` runs an abbreviated form of the same check and fails when the bundled faces
stop covering a script this document promises.

The bundled three-face policy — Comic Neue Regular, Comic Neue Bold, Noto Sans
Regular — covers 2842 codepoints in the Basic Multilingual Plane:

| Script | Blocks | Covered / claimed | Status |
| --- | --- | --- | --- |
| `latin` | 10 | 1160 / 1293 | partial |
| `common` | 18 | 675 / 2208 | partial |
| `cyrillic` | 5 | 441 / 448 | partial |
| `greek` | 2 | 354 / 400 | partial |
| `inherited` | 6 | 210 / 336 | partial |

Per face: Comic Neue Regular and Bold carry 303 codepoints each across 38 ranges;
Noto Sans Regular carries 2840 across 52 ranges. "Partial" is the normal and expected
state, because a declared block reserves codepoints Unicode has not assigned.

Coverage that was always present but never stated, and is now under regression test:

- Vietnamese, through Latin Extended Additional (`U+1E00-U+1EFF`)
- Polytonic Greek, through Greek Extended (`U+1F00-U+1FFF`)
- Historic and minority Cyrillic, through Cyrillic Extended-A, -B, and -C
- Latin Extended-C, -D, and -E orthographies
- IPA and phonetic modifier letters

## Target scripts

Comic Sol's stated goal for text is fidelity to the source language: the storyboard
carries a BCP-47 `language` tag, and lettering is expected to render that language's
own script rather than a transliteration. Targets follow from that goal filtered
through what advance-only placement can honestly draw.

**Bundled targets** — must letter with no extra configuration, asserted by
`tests/test_font_coverage.py`: `latin`, `greek`, `cyrillic`.

**Extension targets** — place linearly, so policy admits them, but no bundled face
carries their glyphs. Each is reachable by configuring one extension font:
`han`, `kana`, `hangul`, `armenian`, `georgian`, `ethiopic`.

Admitting CJK, kana, and precomposed Hangul syllables is a correction as much as an
expansion. They were previously refused as unshapeable, which was the wrong reason:
none of them need reordering or joining, so nominal advances place them correctly.
What they lacked was a font. They are now reported as coverage failures that name the
face which fixes them.

**Out of scope** — refused regardless of which font is configured, because no font
resolves them:

| Property | Scripts |
| --- | --- |
| Contextual joining with bidirectional runs | Arabic, Syriac, N'Ko, Mandaic |
| Bidirectional runs | Hebrew, Thaana, Samaritan |
| Cluster reordering and conjuncts | Devanagari, Bengali, Gurmukhi, Gujarati, Oriya, Tamil, Telugu, Kannada, Malayalam, Sinhala, Khmer, Myanmar |
| Mark stacking beyond nominal advances | Thai, Lao, Tibetan |
| Vertical layout with joining | Mongolian |
| Syllable composition | Conjoining Hangul jamo (`U+1100-U+11FF`) |
| Outside the lettering plane | Everything above `U+FFFF`, including emoji |

Supporting these needs a shaping engine, not a font selection, and that is a separate
change with its own determinism problem to solve.

Hangul shows why the classification is per block rather than per script: conjoining
jamo are refused while the precomposed syllables in `U+AC00-U+D7A3` are admitted.

## Selected extension fonts

Selected for redistributability first. Every entry is SIL Open Font License 1.1, the
same licence the bundled faces already carry, so an extension font travels under
terms the project already satisfies.

| Script | Family | File | Upstream |
| --- | --- | --- | --- |
| `han` | Noto Sans SC | `NotoSansSC-Regular.ttf` | https://github.com/notofonts/noto-cjk |
| `kana` | Noto Sans JP | `NotoSansJP-Regular.ttf` | https://github.com/notofonts/noto-cjk |
| `hangul` | Noto Sans KR | `NotoSansKR-Regular.ttf` | https://github.com/notofonts/noto-cjk |
| `armenian` | Noto Sans Armenian | `NotoSansArmenian-Regular.ttf` | https://github.com/notofonts/armenian |
| `georgian` | Noto Sans Georgian | `NotoSansGeorgian-Regular.ttf` | https://github.com/notofonts/georgian |
| `ethiopic` | Noto Sans Ethiopic | `NotoSansEthiopic-Regular.ttf` | https://github.com/notofonts/ethiopic |

**None of these are bundled, deliberately.** The CJK faces are several megabytes
each. Shipping them in every install so that an occasional project can letter one
language would make every download pay, permanently, for a benefit most runs never
use — against the same package-weight budget that governs the bundled samples. They
are opt-in per run instead, and preflight names the one a refused script needs, so the
cost falls on the project that wants the script.

Record any face you adopt the way `assets/README.md` records the bundled ones: pin
the upstream URL to a commit, note the upstream revision, and record the local
SHA-256. OFL 1.1 requires the licence to travel with the font, so vendor its `OFL.txt`
next to it.

## Configuring an extension font

```
PYTHON scripts/letter_panels.py PROJECT_DIR --font-script han=/fonts/NotoSansSC-Regular.ttf
```

`--font-script` is repeatable, once per script. Script names are the ones the
inventory prints. A script that cannot be lettered even with a covering face is
refused at the policy level rather than accepted and mis-drawn, so
`--font-script arabic=...` fails immediately.

The extension is recorded as a `script:<name>` role in the panel's font policy, and
its digest is bound into `font_policy_sha256`, so a project lettered with an extension
carries proof of which face drew which script.

## Fallback behaviour

One resolution order applies, and preflight and the renderer use the same one:

1. **Styled face** — Comic Neue Bold inside `**emphasis**`, otherwise Comic Neue
   Regular, or whatever `--font` overrides it with.
2. **Unicode fallback** — Noto Sans Regular.
3. **Script extension** — the face configured for that character's script, if any.

The styled face is tried first on purpose: a Japanese page with an English
interjection keeps the interjection in the comic voice instead of handing it to the
CJK face because that face also happens to cover Latin.

Preflight decides per character against the fonts' cmap tables and refuses the whole
batch before any panel is written, so a coverage problem never produces a partly
lettered project. That is why `.notdef` boxes are not a normal outcome: a character no
configured face maps is a hard failure, not a box on the page.

The renderer still keeps Noto Sans as its last stop and lets that face draw its own
`.notdef`. That path is unreachable through the pipeline, which preflights first; it
matters for `letter_panel()` called directly, where a visible box is a better outcome
than a silently dropped character.

## Preflight failures

`panels/<panel-id>/typography.json` records the outcome, and `PREFLIGHT_CHECKS` names
the checks that ran: `typography-shaping-policy` and `typography-glyph-coverage`. The
record also rolls up which face served which script, which is how an unintended
substitution becomes visible.

Two categories, distinguished by whether a font can fix the problem:

- **`missing-glyph`** — the script places correctly, but no configured face maps the
  character. Remediation names the vetted extension font when one is selected.
- **`unsupported-shaping`** — no font would help. The reason states the property that
  forbids it: contextual joining, bidirectional reordering, cluster reordering, mark
  stacking, syllable composition, or a codepoint outside the lettering plane.

Diagnostics carry font file names only, never absolute paths.

Two details change what preflight actually examines:

- **Dialogue is uppercased for display**, so preflight validates the uppercased text.
  This can move a character into a different Unicode block: Georgian Mkhedruli
  uppercases to Mtavruli in `U+1C90-U+1CBF`, and German `ß` becomes `SS`.
- **Text is normalized to NFC first**, so a combining sequence is checked as the
  precomposed codepoint the renderer will actually draw.

## Extending the policy

`tests/fixtures/typography-scripts/` holds one JSON fixture per script scenario, and
`tests/test_typography.py` drives the supported set from those files. Adding a script
means declaring its blocks in `scripts/font_coverage.py`, selecting a licensed face,
and adding a fixture — not editing a test body.

Changing the policy changes `font_policy_sha256` only when a run configures an
extension. A policy with no extensions hashes exactly as it did before extensions
existed, so existing projects are not marked stale by the mechanism's arrival.
