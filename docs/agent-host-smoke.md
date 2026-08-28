# Agent-host smoke evidence

Host support is an evidence claim, separate from Skill placement and installer
mechanics. A host can be marked verified only after a tester runs Comic Sol in that
host and retains one complete, inspectable smoke record. No named host is currently
verified by a retained live smoke record in this repository.

## Smoke-record format

Retain one Markdown record per real host execution with every field below. Use exact
observations and links; use `not available` with an explanation rather than guessing.
`Not available` makes the record honest, but does not make a required observation pass
the verification threshold below.

- **Agent host name and version:** the host-reported product name and version.
- **Comic Sol commit or version:** the exact tested revision or installed release.
- **Installation target and scope:** the selected target, user/project scope, and
  placement used by the test.
- **Filesystem capability:** whether the host actually read and wrote the test project,
  with retained evidence.
- **Shell/tool-execution capability:** which shell or tool route the host actually
  invoked, including any restrictions.
- **Image-generation route:** native image tool, declared external adapter, portable
  handoff, or an honestly blocked result.
- **Portable-handoff route:** whether prepare, inspect, export/import, and result intake
  were exercised, or why they were not applicable.
- **Output evidence:** links to retained evidence, such as the project manifest, page
  PNGs, PDF, QA report, and sanitized execution record. Documentation committed to this
  repository must use repository-relative links, but generated projects and build output
  must not be committed. Store those artifacts in a durable, access-controlled external
  location and link its immutable artifact or record instead. Apply the same sanitization
  rules to either location.
- **Execution date supplied by the tester:** the calendar date the tester states the
  live smoke occurred; do not derive it from file or commit timestamps.
- **Known limitations:** unsupported capabilities, unexercised paths, warnings, and the
  exact boundary of the result.

A complete record must distinguish observed host behavior from Comic Sol's
deterministic validation. Remove credentials, tokens, account identifiers, private
endpoints, private source material, raw provider responses, and unrelated logs before
publication.

## Verification threshold

A host is **Verified** only when one linked record demonstrates all of the following in
that named host:

- the host read and wrote the test project;
- the host successfully invoked the documented shell or tool-execution route;
- the host completed either image generation through a declared route or the full
  portable handoff through result intake;
- Comic Sol accepted the resulting local raster and completed deterministic validation
  and export; and
- the sanitized execution record, manifest, QA report, page PNGs, and PDF remain
  inspectable through durable links.

A field reported as `not available`, a blocked capability, a failed step, missing output,
or an unexercised required route makes the record partial. Partial and blocked records
are useful diagnostics, but the host remains **Experimental**.

## Host evidence status

| Agent host | Status | Retained live smoke evidence |
|---|---|---|
| Codex | Experimental | No retained live smoke record |
| Claude Code | Experimental | No retained live smoke record |
| Google Antigravity | Experimental | No retained live smoke record |
| ZCode | Experimental | No retained live smoke record |

Empty or unproven rows remain experimental. Path-copy tests and installer tests are not
live host verification. Documentation examples, successful Skill placement, and a
portable archive created outside the named host also do not verify that host. Do not
promote a row until its record meets the verification threshold and its output evidence
is retained and linked.

## Claim boundaries

Provider support and host support are separate. A working image provider or adapter does
not verify the agent host, and a successful host smoke does not verify every provider or
model. Likewise, filesystem access alone does not establish shell execution,
image-generation capability, portable handoff, output quality, or broad compatibility.

A failed or partial live run is still useful evidence when the record states the exact
failure and limitations. Never fabricate output, fill a field from assumption, or turn
an installer/path-copy test into a live-host claim.
