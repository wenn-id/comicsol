# Cross-host archive-route evidence — Codex → Antigravity → Codex (2026-08-30)

This record documents one complete Comic Sol **cross-host portable-handoff** route
executed on the same day as the per-host smoke records in this directory. It follows
the field set and verification threshold in
[`../agent-host-smoke.md`](../agent-host-smoke.md). Observations are exact; nothing is
inferred from host, provider, model, or tool name.

- **Route name:** Codex (plan + prepare + export) → Antigravity (import + generate +
  accept-result + export) → coordinator (deterministic finalize + PDF). Both legs
  cross an isolated host boundary and a fresh `CODEX_HOME` / `HOME`. Codex leg-3
  resume was attempted by the host 4× (see limitations) and is a best-effort
  overlay; the finalize phase is coordinator-executed engine work on the accepted
  raster, which is the engine's documented post-acceptance path.
- **Comic Sol commit or version:** commit
  `6292ae1` (merge of `origin/main` `f413d189` into the existing WP5 branch head
  `a2caa43`), version `2.0.0rc6`, installed from the locally built wheel
  `comic_sol-2.0.0rc6-py3-none-any.whl` rebuilt from `6292ae1`.
- **Inbound Codex archive:**
  `C:\Users\acer\comicsol-wp5-xh\archives\codex-plan.comic-sol-handoff`
  (mirrored to `/home/acer/comicsol-wp5-xh/archives/codex-plan.comic-sol-handoff` for
  the Antigravity leg, **SHA-256 match verified on transfer**).
  - SHA-256: `94b967f660e8c1165eaf1246f82df10792aa9656142caf405046fd4a09a4576f`
  - Locked-scope SHA-256: `c0e7545df7decb6e6d58c6afdcbbdf552a6b0dbe1106ed6f6e97af86e658b63d`
  - Bound job_id: `45fc6117c8084387a79c145e0bdfb729c0d7603b6d1ea0a39613d736f7407245`
  - Status at export: `ready`; required artifacts: 8 (all pinned by digest, incl.
    `references/characters/ilya.png`, `plan/character-identity-pack.json`, both
    `prompts/*.txt`, `logs/reference-selection.json`).
- **Outbound Antigravity result archive:**
  `/home/acer/comicsol-wp5-xh/archives/antigravity-result.comic-sol-handoff`
  (10,202,193 B; SHA-256
  `2f3a95841194d24304eef1e227d932b6ea3e4aaf8a2662e78902a4cfa71182af`;
  captured independently at export and cross-host log line appended).
  Locked scope unchanged (`c0e7545d…`); job status `completed (duplicate=False)`;
  accepted raster SHA-256
  `59a22e7059ce75820f8540d97384a99dcef32357c2f02926fbab601fb351e25e`
  (1472×2272 RGB PNG, exactly the storyboard panel rect).
- **Codex (leg 1) host name and version:** `codex` (`codex-cli 0.146.0`) running
  non-interactively on Windows; `CODEX_HOME` was a disposable
  `C:\Users\acer\comicsol-wp5-xh\codex-home\.codex` that mirrored only
  `auth.json` and `config.toml` from the real `~/.codex` (token values never
  copied to a convertible evidence config; real `~/.codex` left untouched).
  Skill placement at `~/.codex/skills/comic-sol` (user scope, disposable home).
- **Antigravity (leg 2) host name and version:** `agy` (Google Antigravity CLI)
  `1.1.22`, invoked with `-p`, `--dangerously-skip-permissions`,
  `--model gemini-3.1-pro-high`, `--effort high`, `--print-timeout 25m`. `HOME`
  was a disposable `/home/acer/comicsol-wp5-xh/ag-home` that mirrored only the
  four auth/session files (`antigravity-oauth-token`, `settings.json`,
  `installation_id`, `jetski_state.pbtxt`) from the real `~/.gemini/antigravity-cli/`
  (token values never printed or copied to a convertible evidence config; real
  `~/.gemini` left untouched). Skill placement at
  `<project>/.agents/skills/comic-sol` (project scope, isolated to `ag-project`).
- **Codex (leg 3) host attempts, and finalize executor:** the disposable Codex
  lane from leg 1 was re-launched 4× to resume the accepted project and run
  finalize; every attempt blocked on lane/environment mismatches (drvfs path
  divergence, a nonexistent skill reference removed from the prompt, WSL
  `python` absent → Windows Python 3.11.9 pinned, and PowerShell self-quoting),
  last attempt killed (EXIT 143). The finalize phase was then executed by the
  **coordinator** (Hermes, WSL venv, dev launcher `python scripts/comic_sol.py`)
  on the ext4 copy: `promote-attempt` → agent-authored panel QA →
  `finalize` (letter + compose) → published page QA via `publish_page_quality_record`
  → `finalize` (PDF export, `COMPLETE`). This is engine-versioned deterministic
  work on the host-accepted raster, not a host claim; the cross-host claim is the
  archive route (legs 1–2), which is fully host-executed and digest-verified.
- **Filesystem capability (leg 1, Codex):** the host read and wrote
  `C:\Users\acer\comicsol-wp5-xh\codex-plan\cross-host-codex-plan`; produced
  `references/characters/ilya.png` (1254×1254 PNG, SHA-256
  `4a5e1297991d9aba8c570629e5502b18253bab394770651fc3f469da690b841a`),
  `plan/character-identity-pack.json`, `prompts/references/ilya.txt`,
  `prompts/panels/p01-01.txt`, `logs/reference-selection.json`, and the
  bound handoff manifest.
- **Filesystem capability (leg 2, Antigravity):** the host imported the inbound
  archive into `/home/acer/comicsol-wp5-xh/antigravity-import`, generated the
  single panel raster, accepted the result back through
  `comic-sol handoff accept-result`, and re-exported the updated archive.
- **Shell/tool-execution capability (both legs):** documented
  `python scripts/comic_sol.py <subcommand> "<project>" --json` (dev launcher,
  has `--json` on `handoff prepare`/`handoff inspect`/`finalize`) and the
  packaged `comic-sol` (wheel) `init`/`status`/`handoff import|inspect|accept-result|export`
  (no `--json` on prepare/inspect in the wheel; verified by `--help`).
- **Image-generation route (leg 2, Antigravity):** declared native
  `agent-image-generation` — capability reported by the host from its actual
  exposed tool list, never inferred from model name. No credential, account,
  endpoint, or raw provider response was recorded.
- **Portable-handoff route:** **fully exercised** in both directions and to
  completion — prepare → export (Codex leg 1), import → inspect → accept-result →
  export (Antigravity leg 2), promote accepted raster → author panel QA
  record → letter/compose (finalize call 1) → publish page QA record (engine
  API + observed evidence) → finalize call 2 → PDF export (**COMPLETE**, leg 3).
  Final engine status `cross-host-codex-plan: COMPLETE`, and
  `python scripts/validate_project.py <project> --stage all` reports
  `VALID … (all)` — zero issues. The engine's own
  `exports/pdf-verification.json` re-rasterises the PDF and compares it to
  `pages/page-001.png`: `mean_absolute_channel_error 0.749824`,
  `maximum_grid_region_error 1.235394`, `high_error_pixel_ratio 0.0`, all inside
  the exporter tolerance (3.1 / 16.0 / 0.021).
- **Output evidence:** retained on the tester's isolated local lanes only.
  Repository-relative links are not available because generated projects,
  images, PDFs, and build output must not be committed. Digests below identify
  the exact retained artifacts (all in this repository's working tree or in the
  disposable evidence root under `comicsol-wp5-xh/`).

  | Artifact | Location | SHA-256 |
  |---|---|---|
  | Inbound Codex archive | `comicsol-wp5-xh/archives/codex-plan.comic-sol-handoff` | `94b967f660e8c1165eaf1246f82df10792aa9656142caf405046fd4a09a4576f` |
  | Inbound locked-scope digest | `comicsol-wp5-xh/archives/.../project/handoff/manifest.json` | `c0e7545df7decb6e6d58c6afdcbbdf552a6b0dbe1106ed6f6e97af86e658b63d` |
  | Inbound job_id | same manifest | `45fc6117c8084387a79c145e0bdfb729c0d7603b6d1ea0a39613d736f7407245` |
  | Outbound Antigravity result archive | `comicsol-wp5-xh/archives/antigravity-result.comic-sol-handoff` | `2f3a95841194d24304eef1e227d932b6ea3e4aaf8a2662e78902a4cfa71182af` |
  | Accepted raster (inbound leg 2, `incoming/p01-01.png`) | `comicsol-wp5-xh/antigravity-import/.../incoming/p01-01.png` | `59a22e7059ce75820f8540d97384a99dcef32357c2f02926fbab601fb351e25e` |
  | Panel QA record (leg 3, `qa/panels/p01-01.json`) | `comicsol-wp5-xh/antigravity-import/.../qa/panels/p01-01.json` | `dfa61daea5846fd1943dc07c0068a8192606414fe908869eda2ac25efd897f36` |
  | Page QA record (leg 3, `qa/pages/page-001.json`) | `comicsol-wp5-xh/antigravity-import/.../qa/pages/page-001.json` | `60ee6d7754c1f45077c0ab7b66ab71da156ac7f95a47670b7c9dba94bd8a8f4a` |
  | Composed page (leg 3, `pages/page-001.png`, 1600×2400) | `comicsol-wp5-xh/antigravity-import/.../pages/page-001.png` | `cb6d03ffa64b6e7ea943ff67674b78367ebadc90f45921f752cbd5e1d948c05a` |
  | Final PDF (leg 3, `exports/cross-host-codex-plan.pdf`) | `comicsol-wp5-xh/antigravity-import/.../exports/cross-host-codex-plan.pdf` | `a30ae75fd0b883634339e4505dec2218ad1a79f8b19bb539adf2d7245a46b67f` |
  | QA report (leg 3, `qa/report.md`) | `comicsol-wp5-xh/antigravity-import/.../qa/report.md` | `8a95f8e6dda8d266895a053782f0679d65e2289196cf6a3f392975536d7cd1e7` |
  | PDF raster render (150 dpi, pymupdf) | derived | `ad2f3e7fe0c2ff60c88be18be218e645b68da05a52684c4c8d765cfcc8155ec6` |
  | Engine PDF verification record | `comicsol-wp5-xh/antigravity-import/.../exports/pdf-verification.json` | mean abs error `0.749824`, max grid error `1.235394`, high-error ratio `0.0` |
  | Codex leg-1 cross-host log line | `comicsol-wp5-xh/logs/cross-host.codex-plan.log` | one-line append |
  | Antigravity leg-2 cross-host log line | `comicsol-wp5-xh/logs/cross-host.antigravity.log` | one-line append |
  | Codex leg-1 run log | `comicsol-wp5-xh/logs/codex-handoff-run3.log` | retained |
  | Antigravity leg-2 run log | `comicsol-wp5-xh/logs/antigravity-run3.log` | retained |

- **Execution date supplied by the tester:** 2026-08-30 (Asia/Jakarta, UTC+07:00).
  Confirmed by the three retained run-log header lines:
  - `=== Codex cross-host handoff pass 3 started 2026-08-30T08:13:09+07:00 ===`
  - `=== Antigravity live run started 2026-08-30T08:20:xx+07:00 ===`
  - (leg 3 same day)
  GitHub's UTC display of these timestamps is one day earlier; the files keep
  the tester-supplied date.
- **Known limitations:**
  - The Codex leg was retried after the `minimal-one-page` starter was found to
    declare `references/characters/ilya.png` in the bible without shipping the
    file. The first derive failed closed (`character-identity-pack character
    'ilya' reference view file is missing: references/characters/ilya.png`); the
    lane authored the reference via the host's declared `agent-image-generation`
    tool and re-ran derive, then authored the `prompts/*.txt` files exactly as
    the original WP5 Codex lane did. This is a real product defect in the
    starter; tracked separately.
  - The wheel `comic-sol` CLI's `handoff prepare`/`handoff inspect` do not
    accept `--json`; the dev launcher `scripts/comic_sol.py` does. The lane
    used the dev launcher for the JSON-emitting handoff subcommands and the
    wheel for `init`/`status`/`import`/`accept-result`/`export` (verified by
    `--help`).
  - The disposable `CODEX_HOME` and `HOME` mirrored only the four auth-bearing
    files from the real `~/.codex` and `~/.gemini/antigravity-cli/`. Token
    values were never printed and never written to a convertible evidence
    config. The real account files were not modified.
  - No durable external evidence links are authorized for this run, so the
    `Verified` threshold in `docs/agent-host-smoke.md` is **not** met by this
    record; the route remains **Experimental** for both Codex and Antigravity
    even though the route itself completed end to end with real host execution
    on both sides.
