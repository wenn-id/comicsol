# Sunlight Courier sample

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
