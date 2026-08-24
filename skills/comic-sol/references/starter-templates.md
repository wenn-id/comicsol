# Starter templates

Comic Sol ships three provider-neutral starter project bundles. Select one when a proven page rhythm is more useful than a blank `INIT` project. Each fixed ID resolves to an immutable product path under `templates/starters/v1/`; `v1` is the starter-bundle version, not the `project.json` schema version. Starter projects continue to use project schema `1.0` and the standard source, request, story-plan, character-bible, and storyboard schemas.

## Available v1 starters

| ID | Intended use | Format characteristics |
| --- | --- | --- |
| `minimal-one-page` | A first project, visual poem, poster-like beat, or compact proof of concept. | One page, one full-page panel, one character, and one caption. |
| `dialogue-two-page` | A conversation, reunion, negotiation, or other character-led scene where attribution and pacing matter. | Two pages, four panels, two characters, multiple spoken turns, and explicit speaker anchors. |
| `action-focused` | A chase, escape, sports beat, or other movement-led sequence. | Two pages, six panels, dynamic fixed layouts, sparse prose, and authored visual SFX. |

Starter content is original example material intended to be edited. It is not a style preset, named-artist imitation, provider configuration, or promise of finished art.

## Initialize from a starter

Installed CLI:

```bash
comic-sol --json init --output-root /absolute/projects \
  --title "First Leaf Draft" --starter minimal-one-page
```

Source CLI:

```bash
PYTHON scripts/comic_sol.py init --output-root OUTPUT_ROOT \
  --title "Bridge Run Draft" --starter action-focused
```

The installed guided initializer also offers `blank` and the three fixed IDs:

```bash
comic-sol init --interactive
```

The existing `comic_init` MCP tool accepts the same optional `starter` field. Selecting a starter supplies its source, request, and page count as one coherent bundle. Therefore `--starter` (or MCP `starter`) must not be combined with explicit source, request, or page-count values; Comic Sol rejects the conflict rather than silently choosing one.

Omitting `--starter` preserves blank initialization: callers provide source/request data and the page count still defaults to 2.

## Resulting project and QA boundary

A selected starter is validated with the normal validators, materialized through the same atomic initialization transaction, and published as a normal `STORYBOARDED` project. Its manifest contains standard `story_plan`, `character_bible`, and `storyboard` artifact descriptors; its planning and storyboard resume records are standard stage-cache entries. There is no starter-specific field in `project.json` and no project-schema migration.

A starter stops at the storyboard boundary. It contains no character reference raster, panel raster, prompt, panel QA record, page QA record, QA report, PDF, or visual-quality evidence. `validate --stage storyboard` succeeds, while `validate --stage panels` fails until real art and current QA provenance are produced. This is deliberate: template validation proves structural and narrative consistency only. It does not bypass image capability checks, visual review, retry limits, page QA, export guards, or any later lifecycle transition.

All starter files and engine behavior are provider-neutral. They name no provider, SDK, model, endpoint, account, or credential. Image generation remains agent-managed; the deterministic engine only accepts and validates local artifacts.
