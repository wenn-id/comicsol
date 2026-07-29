import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from comic_sol_product.distribution import (
    ReleaseIdentity,
    artifact_name,
    verify_release_directory,
    write_checksums,
    write_release_metadata,
    write_sbom,
)


class NativeDistributionContractTests(unittest.TestCase):
    def setUp(self):
        self.identity = ReleaseIdentity(
            version="2.0.0rc1", platform="linux", architecture="x86_64"
        )

    def test_identity_and_artifact_names_are_canonical(self):
        self.assertEqual("v2.0.0rc1", self.identity.tag)
        self.assertEqual(
            "comic-sol-2.0.0rc1-linux-x86_64.tar.gz",
            artifact_name(self.identity, "tar.gz"),
        )
        with self.assertRaises(ValueError):
            ReleaseIdentity("2.0.0-rc1", "linux", "x86_64")
        with self.assertRaises(ValueError):
            ReleaseIdentity("2.0.0rc1", "Linux", "amd64")

    def test_metadata_checksum_and_sbom_are_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            release = Path(temporary_directory)
            first = release / artifact_name(self.identity, "tar.gz")
            second = release / "comic-sol-2.0.0rc1-linux-x86_64.sbom.json"
            first.write_bytes(b"portable-runtime")
            second.write_text("{}\n", encoding="utf-8")

            metadata = write_release_metadata(release, self.identity, [first.name])
            checksums = write_checksums(release, [second, first])
            sbom = write_sbom(release, self.identity)

            metadata_record = json.loads(metadata.read_text(encoding="utf-8"))
            self.assertEqual("unsigned", metadata_record["signature_status"])
            self.assertEqual([first.name], metadata_record["artifacts"])
            self.assertNotIn(str(release), metadata.read_text(encoding="utf-8"))

            checksum_lines = checksums.read_text(encoding="utf-8").splitlines()
            self.assertEqual(sorted(checksum_lines), checksum_lines)
            expected = hashlib.sha256(first.read_bytes()).hexdigest()
            self.assertIn(f"{expected}  {first.name}", checksum_lines)

            sbom_record = json.loads(sbom.read_text(encoding="utf-8"))
            self.assertEqual("CycloneDX", sbom_record["bomFormat"])
            self.assertEqual("1.6", sbom_record["specVersion"])
            self.assertEqual("comic-sol", sbom_record["metadata"]["component"]["name"])
            self.assertEqual("2.0.0rc1", sbom_record["metadata"]["component"]["version"])

    def test_verifier_rejects_missing_or_tampered_artifact(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            release = Path(temporary_directory)
            artifact = release / artifact_name(self.identity, "tar.gz")
            artifact.write_bytes(b"original")
            write_release_metadata(release, self.identity, [artifact.name])
            write_sbom(release, self.identity)
            write_checksums(
                release,
                [artifact, release / "comic-sol-2.0.0rc1-linux-x86_64.sbom.json"],
            )
            verify_release_directory(release, self.identity)

            artifact.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                verify_release_directory(release, self.identity)

            artifact.unlink()
            with self.assertRaisesRegex(ValueError, "missing artifact"):
                verify_release_directory(release, self.identity)


if __name__ == "__main__":
    unittest.main()
