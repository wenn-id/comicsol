# Typography script fixtures

Each JSON file in this directory is one named script scenario for the typography
preflight, so the supported set is described as data instead of being spelled out
inside a test body. Adding a script to the policy means adding a fixture here, and
`tests/test_typography.py` fails if any fixture stops behaving as declared.

Text is the only input: a scenario is authored dialogue or caption content plus the
outcome the pinned bundled font policy must produce for it. Rasters are deliberately
not involved, because coverage and shaping are decided before anything is drawn.

## Fixture contract

| Field | Meaning |
| --- | --- |
| `description` | Why the scenario is interesting, in reviewer language. |
| `content` | Authored text, exactly as a storyboard text item would carry it. |
| `kind` | Text-item kind. `dialogue` is displayed uppercased, which can move a character into a different Unicode block. |
| `expected_status` | `pass` when preflight must accept the content, `fail` when it must refuse it. |
| `expected_scripts` | For a passing scenario, the script-to-font-id roll-up the record must report. |
| `expected_category` | For a failing scenario, `missing-glyph` or `unsupported-shaping`. |
| `expected_codepoint` | For a failing scenario, the first refused codepoint as `U+XXXX`. |
| `expected_reason_contains` | Substring the refusal reason must carry, so a reclassified script cannot silently keep passing for the wrong reason. |
| `expected_remediation_contains` | Substring the remediation must carry, normally the vetted extension font's family name. |

Unused fields are present and `null` so every fixture has the same shape.

## Combining marks

A single mark over a base from the same face is admitted, because a mark glyph
carries no advance and a negative left bearing and therefore lands on the base it
follows. Three arrangements are refused instead, and no font choice fixes any of
them: a mark with no base, a second mark stacking above the first, and a mark
drawn from a different face than its base.

## The two ways a script can be refused

The distinction the `bad-*` fixtures pin down is the point of the policy:

- `missing-glyph` means the script places correctly under advance-only lettering
  and simply has no covering face bundled. It is resolved by configuring the
  script's extension font, and the remediation names which one.
- `unsupported-shaping` means no font would help, because the script needs
  contextual joining, cluster reordering, mark stacking, or bidirectional runs
  that nominal glyph advances cannot express.

## Scenarios

| File | Exercises |
| --- | --- |
| `good-latin-vietnamese.json` | Vietnamese precomposed tone marks from Latin Extended Additional. |
| `good-greek-polytonic.json` | Polytonic Greek from Greek Extended. |
| `good-cyrillic-extended.json` | Historic and minority Cyrillic from Cyrillic Extended-A and -B. |
| `good-latin-extended-c-d.json` | Latin Extended-C and -D orthographies. |
| `good-phonetic-extensions.json` | IPA and phonetic modifier letters placed as linear glyphs. |
| `good-mixed-latin-greek-cyrillic.json` | Mixed-script dialogue keeping the comic face for Latin and emphasis. |
| `good-latin-combining-mark.json` | One combining mark over a base from the same face. |
| `bad-orphan-combining-mark.json` | A combining mark with no base glyph to attach to. |
| `bad-stacked-combining-marks.json` | Two marks on one base, which needs anchor geometry. |
| `bad-cross-face-combining-mark.json` | A mark drawn from a different face than its base. |
| `bad-undeclared-block.json` | A codepoint in no classified block, refused rather than assumed linear. |
| `bad-han-uncovered.json` | CJK ideographs: linear, uncovered, resolved by the Han extension. |
| `bad-kana-uncovered.json` | Hiragana: linear, uncovered, resolved by the Kana extension. |
| `bad-hangul-syllable-uncovered.json` | Precomposed Hangul syllables: linear, uncovered. |
| `bad-armenian-uncovered.json` | Armenian, checked through its uppercased dialogue form. |
| `bad-georgian-uncovered.json` | Georgian, whose uppercased form is Mtavruli in a different block. |
| `bad-arabic-shaping.json` | Contextual joining plus bidirectional reordering. |
| `bad-hebrew-shaping.json` | Bidirectional reordering with no joining. |
| `bad-devanagari-shaping.json` | Syllable cluster reordering and conjunct formation. |
| `bad-thai-shaping.json` | Mark stacking that nominal advances cannot express. |
| `bad-hangul-jamo-shaping.json` | Conjoining jamo, refused where precomposed syllables are admitted. |
| `bad-astral-pictograph-shaping.json` | A codepoint outside the lettering plane. |
