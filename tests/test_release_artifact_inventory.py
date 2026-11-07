import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.release_artifact_inventory import EXPECTED_ARTIFACTS, compute_inventory, main


def valid_artifact(name: str, artifact_id: int) -> dict[str, object]:
    return {
        "id": artifact_id,
        "name": name,
        "digest": f"sha256:{artifact_id:064x}",
        "expired": False,
        "url": f"https://api.github.com/repos/wenn-id/comicsol/actions/artifacts/{artifact_id}",
        "archive_download_url": (
            f"https://api.github.com/repos/wenn-id/comicsol/actions/artifacts/{artifact_id}/zip"
        ),
    }


def valid_response() -> dict[str, object]:
    return {
        "artifacts": [
            valid_artifact(name, index)
            for index, name in enumerate(sorted(EXPECTED_ARTIFACTS), start=1)
        ]
    }


class ReleaseArtifactInventoryTests(unittest.TestCase):
    def test_inventory_is_complete_sorted_and_order_independent(self):
        response = valid_response()
        expected = sorted(EXPECTED_ARTIFACTS)
        inventory, digest = compute_inventory(response)
        reversed_inventory, reversed_digest = compute_inventory(
            {"artifacts": list(reversed(response["artifacts"]))}
        )

        self.assertEqual(expected, [item["name"] for item in inventory])
        self.assertEqual(inventory, reversed_inventory)
        self.assertEqual(digest, reversed_digest)
        self.assertEqual(
            hashlib.sha256(
                json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            digest,
        )

    def test_unrelated_artifacts_do_not_change_expected_inventory(self):
        response = valid_response()
        response["artifacts"].append({"name": "coverage-report"})

        inventory, digest = compute_inventory(response)

        self.assertEqual(len(EXPECTED_ARTIFACTS), len(inventory))
        self.assertEqual(64, len(digest))

    def test_rejects_invalid_roots_and_artifact_records(self):
        for response in (None, [], {"artifacts": None}, {"artifacts": [None]}):
            with self.subTest(response=response):
                with self.assertRaises(ValueError):
                    compute_inventory(response)

        response = valid_response()
        response["artifacts"][0]["name"] = None
        with self.assertRaisesRegex(ValueError, "name is invalid"):
            compute_inventory(response)

    def test_rejects_missing_and_duplicate_artifact_names(self):
        response = valid_response()
        response["artifacts"].pop()
        with self.assertRaisesRegex(ValueError, "artifact mismatch"):
            compute_inventory(response)

        response = valid_response()
        response["artifacts"].append(dict(response["artifacts"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate Actions artifact"):
            compute_inventory(response)

    def test_rejects_invalid_ids_digest_expiry_and_urls(self):
        cases = (
            ("id", True, "ID is invalid"),
            ("id", 0, "ID is invalid"),
            ("digest", "sha256:not-hex", "digest is invalid"),
            ("expired", True, "expired or has unknown state"),
            ("expired", None, "expired or has unknown state"),
            ("url", "http://api.github.com/artifact", "URL is invalid"),
            (
                "archive_download_url",
                "http://api.github.com/artifact.zip",
                "archive URL is invalid",
            ),
        )
        for field, value, message in cases:
            with self.subTest(field=field, value=value):
                response = valid_response()
                response["artifacts"][0][field] = value
                with self.assertRaisesRegex(ValueError, message):
                    compute_inventory(response)

    def test_rejects_duplicate_artifact_ids(self):
        response = valid_response()
        response["artifacts"][1]["id"] = response["artifacts"][0]["id"]

        with self.assertRaisesRegex(ValueError, "IDs are not unique"):
            compute_inventory(response)

    def test_cli_writes_inventory_and_github_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.json"
            target = root / "output" / "identities.json"
            github_output = root / "github-output"
            source.write_text(json.dumps(valid_response()), encoding="utf-8")
            _, expected_digest = compute_inventory(valid_response())
            stdout = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_OUTPUT": str(github_output)}):
                with contextlib.redirect_stdout(stdout):
                    result = main(
                        [
                            "--input",
                            str(source),
                            "--output",
                            str(target),
                            "--expected-sha256",
                            expected_digest,
                        ]
                    )

            self.assertEqual(0, result)
            self.assertEqual(f"{expected_digest}\n", stdout.getvalue())
            self.assertEqual(
                sorted(EXPECTED_ARTIFACTS),
                [item["name"] for item in json.loads(target.read_text(encoding="utf-8"))],
            )
            self.assertEqual(
                f"artifact_inventory_sha256={expected_digest}\n",
                github_output.read_text(encoding="utf-8"),
            )

    def test_cli_rejects_mismatched_expected_digest_without_traceback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.json"
            target = root / "identities.json"
            source.write_text(json.dumps(valid_response()), encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = main(
                    [
                        "--input",
                        str(source),
                        "--output",
                        str(target),
                        "--expected-sha256",
                        "0" * 64,
                    ]
                )

            self.assertEqual(1, result)
            self.assertIn("changed after read-only validation", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertFalse(target.exists())

    def test_cli_rejects_malformed_expected_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.json"
            target = root / "identities.json"
            source.write_text(json.dumps(valid_response()), encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = main(
                    [
                        "--input",
                        str(source),
                        "--output",
                        str(target),
                        "--expected-sha256",
                        "invalid",
                    ]
                )

            self.assertEqual(1, result)
            self.assertIn(
                "expected Actions artifact inventory digest is invalid", stderr.getvalue()
            )


if __name__ == "__main__":
    unittest.main()
