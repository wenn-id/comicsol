#!/usr/bin/env python3
"""SFX render-mode policy, deterministic verification flags, and provenance.

Comic Sol letters dialogue and captions itself, but SFX has always been handed to
the image model to bake into the artwork as motion typography. That trade buys
integrated, hand-drawn-looking effects, and it costs verifiability: the model is
non-deterministic, so an authored ``KRAK!`` can come back misspelled, mirrored,
illegible at panel scale, drawn twice, or silently missing, and nothing in the
project recorded that the effect was unverifiable in the first place.

This module makes that distinction explicit rather than implied. Three ideas
carry it:

- **Render mode is authored, not inferred.** An SFX item declares
  ``render_mode``: ``generated-visual`` asks the image model to draw it, and
  ``deterministic-lettering`` asks Pillow to. ``generated-visual`` is the default
  so every storyboard written before this policy keeps its exact meaning.
- **Provenance names the producer.** Every SFX item is recorded with the
  ``origin`` that actually put ink on the panel — ``image-model`` or
  ``comic-sol-lettering`` — and with how strongly that ink can be checked. A
  reviewer no longer has to guess whether a suspect effect came from the model or
  from the engine, which is the difference between re-rolling a panel and fixing
  a placement.
- **Flags state risk, they do not claim to read artwork.** Nothing here performs
  OCR or inspects pixels. What *is* deterministically knowable from the
  storyboard is which authored SFX the image model is unlikely to render
  faithfully, and which authored SFX a reviewer could not attribute even if it
  were rendered. Those are reported; semantic accuracy of arbitrary artwork
  stays a human judgement, exactly as it was.

Every function is pure and reads no clock, locale, or random seed, so one
storyboard always produces identical flag and provenance bytes. The module
deliberately imports nothing from the project I/O or lettering layers so that
policy stays usable by the renderer, the validator, and the lifecycle CLI alike.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence


# Authored render modes. `generated-visual` is the historical behaviour and the
# default for an item that declares nothing, so adding this field changes no
# existing project's meaning.
GENERATED_VISUAL = "generated-visual"
DETERMINISTIC_LETTERING = "deterministic-lettering"
SFX_RENDER_MODES = (GENERATED_VISUAL, DETERMINISTIC_LETTERING)
DEFAULT_SFX_RENDER_MODE = GENERATED_VISUAL

# What actually drew the effect, recorded per item so provenance answers the
# question directly instead of requiring the reader to re-derive it from the
# render mode plus the engine's behaviour at the time.
ORIGIN_IMAGE_MODEL = "image-model"
ORIGIN_LETTERING = "comic-sol-lettering"

# How strongly the recorded effect can be checked. Generated SFX is reviewable
# only by a human looking at the raster; lettered SFX is reproducible from the
# storyboard, the clean raster, and the pinned font policy.
VERIFICATION_REVIEWER = "reviewer-only"
VERIFICATION_DETERMINISTIC = "deterministic"

SFX_PROVENANCE_SCHEMA_VERSION = "1.0"
SFX_FLAG_METHOD = "sfx-policy-v1"
SFX_FLAG_REVIEWER = "comic-sol"

# Flag vocabulary, in the order flags are evaluated and reported. The order is
# normative: `validate_project` recomputes this list and compares it to the
# recorded one, so a reordering is a schema change.
SFX_FLAG_IDS = (
    "sfx-glyph-risk",
    "sfx-duplicate-content",
    "sfx-legibility-budget",
    "sfx-unprohibited-generation",
)

# Punctuation an image model draws as reliably as it draws letters. Everything
# outside Latin letters, digits, spaces, and this set is reported as glyph risk:
# a model asked to bake Cyrillic, Han, combining marks, or emoji into artwork
# routinely returns shapes that resemble the request without being it, and no
# amount of prompt wording makes that outcome checkable.
RELIABLE_SFX_PUNCTUATION = frozenset("!?.,'\"-–—*~/&:;()")

# Legibility budget for generated SFX. A single very long token has to be
# compressed to fit inside artwork, which is where dropped and doubled letters
# come from; a long total blocks the panel it is supposed to punctuate. Both are
# advisory: the 3-word schema limit remains the hard ceiling.
SFX_TOKEN_LENGTH_LIMIT = 12
SFX_CONTENT_LENGTH_LIMIT = 24

# A panel that letters its own SFX needs the image model told not to draw one,
# or the regenerated artwork keeps the baked effect and the drawn effect sits on
# top of it.
#
# The prohibition has to name *generated* SFX, because the negative every panel
# already carries does not. `unauthorized sfx` and `sfx other than the exact
# authored text` both say the opposite of what a lettered effect needs: they
# license the authored effect and forbid the rest, while a lettered effect is
# exactly the authored one the model must not draw. Accepting any entry that
# merely mentions SFX would be satisfied by that boilerplate on every panel, so
# the check would never fire on a real project — which is why the marker phrase
# is required instead of any wording the author likes.
GENERATED_SFX_PROHIBITION = "generated sfx"
DETERMINISTIC_SFX_NEGATIVE = "generated sfx text"


def is_sfx(item: object) -> bool:
    """Report whether a storyboard text item is an SFX item."""
    return isinstance(item, Mapping) and item.get("kind") == "sfx"


def sfx_render_mode(item: Mapping[str, object]) -> str:
    """Return the render mode an SFX item declares, defaulting to generation.

    An absent field is the documented default rather than an error, which is what
    keeps every storyboard authored before this policy readable unchanged. An
    unrecognized value is returned as-is so callers report it against the item
    instead of silently treating it as the default.
    """
    mode = item.get("render_mode")
    if mode is None:
        return DEFAULT_SFX_RENDER_MODE
    return mode if isinstance(mode, str) else ""


def is_generated_sfx(item: object) -> bool:
    """Report whether an item is SFX the image model is asked to draw."""
    return is_sfx(item) and sfx_render_mode(item) == GENERATED_VISUAL  # type: ignore[arg-type]


def is_deterministic_sfx(item: object) -> bool:
    """Report whether an item is SFX Comic Sol letters itself."""
    return is_sfx(item) and sfx_render_mode(item) == DETERMINISTIC_LETTERING  # type: ignore[arg-type]


def render_mode_problem(item: Mapping[str, object]) -> str | None:
    """Return why one text item's ``render_mode`` is invalid, or ``None``.

    The field belongs to SFX alone. Allowing it on dialogue or a caption would
    imply those kinds have a generated variant, which they do not: they are
    deterministic lettering inputs by definition.
    """
    if "render_mode" not in item:
        return None
    if item.get("kind") != "sfx":
        return "must be omitted for dialogue and caption"
    mode = item.get("render_mode")
    if not isinstance(mode, str) or mode not in SFX_RENDER_MODES:
        return f"must be one of {', '.join(SFX_RENDER_MODES)}"
    return None


def sfx_material(item: Mapping[str, object]) -> dict[str, object]:
    """Return one text item normalized for stage cache material.

    A `render_mode` that merely restates the default is dropped. Two consequences
    matter, and both are the point:

    - Writing the default explicitly is a documentation change, not a request to
      re-roll artwork or re-letter a page, so it must not invalidate a stage.
    - Every storyboard authored before this field existed keeps the exact material
      bytes it was generated under, so introducing the field costs no project a
      single provider call.

    A declared `deterministic-lettering` is preserved, because that genuinely
    changes what each stage is asked to produce.
    """
    material = dict(item)
    if is_sfx(material) and material.get("render_mode") == DEFAULT_SFX_RENDER_MODE:
        del material["render_mode"]
    return material


def normalized_text_material(text_items: object) -> object:
    """Return a panel's text items with restated default render modes removed.

    A malformed `text` is returned unchanged rather than replaced with an empty
    list. Stage material is computed from whatever is on disk, including a
    storyboard validation would reject, and quietly normalizing a broken panel to
    "no text" would let its cache key collide with a panel that genuinely has none.
    """
    if not isinstance(text_items, Sequence) or isinstance(text_items, (str, bytes)):
        return text_items
    return [
        sfx_material(item) if isinstance(item, Mapping) else item
        for item in text_items
    ]


def sfx_items(text_items: object) -> list[Mapping[str, object]]:
    """Return a panel's SFX items in the order lettering places them.

    Ordering matches the renderer's `(priority, id)` sort so provenance, flags,
    and placements all address the same item by the same position.
    """
    if not isinstance(text_items, Sequence) or isinstance(text_items, (str, bytes)):
        return []
    items = [item for item in text_items if is_sfx(item)]
    return sorted(
        items,
        key=lambda item: (_sort_priority(item.get("priority")), str(item.get("id", ""))),
    )


def _sort_priority(value: object) -> float:
    """Return a sortable priority that tolerates an unvalidated record."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _comparable(content: object) -> str:
    """Return a case- and whitespace-insensitive form used only for comparison.

    This is never rendered. It exists so `KRAK!` and `krak!` are recognized as
    the same authored effect, which is precisely the pair a reviewer cannot tell
    apart once the model has drawn both in its own hand.
    """
    if not isinstance(content, str):
        return ""
    normalized = unicodedata.normalize("NFC", content)
    return " ".join(normalized.split()).casefold()


def _risky_codepoints(content: object) -> list[str]:
    """Return the codepoints an image model is unlikely to letter faithfully."""
    if not isinstance(content, str):
        return []
    risky: list[str] = []
    for character in unicodedata.normalize("NFC", content):
        if character.isspace():
            continue
        if character.isascii() and character.isalnum():
            continue
        if character in RELIABLE_SFX_PUNCTUATION:
            continue
        risky.append(f"U+{ord(character):04X}")
    # Sorted and de-duplicated so the evidence string depends on the content and
    # not on where in the content a character happened to appear.
    return sorted(set(risky))


def _flag(
    flag_id: str,
    item_ids: Sequence[str],
    evidence: str,
    remediation: str,
    severity: str = "warning",
) -> dict[str, object]:
    """Build one deterministic SFX flag record."""
    return {
        "evidence": evidence,
        "id": flag_id,
        "item_ids": sorted(set(item_ids)),
        "method": SFX_FLAG_METHOD,
        "remediation": remediation,
        "result": "warning",
        "reviewer": SFX_FLAG_REVIEWER,
        "severity": severity,
    }


def negatives_prohibit_generated_sfx(panel: Mapping[str, object]) -> bool:
    """Report whether a panel's negatives forbid the image model drawing any SFX.

    An entry must contain `generated sfx`. Surrounding wording is free — `no
    generated SFX of any kind` and `generated sfx text` both count — but the two
    words have to appear, because every other SFX negative in this project says
    the opposite. `unauthorized sfx` permits the authored effect, and a lettered
    effect *is* the authored one, so a looser test would be satisfied by
    boilerplate on every panel and would never catch the panel that needs it.
    """
    negatives = panel.get("negative")
    if not isinstance(negatives, Sequence) or isinstance(negatives, (str, bytes)):
        return False
    return any(
        isinstance(entry, str) and GENERATED_SFX_PROHIBITION in entry.casefold()
        for entry in negatives
    )


def evaluate_sfx_flags(panel: Mapping[str, object]) -> list[dict[str, object]]:
    """Return every SFX verification flag one panel raises.

    Only flags that fired are returned, because a flag is a statement that
    something needs a human decision; the passing case is the recorded
    provenance itself. Flags never fail a build — they are warnings that name the
    item, the reason, and the one action that resolves it.
    """
    items = sfx_items(panel.get("text"))
    if not items:
        return []
    flags: list[dict[str, object]] = []
    generated = [item for item in items if is_generated_sfx(item)]

    risky = [
        (str(item.get("id", "")), _risky_codepoints(item.get("content")))
        for item in generated
    ]
    risky_items = [(item_id, codepoints) for item_id, codepoints in risky if codepoints]
    if risky_items:
        detail = "; ".join(
            f"{item_id}: {', '.join(codepoints)}" for item_id, codepoints in risky_items
        )
        flags.append(_flag(
            "sfx-glyph-risk",
            [item_id for item_id, _ in risky_items],
            "Generated SFX uses codepoints outside the repertoire an image model "
            f"letters reliably ({detail}).",
            "author the effect with Latin letters, digits, and comic punctuation, "
            f"or letter it with render_mode {DETERMINISTIC_LETTERING} under a font "
            "policy that covers the script (letter_panels.py --font-script)",
        ))

    seen: dict[str, list[str]] = {}
    for item in generated:
        seen.setdefault(_comparable(item.get("content")), []).append(
            str(item.get("id", ""))
        )
    duplicates = sorted(
        (content, ids) for content, ids in seen.items() if content and len(ids) > 1
    )
    if duplicates:
        detail = "; ".join(
            f"{', '.join(sorted(ids))} share {content!r}" for content, ids in duplicates
        )
        flags.append(_flag(
            "sfx-duplicate-content",
            [item_id for _, ids in duplicates for item_id in ids],
            "Generated SFX items in one panel request the same effect, so a "
            f"drawn effect cannot be attributed to one of them ({detail}).",
            "give each effect distinct content, drop the duplicate, or letter one "
            f"of them with render_mode {DETERMINISTIC_LETTERING}",
        ))

    over_budget: list[tuple[str, str]] = []
    for item in generated:
        content = item.get("content")
        if not isinstance(content, str):
            continue
        normalized = " ".join(unicodedata.normalize("NFC", content).split())
        longest = max((len(token) for token in normalized.split()), default=0)
        if longest > SFX_TOKEN_LENGTH_LIMIT:
            over_budget.append((
                str(item.get("id", "")),
                f"{longest}-character token exceeds {SFX_TOKEN_LENGTH_LIMIT}",
            ))
        elif len(normalized) > SFX_CONTENT_LENGTH_LIMIT:
            over_budget.append((
                str(item.get("id", "")),
                f"{len(normalized)} characters exceed {SFX_CONTENT_LENGTH_LIMIT}",
            ))
    if over_budget:
        detail = "; ".join(f"{item_id}: {reason}" for item_id, reason in over_budget)
        flags.append(_flag(
            "sfx-legibility-budget",
            [item_id for item_id, _ in over_budget],
            "Generated SFX is long enough that the image model must compress it "
            f"to fit the panel, which is where dropped letters come from ({detail}).",
            "shorten the effect, split it across panels, or letter it with "
            f"render_mode {DETERMINISTIC_LETTERING}",
        ))

    lettered = [item for item in items if is_deterministic_sfx(item)]
    if lettered and not negatives_prohibit_generated_sfx(panel):
        flags.append(_flag(
            "sfx-unprohibited-generation",
            [str(item.get("id", "")) for item in lettered],
            "This panel letters its own SFX, but its negatives never prohibit "
            "generated SFX, so regenerated artwork can bake an effect underneath "
            "the drawn one.",
            f"add a negative such as {DETERMINISTIC_SFX_NEGATIVE!r} to the panel",
        ))

    # Ordered by the declared vocabulary rather than lexically, because
    # `SFX_FLAG_IDS` is the normative order this record is compared against and a
    # reader should meet the flags in the order the policy lists them.
    return sorted(
        flags,
        key=lambda flag: (
            SFX_FLAG_IDS.index(flag["id"])  # type: ignore[arg-type]
            if flag["id"] in SFX_FLAG_IDS
            else len(SFX_FLAG_IDS),
            tuple(flag["item_ids"]),  # type: ignore[arg-type]
        ),
    )


def _placement(placements: object, item_id: str) -> Mapping[str, object] | None:
    """Return the placement recorded for one item, when it was drawn."""
    if not isinstance(placements, Sequence) or isinstance(placements, (str, bytes)):
        return None
    for placement in placements:
        if isinstance(placement, Mapping) and placement.get("id") == item_id:
            return placement
    return None


def _placement_box(placement: Mapping[str, object] | None) -> dict[str, int] | None:
    """Return the drawn rectangle a placement occupies."""
    if placement is None:
        return None
    box = placement.get("box")
    if not isinstance(box, Mapping):
        return None
    try:
        return {key: int(box[key]) for key in ("x", "y", "width", "height")}
    except (KeyError, TypeError, ValueError):
        return None


def sfx_provenance(
    panel: Mapping[str, object],
    placements: object = (),
) -> dict[str, object]:
    """Build the SFX provenance block recorded in lettering geometry.

    ``placements`` is the lettering summary's placement list, used only to bind a
    lettered effect to the rectangle it actually occupies. Content is recorded as
    the storyboard authored it rather than as it was displayed, so the block can
    be recomputed from `plan/storyboard.json` alone and compared byte for byte.
    """
    items: list[dict[str, object]] = []
    for item in sfx_items(panel.get("text")):
        item_id = str(item.get("id", ""))
        mode = sfx_render_mode(item)
        lettered = mode == DETERMINISTIC_LETTERING
        placement = _placement(placements, item_id) if lettered else None
        box = _placement_box(placement)
        # For a drawn effect the anchor is the one placement settled on, not the
        # one the storyboard asked for: the eight-anchor overlap search relocates
        # an effect whose requested anchor was taken, and provenance reporting the
        # request next to the resulting box would contradict itself.
        anchor = placement.get("anchor") if placement is not None else None
        items.append({
            "anchor": item.get("anchor") if anchor is None else anchor,
            "box": box,
            "content": item.get("content"),
            "id": item_id,
            "origin": ORIGIN_LETTERING if lettered else ORIGIN_IMAGE_MODEL,
            "render_mode": mode,
            "verification": (
                VERIFICATION_DETERMINISTIC if lettered else VERIFICATION_REVIEWER
            ),
        })
    return {
        "flags": evaluate_sfx_flags(panel),
        "items": items,
        "schema_version": SFX_PROVENANCE_SCHEMA_VERSION,
    }
