# Official Plugin Directory Listing

## Identity

- **Submission type:** Skills only
- **Plugin name:** Comic Sol
- **Plugin ID:** `comic-sol`
- **Version:** `2.0.0`
- **Category:** Developer Tools
- **Developer:** Alwan Juliawan (`wenn-id`)
- **Repository:** https://github.com/wenn-id/comicsol
- **Canonical engine:** https://github.com/wenn-id/comicsol
- **Logo upload:** `assets/comic-sol-logo.png`
- **Screenshot upload:** `assets/comic-sol-thumbnail.png`

## Public URLs

- **Website:** https://github.com/wenn-id/comicsol
- **Support:** https://github.com/wenn-id/comicsol/issues
- **Privacy:** https://github.com/wenn-id/comicsol/blob/main/PRIVACY.md
- **Terms:** https://github.com/wenn-id/comicsol/blob/main/TERMS.md

## Short description

Turn prompts into editable, QA-checked manga comics.

## Long description

Comic Sol is a local-first Codex workflow for turning a short prompt, story, or
source file into an original manga/anime comic. It creates editable story
plans, character and scene references, panel prompts, generated panel PNGs,
deterministic lettering, composed pages, a PDF, manifest, hashes, and
transparent QA evidence. It uses the image-generation capability exposed by
the current Codex session and keeps provider credentials outside the project.

Comic Sol has no hosted backend or required account. Generated project files
stay in the local output directory. The skill preserves intermediate artifacts
so interrupted work can resume instead of restarting from scratch.

## Starter prompts

1. Make a 2-page manga about a courier delivering sunlight to an underground city.
2. Turn this story into a finished comic with editable intermediate files and a PDF.
3. Resume my interrupted Comic Sol project and finish only valid stale stages.
4. Create a teen-rated original comic from this local Markdown story file.

## Release notes

Initial skills-only plugin packaging. Bundles the Comic Sol workflow, deterministic
scripts, schemas, templates, fonts, font licenses, visual assets, and provider-
neutral local-first documentation. No hosted MCP server is included.
