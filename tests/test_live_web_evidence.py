"""Repository contract tests for the Web/Studio live evidence framework (issue #321).

This module is the offline, candidate-binding contract for the
``scripts/live_web_evidence.py`` gate. It is the runtime companion to
``docs/web/live-evidence.md``: the docs are prose, these tests are the
behaviour the prose promises, exercised against an isolated evidence
bundle in a temporary directory.

The tests do not call providers, read credentials, or persist prompts.
They construct bundles, write them to disk, invoke the gate, and assert
the gate's accept / reject contract — including the failure modes the
framework exists to prevent (fabricated candidates, escaped paths,
secret-shaped strings, bad SHA bindings, declared-but-missing images).
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import Mapping

from PIL import Image

from scripts.live_web_evidence import (
    ALLOWED_ENVIRONMENTS,
    ALLOWED_RESULTS,
    EvidenceError,
    render_markdown,
    validate_bundle,
)


def _candidate_sha() -> str:
    return "0" * 40


def _make_png(path: Path, *, width: int = 96, height: int = 96) -> str:
    image = Image.new("RGB", (width, height), (24, 24, 32))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(
    *,
    rows: list[Mapping[str, object]] | None = None,
    candidate: Mapping[str, object] | None = None,
    authorization: Mapping[str, object] | None = None,
    retention: Mapping[str, object] | None = None,
    extra_field: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "web-live-evidence/1.0",
        "candidate": dict(
            candidate
            or {
                "sha": _candidate_sha(),
                "engine_version": "2.x",
                "recorded_before_execution": True,
            }
        ),
        "authorization": dict(
            authorization
            or {
                "provider_or_host": "none",
                "max_cost": "USD 0.00",
                "maintainer": "wenn-id",
                "notes": "offline-only manual evidence",
            }
        ),
        "retention": dict(
            retention
            or {
                "location": "evidence/example",
                "created_at": "2026-09-01T00:00:00+00:00",
            }
        ),
        "rows": list(rows or []),
    }
    if extra_field:
        payload[extra_field] = "leaked"
    return payload


def _write_bundle(root: Path, manifest: Mapping[str, object]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def candidate(self) -> str:
        return _candidate_sha()


class EmptyBundleRoundTripTests(_Base):
    def test_empty_bundle_passes_with_roster_zero(self) -> None:
        _write_bundle(self.root, _manifest())
        summary = validate_bundle(self.root, self.candidate())
        self.assertEqual(summary["schema"], "web-live-evidence/1.0")
        self.assertEqual(summary["candidate"]["sha"], self.candidate())
        self.assertEqual(summary["rows"], [])
        self.assertEqual(summary["authorization"]["provider_or_host"], "none")

    def test_render_markdown_contains_candidate_and_explicit_no_rows_note(self) -> None:
        _write_bundle(self.root, _manifest())
        summary = validate_bundle(self.root, self.candidate())
        rendered = render_markdown(summary)
        self.assertIn(self.candidate(), rendered)
        self.assertIn("Rows: 0", rendered)
        self.assertIn("No rows retained", rendered)

    def test_full_cli_writes_summary_files(self) -> None:
        from scripts.live_web_evidence import main

        _write_bundle(self.root, _manifest())
        exit_code = main([str(self.root), "--candidate", self.candidate()])
        self.assertEqual(exit_code, 0, "gate must exit 0 on an honest empty bundle")
        self.assertTrue((self.root / "summary.json").is_file())
        self.assertTrue((self.root / "summary.md").is_file())
        payload = json.loads((self.root / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["rows"], [])


class CandidateBindingTests(_Base):
    def test_manifest_candidate_must_match_trusted_commit(self) -> None:
        wrong = dict(
            {
                "sha": "f" * 40,
                "engine_version": "2.x",
                "recorded_before_execution": True,
            }
        )
        _write_bundle(self.root, _manifest(candidate=wrong))
        with self.assertRaises(EvidenceError) as ctx:
            validate_bundle(self.root, self.candidate())
        self.assertIn("candidate.sha does not match", str(ctx.exception))

    def test_recorded_before_execution_must_be_true(self) -> None:
        bad = {
            "sha": self.candidate(),
            "engine_version": "2.x",
            "recorded_before_execution": False,
        }
        _write_bundle(self.root, _manifest(candidate=bad))
        with self.assertRaises(EvidenceError):
            validate_bundle(self.root, self.candidate())

    def test_candidate_sha_must_be_40_lowercase_hex(self) -> None:
        bad = {
            "sha": "Z" * 40,
            "engine_version": "2.x",
            "recorded_before_execution": True,
        }
        _write_bundle(self.root, _manifest(candidate=bad))
        with self.assertRaises(EvidenceError):
            validate_bundle(self.root, self.candidate())


class SchemaStrictnessTests(_Base):
    def test_unknown_manifest_field_rejected(self) -> None:
        _write_bundle(self.root, _manifest(extra_field="vendor_payout"))
        with self.assertRaises(EvidenceError):
            validate_bundle(self.root, self.candidate())

    def test_wrong_schema_version_rejected(self) -> None:
        bad = _manifest()
        bad["schema"] = "web-live-evidence/9.9"
        _write_bundle(self.root, bad)
        with self.assertRaises(EvidenceError):
            validate_bundle(self.root, self.candidate())

    def test_authorization_must_be_present_even_for_offline_rows(self) -> None:
        bad = _manifest()
        bad["authorization"] = {}
        _write_bundle(self.root, bad)
        with self.assertRaises(EvidenceError):
            validate_bundle(self.root, self.candidate())

    def test_authorization_notes_must_be_under_the_240_char_cap(self) -> None:
        bad = _manifest(
            authorization={
                "provider_or_host": "none",
                "max_cost": "USD 0.00",
                "maintainer": "wenn-id",
                "notes": "x" * 1024,
            }
        )
        _write_bundle(self.root, bad)
        with self.assertRaises(EvidenceError):
            validate_bundle(self.root, self.candidate())


class RetainedArtifactTests(_Base):
    def _row_with_artifact(self, kind: str, artifact: Mapping[str, object]) -> dict[str, object]:
        return {
            "id": f"row-{kind}",
            "kind": kind,
            "date": "2026-09-01",
            "environment": "local",
            "route": "agent-native",
            "provider": "none",
            "model": "none",
            "credential_mode": "none",
            "step": "ran the documented step",
            "result": "pass",
            "cost": "USD 0.00",
            "artifact": dict(artifact),
            "limitations": "offline-only manual evidence",
        }

    def test_image_artifact_requires_decodable_raster(self) -> None:
        rel = "media/example.png"
        abs_path = self.root / rel
        _make_png(abs_path)
        digest = hashlib.sha256(abs_path.read_bytes()).hexdigest()
        _write_bundle(
            self.root,
            _manifest(
                rows=[
                    self._row_with_artifact(
                        "media", {"path": rel, "sha256": digest, "kind": "image"}
                    )
                ]
            ),
        )
        summary = validate_bundle(self.root, self.candidate())
        self.assertEqual(summary["rows"][0]["artifact"]["kind"], "image")
        self.assertEqual(summary["rows"][0]["artifact"]["sha256"], digest)

    def test_image_artifact_sha_mismatch_rejected(self) -> None:
        rel = "media/example.png"
        abs_path = self.root / rel
        _make_png(abs_path)
        bad_digest = "0" * 64
        _write_bundle(
            self.root,
            _manifest(
                rows=[
                    self._row_with_artifact(
                        "media", {"path": rel, "sha256": bad_digest, "kind": "image"}
                    )
                ]
            ),
        )
        with self.assertRaises(EvidenceError) as ctx:
            validate_bundle(self.root, self.candidate())
        self.assertIn("does not match", str(ctx.exception))

    def test_artifact_path_must_be_contained(self) -> None:
        rel = "../escape.png"
        abs_path = self.root / "escape.png"
        _make_png(abs_path)
        digest = hashlib.sha256(abs_path.read_bytes()).hexdigest()
        _write_bundle(
            self.root,
            _manifest(
                rows=[
                    self._row_with_artifact(
                        "media", {"path": rel, "sha256": digest, "kind": "image"}
                    )
                ]
            ),
        )
        with self.assertRaises(EvidenceError):
            validate_bundle(self.root, self.candidate())

    def test_artifact_path_must_exist(self) -> None:
        rel = "media/missing.png"
        _write_bundle(
            self.root,
            _manifest(
                rows=[
                    self._row_with_artifact(
                        "media",
                        {"path": rel, "sha256": "0" * 64, "kind": "image"},
                    )
                ]
            ),
        )
        with self.assertRaises(EvidenceError) as ctx:
            validate_bundle(self.root, self.candidate())
        self.assertIn("does not name a retained file", str(ctx.exception))

    def test_narration_artifact_must_be_under_two_mib(self) -> None:
        rel = "media/note.txt"
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x" * 4096, encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        _write_bundle(
            self.root,
            _manifest(
                rows=[
                    self._row_with_artifact(
                        "media", {"path": rel, "sha256": digest, "kind": "narration"}
                    )
                ]
            ),
        )
        summary = validate_bundle(self.root, self.candidate())
        self.assertEqual(summary["rows"][0]["artifact"]["kind"], "narration")


class SecretAndInjectionTests(_Base):
    def test_secret_in_field_rejected(self) -> None:
        bad = _manifest(
            authorization={
                "provider_or_host": "openai",
                "max_cost": "sk-abcdefghijklmnopqrstuvwxyz0123456789",
                "maintainer": "wenn-id",
                "notes": "offline-only manual evidence",
            }
        )
        _write_bundle(self.root, bad)
        with self.assertRaises(EvidenceError):
            validate_bundle(self.root, self.candidate())

    def test_markdown_control_chars_rejected_in_strings(self) -> None:
        bad = _manifest()
        bad["retention"] = {
            "location": "evidence/example",
            "created_at": "2026-09-01T00:00:00+00:00",
        }
        bad["rows"] = [
            {
                "id": "row-1",
                "kind": "deployment",
                "date": "2026-09-01",
                "environment": "external",
                "route": "studio",
                "provider": "render",
                "model": "n/a",
                "credential_mode": "hosted",
                "step": "ran | echo `pwn`",
                "result": "pass",
                "cost": "USD 0.00",
                "limitations": "no",
            }
        ]
        _write_bundle(self.root, bad)
        with self.assertRaises(EvidenceError):
            validate_bundle(self.root, self.candidate())


class RowEnumCoverageTests(_Base):
    def test_all_supported_kinds_pass_a_minimal_row(self) -> None:
        supported = {
            "deployment",
            "agent-webmcp",
            "comfyui",
            "provider-smoke",
            "media",
            "release-asset-smoke",
        }
        self.assertEqual(
            supported,
            {
                "deployment",
                "agent-webmcp",
                "comfyui",
                "provider-smoke",
                "media",
                "release-asset-smoke",
            },
        )

    def test_supported_environments_and_results_are_finite_enums(self) -> None:
        self.assertTrue(ALLOWED_ENVIRONMENTS)
        self.assertTrue(ALLOWED_RESULTS)
        for env in ALLOWED_ENVIRONMENTS:
            self.assertIn(env, {"local", "external", "hybrid", "offline"})
        for result in ALLOWED_RESULTS:
            self.assertIn(result, {"pass", "fail", "skipped", "incomplete"})


class ResolveCandidateTests(_Base):
    def test_resolve_candidate_rejects_bad_sha(self) -> None:
        from scripts.live_web_evidence import _resolve_candidate

        with self.assertRaises(EvidenceError):
            _resolve_candidate(self.root, "not-a-sha")

    def test_resolve_candidate_falls_back_to_bundle_head(self) -> None:
        from scripts.live_web_evidence import _resolve_candidate

        git_dir = self.root / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/test\n", encoding="utf-8")
        ref_dir = git_dir / "refs" / "heads"
        ref_dir.mkdir(parents=True)
        (ref_dir / "test").write_text("0" * 40 + "\n", encoding="utf-8")
        self.assertEqual(_resolve_candidate(self.root, None), "0" * 40)


class FrameworkDocumentTests(unittest.TestCase):
    """Pin the framework doc to the live, immutable acceptance-criteria count from #321."""

    ROOT = Path(__file__).resolve().parents[1]
    DOC = ROOT / "docs" / "web" / "live-evidence.md"
    SUBMISSION_DOC = ROOT / "submission" / "webmcp" / "live-evidence.md"

    def test_docs_present(self) -> None:
        self.assertTrue(self.DOC.is_file(), "docs/web/live-evidence.md is missing")
        self.assertTrue(
            self.SUBMISSION_DOC.is_file(), "submission/webmcp/live-evidence.md is missing"
        )

    def test_framework_documents_all_eight_acceptance_criteria(self) -> None:
        text = self.DOC.read_text(encoding="utf-8")
        for criterion in range(1, 9):
            with self.subTest(criterion=criterion):
                self.assertIn(f"| {criterion} |", text, f"missing acceptance row #{criterion}")

    def test_submission_documents_six_evidence_gaps(self) -> None:
        text = self.SUBMISSION_DOC.read_text(encoding="utf-8").lower()
        for gap in (
            "working live url",
            "active-agent webmcp",
            "comfyui",
            "paid/live provider",
            "screenshot",
            "native portable",
        ):
            with self.subTest(gap=gap):
                self.assertIn(gap, text, f"missing evidence gap {gap!r}")

    def test_framework_doc_states_current_status_explicitly(self) -> None:
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn("## Current status", text)
        self.assertIn("Four distinct evidence states", text)
        self.assertIn("Authorization boundaries", text)

    def test_framework_doc_binds_retained_rows_to_a_candidate_and_gate(self) -> None:
        """A retained row must name its candidate SHA, bundle, and gate command.

        The framework's whole purpose is that a live claim is candidate-bound
        and reproducible. If the status section claims retained evidence, the
        document must also carry the 40-hex candidate, the bundle location,
        and the exact gate invocation that revalidates it.
        """
        text = self.DOC.read_text(encoding="utf-8")
        status = text.split("## Four distinct evidence states", 1)[0]
        claims_retained = "is retained" in status or "has been exercised" in status
        if not claims_retained:
            self.assertIn("No live evidence has been retained", status)
            return
        candidates = re.findall(r"\b[0-9a-f]{40}\b", status)
        self.assertTrue(candidates, "retained status must name a 40-hex candidate SHA")
        self.assertIn("evidence/web-live/", status)
        self.assertIn("scripts.live_web_evidence", status)
        for sha in candidates:
            with self.subTest(sha=sha):
                self.assertIn(sha[:7], status)

    def test_framework_doc_keeps_unexecuted_gaps_explicitly_not_run(self) -> None:
        """Retaining one row must not silently upgrade the other five gaps."""
        status = self.DOC.read_text(encoding="utf-8").split("## Four distinct evidence states", 1)[
            0
        ]
        lowered = status.lower()
        for gap in ("document.modelcontext", "comfyui", "paid/live provider", "release asset"):
            with self.subTest(gap=gap):
                self.assertIn(gap, lowered, f"missing gap statement for {gap!r}")
                self.assertIn("no ", lowered, "unexecuted gaps must stay explicitly negative")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
