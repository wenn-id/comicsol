# Comic Sol Privacy Policy

**Last updated:** 2026-08-23

Comic Sol is a local-first product published by Alwan Juliawan (`wenn-id`) at
[github.com/wenn-id/comicsol](https://github.com/wenn-id/comicsol). It is
distributed as several surfaces — a Codex Skill and Codex Plugin bundle, a
source checkout, an installed Python CLI, native portable archives, an optional
`stdio` MCP server, and an OCI image — and this policy covers all of them.
Unless a sentence names a surface, it applies to every one.

## Data handled locally

- Comic Sol has no hosted backend, account system, analytics, advertising, or
  database on any surface.
- Project files, prompts, generated images, PDFs, logs, and QA artifacts are
  written to the local project/output directory selected by the user: the
  platform-default output root for Skill, plugin, source, CLI, and
  native-archive surfaces; the explicit `--root` for the MCP server; and the
  `/data` volume for the OCI image. Where each surface writes is documented in
  [`docs/surfaces.md`](docs/surfaces.md).
- The software does not intentionally collect names, email addresses, payment
  data, credentials, API keys, or device identifiers, and it adds no telemetry.
- Logs keep sanitized paths, hashes, categories, and state changes rather than
  raw credentials or story content.

## Data that leaves the machine

- The agent host (for example Codex or another MCP client) may process prompts
  and local files according to your account and product settings with that
  host.
- When the active agent uses an image-generation capability, the story and
  visual facts needed for that request are sent to that capability's provider.
  Provider retention and processing are governed by that provider's terms and
  privacy policy. Comic Sol does not promise provider-side deletion.
- The OCI image runs locally with no network access by default. If you deploy
  or push it to your own shared infrastructure, the administrators and policies
  of that infrastructure additionally apply.

## What Comic Sol does not do

Comic Sol does not upload project files to a Comic Sol server, sell personal
information, or ask users to put secrets in prompts, project manifests, or
logs. Users remain responsible for reviewing the active agent host, image
provider, MCP client and server configuration, and operating-system permissions.

## Manual opt-in dogfood reports

The optional [creator dogfood program](docs/dogfood.md) adds no telemetry, upload
service, network submission, database, or automatic collector. A participant generates,
previews, validates, and inspects a sanitized dogfood report locally, then may choose
manual opt-in submission through a public GitHub issue. Explicit report-sharing consent
covers only that sanitized report and is separate from comic/showcase publication
consent.

A dogfood submission must not contain the Comic Sol project; story/source text; prompts
or negative prompts; images, page PNGs, PDFs, or reference art; credentials, API keys,
cookies, tokens, or account identifiers; provider request/response bodies; endpoints
containing secrets; filesystem paths or home directories; or raw logs, stack traces,
exceptions, or unrelated diagnostics. If a report appears to contain sensitive material,
do not submit it publicly; use the private route in [`SUPPORT.md`](SUPPORT.md) and
[`SECURITY.md`](SECURITY.md).

## Retention and deletion

Local artifacts remain until you delete them. No surface has separate remote
storage to delete. Remove generated project directories, the MCP `--root`, or
the container's `/data` volume, and any agent-host or provider-side artifacts
through those products' own controls.

## Contact

For support or privacy questions, open an issue at
[github.com/wenn-id/comicsol/issues](https://github.com/wenn-id/comicsol/issues).
If the report involves material you cannot share publicly (private source text,
private images, or suspected exposure), use the private route described in
[`SUPPORT.md`](SUPPORT.md) instead of a public issue. Do not include secrets,
private source text, or personal data in a public issue.

## Changes

This policy may change when the surfaces or architecture change. The date above
identifies the current version of this document.
