import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from tests.consistency_benchmark import scorecard_template

from scripts.live_visual_evidence import (
    CANONICAL_PANELS,
    EXPECTED_GROUPS,
    KIND,
    REQUIRED_MATERIAL_CHANGES,
    REQUIRED_QUALITY_REVIEWS,
    EvidenceError,
    main,
    validate_evidence,
    write_evidence_archive,
)


COMMIT = "a" * 40
VERSION = "2.0.0rc6"
REVIEWED_AT = "2026-08-22T10:00:00Z"


class LiveVisualEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.addCleanup(self.temporary_directory.cleanup)

    def _artifact(self, relative, payload):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return {"path": relative, "sha256": hashlib.sha256(payload).hexdigest()}

    def _png(self, relative, color):
        stream = io.BytesIO()
        Image.new("RGB", (64, 64), color).save(stream, format="PNG")
        return self._artifact(relative, stream.getvalue())

    def _scorecard(self, score=4):
        scorecard = scorecard_template()
        for panel in scorecard["panels"].values():
            for scores in panel["characters"].values():
                for dimension in scores:
                    scores[dimension] = score
        scorecard["review"].update(
            {
                "engine_version": VERSION,
                "evidence_mode": "live-visual",
                "provider": "example provider",
                "model": "example image model",
                "reviewer": "release visual reviewer",
                "method": "bounded human review against the fixed benchmark",
                "reviewed_at": REVIEWED_AT,
            }
        )
        return scorecard

    def _manifest(self):
        scorecard_payload = json.dumps(
            self._scorecard(), ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        scorecard = self._artifact("scorecard.json", scorecard_payload)
        attestation = self._artifact("reviewer-attestation.json", b'{"approved":true}\n')
        attempts = []
        panel_attempts = {}
        for index, panel_id in enumerate(sorted(CANONICAL_PANELS)):
            attempt_id = f"{panel_id}-0"
            panel_attempts[panel_id] = attempt_id
            attempts.append(
                {
                    "id": attempt_id,
                    "panel_id": panel_id,
                    "sequence": 0,
                    "kind": "initial",
                    "outcome": "accepted",
                    "raster": self._png(
                        f"attempts/{attempt_id}.png", ((index * 17) % 256, 80, 120)
                    ),
                }
            )
        changes = []
        for index, change_id in enumerate(sorted(REQUIRED_MATERIAL_CHANGES)):
            changes.append(
                {
                    "id": change_id,
                    "summary": f"Reviewed material quality change {change_id}",
                    "before": self._png(f"renders/{change_id}-before.png", (index, 10, 20)),
                    "after": self._png(f"renders/{change_id}-after.png", (index, 30, 40)),
                    "decision": "improved",
                }
            )
        accepted_ids = sorted(panel_attempts.values())
        return {
            "schema_version": "1.0",
            "kind": KIND,
            "candidate": {
                "engine_version": VERSION,
                "commit_sha": COMMIT,
                "milestone": "v2.2",
            },
            "provenance": {
                "evidence_mode": "live-visual",
                "provider": "example provider",
                "model": "example image model",
                "reviewer": "release visual reviewer",
                "method": "bounded human review against the fixed benchmark",
                "reviewed_at": REVIEWED_AT,
                "approval": "approved",
            },
            "reviewer_attestation": attestation,
            "material_changes": changes,
            "character_consistency": {
                "scorecard": scorecard,
                "scored_dimensions": 105,
                "total_dimensions": 105,
                "overall_mean": 4.0,
                "minimum_score": 4,
                "group_means": {
                    axis: {name: 4.0 for name in sorted(names)}
                    for axis, names in EXPECTED_GROUPS.items()
                },
                "panel_attempts": panel_attempts,
            },
            "attempts": attempts,
            "defects": [],
            "accepted_warnings": [],
            "quality_reviews": {
                category: {
                    "result": "pass",
                    "evidence": f"Reviewed {category} on every accepted panel raster",
                    "warning_id": None,
                    "attempt_ids": accepted_ids,
                }
                for category in REQUIRED_QUALITY_REVIEWS
            },
            "limitations": [
                "One provider, one model, and one reviewer do not establish universal quality."
            ],
        }

    def _validate(self, manifest, **overrides):
        options = {
            "expected_commit": COMMIT,
            "expected_engine_version": VERSION,
            "expected_reviewer_attestation_sha256": manifest["reviewer_attestation"]["sha256"],
        }
        options.update(overrides)
        return validate_evidence(manifest, self.root, **options)

    def _replace_scorecard(self, manifest, score, *, missing=False):
        scorecard = self._scorecard(score)
        if missing:
            scorecard["panels"]["p01-01"]["characters"]["rani"]["face"] = None
        payload = json.dumps(scorecard, ensure_ascii=False, sort_keys=True).encode("utf-8")
        manifest["character_consistency"]["scorecard"] = self._artifact("scorecard.json", payload)
        manifest["character_consistency"]["scored_dimensions"] = 104 if missing else 105
        manifest["character_consistency"]["total_dimensions"] = 105
        manifest["character_consistency"]["overall_mean"] = float(score)
        manifest["character_consistency"]["minimum_score"] = score
        manifest["character_consistency"]["group_means"] = {
            axis: {name: float(score) for name in sorted(names)}
            for axis, names in EXPECTED_GROUPS.items()
        }

    def test_complete_candidate_bound_bundle_is_approved(self):
        summary = self._validate(self._manifest())
        self.assertEqual("PROMOTION APPROVED", summary["decision"])
        self.assertEqual(105, summary["threshold"]["expected_scores"])
        self.assertEqual(12, len(summary["attempts"]))
        self.assertEqual(10, len(summary["material_changes"]))

    def test_trusted_candidate_and_reviewer_attestation_are_required(self):
        manifest = self._manifest()
        with self.assertRaisesRegex(EvidenceError, "trusted expected commit"):
            self._validate(manifest, expected_commit="b" * 40)
        with self.assertRaisesRegex(EvidenceError, "trusted expected version"):
            self._validate(manifest, expected_engine_version="2.0.0rc5")
        with self.assertRaisesRegex(EvidenceError, "trusted expected attestation"):
            self._validate(manifest, expected_reviewer_attestation_sha256="0" * 64)

    def test_scorecard_is_derived_complete_and_above_threshold(self):
        manifest = self._manifest()
        self._replace_scorecard(manifest, 4, missing=True)
        with self.assertRaisesRegex(EvidenceError, "105/105"):
            self._validate(manifest)
        manifest = self._manifest()
        self._replace_scorecard(manifest, 3)
        with self.assertRaisesRegex(EvidenceError, "at least 3.5"):
            self._validate(manifest)
        manifest = self._manifest()
        manifest["character_consistency"]["overall_mean"] = 3.9
        with self.assertRaisesRegex(EvidenceError, "does not match"):
            self._validate(manifest)

    def test_visual_artifacts_are_real_unique_pngs_and_cover_changes(self):
        manifest = self._manifest()
        manifest["material_changes"].pop()
        with self.assertRaisesRegex(EvidenceError, "every v2.2 material change"):
            self._validate(manifest)
        manifest = self._manifest()
        broken = self._artifact("renders/not-an-image.png", b"not a raster")
        manifest["material_changes"][0]["before"] = broken
        with self.assertRaisesRegex(EvidenceError, "decodable PNG"):
            self._validate(manifest)
        manifest = self._manifest()
        manifest["material_changes"][0]["after"] = manifest["material_changes"][0]["before"]
        with self.assertRaisesRegex(EvidenceError, "must differ"):
            self._validate(manifest)

    def test_every_scorecard_panel_binds_one_accepted_raster(self):
        manifest = self._manifest()
        manifest["attempts"].pop()
        with self.assertRaisesRegex(EvidenceError, "one accepted raster"):
            self._validate(manifest)
        manifest = self._manifest()
        manifest["character_consistency"]["panel_attempts"]["p01-01"] = "p01-02-0"
        with self.assertRaisesRegex(EvidenceError, "each scorecard panel"):
            self._validate(manifest)

    def test_repair_links_rejected_source_to_later_distinct_same_panel_retry(self):
        manifest = self._manifest()
        source = next(item for item in manifest["attempts"] if item["panel_id"] == "p01-01")
        source["outcome"] = "rejected"
        retry = {
            "id": "p01-01-1",
            "panel_id": "p01-01",
            "sequence": 1,
            "kind": "selective-repair",
            "outcome": "accepted",
            "raster": self._png("attempts/p01-01-1.png", (240, 30, 40)),
        }
        manifest["attempts"].append(retry)
        manifest["character_consistency"]["panel_attempts"]["p01-01"] = retry["id"]
        for review in manifest["quality_reviews"].values():
            review["attempt_ids"] = [
                retry["id"] if item == source["id"] else item for item in review["attempt_ids"]
            ]
        manifest["defects"] = [
            {
                "id": "defect-1",
                "category": "anatomy",
                "severity": "error",
                "attempt_id": source["id"],
                "observation": "The initial hand anatomy was malformed.",
                "repair": "Regenerated the bounded hand region.",
                "retry_attempt_id": retry["id"],
                "resolution": "repaired",
                "reviewer_decision": "The retained retry resolves the defect.",
            }
        ]
        self.assertEqual("repaired", self._validate(manifest)["defects"][0]["resolution"])
        manifest["defects"][0]["retry_attempt_id"] = source["id"]
        with self.assertRaisesRegex(EvidenceError, "later distinct accepted retry"):
            self._validate(manifest)

    def test_unknown_sensitive_fields_and_markdown_injection_are_blocked(self):
        manifest = self._manifest()
        manifest["provenance"]["client_secret"] = "hidden"
        with self.assertRaisesRegex(EvidenceError, "unknown=.*client_secret"):
            self._validate(manifest)
        manifest = self._manifest()
        manifest["limitations"][0] += "\n# forged approval"
        with self.assertRaisesRegex(EvidenceError, "Markdown control"):
            self._validate(manifest)
        manifest = self._manifest()
        manifest["limitations"] = ["No limitations"]
        with self.assertRaisesRegex(EvidenceError, "provider, model, and reviewer"):
            self._validate(manifest)

    def test_public_archive_is_deterministic_and_excludes_unreferenced_files(self):
        manifest = self._manifest()
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        (self.root / "private-provider-response.json").write_text("private", encoding="utf-8")
        summary = self._validate(manifest)
        first = self.root / "first.tar.gz"
        second = self.root / "second.tar.gz"
        write_evidence_archive(summary, manifest, manifest_path, first)
        write_evidence_archive(summary, manifest, manifest_path, second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with tarfile.open(first, "r:gz") as archive:
            names = set(archive.getnames())
        self.assertIn("v2.2-live-visual-evidence/manifest.json", names)
        self.assertIn("v2.2-live-visual-evidence/scorecard.json", names)
        self.assertIn("v2.2-live-visual-evidence/reviewer-attestation.json", names)
        self.assertNotIn("v2.2-live-visual-evidence/private-provider-response.json", names)

        manifest_path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(EvidenceError, "manifest changed after validation"):
            write_evidence_archive(summary, manifest, manifest_path, self.root / "manifest.tar.gz")
        manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

        conflicting = json.loads(json.dumps(summary))
        conflicting["material_changes"][0]["after"]["path"] = conflicting["material_changes"][0][
            "before"
        ]["path"]
        with self.assertRaisesRegex(EvidenceError, "conflicting digests"):
            write_evidence_archive(
                conflicting, manifest, manifest_path, self.root / "conflicting.tar.gz"
            )

        escaping = json.loads(json.dumps(summary))
        escaping["material_changes"][0]["before"]["path"] = "../outside.png"
        with self.assertRaisesRegex(EvidenceError, "escapes the bundle"):
            write_evidence_archive(escaping, manifest, manifest_path, self.root / "escaping.tar.gz")

        (self.root / "scorecard.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(EvidenceError, "changed after validation"):
            write_evidence_archive(summary, manifest, manifest_path, self.root / "changed.tar.gz")

    def test_cli_publishes_json_and_markdown_or_fails_closed(self):
        manifest = self._manifest()
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        json_output = self.root / "published/summary.json"
        markdown_output = self.root / "published/summary.md"
        archive_output = self.root / "published/evidence.tar.gz"
        common = [
            "--manifest",
            str(manifest_path),
            "--expected-commit",
            COMMIT,
            "--expected-engine-version",
            VERSION,
            "--expected-reviewer-attestation-sha256",
            manifest["reviewer_attestation"]["sha256"],
            "--json-output",
            str(json_output),
            "--markdown-output",
            str(markdown_output),
            "--archive-output",
            str(archive_output),
        ]
        self.assertEqual(0, main(common))
        self.assertTrue(archive_output.is_file())
        self.assertEqual("PROMOTION APPROVED", json.loads(json_output.read_text())["decision"])
        manifest["provenance"]["approval"] = "pending"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(1, main(common))


if __name__ == "__main__":
    unittest.main()
