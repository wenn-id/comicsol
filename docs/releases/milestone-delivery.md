# Comic Sol milestone delivery record

This document records what each Comic Sol milestone delivered, which issue asked for it, and
which pull request closed it. It exists so a completed milestone can be audited without
reading the whole commit history, and so a delivered change cannot go unrecorded when the
next milestone starts.

It is a delivery record, not a release announcement. Milestone titles (`v2.1`, `v2.2`) are
planning labels and are **not** version tags. The published version is `2.0.0rc4`; the
release notes for a tag live in `docs/releases/v<tag>.md`, and the per-change record lives in
`CHANGELOG.md`.

## Status summary

| Milestone | Issues | Status | Released |
|---|---:|---|---|
| v2.0 — Stability | 10 | Delivered | No — in `CHANGELOG.md` under Unreleased |
| v2.1 — Reliability & DX | 8 | Delivered | No — in `CHANGELOG.md` under Unreleased |
| v2.2 — Comic Quality | 10 | Delivered | No — in `CHANGELOG.md` under Unreleased |
| v2.3 — User Experience | 7 | Planned | — |

**No completed milestone is released yet.** This is the point most easily got wrong, so it is
stated precisely: the four `2.0.0rc*` prereleases shipped the v2.0 *product line* on
2026-07-29/30, but every issue in the v2.0 *milestone* merged on 2026-08-18/19 — after `rc4`
was published. The milestone hardened and qualified what the prereleases had already shipped;
it is not what they contain.

So all 28 delivered issues across v2.0, v2.1, and v2.2 ship in the first tag cut after
`2.0.0rc4`. Until then `CHANGELOG.md` holds them under Unreleased. The published version is
`2.0.0rc4`.

### Delivery timeline

The two halves of this table are the whole argument, so they are recorded as data rather than
prose. Merge windows are the first and last closing pull request of each milestone; publish
dates are the released tags. Every milestone merged *after* the last tag was published, which
is why the `Released` column above is `No` throughout.

| Milestone | Merged |
|---|---|
| v2.0 — Stability | 2026-08-18 … 2026-08-19 |
| v2.1 — Reliability & DX | 2026-08-19 … 2026-08-20 |
| v2.2 — Comic Quality | 2026-08-20 … 2026-08-21 |

| Tag | Published |
|---|---|
| `2.0.0rc1` | 2026-07-29 |
| `2.0.0rc2` | 2026-07-30 |
| `2.0.0rc3` | 2026-07-30 |
| `2.0.0rc4` | 2026-07-30 |

## v2.0 — Stability

Hardened and qualified the product line the `2.0.0rc*` prereleases had already shipped:
end-to-end and failure-injection coverage, resume idempotence, structured diagnostics and
error codes, schema-migration safety, and a release qualification gate. Every issue here
merged on 2026-08-18/19, after `rc4` was published on 2026-07-30, so **none of it is in a
released version** — a distinction worth keeping, because the milestone shares its name with
the shipped prereleases.

The release gate for the stable tag is `docs/releases/v2.0-stable-criteria.md`, which is
authoritative over this document for anything gating a release.

| Issue | ID | Delivered | PR |
|---|---|---|---|
| [#108](https://github.com/wenn-id/comicsol/issues/108) | `CS-001` | v2.0 stable release criteria | [#141](https://github.com/wenn-id/comicsol/pull/141) |
| [#109](https://github.com/wenn-id/comicsol/issues/109) | `CS-002` | Golden end-to-end pipeline coverage | [#142](https://github.com/wenn-id/comicsol/pull/142) |
| [#110](https://github.com/wenn-id/comicsol/issues/110) | `CS-003` | Interrupted generation and resume regression coverage | [#143](https://github.com/wenn-id/comicsol/pull/143) |
| [#111](https://github.com/wenn-id/comicsol/issues/111) | `CS-004` | Project lifecycle failure-injection suite | [#144](https://github.com/wenn-id/comicsol/pull/144) |
| [#112](https://github.com/wenn-id/comicsol/issues/112) | `CS-005` | Clean-installation verification across supported platforms | [#145](https://github.com/wenn-id/comicsol/pull/145) |
| [#113](https://github.com/wenn-id/comicsol/issues/113) | `CS-006` | Hardened `comic-sol doctor` diagnostics | [#146](https://github.com/wenn-id/comicsol/pull/146) |
| [#114](https://github.com/wenn-id/comicsol/issues/114) | `CS-007` | Project schema and migration safety audit | [#150](https://github.com/wenn-id/comicsol/pull/150) |
| [#115](https://github.com/wenn-id/comicsol/issues/115) | `CS-008` | Installer upgrade, rollback, and uninstall safety audit | [#147](https://github.com/wenn-id/comicsol/pull/147) |
| [#116](https://github.com/wenn-id/comicsol/issues/116) | `CS-009` | Structured Comic Sol error codes | [#149](https://github.com/wenn-id/comicsol/pull/149) |
| [#117](https://github.com/wenn-id/comicsol/issues/117) | `CS-010` | v2.0 release qualification workflow | [#148](https://github.com/wenn-id/comicsol/pull/148) |

## v2.1 — Reliability & DX

Made engine quality measurable and the first run survivable. Four issues built the benchmark
plane; four made the product legible to a new user and to an agent changing the repository.

| Issue | ID | Delivered | PR |
|---|---|---|---|
| [#118](https://github.com/wenn-id/comicsol/issues/118) | `CS-011` | Benchmark framework (`scripts/benchmark.py`) with a validated case contract and a fail-closed two-revision diff | [#151](https://github.com/wenn-id/comicsol/pull/151) |
| [#119](https://github.com/wenn-id/comicsol/issues/119) | `CS-012` | Real-world benchmark corpus of nine plan-complete projects | [#152](https://github.com/wenn-id/comicsol/pull/152) |
| [#120](https://github.com/wenn-id/comicsol/issues/120) | `CS-013` | Character consistency benchmark and unscored scorecard contract | [#153](https://github.com/wenn-id/comicsol/pull/153) |
| [#121](https://github.com/wenn-id/comicsol/issues/121) | `CS-014` | Version-tagged benchmark summary and comparison reporting | [#155](https://github.com/wenn-id/comicsol/pull/155) |
| [#122](https://github.com/wenn-id/comicsol/issues/122) | `CS-015` | Stage-aware CLI progress on `stderr`, with the JSON envelope preserved | [#154](https://github.com/wenn-id/comicsol/pull/154) |
| [#123](https://github.com/wenn-id/comicsol/issues/123) | `CS-016` | First-run onboarding path (`docs/onboarding.md`) | [#156](https://github.com/wenn-id/comicsol/pull/156) |
| [#124](https://github.com/wenn-id/comicsol/issues/124) | `CS-017` | Official example projects, catalog, and on-demand example builder | [#158](https://github.com/wenn-id/comicsol/pull/158) |
| [#125](https://github.com/wenn-id/comicsol/issues/125) | `CS-018` | `AGENTS.md` development constitution and its guard test | [#160](https://github.com/wenn-id/comicsol/pull/160) |

## v2.2 — Comic Quality

Attacked the two failure modes that make generated comics read as machine output: characters
that drift between panels, and lettering that cannot be attributed or verified. Two follow-up
issues (`CS-034`, `CS-035`) closed gaps the balloon-QA change opened.

| Issue | ID | Delivered | PR |
|---|---|---|---|
| [#126](https://github.com/wenn-id/comicsol/issues/126) | `CS-019` | Character Identity Pack, derived and byte-reproducible | [#161](https://github.com/wenn-id/comicsol/pull/161) |
| [#127](https://github.com/wenn-id/comicsol/issues/127) | `CS-020` | Shot-aware character reference strategy with published selections | [#163](https://github.com/wenn-id/comicsol/pull/163) |
| [#128](https://github.com/wenn-id/comicsol/issues/128) | `CS-021` | Per-trait character-consistency QA with reusability bindings | [#164](https://github.com/wenn-id/comicsol/pull/164) |
| [#129](https://github.com/wenn-id/comicsol/issues/129) | `CS-022` | Selective panel repair planning with recorded fallback reasons | [#165](https://github.com/wenn-id/comicsol/pull/165) |
| [#130](https://github.com/wenn-id/comicsol/issues/130) | `CS-023` | Deterministic speech balloon placement QA (three new page checks) | [#176](https://github.com/wenn-id/comicsol/pull/176) |
| [#131](https://github.com/wenn-id/comicsol/issues/131) | `CS-024` | Verifiable multi-character dialogue attribution | [#186](https://github.com/wenn-id/comicsol/pull/186) |
| [#132](https://github.com/wenn-id/comicsol/issues/132) | `CS-025` | Inventoried typography coverage and per-script font extensions | [#187](https://github.com/wenn-id/comicsol/pull/187) |
| [#133](https://github.com/wenn-id/comicsol/issues/133) | `CS-026` | SFX render modes, provenance, verification flags, and repair path | [#191](https://github.com/wenn-id/comicsol/pull/191) |
| [#178](https://github.com/wenn-id/comicsol/issues/178) | `CS-034` | `dialogue_correctness` measures the new balloon checks | [#181](https://github.com/wenn-id/comicsol/pull/181) |
| [#179](https://github.com/wenn-id/comicsol/issues/179) | `CS-035` | Page-QA record versioned to `2.1` with a registered migration | [#182](https://github.com/wenn-id/comicsol/pull/182) |

### Artifact contract changes carried by v2.2

These are the compatibility-relevant outcomes, collected here because they are what a
consumer of an existing project needs to know:

| Artifact | Change | Effect on an existing project |
|---|---|---|
| `panels/{id}/lettering.json` | `1.0` → `1.1` → `1.2` (attribution, then SFX provenance) | Re-lettered, never migrated; reported as `lettering-record-stale` |
| `panels/{id}/typography.json` | `1.1` (recorded preflight checks and script roll-up) | Re-lettered under the current script policy |
| `qa/pages/*.json` | `2.0` → `2.1` (ten checks, `normalization_sha256s`) | Migrated in place by the registered `("2.0", "2.1")` hook |
| Lettering stage cache | `"2"` → `"3"` | Lettering reruns instead of reusing a cached result |
| Benchmark `HARNESS_VERSION` | `"1"` → `"2"` | Pre-bump result records are refused rather than compared |
| `plan/character-identity-pack.json` | New, schema `1.0` | Opt-in; derived from the character bible |
| `logs/reference-selection.json` | New, schema `1.0` | Derived provenance, republished on plan change |
| `logs/repair-plan.json` | New, schema `1.0` | Derived from the current QA record |
| `panels/{id}/sfx-audit.json` | New, schema `1.0` | Written only by `scripts/sfx_repair.py` |

No `project.json` schema version changed in v2.1 or v2.2; it remains `1.0`. The MCP surface
remains exactly 17 `comic_*` tools.

## v2.3 — User Experience

Planned. Listed here so the record covers every milestone rather than only the finished ones.

| Issue | ID | Scope |
|---|---|---|
| [#134](https://github.com/wenn-id/comicsol/issues/134) | `CS-027` | Simplify one-command installation |
| [#135](https://github.com/wenn-id/comicsol/issues/135) | `CS-028` | Automatic capability detection |
| [#136](https://github.com/wenn-id/comicsol/issues/136) | `CS-029` | Improve setup repair workflow |
| [#137](https://github.com/wenn-id/comicsol/issues/137) | `CS-030` | Interactive project initializer |
| [#138](https://github.com/wenn-id/comicsol/issues/138) | `CS-031` | Improve project status visualization |
| [#139](https://github.com/wenn-id/comicsol/issues/139) | `CS-032` | Richer starter templates |
| [#140](https://github.com/wenn-id/comicsol/issues/140) | `CS-033` | Improve non-developer documentation |

## Keeping this record honest

`tests/test_release_docs.py` checks this file offline, because a delivery record that can
silently omit or overstate a delivery is worth less than no record at all:

- the counts in the status summary must match the rows in each milestone table;
- every issue must appear exactly once across the whole document;
- every delivered issue must cite a closing pull request and a `CS-` identifier;
- every delivered issue's headline artifact must actually appear in `CHANGELOG.md`, scoped to
  the Unreleased section alone;
- a `Released` cell must be `No`, or name a tag that this repository published *after* the
  milestone's last merge — the check that catches this document's first version, which
  claimed the v2.0 milestone shipped in tags cut nineteen days before it merged.

The evidence map in that test is keyed off the delivered milestones listed here, so marking a
milestone delivered without extending the map fails rather than quietly narrowing the check.

When a milestone completes: add its rows, its merge window, and its evidence probes, update
the status summary, then close the milestone on GitHub so the tracker and this document agree.
