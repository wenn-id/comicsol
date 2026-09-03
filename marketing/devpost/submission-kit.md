# Devpost Submission Kit — Comic Sol Studio (WebMCP Challenge)

Copy-paste ready. Deadline: **2026-09-03 13:00 PT / 16:00 EDT**.

## Title

**Comic Sol Studio — create and revise manga by talking to a browser agent**

## Tagline

Turn a natural-language comic idea into a reviewable story, character bible,
storyboard, and visual identity — then revise it conversationally through
WebMCP instead of operating the editor step by step.

## Live URL

```
https://comic-sol-studio.vercel.app
```

## Public repository

```
https://github.com/wenn-id/comicsol
```

MIT licensed.

## What to show first

The page exposes **17 WebMCP tools** in two layers:

- **3 creator-first tools** registered by `app.js`:
  `create_comic`, `get_comic_context`, `revise_comic`.
- **14 production primitives** registered by `webmcp.js` for project state,
  generation, provider decisions, QA, and export.

The three creator tools intentionally hide revision IDs, idempotency keys,
provider selection, and generation job IDs from the creative request.

### Live hosted behavior

The Vercel deployment is a static Studio, so FastAPI, SQLite, and the durable
generation queue are **not deployed**. The three creator-first tools therefore
have an **ephemeral browser-local mode**: the agent can create a Plan, read it,
and revise it while the tab remains open. The Plan is held only in memory and
is not written to `localStorage` or `sessionStorage`; refreshing the page clears
it.

The 14 production primitives remain the backend-oriented surface. Generation,
QA, provider switching, and export are qualified by the repository's offline
backend tests and must **not** be presented as live Vercel execution.

## Text description (paste into Devpost)

**Comic Sol Studio turns WebMCP from API remote control into a creative
interface.** A comic workflow is naturally multi-step: turn an idea into a
story, define characters, break it into pages and panels, establish a visual
identity, generate assets, check consistency, and export. A normal browser
agent would have to inspect the DOM and reproduce those UI steps. Comic Sol
instead exposes typed WebMCP tools that describe what the product can do.

For the creator, the important layer is deliberately small. `create_comic`
accepts a title, concept, language, page count, visual style, and a structured
four-part Plan drafted by the browser agent. Comic Sol immediately turns that
into the active project shown in the Studio. `get_comic_context` explicitly
returns the active story plan, character bible, storyboard, and visual identity
so the agent can reason about the work it is editing. `revise_comic` applies a
new complete Plan after a request such as “make the protagonist more arrogant
and make the ending bittersweet.” The user talks about the comic; Comic Sol
handles project mechanics.

That creator surface sits on top of the existing 14 production-oriented WebMCP
primitives for project state, generation routing, job state, provider-switch
approval, QA, asset submission, and export. Those lower-level tools preserve
strict closed schemas, revision binding, and idempotency boundaries for a full
Studio backend. The new layer is additive rather than a replacement.

The hosted Vercel demo is intentionally honest about its boundary. It is a
static deployment, so it does not pretend to run the FastAPI/SQLite generation
backend. Instead, `create_comic`, `get_comic_context`, and `revise_comic` can
operate in an ephemeral in-memory browser mode. This makes the core WebMCP user
experience directly demonstrable on the hosted site without storing private
story content in browser persistence. Refreshing the page clears that local
project. Backend generation, QA, provider switching, and export remain
qualified through deterministic repository tests rather than claimed as live
hosted behavior.

The result is the difference between **“an agent can operate Comic Sol”** and
**“a creator can make and revise a comic with Comic Sol by talking to an
agent.”** WebMCP removes the coordination layer between creative intent and the
application while keeping the product's structured workflow and safety
boundaries explicit.

## Demo video script (target: 2:30–2:50)

| Time | Visual | Narration |
|---|---|---|
| 0:00–0:20 | Open the hosted Studio with WebMCP enabled. | “Making a comic normally means moving between story planning, character notes, storyboards, generation, and revisions. I wanted the creator to talk about the comic instead of operating all of those steps.” |
| 0:20–1:00 | Ask the browser agent: **“Create a 4-page dark fantasy manga about a ronin protecting a cursed child. Cinematic black-and-white ink.”** Show the `create_comic` call and the resulting Plan in the Studio. | “The agent converts my intent into Comic Sol's structured Plan and calls one creator-facing tool. I never provide a revision ID, provider, or job ID.” |
| 1:00–1:40 | Ask: **“Make the ronin more arrogant and change the ending from tragic to bittersweet.”** Show `get_comic_context`, then `revise_comic`, then the updated Plan. | “For a revision the agent first reads the actual comic context, reasons over the character bible and storyboard, and updates the project. This is where WebMCP removes real UI coordination.” |
| 1:40–2:10 | Show `app.js` creator tool definitions, then `webmcp.js` core definitions. | “There are three high-level creator tools on top of fourteen production primitives. The simple creative interface does not remove the strict low-level contracts needed by the full backend.” |
| 2:10–2:35 | Show the hosted URL and briefly mention ephemeral mode / tests. | “This hosted build is static, so create and revise use an ephemeral in-memory mode and never persist the private Plan in browser storage. Generation, QA, and export are backend-qualified in the repository; I don't claim those as live Vercel execution.” |
| 2:35–2:50 | Return to the revised Plan. | “The point is simple: the agent is no longer just controlling Comic Sol's API. The creator is creating with Comic Sol.” |

## Demo prompt

Use this exact first prompt so the result is easy to understand on video:

> Create a 4-page dark fantasy manga titled **The Cursed Heir**. A proud ronin
> protects a child who is secretly heir to the clan that destroyed his family.
> Use cinematic black-and-white ink, strong silhouettes, sparse dialogue, and a
> tragic-looking final page.

Then revise with:

> Make the ronin more arrogant and guarded. Change the final page to bittersweet:
> the child leaves with him at dawn, but the ronin still does not know the
> child's true identity.

## Verification / honesty checklist

- [ ] Hosted page registers the 3 creator tools plus the existing 14 core tools.
- [ ] Record `create_comic` on the hosted page.
- [ ] Record `get_comic_context` → `revise_comic` on the same tab.
- [ ] Confirm refresh clears the ephemeral browser-local project.
- [ ] Do not claim live image generation, live QA, live export, paid-provider
      execution, or deployed FastAPI/SQLite unless separately verified before
      recording.
- [ ] Show the public MIT repository.
- [ ] Upload a public demo video with audio.
- [ ] Submit before **2026-09-03 13:00 PT / 16:00 EDT**.
