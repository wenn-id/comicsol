# Sunlight Courier — two-page example

Sunlight Courier is the repository's **retained live visual evidence** and the
**only initial visual-quality sample**: two pages, four panels, and one character.
Its panel artwork came from an image capability rather than a deterministic
placeholder. This one project does not establish broad or universal illustration
quality. See the [sample catalog](../README.md) for the evidence tiers and the
[showcase contract](../../docs/showcase.md) for the publication boundary.

## Retained evidence

- **Provider/model provenance:** [`project.json`](project.json) retains the capability
  identifier `built-in-imagegen-gpt-image-2`, availability, and declared reference and
  dimension support. That is the available provenance; the provider and model are not
  separately identified.
- **Generation attempts and provenance:** the [QA report](qa/report.md) records four
  attempts, zero regenerated panels, and zero accepted warnings. The manifest, panel
  prompts, normalization records, and sanitized event log retain the other available
  project provenance.
- **Reviewer and visual-QA evidence:** the retained panel QA records identify the
  reviewer label `release-sample-reviewer`, the `agent-review` method, check-specific
  evidence, and an `accept` decision for every panel. The [QA report](qa/report.md)
  consolidates those checks and artifact-integrity results.
- **Published pages:** [page 1](pages/page-001.png) and
  [page 2](pages/page-002.png).
- **Published PDF:** [sunlight-courier.pdf](exports/sunlight-courier.pdf).
- **Editable project:** [project.json](project.json), with retained plans, prompts,
  accepted clean panels, reference art, and QA records in this directory.

## Known limitations

This is one two-page project, not a universal quality guarantee. The provider and model
are not separately identified by the retained capability record. The retained reviewer
label does not identify a human reviewer. The evidence covers only this prompt, style,
cast, image route, and output; it does not establish another host, provider, model,
creator, longer story, or illustration style as verified.

## Editable-project compatibility copies

The canonical clean panel art is tracked once at `panels/<panel-id>/clean.png`. Raw and
legacy-clean compatibility copies are generated locally and ignored by Git.

From the repository root, materialize those copies before using this directory as an
editable project:

```powershell
python scripts/materialize_sample.py
```

The command overwrites compatibility copies byte-for-byte from the canonical panels.
Final lettered panels and page PNGs remain tracked as visually reviewed delivery
derivatives, encoded as opaque RGB PNGs to keep the clone lightweight; the canonical
clean panels and character reference remain full-fidelity source art. The PDF and QA
evidence remain tracked and hash-bound to those derivatives.
