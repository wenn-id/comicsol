"""Real-world comic benchmark corpus for Comic Sol.

A single golden project cannot represent the layouts and story structures the
pipeline has to handle. This module defines a compact benchmark corpus: one
plan-complete comic project per realistic production scenario, each annotated
with the capability it stresses.

Every project is materialized programmatically, which keeps the committed
fixtures text-only and small, lets the engine write canonical JSON, and takes
panel rectangles from the immutable layout registry instead of duplicating
geometry by hand.

Materialize the whole corpus for local evaluation with:

    python -m tests.benchmark_corpus OUTPUT_ROOT
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.comic_sol import (
    atomic_write_json,
    init_project,
    layout_rects,
    read_json,
    sha256_file,
    transition,
)

SCHEMA_VERSION = "1.0"
LANGUAGE = "en"
INPUT_MODE = "short_prompt"

BASE_NEGATIVE = (
    "speech bubbles",
    "captions",
    "generated text",
    "watermark",
    "logos",
    "signatures",
)
BASE_AVOID = ("logos", "generated text", "franchise characters")


def _fingerprint(silhouette, face, hair, wardrobe, palette, props, invariants):
    """Return a stable visible-trait fingerprint object."""
    return {
        "avoid": list(BASE_AVOID),
        "face": face,
        "hair": hair,
        "invariants": list(invariants),
        "palette": list(palette),
        "signature_props": list(props),
        "silhouette": silhouette,
        "wardrobe": wardrobe,
    }


def _character(
    identifier, name, role, age_band, pronouns, personality, motivation, speech, fingerprint
):
    """Return one character-bible entry."""
    return {
        "age_band": age_band,
        "id": identifier,
        "motivation": motivation,
        "name": name,
        "personality": list(personality),
        "pronouns": pronouns,
        "reference_path": f"references/characters/{identifier}.png",
        "role": role,
        "speech": speech,
        "visual_fingerprint": fingerprint,
    }


def _bible(characters):
    """Return a complete character bible artifact."""
    return {"characters": list(characters), "schema_version": SCHEMA_VERSION}


def _scene(identifier, purpose, location, time, characters, anchor):
    """Return one story-plan scene."""
    return {
        "characters": list(characters),
        "continuity_anchor": anchor,
        "id": identifier,
        "location": location,
        "purpose": purpose,
        "time": time,
    }


def _story(title, logline, theme, tone, setting, beginning, turn, climax, ending, scenes):
    """Return a complete story-plan artifact."""
    return {
        "beginning": beginning,
        "climax": climax,
        "ending": ending,
        "logline": logline,
        "rating": "teen",
        "scenes": list(scenes),
        "schema_version": SCHEMA_VERSION,
        "setting": setting,
        "theme": theme,
        "title": title,
        "tone": list(tone),
        "turn": turn,
    }


def _dialogue(speaker, content, anchor, priority, speaker_anchor, voice_source="human"):
    """Return one dialogue text item without its generated identifier."""
    return {
        "anchor": anchor,
        "content": content,
        "kind": "dialogue",
        "priority": priority,
        "speaker": speaker,
        "speaker_anchor": [speaker_anchor[0], speaker_anchor[1]],
        "voice_source": voice_source,
    }


def _caption(content, anchor, priority):
    """Return one caption text item without its generated identifier."""
    return {
        "anchor": anchor,
        "content": content,
        "kind": "caption",
        "priority": priority,
        "speaker": None,
    }


def _sfx(content, anchor, priority):
    """Return one authored sound-effect item without its generated identifier."""
    return {
        "anchor": anchor,
        "content": content,
        "kind": "sfx",
        "priority": priority,
        "speaker": None,
    }


def _panel(
    scene_id,
    beat,
    characters,
    shot,
    composition,
    action,
    expression,
    lighting,
    continuity,
    text,
    negative=(),
):
    """Return one storyboard panel without its generated identity or rectangle."""
    return {
        "action": action,
        "beat": beat,
        "characters": list(characters),
        "composition": composition,
        "continuity": list(continuity),
        "expression": expression,
        "lighting": lighting,
        "negative": list(BASE_NEGATIVE) + list(negative),
        "scene_id": scene_id,
        "shot": shot,
        "text": list(text),
    }


def _page(layout, panels):
    """Return one storyboard page without its generated number."""
    return {"layout": layout, "panels": list(panels)}


def build_storyboard(pages):
    """Return a storyboard with registry rectangles and canonical identities.

    Panel identity, reading order, rectangles, and text identifiers are derived
    here so a benchmark scenario only declares creative content.
    """
    resolved_pages = []
    for page_index, page in enumerate(pages, start=1):
        rectangles = layout_rects(page["layout"])
        panels = page["panels"]
        if len(panels) != len(rectangles):
            raise ValueError(
                f"page {page_index} declares {len(panels)} panels for layout "
                f"{page['layout']} which fixes {len(rectangles)}"
            )
        resolved_panels = []
        for panel_index, panel in enumerate(panels, start=1):
            panel_id = f"p{page_index:02d}-{panel_index:02d}"
            rectangle = rectangles[panel_index - 1]
            resolved_panels.append({
                **panel,
                "id": panel_id,
                "order": panel_index,
                "rect": {
                    "height": int(rectangle["height"]),
                    "width": int(rectangle["width"]),
                    "x": int(rectangle["x"]),
                    "y": int(rectangle["y"]),
                },
                "text": [
                    {**item, "id": f"{panel_id}-t{text_index:02d}"}
                    for text_index, item in enumerate(panel["text"], start=1)
                ],
            })
        resolved_pages.append({
            "layout": page["layout"],
            "number": page_index,
            "panels": resolved_panels,
        })
    return {"pages": resolved_pages, "schema_version": SCHEMA_VERSION}


# --- dialogue-heavy ---------------------------------------------------------

NADIA = ("grey press vest", "folded notebook")
BRAM = ("loosened tie", "red pencil")
PRESS_ROOM = "fluorescent tubes over stacked paper and cold coffee"
NIGHT_DESK = "one green desk lamp against black windows"

DIALOGUE_HEAVY = {
    "capability": (
        "Dense multi-speaker dialogue: three text items per panel, alternating speakers, "
        "and explicit human speaker anchors."
    ),
    "stresses": ("text:dialogue-density", "text:speaker-anchor", "characters:pair"),
    "title": "Dialogue Heavy Benchmark",
    "source": (
        "A late-night newsroom argument about whether to publish a dock story "
        "before the deadline closes.\n"
    ),
    "expected_pages": 1,
    "expected_panels": 2,
    "characters": _bible([
        _character(
            "nadia", "Nadia", "investigative reporter", "adult", "she/her",
            ("persistent", "blunt"),
            "publish the dock story before the deadline closes",
            "short declarative sentences",
            _fingerprint(
                "tall narrow build with square shoulders",
                "angular face with a thin scar over the left brow",
                "short black hair pushed behind the ears",
                "grey press vest over a white shirt",
                ("grey", "white", "ink blue"),
                ("folded notebook",),
                NADIA,
            ),
        ),
        _character(
            "bram", "Bram", "night editor", "middle-aged", "he/him",
            ("cautious",),
            "protect the paper from an unsourced claim",
            "measured questions",
            _fingerprint(
                "broad heavy build",
                "round face with deep laugh lines",
                "greying hair combed straight back",
                "rolled shirtsleeves and a loosened tie",
                ("brown", "cream", "brass"),
                ("red pencil",),
                BRAM,
            ),
        ),
    ]),
    "story": _story(
        "Fourteen Minutes",
        "A reporter and her editor argue over one dock story minutes before deadline.",
        "Proof is what turns anger into journalism.",
        ("tense", "grounded"),
        "A small city paper that still prints a night edition.",
        "Nadia demands the front page for the dock story.",
        "Bram refuses to run it without a ledger.",
        "Nadia produces a signed manifest instead.",
        "The story runs with the ledger named as missing.",
        [
            _scene(
                "press-room", "open the argument", "cramped newsroom bullpen",
                "late night", ("nadia", "bram"), PRESS_ROOM,
            ),
            _scene(
                "night-desk", "force a decision", "editor corner desk",
                "past midnight", ("nadia", "bram"), NIGHT_DESK,
            ),
        ],
    ),
    "pages": [
        _page("two-horizontal", [
            _panel(
                "press-room",
                "Nadia demands the front page.",
                ("nadia", "bram"),
                "medium two-shot at eye level",
                "speakers on opposite thirds with a clear upper text band",
                "Nadia drops the notebook on the desk while Bram leans back",
                "impatient against wary",
                "flat overhead fluorescent key with weak fill",
                (f"nadia:{NADIA[0]}", f"bram:{BRAM[1]}", f"press-room:{PRESS_ROOM}"),
                [
                    _dialogue(
                        "nadia", "The dock story runs tonight or it never runs at all.",
                        "top-left", 1, (0.28, 0.42),
                    ),
                    _dialogue(
                        "bram", "Then give me a source I can actually print.",
                        "top-right", 2, (0.71, 0.46),
                    ),
                    _caption("Fourteen minutes to deadline.", "bottom-center", 3),
                ],
            ),
            _panel(
                "night-desk",
                "Bram sets one condition.",
                ("nadia", "bram"),
                "tight two-shot across the desk",
                "faces low in frame with a protected top text band",
                "Nadia slides the manifest forward while Bram marks it",
                "steady against reluctant",
                "single warm desk-lamp key with deep shadow fill",
                (f"nadia:{NADIA[1]}", f"bram:{BRAM[0]}", f"night-desk:{NIGHT_DESK}"),
                [
                    _dialogue(
                        "nadia", "I have the manifest. Two names, both signed.",
                        "top-left", 1, (0.30, 0.40),
                    ),
                    _dialogue(
                        "bram", "Names are not proof. Bring me the ledger.",
                        "top-right", 2, (0.69, 0.44),
                    ),
                    _dialogue("nadia", "Then hold the front page.", "middle-left", 3, (0.32, 0.63)),
                ],
            ),
        ]),
    ],
}


# --- action ----------------------------------------------------------------

KAVI = ("orange running jacket", "canvas drop bag")
TUAN = ("long charcoal coat", "steel whistle")
ROOFTOP_RUN = "hard white sun on corrugated tin and dust"
MARKET_DROP = "striped awning shade over crowded stalls"

ACTION_SEQUENCE = {
    "capability": (
        "Rapid action beats: three stacked panels, authored sound effects, and motion-led "
        "composition with minimal dialogue."
    ),
    "stresses": ("motion:action-beats", "text:authored-sfx", "layout:three-horizontal"),
    "title": "Action Sequence Benchmark",
    "source": (
        "A rooftop courier is chased across a market district and has to drop a "
        "package mid-run.\n"
    ),
    "expected_pages": 1,
    "expected_panels": 3,
    "characters": _bible([
        _character(
            "kavi", "Kavi", "rooftop courier", "young-adult", "they/them",
            ("quick", "reckless"),
            "reach the drop point before the pursuer closes",
            "clipped shouts",
            _fingerprint(
                "lean athletic build",
                "sharp face with a split lower lip",
                "shaved sides with a short top knot",
                "orange running jacket with taped sleeves",
                ("orange", "slate", "white"),
                ("canvas drop bag",),
                KAVI,
            ),
        ),
        _character(
            "tuan", "Tuan", "syndicate pursuer", "adult", "he/him",
            ("relentless",),
            "recover the package for the syndicate",
            "flat threats",
            _fingerprint(
                "square powerful build",
                "wide face with a flattened nose",
                "black hair cropped to the scalp",
                "long charcoal coat over heavy boots",
                ("charcoal", "steel", "rust"),
                ("steel whistle",),
                TUAN,
            ),
        ),
    ]),
    "story": _story(
        "Tin Roof Run",
        "A courier outruns a syndicate enforcer long enough to hide one package.",
        "Speed buys a choice that strength cannot.",
        ("kinetic", "urgent"),
        "A market district roofed with corrugated tin and hanging cable.",
        "Kavi breaks into a sprint across the rooftops.",
        "A tin sheet buckles and the pursuer closes the gap.",
        "Kavi shoves the package under a market stall.",
        "The drop survives even though Kavi is caught.",
        [
            _scene(
                "rooftop-run", "start the chase", "tin rooftops above the market",
                "afternoon", ("kavi", "tuan"), ROOFTOP_RUN,
            ),
            _scene(
                "market-drop", "complete the drop", "covered market aisle",
                "afternoon", ("kavi", "tuan"), MARKET_DROP,
            ),
        ],
    ),
    "pages": [
        _page("three-horizontal", [
            _panel(
                "rooftop-run",
                "Kavi breaks into a sprint.",
                ("kavi",),
                "wide tracking shot low to the roofline",
                "runner on the left third with open motion space to the right",
                "Kavi leaps a vent duct at full speed",
                "locked concentration",
                "hard overhead sun with sharp cast shadows",
                (f"kavi:{KAVI[0]}", f"rooftop-run:{ROOFTOP_RUN}"),
                [_sfx("DASH", "bottom-right", 1)],
            ),
            _panel(
                "rooftop-run",
                "The roof gives way underfoot.",
                ("kavi", "tuan"),
                "medium action shot from behind the runner",
                "both figures centered with impact space above",
                "A tin sheet buckles as Tuan lands behind Kavi",
                "alarm against grim focus",
                "hard sun key with bounced glare from the tin",
                (f"kavi:{KAVI[1]}", f"tuan:{TUAN[0]}"),
                [_sfx("CRACK", "top-center", 1)],
            ),
            _panel(
                "market-drop",
                "Kavi releases the package.",
                ("kavi", "tuan"),
                "low-angle medium shot through the stalls",
                "drop bag centered with speaker space on the left",
                "Kavi shoves the bag under a stall as Tuan reaches",
                "defiance against fury",
                "striped awning shade broken by hot light gaps",
                (f"tuan:{TUAN[1]}", f"market-drop:{MARKET_DROP}"),
                [
                    _dialogue(
                        "tuan", "You cannot outrun a whistle.", "middle-left", 1, (0.66, 0.48),
                    ),
                    _sfx("THUD", "bottom-right", 2),
                ],
            ),
        ]),
    ],
}


# --- two-character ---------------------------------------------------------

MEI = ("faded denim overalls", "brass wrench")
ARI = ("pale green shirt", "canvas satchel")
RIVER_BENCH = "brown floodwater against a cracked river wall"
BUS_STOP = "one bare bulb under a rusted shelter roof"

TWO_CHARACTER = {
    "capability": (
        "Sustained two-character staging: alternating speakers, shared scene anchors, and "
        "stable pair continuity across panels."
    ),
    "stresses": ("characters:pair", "continuity:shared-anchor", "staging:two-shot"),
    "title": "Two Character Benchmark",
    "source": "Two friends decide on a riverbank whether to leave their flooded town.\n",
    "expected_pages": 1,
    "expected_panels": 2,
    "characters": _bible([
        _character(
            "mei", "Mei", "boat mechanic", "young-adult", "she/her",
            ("practical",),
            "keep the family workshop above water",
            "plain short answers",
            _fingerprint(
                "compact sturdy build",
                "broad face with sunburned cheeks",
                "long brown hair tied in a low knot",
                "faded denim overalls",
                ("denim blue", "rust", "straw"),
                ("brass wrench",),
                MEI,
            ),
        ),
        _character(
            "ari", "Ari", "village schoolteacher", "young-adult", "he/him",
            ("hopeful",),
            "convince Mei to move upriver",
            "long gentle sentences",
            _fingerprint(
                "tall thin build",
                "narrow face with round glasses",
                "black curls above the collar",
                "pale green shirt with rolled cuffs",
                ("pale green", "ochre", "grey"),
                ("canvas satchel",),
                ARI,
            ),
        ),
    ]),
    "story": _story(
        "Every Flood",
        "Two friends bargain over leaving a town the river keeps taking back.",
        "Leaving and returning can be the same promise.",
        ("quiet", "warm"),
        "A river town where the flood line is repainted every year.",
        "Ari asks Mei to leave for dry ground upriver.",
        "Mei refuses to abandon the family workshop.",
        "Mei agrees to go only if they return each flood.",
        "They buy two tickets and keep the workshop key.",
        [
            _scene(
                "river-bench", "raise the question", "concrete bench on the river wall",
                "late afternoon", ("mei", "ari"), RIVER_BENCH,
            ),
            _scene(
                "bus-stop", "settle the answer", "roadside bus shelter",
                "dusk", ("mei", "ari"), BUS_STOP,
            ),
        ],
    ),
    "pages": [
        _page("two-horizontal", [
            _panel(
                "river-bench",
                "Ari asks Mei to leave.",
                ("mei", "ari"),
                "medium two-shot from the side",
                "pair on the lower third with sky space reserved for text",
                "Ari turns toward Mei while she watches the water",
                "gentle appeal against guarded calm",
                "low warm sun from the left with cool water bounce",
                (f"mei:{MEI[0]}", f"ari:{ARI[1]}", f"river-bench:{RIVER_BENCH}"),
                [
                    _dialogue(
                        "ari", "Upriver there is work and dry ground.",
                        "top-left", 1, (0.62, 0.45),
                    ),
                    _dialogue(
                        "mei", "There is also nothing of ours.", "top-right", 2, (0.34, 0.47),
                    ),
                ],
            ),
            _panel(
                "bus-stop",
                "Mei answers with a condition.",
                ("mei", "ari"),
                "tight two-shot at eye level",
                "faces on opposite thirds with a clear upper band",
                "Mei hands Ari the wrench while the bus lights rise",
                "resolve against relief",
                "single bare-bulb key with blue dusk fill",
                (f"mei:{MEI[1]}", f"ari:{ARI[0]}", f"bus-stop:{BUS_STOP}"),
                [
                    _dialogue(
                        "mei", "We go, and we come back every flood.",
                        "top-left", 1, (0.33, 0.44),
                    ),
                    _dialogue("ari", "Then I will buy two tickets.", "top-right", 2, (0.66, 0.46)),
                ],
            ),
        ]),
    ],
}


# --- multi-character -------------------------------------------------------

DEWI = ("navy steward jacket", "ration ledger")
OTTO = ("oil-stained work coat", "hand pump handle")
PIA = ("yellow patterned wrap", "coin tin")
REX = ("green guard anorak", "battery lantern")
UNION_HALL = "two hanging bulbs over a long folding table"
BACK_ALLEY = "wet brick walls under one blue security light"

MULTI_CHARACTER = {
    "capability": (
        "Ensemble framing: four distinct characters in one full-page panel with unique "
        "fingerprints and unambiguous speakers."
    ),
    "stresses": ("characters:ensemble", "composition:crowded-frame", "continuity:multi-owner"),
    "title": "Multi Character Benchmark",
    "source": "Four neighbours meet in a union hall to divide one week of water rations.\n",
    "expected_pages": 1,
    "expected_panels": 1,
    "characters": _bible([
        _character(
            "dewi", "Dewi", "hall steward", "adult", "she/her",
            ("orderly",),
            "end the meeting with a signed list",
            "formal short rulings",
            _fingerprint(
                "medium upright build",
                "oval face with a mole on the chin",
                "grey hair in a tight bun",
                "navy steward jacket",
                ("navy", "white", "brass"),
                ("ration ledger",),
                DEWI,
            ),
        ),
        _character(
            "otto", "Otto", "well driller", "middle-aged", "he/him",
            ("gruff",),
            "secure water for the outer row of houses",
            "blunt fragments",
            _fingerprint(
                "heavy stooped build",
                "square face under grey stubble",
                "bald with a fringe at the ears",
                "oil-stained work coat",
                ("olive", "oil black", "copper"),
                ("hand pump handle",),
                OTTO,
            ),
        ),
        _character(
            "pia", "Pia", "market seller", "young-adult", "she/her",
            ("sharp",),
            "keep the market taps open",
            "fast bargaining lines",
            _fingerprint(
                "short quick build",
                "heart-shaped face with wide-set eyes",
                "braided black hair over one shoulder",
                "yellow patterned wrap",
                ("yellow", "magenta", "clay"),
                ("coin tin",),
                PIA,
            ),
        ),
        _character(
            "rex", "Rex", "night guard", "young-adult", "he/him",
            ("quiet",),
            "avoid another fight at the taps",
            "one-line replies",
            _fingerprint(
                "lanky loose build",
                "long face with a broken eyebrow",
                "dark hair under a knitted cap",
                "green guard anorak",
                ("forest green", "grey", "amber"),
                ("battery lantern",),
                REX,
            ),
        ),
    ]),
    "story": _story(
        "One Week of Water",
        "Four neighbours divide a single week of rations without losing the street.",
        "A fair list is slower than a fight and lasts longer.",
        ("tense", "communal"),
        "A dry district where water arrives by tanker twice a week.",
        "The hall fills and every claim is spoken at once.",
        "The outer row and the market both demand priority.",
        "Dewi forces the claims into a written order.",
        "The list is signed and the taps stay open.",
        [
            _scene(
                "union-hall", "divide the rations", "long union hall with folding chairs",
                "evening", ("dewi", "otto", "pia", "rex"), UNION_HALL,
            ),
            _scene(
                "back-alley", "settle the last claim", "alley behind the hall",
                "night", ("otto", "rex"), BACK_ALLEY,
            ),
        ],
    ),
    "pages": [
        _page("full-page", [
            _panel(
                "union-hall",
                "The four neighbours state their claims at once.",
                ("dewi", "otto", "pia", "rex"),
                "wide full-page group shot at table height",
                "four figures across the middle band with text space above and below",
                "Dewi holds the ledger open while the other three lean in",
                "controlled tension across four faces",
                "two warm hanging bulbs with dark corners",
                (
                    f"dewi:{DEWI[1]}",
                    f"otto:{OTTO[0]}",
                    f"pia:{PIA[0]}",
                    f"rex:{REX[1]}",
                    f"union-hall:{UNION_HALL}",
                ),
                [
                    _dialogue(
                        "dewi", "One claim each, in order, and I write it down.",
                        "top-center", 1, (0.48, 0.36),
                    ),
                    _dialogue(
                        "pia", "The market taps feed everyone here.",
                        "middle-right", 2, (0.72, 0.52),
                    ),
                    _caption("Nobody had signed anything for six weeks.", "bottom-center", 3),
                ],
            ),
        ]),
    ],
}


# --- silent manga ----------------------------------------------------------

SORA = ("oversized grey coat", "paper ticket")
EMPTY_STATION = "one flickering platform light over empty benches"
FIRST_LIGHT = "pale gold sky behind cold steel rails"
SILENT_NEGATIVE = ("sound effects", "onomatopoeia", "dialogue")

SILENT_MANGA = {
    "capability": (
        "Wordless storytelling: a four-panel grid with zero text items and no authored "
        "sound effects, carried entirely by staging."
    ),
    "stresses": ("text:none", "pacing:silent", "layout:four-grid"),
    "title": "Silent Manga Benchmark",
    "source": (
        "A commuter waits alone through the night at an empty station until the "
        "first train arrives.\n"
    ),
    "expected_pages": 1,
    "expected_panels": 4,
    "characters": _bible([
        _character(
            "sora", "Sora", "night commuter", "young-adult", "she/her",
            ("patient",),
            "wait for the first train home",
            "silent, expressed through posture rather than words",
            _fingerprint(
                "slight narrow build",
                "soft round face with tired eyes",
                "straight black hair to the jaw",
                "oversized grey coat",
                ("grey", "teal", "pale gold"),
                ("paper ticket",),
                SORA,
            ),
        ),
    ]),
    "story": _story(
        "First Train",
        "A commuter outlasts a night of failing platform lights to catch the first train.",
        "Waiting is its own kind of movement.",
        ("still", "melancholy"),
        "A rural station with one platform and no staff after midnight.",
        "Sora settles on the bench under a flickering light.",
        "The light fails and the platform goes dark.",
        "Dawn reaches the rails before the train does.",
        "Sora boards the first train with the ticket still folded.",
        [
            _scene(
                "empty-station", "hold the wait", "open platform of a rural station",
                "middle of the night", ("sora",), EMPTY_STATION,
            ),
            _scene(
                "first-light", "release the wait", "the same platform at dawn",
                "first light", ("sora",), FIRST_LIGHT,
            ),
        ],
    ),
    "pages": [
        _page("four-grid", [
            _panel(
                "empty-station",
                "Sora sits alone under the platform light.",
                ("sora",),
                "wide establishing shot from the far bench",
                "figure small on the right with empty platform space",
                "Sora tucks the ticket into her sleeve",
                "quiet fatigue",
                "single flickering overhead light with deep shadow",
                (f"sora:{SORA[0]}", f"empty-station:{EMPTY_STATION}"),
                [],
                SILENT_NEGATIVE,
            ),
            _panel(
                "empty-station",
                "The light stutters and the platform darkens.",
                ("sora",),
                "medium shot of the bench",
                "figure centered low with heavy negative space above",
                "Sora pulls the coat closed as the light dims",
                "still endurance",
                "failing overhead light with cold blue spill",
                (f"sora:{SORA[1]}", f"empty-station:{EMPTY_STATION}"),
                [],
                SILENT_NEGATIVE,
            ),
            _panel(
                "first-light",
                "The rails begin to brighten.",
                (),
                "close shot along the rails",
                "rails cutting the frame diagonally with no figure",
                "Dawn light spreads along the steel rails",
                "no visible face in frame",
                "pale dawn light from the far end of the platform",
                (f"first-light:{FIRST_LIGHT}",),
                [],
                SILENT_NEGATIVE,
            ),
            _panel(
                "first-light",
                "Sora stands as the train arrives.",
                ("sora",),
                "medium wide shot from the platform edge",
                "figure on the left third facing the incoming train",
                "Sora rises and lifts the ticket",
                "relieved calm",
                "warm dawn key with headlight glare",
                (f"sora:{SORA[0]}", f"first-light:{FIRST_LIGHT}"),
                [],
                SILENT_NEGATIVE,
            ),
        ]),
    ],
}


# --- night and low light ---------------------------------------------------

HANA = ("red hooded raincoat", "cloth bread bag")
LAMPLIGHTER = ("long oilskin cloak", "brass lamp pole")
BLACK_CANAL = "black water reflecting a single brass lamp"
LANTERN_SHED = "oil cans and wicks under a low hanging lantern"

NIGHT_LOW_LIGHT = {
    "capability": (
        "Night and low-light rendering: single-source dark key lighting, narrow night "
        "palettes, and text legibility at low contrast."
    ),
    "stresses": ("lighting:low-light", "palette:night", "contrast:legibility"),
    "title": "Night Low Light Benchmark",
    "source": "A canal lamplighter guides a lost child home through a city blackout.\n",
    "expected_pages": 1,
    "expected_panels": 3,
    "characters": _bible([
        _character(
            "hana", "Hana", "lost child", "child", "she/her",
            ("stubborn",),
            "find the way back to the bakery",
            "short worried questions",
            _fingerprint(
                "small light build",
                "small face with a scraped cheek",
                "short black hair with a crooked fringe",
                "red hooded raincoat",
                ("deep red", "black", "pale blue"),
                ("cloth bread bag",),
                HANA,
            ),
        ),
        _character(
            "lamplighter", "Pak Umar", "canal lamplighter", "elder", "he/him",
            ("calm",),
            "keep the canal path lit until dawn",
            "slow reassuring lines",
            _fingerprint(
                "stooped tall build",
                "hollow face behind a white beard",
                "thin white hair under a flat cap",
                "long oilskin cloak",
                ("black", "brass", "dim gold"),
                ("brass lamp pole",),
                LAMPLIGHTER,
            ),
        ),
    ]),
    "story": _story(
        "Two Bridges",
        "A lamplighter walks a lost child home along a blacked-out canal.",
        "One small light is enough to move by.",
        ("hushed", "tender"),
        "A canal district whose grid fails for hours at a time.",
        "A lamp finds Hana alone on the towpath.",
        "The lamp runs low and has to be refilled.",
        "The lamplighter points out the route past two bridges.",
        "Hana reaches the bakery before the grid returns.",
        [
            _scene(
                "black-canal", "meet in the dark", "canal towpath during a blackout",
                "night", ("hana", "lamplighter"), BLACK_CANAL,
            ),
            _scene(
                "lantern-shed", "light the way home", "lamplighter storage shed",
                "night", ("hana", "lamplighter"), LANTERN_SHED,
            ),
        ],
    ),
    "pages": [
        _page("hero-top-two-bottom", [
            _panel(
                "black-canal",
                "The lamp finds Hana on the towpath.",
                ("hana", "lamplighter"),
                "wide low-light shot along the towpath",
                "lamp glow centered with both figures inside the light pool and dark text space",
                "The lamplighter lowers the pole toward Hana",
                "startled against steady",
                "single brass-lamp key, no fill, deep black surround",
                (
                    f"hana:{HANA[0]}",
                    f"lamplighter:{LAMPLIGHTER[1]}",
                    f"black-canal:{BLACK_CANAL}",
                ),
                [
                    _dialogue(
                        "lamplighter", "Stay inside the light and walk with me.",
                        "top-center", 1, (0.55, 0.44),
                    ),
                    _caption("The city had been dark for three hours.", "bottom-left", 2),
                ],
            ),
            _panel(
                "lantern-shed",
                "Hana counts wicks while the lamp is refilled.",
                ("hana",),
                "medium close shot inside the shed",
                "figure low right with the dark upper area reserved for text",
                "Hana lifts a wick toward the lantern",
                "cautious curiosity",
                "low hanging lantern key with heavy falloff",
                (f"hana:{HANA[1]}", f"lantern-shed:{LANTERN_SHED}"),
                [_dialogue("hana", "Is the bakery still there?", "top-left", 1, (0.44, 0.58))],
            ),
            _panel(
                "black-canal",
                "The lamplighter points along the water.",
                ("lamplighter",),
                "medium shot from behind the lamp",
                "figure right with lamp flare left and a clear top band",
                "He raises the pole toward the far bridge",
                "quiet certainty",
                "backlit lamp flare with silhouette edges",
                (f"lamplighter:{LAMPLIGHTER[0]}", f"black-canal:{BLACK_CANAL}"),
                [
                    _dialogue(
                        "lamplighter", "Two bridges, then warm bread.",
                        "top-right", 1, (0.58, 0.52),
                    ),
                ],
            ),
        ]),
    ],
}


# --- long dialogue ---------------------------------------------------------

ILMA = ("charcoal wool cardigan", "cotton archive gloves")
TARI = ("oversized denim jacket", "folded letter")
LECTURE_HALL = "tall dusty windows above tiered wooden seats"
QUIET_CORRIDOR = "pale corridor light on a scuffed stone floor"
LONG_LINE = (
    "The archive keeps every letter we ever wrote to the city, and tonight I want you "
    "to read one aloud so the room remembers why we started this work at all."
)

LONG_DIALOGUE = {
    "capability": (
        "Long-form dialogue close to the 32-word ceiling, paired with a caption inside a "
        "full-page panel that has to reserve a tall text column."
    ),
    "stresses": ("text:long-dialogue", "text:word-budget", "layout:full-page"),
    "title": "Long Dialogue Benchmark",
    "source": "An archivist asks a student to read one old letter aloud to a full lecture hall.\n",
    "expected_pages": 1,
    "expected_panels": 1,
    "characters": _bible([
        _character(
            "professor-ilma", "Professor Ilma", "city archivist", "elder", "she/her",
            ("deliberate",),
            "make the city hear its own letters",
            "long unhurried sentences",
            _fingerprint(
                "small straight-backed build",
                "lined face with steady dark eyes",
                "white hair pinned close to the head",
                "charcoal wool cardigan",
                ("charcoal", "paper cream", "ink"),
                ("cotton archive gloves",),
                ILMA,
            ),
        ),
        _character(
            "tari", "Tari", "student reader", "young-adult", "they/them",
            ("nervous",),
            "get through the reading without stopping",
            "quiet hesitant lines",
            _fingerprint(
                "medium slouched build",
                "round face with a nose stud",
                "dark hair shaved close on one side",
                "oversized denim jacket",
                ("indigo", "white", "brick"),
                ("folded letter",),
                TARI,
            ),
        ),
    ]),
    "story": _story(
        "Read It Aloud",
        "An archivist finally asks a student to read a hundred-year-old letter to a full hall.",
        "A record is only kept if someone says it out loud.",
        ("reflective", "warm"),
        "A university archive that has outlived three city governments.",
        "Ilma opens the sealed folder in front of the hall.",
        "Tari is asked to read instead of the archivist.",
        "Tari reads the letter through to the end.",
        "The hall applauds the letter rather than the reader.",
        [
            _scene(
                "lecture-hall", "make the request", "tiered lecture hall",
                "late morning", ("professor-ilma", "tari"), LECTURE_HALL,
            ),
            _scene(
                "quiet-corridor", "answer the request", "corridor outside the hall",
                "late morning", ("professor-ilma", "tari"), QUIET_CORRIDOR,
            ),
        ],
    ),
    "pages": [
        _page("full-page", [
            _panel(
                "lecture-hall",
                "Ilma asks Tari to read the letter aloud.",
                ("professor-ilma", "tari"),
                "full-page wide shot from the back of the hall",
                "two figures at the lectern on the lower third with a tall protected text column",
                "Ilma sets the letter into Tari's hands",
                "patient insistence against uncertainty",
                "cool window daylight from the left with soft interior fill",
                (
                    f"professor-ilma:{ILMA[1]}",
                    f"tari:{TARI[0]}",
                    f"lecture-hall:{LECTURE_HALL}",
                ),
                [
                    _dialogue("professor-ilma", LONG_LINE, "top-center", 1, (0.44, 0.72)),
                    _caption("Ilma had rehearsed the request for eleven years.", "bottom-left", 2),
                ],
            ),
        ]),
    ],
}


# --- complex background ----------------------------------------------------

ZAKI = ("blue delivery shirt", "hand trolley")
OMA_LIN = ("purple batik blouse", "woven basket")
NIGHT_MARKET = "hanging bulbs and stacked signage over narrow stalls"
RAIL_YARD = "floodlit gravel between parked freight cars"

COMPLEX_BACKGROUND = {
    "capability": (
        "Dense environmental detail: crowded, signage-heavy backgrounds that still reserve "
        "text-safe space in every panel."
    ),
    "stresses": (
        "background:dense-detail",
        "composition:text-safe-space",
        "layout:two-top-hero-bottom",
    ),
    "title": "Complex Background Benchmark",
    "source": (
        "A grandmother and her grandson search a crowded night market for a missing "
        "delivery crate.\n"
    ),
    "expected_pages": 1,
    "expected_panels": 3,
    "characters": _bible([
        _character(
            "zaki", "Zaki", "delivery helper", "teen", "he/him",
            ("eager",),
            "find the missing crate before the market closes",
            "rushed questions",
            _fingerprint(
                "thin tall build",
                "narrow face with a chipped front tooth",
                "black hair flattened by a cap",
                "blue delivery shirt with a torn pocket",
                ("blue", "white", "tin grey"),
                ("hand trolley",),
                ZAKI,
            ),
        ),
        _character(
            "oma-lin", "Oma Lin", "market grandmother", "elder", "she/her",
            ("shrewd",),
            "protect the family delivery contract",
            "dry short remarks",
            _fingerprint(
                "short round build",
                "wide face with laughing eyes",
                "silver hair under a floral scarf",
                "purple batik blouse",
                ("purple", "gold", "chili red"),
                ("woven basket",),
                OMA_LIN,
            ),
        ),
    ]),
    "story": _story(
        "Aisle Nine",
        "A grandmother and her grandson trace a lost crate from the market to the rail yard.",
        "Detail is what separates a search from a panic.",
        ("bustling", "affectionate"),
        "A covered night market that backs onto a working rail yard.",
        "Zaki pushes into the crowded aisle to ask after the crate.",
        "The chalked stall numbers turn out to be wrong.",
        "The crate is found under a freight car in the yard.",
        "The delivery contract survives another night.",
        [
            _scene(
                "night-market", "search the stalls", "covered night market with hanging signage",
                "evening", ("zaki", "oma-lin"), NIGHT_MARKET,
            ),
            _scene(
                "rail-yard", "find the crate", "loading rail yard behind the market",
                "night", ("zaki", "oma-lin"), RAIL_YARD,
            ),
        ],
    ),
    "pages": [
        _page("two-top-hero-bottom", [
            _panel(
                "night-market",
                "Zaki pushes into the crowded aisle.",
                ("zaki",),
                "medium shot compressed by the stalls",
                "figure center-low with a cleared signage-free text band at the top",
                "Zaki lifts the trolley over an open drain",
                "hurried focus",
                "mixed hanging bulbs with strong colour spill",
                (f"zaki:{ZAKI[1]}", f"night-market:{NIGHT_MARKET}"),
                [_dialogue("zaki", "Which aisle takes the crates?", "top-left", 1, (0.46, 0.56))],
            ),
            _panel(
                "night-market",
                "Oma Lin reads the stall numbers.",
                ("oma-lin",),
                "medium shot past hanging goods",
                "figure right with foreground clutter kept below the text band",
                "Oma Lin tilts the basket to read a chalked number",
                "narrow concentration",
                "warm bulb key with deep interior shadow",
                (f"oma-lin:{OMA_LIN[1]}", f"night-market:{NIGHT_MARKET}"),
                [_dialogue("oma-lin", "Numbers lie after eight.", "top-right", 1, (0.60, 0.54))],
            ),
            _panel(
                "rail-yard",
                "The crate is found under a freight car.",
                ("zaki", "oma-lin"),
                "wide hero shot across the yard",
                "both figures on the lower left with a clean floodlit sky band for text",
                "Zaki drags the crate clear while Oma Lin holds the light",
                "triumph against dry satisfaction",
                "hard floodlight key with long gravel shadows",
                (f"zaki:{ZAKI[0]}", f"oma-lin:{OMA_LIN[0]}", f"rail-yard:{RAIL_YARD}"),
                [
                    _dialogue(
                        "oma-lin", "Your uncle will hear about this.",
                        "top-center", 1, (0.34, 0.62),
                    ),
                    _caption("The contract survived another night.", "bottom-right", 2),
                ],
            ),
        ]),
    ],
}


# --- four page story ------------------------------------------------------

LIRA = ("faded scarf", "sand-coloured work tunic")
BAKAR = ("grey sleeveless shirt", "water can")
DRY_WELL = "split clay ground around an empty stone well"
LONG_ROAD = "white dust road between bleached fields"
STORM_RIDGE = "brown dust wall closing over a bare ridge"
GREEN_VALLEY = "green channel water under low evening light"

FOUR_PAGE_STORY = {
    "capability": (
        "A complete four-page arc: contiguous page numbering, four different layouts, and a "
        "beginning-to-resolution structure across seven panels."
    ),
    "stresses": ("story:four-page-arc", "layout:mixed", "pages:contiguous-numbering"),
    "title": "Four Page Story Benchmark",
    "source": (
        "Two siblings carry the last well pump across a drought road to a valley "
        "that still has water.\n"
    ),
    "expected_pages": 4,
    "expected_panels": 7,
    "characters": _bible([
        _character(
            "lira", "Lira", "older sister", "young-adult", "she/her",
            ("determined",),
            "reach the valley before the pump seizes",
            "short encouraging lines",
            _fingerprint(
                "wiry strong build",
                "thin face with a sunburned nose",
                "black hair wrapped in a faded scarf",
                "sand-coloured work tunic",
                ("sand", "rust", "dry green"),
                ("well pump",),
                LIRA,
            ),
        ),
        _character(
            "bakar", "Bakar", "younger brother", "teen", "he/him",
            ("tired", "loyal"),
            "keep up with Lira without complaining",
            "short blunt complaints",
            _fingerprint(
                "short square build",
                "round face with a dust-streaked chin",
                "cropped brown hair",
                "grey sleeveless shirt",
                ("grey", "clay", "pale blue"),
                ("water can",),
                BAKAR,
            ),
        ),
    ]),
    "story": _story(
        "Carry the Pump",
        "Two siblings carry a village well pump across a drought road to running water.",
        "What a village cannot keep, it can still carry.",
        ("weathered", "hopeful"),
        "A drought district where one valley channel still runs.",
        "Lira lifts the pump out of the dry village well.",
        "A dust storm catches the siblings on an exposed ridge.",
        "The pump reaches the valley channel and turns again.",
        "Water reaches the village nine days later.",
        [
            _scene(
                "dry-well", "leave the dry village", "cracked village well",
                "morning", ("lira", "bakar"), DRY_WELL,
            ),
            _scene(
                "long-road", "carry the pump", "straight road across dry fields",
                "midday", ("lira", "bakar"), LONG_ROAD,
            ),
            _scene(
                "storm-ridge", "survive the dust storm", "exposed ridge above the road",
                "late afternoon", ("lira", "bakar"), STORM_RIDGE,
            ),
            _scene(
                "green-valley", "reach water", "valley with a running channel",
                "evening", ("lira", "bakar"), GREEN_VALLEY,
            ),
        ],
    ),
    "pages": [
        _page("full-page", [
            _panel(
                "dry-well",
                "Lira lifts the pump from the dry well.",
                ("lira", "bakar"),
                "full-page wide shot at ground level",
                "siblings centered low with a tall open sky band for text",
                "Lira hauls the pump free while Bakar steadies the frame",
                "resolve against reluctance",
                "flat early sun with pale shadows",
                (f"lira:{LIRA[0]}", f"bakar:{BAKAR[1]}", f"dry-well:{DRY_WELL}"),
                [
                    _caption("The well had been dry since the second harvest.", "top-center", 1),
                    _dialogue(
                        "lira", "We carry it to the valley today.",
                        "middle-left", 2, (0.42, 0.66),
                    ),
                ],
            ),
        ]),
        _page("two-horizontal", [
            _panel(
                "long-road",
                "The road offers no shade.",
                ("lira", "bakar"),
                "wide two-shot along the road",
                "figures small on the right with road space left for text",
                "Bakar shifts the pump onto his shoulder",
                "grim endurance",
                "harsh midday sun overhead with short shadows",
                (f"lira:{LIRA[1]}", f"long-road:{LONG_ROAD}"),
                [
                    _dialogue("bakar", "How far is the ridge?", "top-left", 1, (0.62, 0.48)),
                    _dialogue("lira", "Two hours. Drink now.", "top-right", 2, (0.70, 0.46)),
                ],
            ),
            _panel(
                "long-road",
                "They ration the last water.",
                ("lira", "bakar"),
                "medium two-shot at the roadside",
                "pair centered with a clear upper text band",
                "Lira tips the can toward Bakar",
                "care against exhaustion",
                "high sun key with hot ground bounce",
                (f"bakar:{BAKAR[1]}", f"long-road:{LONG_ROAD}"),
                [_dialogue("lira", "Half now, half at the ridge.", "top-center", 1, (0.38, 0.50))],
            ),
        ]),
        _page("three-horizontal", [
            _panel(
                "storm-ridge",
                "Dust closes over the ridge.",
                ("lira", "bakar"),
                "wide shot of the incoming dust wall",
                "figures left with the dust mass filling the right",
                "Lira pulls the scarf across Bakar's face",
                "urgency against fear",
                "brown diffused light with no clear key",
                (f"lira:{LIRA[0]}", f"storm-ridge:{STORM_RIDGE}"),
                [_sfx("HOWL", "top-right", 1)],
            ),
            _panel(
                "storm-ridge",
                "Bakar refuses to let go of the pump.",
                ("bakar",),
                "medium close shot inside the dust",
                "figure centered with flat dust space above for text",
                "Bakar locks both arms around the pump frame",
                "stubborn strain",
                "flat brown ambient light with no shadows",
                (f"bakar:{BAKAR[0]}", f"storm-ridge:{STORM_RIDGE}"),
                [_dialogue("bakar", "I still have it.", "top-left", 1, (0.48, 0.58))],
            ),
            _panel(
                "storm-ridge",
                "The storm passes and the ridge clears.",
                ("lira", "bakar"),
                "wide shot from the ridge crest",
                "pair on the right with cleared sky open for text",
                "Lira stands and shakes dust from the scarf",
                "spent relief",
                "returning low sun with long dusty shadows",
                (f"lira:{LIRA[1]}", f"bakar:{BAKAR[1]}"),
                [_caption("The dust took an hour and both canteens.", "top-left", 1)],
            ),
        ]),
        _page("full-page", [
            _panel(
                "green-valley",
                "The pump reaches running water.",
                ("lira", "bakar"),
                "full-page wide shot down into the valley",
                "siblings on the lower left with the channel leading right and open sky text space",
                "Bakar sets the pump into the channel bank while Lira drinks",
                "quiet joy across both faces",
                "low golden evening key with cool water reflections",
                (f"lira:{LIRA[0]}", f"bakar:{BAKAR[0]}", f"green-valley:{GREEN_VALLEY}"),
                [
                    _dialogue(
                        "lira", "It still turns. We can start here.",
                        "top-center", 1, (0.34, 0.70),
                    ),
                    _caption("Water reached the village nine days later.", "bottom-right", 2),
                ],
            ),
        ]),
    ],
}


BENCHMARK_SCENARIOS = {
    "dialogue-heavy": DIALOGUE_HEAVY,
    "action-sequence": ACTION_SEQUENCE,
    "two-character": TWO_CHARACTER,
    "multi-character": MULTI_CHARACTER,
    "silent-manga": SILENT_MANGA,
    "night-low-light": NIGHT_LOW_LIGHT,
    "long-dialogue": LONG_DIALOGUE,
    "complex-background": COMPLEX_BACKGROUND,
    "four-page-story": FOUR_PAGE_STORY,
}


def benchmark_metadata(scenario):
    """Return the benchmark metadata record for one scenario."""
    specification = BENCHMARK_SCENARIOS[scenario]
    storyboard = build_storyboard(specification["pages"])
    pages = storyboard["pages"]
    panels = [panel for page in pages for panel in page["panels"]]
    return {
        "capability": specification["capability"],
        "evidence_mode": "structural",
        "expected": {
            "character_count": len(specification["characters"]["characters"]),
            "layouts": [page["layout"] for page in pages],
            "page_count": specification["expected_pages"],
            "panel_count": specification["expected_panels"],
            "panels": [panel["id"] for panel in panels],
            "text_item_count": sum(len(panel["text"]) for panel in panels),
        },
        "local_only": True,
        "scenario": scenario,
        "schema_version": SCHEMA_VERSION,
        "stresses": list(specification["stresses"]),
        "title": specification["title"],
    }


def build_benchmark_project(root, scenario):
    """Materialize one plan-complete benchmark project and return its directory.

    The project advances to ``STORYBOARDED`` so it can be validated at the
    ``storyboard`` stage without any provider call or committed raster.
    """
    specification = BENCHMARK_SCENARIOS.get(scenario)
    if specification is None:
        raise ValueError(f"unknown benchmark scenario: {scenario}")
    root = Path(root)
    storyboard = build_storyboard(specification["pages"])
    panel_ids = [
        panel["id"] for page in storyboard["pages"] for panel in page["panels"]
    ]
    project = init_project(
        root,
        specification["title"],
        specification["source"].encode("utf-8"),
        {
            "language": LANGUAGE,
            "mode": INPUT_MODE,
            "title": specification["title"],
        },
    )
    atomic_write_json(project / "plan/story-plan.json", specification["story"])
    atomic_write_json(project / "plan/character-bible.json", specification["characters"])
    atomic_write_json(project / "plan/storyboard.json", storyboard)

    manifest = read_json(project / "project.json")
    descriptors = {
        "character_bible": "plan/character-bible.json",
        "story_plan": "plan/story-plan.json",
        "storyboard": "plan/storyboard.json",
    }
    manifest.update({
        "project_id": scenario,
        "title": specification["title"],
        "panels": panel_ids,
        "artifacts": {
            name: {"path": relative, "sha256": sha256_file(project / relative)}
            for name, relative in descriptors.items()
        },
        "settings": {
            **manifest["settings"],
            "page_count": len(storyboard["pages"]),
            "panel_count": len(panel_ids),
        },
        "input": {
            **manifest["input"],
            "source_sha256": sha256_file(project / "source/input.txt"),
        },
    })
    atomic_write_json(project / "project.json", manifest)
    transition(project, "PLANNED")
    transition(project, "SCRIPTED")
    transition(project, "STORYBOARDED")

    # The metadata sidecar stays outside the project boundary so a benchmark
    # project contains only artifacts the engine itself recognizes.
    atomic_write_json(root / f"{scenario}.benchmark.json", benchmark_metadata(scenario))
    return project


def build_benchmark_corpus(root):
    """Materialize every benchmark project under one root and return them by scenario."""
    return {
        scenario: build_benchmark_project(root, scenario)
        for scenario in BENCHMARK_SCENARIOS
    }


def main(argv=None):
    """Materialize the benchmark corpus for local evaluation."""
    parser = argparse.ArgumentParser(prog="tests.benchmark_corpus")
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--scenario", choices=sorted(BENCHMARK_SCENARIOS), default=None)
    arguments = parser.parse_args(argv)
    arguments.output_root.mkdir(parents=True, exist_ok=True)
    scenarios = [arguments.scenario] if arguments.scenario else list(BENCHMARK_SCENARIOS)
    for scenario in scenarios:
        project = build_benchmark_project(arguments.output_root, scenario)
        print(f"{scenario}\t{project}")
    return 0


if __name__ == "__main__":  # pragma: no cover - manual materialization
    raise SystemExit(main())
