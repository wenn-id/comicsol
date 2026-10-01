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

Provider-neutral, local-first comic production pipeline

## Long description

Comic Sol is a provider-neutral, local-first comic production pipeline. The
deterministic engine is the product; Agent Skills, CLI, and MCP are adapters. The core
CLI does not create artwork by itself: it validates, persists, resumes, repairs, letters,
composes, and exports around an agent-supplied compatible image generator. Comic Sol
stores no provider credentials.

The Agent Skill creates editable story plans, character and scene references, panel
prompts, generated panel PNGs, deterministic lettering, composed pages, a PDF, manifest,
hashes, and transparent QA evidence. It selects image execution from declared capability:
a compatible native tool, compatible external adapter/API tool, portable handoff, or an
actionable `BLOCKED` state that preserves editable intermediates.

Comic Sol has no hosted backend and requires no Comic Sol account or demo
credentials. A Codex session and the selected image provider may require their own
account or access. Generated project files stay in the local output directory. The
skill preserves intermediate artifacts so interrupted work can resume instead of
restarting from scratch.

## Starter prompts

1. Make a 2-page manga about a courier delivering sunlight to an underground city.
2. Turn this story into a finished comic with editable intermediate files and a PDF.
3. Resume my interrupted Comic Sol project and finish only valid stale stages.
4. Create a teen-rated original comic from this local Markdown story file.

## Release notes

Initial skills-only plugin packaging. Bundles the Comic Sol workflow, deterministic
scripts, schemas, templates, fonts, font licenses, visual assets, and provider-
neutral local-first documentation. No hosted MCP server is included.
