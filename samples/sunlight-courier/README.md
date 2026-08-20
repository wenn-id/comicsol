# Sunlight Courier — two-page example

The **live-generated** reference project: two pages, four panels, one character.
This is the example to look at for real image output, because its panel artwork
came from an image capability rather than a deterministic placeholder. See
[`../README.md`](../README.md) for the full catalog and the evidence tiers.

The canonical clean panel art is tracked once at
`panels/<panel-id>/clean.png`. Raw and legacy-clean compatibility copies are
generated locally and ignored by Git.

From the repository root, materialize those copies before using this directory as
an editable project:

```powershell
python scripts/materialize_sample.py
```

The command overwrites compatibility copies byte-for-byte from the canonical
panels. Final lettered panels, pages, QA evidence, and the PDF remain tracked.
