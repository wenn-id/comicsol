# Native Comic Sol CLI

Build from the Comic Sol checkout with a Rust toolchain, a matching C compiler
and linker, and CMake 3.22 or later. Linux also needs a C++ compiler and the
system zlib development package.
The build reads schema versions, JSON limits, transaction settings, CLI errors,
and the original Agent Skill bundle from the checkout as data. The executable
uses no Python interpreter or Python extension at runtime.

```sh
cargo build --release --locked --manifest-path rust/status/Cargo.toml
rust/status/target/release/comic-sol --json status PATH_TO_PROJECT
rust/status/target/release/comic-sol --json validate PATH_TO_PROJECT --stage storyboard
rust/status/target/release/comic-sol --json resume PATH_TO_PROJECT
rust/status/target/release/comic-sol --json finalize PATH_TO_PROJECT
rust/status/target/release/comic-sol mcp --root /absolute/project/root
```

On Windows, run `rust/status/target/release/comic-sol.exe`. The binary implements
the installed CLI with human and JSON output, including initialization, plan
binding, all handoff and dogfood commands, integration setup, repair and
uninstall, skill placement, and the 18 MCP tools over stdio. Validation uses the
same project lock and journal recovery and preserves the sorted JSON issue
format across all six stage selectors. It verifies handoff jobs, receipts and
locked scope, panel QA and repair plans, character provenance, lettering and
typography, page QA, and published artifact bindings.

JSON resume computes the reusable stages from the existing cache. When a
blocker is resolved, it publishes cache, event and manifest updates through the
same journal format used by Python. A pending image capability leaves the
project blocked. Generated artifacts and accepted rasters stay on disk. If
`logs/events.jsonl` is a directory, resume rejects the target before publishing
any project changes.

JSON finalize uses the same lock and journal format to letter panels, compose
ordered pages, export a verified 150-DPI PDF, and publish the QA report. It
requires a current agent-produced page QA record for each composed page before
export. When lettering or composition changes a reviewed artifact, review the
new page and publish a current QA record before running finalize again.

Finalize links FreeType 2.14.3, libjpeg-turbo 3.2.0, and Pillow 13.0.0's
standalone polygon and ellipse routines statically. The Windows build uses
BASIC font layout and zlib-ng 2.3.3. The Linux build uses the system
`libz.so.1` and enables RAQM 0.11.0 with the bundled HarfBuzz 14.5.0 when it
can load the system FriBiDi library, as the Pillow Linux wheel does. Linux
uses BASIC layout when FriBiDi is unavailable.

The build verifies source archive hashes. Set `COMIC_SOL_CODEC_ARCHIVES` to a
directory containing the platform's pinned archives from the
[native raster dependency lock](../../requirements/locks/native-raster.cmake)
for an offline build. CMake uses the compiler and generator from its normal
environment. When cross-compiling, set `CMAKE_TOOLCHAIN_FILE` to a toolchain
matching the Cargo target.

The finalize suite compares lettering geometry, composed PNGs, and PDFs
against the platform's Pillow 13.0.0 reference. When the composition stage
reruns, it publishes the encoded PNG and its hash together. A changed hash
requires a new page review.

Doctor reports the native runtime, raster codecs, and MCP server. Resume
suggests the native `finalize` command when deterministic stages need rerunning.
Handoff export uses a prefixed zlib 1.3.1 compressor to match the reference ZIP
bytes. Archive extraction validates hashes, CRCs, resource bounds, and project
schema in a private staging directory before publication.

The Agent Skill uses this executable for the full deterministic workflow.
`comic-sol skill --help` lists its lifecycle, character identity and QA,
reference selection, normalization, repair, lettering, and export commands.
`comic-sol skill-install --target CLIENT --scope user` installs the instructions,
templates, and fonts. It upgrades intact older installations in place. The
Python package remains available as an independent reference implementation.

Status accepts current and legacy manifests, including manifests without a
schema version. It holds the existing project lock through transaction recovery
and the manifest read. Recovery recognizes journals written by the Python engine
and checks all rollback paths before making changes.

To verify interoperability, set `COMIC_SOL_NATIVE_TEST_EXE` to the executable's
absolute path and run `python rust/status/tests/verify.py -v` from the checkout.
The tests use Python to create and compare reference projects.

Run `cargo test --locked --manifest-path rust/status/Cargo.toml` for the
transaction recovery tests.
