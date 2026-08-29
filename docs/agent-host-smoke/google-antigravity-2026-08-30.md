# Google Antigravity live agent-host smoke record — 2026-08-30

This record documents one live Comic Sol execution inside the Google Antigravity agent
host. It follows the field set and verification threshold in
[`docs/agent-host-smoke.md`](../agent-host-smoke.md). Observations are exact; nothing is
inferred from the host, provider, model, or tool name.

- **Agent host name and version:** `agy` (Antigravity CLI) `1.1.22`, invoked
  non-interactively with `-p`/`--print`, `--dangerously-skip-permissions`, and
  `--model gemini-3.1-pro-high`.
- **Comic Sol commit or version:** commit `9b47561c810db88a047e837f96594290ebe5b198`,
  version `2.0.0rc6`, installed from the locally built wheel
  `comic_sol-2.0.0rc6-py3-none-any.whl`.
- **Installation target and scope:** target `antigravity`, project scope. Placement
  observed at `<project>/.agents/skills/comic-sol`, with the install marker
  `.comic-sol-install.json` recording target, scope, version, bundle digest, and managed
  relative paths only.
- **Filesystem capability:** the host read and wrote its own isolated project at
  `<lane-root>/lane-antigravity/project/output/agent-gate`. Retained manifest, plan,
  prompts, panels, QA records, page, and export are all host-written files.
- **Shell/tool-execution capability:** the host invoked the documented deterministic
  shell route through its own shell tool on its native Python 3.11 interpreter. Every
  lifecycle stage from init through finalize and export ran through that route.
- **Image-generation route:** declared **native image tool** — capability
  `agent-image-generation` reported by the host and recorded in the retained QA report
  (`Status: available, Capability: agent-image-generation`). Reference images were
  generated and used; explicit output dimensions were not exposed and the panel was
  normalized by Comic Sol.
- **Portable-handoff route:** **not applicable** in this run, because the host completed
  generation through its declared native image tool instead of a handoff. No handoff
  archive was required.
- **Output evidence:** retained on the tester's local host lane only. Repository-relative
  links are not available because generated projects, images, PDFs, and build output must
  not be committed. Digests below identify the exact retained artifacts.

  | Artifact | Project-relative path | SHA-256 |
  |---|---|---|
  | Exported PDF | `exports/agent-gate.pdf` | `c614adcb8c97fcade6310688ab691352576c00fdace2b4a04e4a6f91facb8ca2` |
  | Composed page | `pages/page-001.png` | `7a6c5eee068f85f868edcb98bcb30258b3a6ddf57a764165916b2092d55567d7` |
  | Accepted panel raster | `panels/raw/p01-01.png` | `b338258e73562d123b9dd24f46066de1e8c15bfade929433331839b4d65d9e3a` |
  | Canonical character reference | `references/characters/ilya.png` | `66f500aba163bfa3f1f47753e8e7e84ca0ec27ee9b79deae9b08b2be15ec2cb4` |
  | Page QA record | `qa/pages/page-001.json` | `f2fdd1f72223c9c6baa26db563559934f250744ce0cf21182e3522635d2c9b15` |
  | Storyboard | `plan/storyboard.json` | `93c7a28c9036f79d8d9c0afe044a19a08d3e543e821f24cf795b541df3ede635` |
  | QA report | `qa/report.md` | retained locally |

  Deterministic validation and export both completed. The project manifest reports final
  status `COMPLETE`, the QA report reports 1 page, 1 panel, 0 accepted warnings, and
  0 hard failures, and `exports/pdf-verification.json` confirms the exported PDF against
  the composed page with `mean_absolute_channel_error 0.578588` and
  `high_error_pixel_ratio 0.0`, both inside the published tolerance.
- **Execution date supplied by the tester:** 2026-08-30.
- **Known limitations:**
  - The retained artifacts live only on the tester's local machine. No durable,
    access-controlled external location was authorized for this run, so the required
    durable evidence links are **not available** and this host stays Experimental.
  - The host does not expose exact output pixel dimensions, so the raster was normalized
    by Comic Sol rather than generated at the requested size.
  - Scope was the minimum honest smoke: one page, one panel, one character.
  - Visual QA was a bounded review by the host acting as reviewer; it is not an
    independent third-party assessment of illustration quality.
  - No provider name, model name, credential, account identifier, endpoint, or raw
    provider response is recorded in the project or in this record.
  - This record verifies the Antigravity host only. It does not verify any other host,
    provider, or model, and it makes no broad illustration-quality claim.
