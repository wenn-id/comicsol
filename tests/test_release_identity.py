import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.release_identity import (
    IdentityError,
    github_ref_pattern_matches,
    main,
    prepare_identity,
    validate_release_metadata,
    validate_remote_refs,
    validate_tag_rulesets,
)


TAG = "v2.0.0rc6"
VERSION = "2.0.0rc6"


class GitFixture:
    def __init__(self, root: Path):
        self.repository = root / "repository"
        self.repository.mkdir()
        self.git("init", "--initial-branch=main")
        self.git("config", "user.name", "Release Test")
        self.git("config", "user.email", "release-test@example.invalid")
        self.commit("main.txt", "main\n", "main candidate")

    def git(self, *arguments: str, check: bool = True) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if check and result.returncode != 0:
            raise AssertionError(result.stderr)
        return result.stdout.strip()

    def commit(self, name: str, contents: str, message: str) -> str:
        (self.repository / name).write_text(contents, encoding="utf-8")
        self.git("add", name)
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD")

    def annotated_tag(self, target: str = "HEAD", message: str = "release") -> tuple[str, str]:
        self.git("tag", "-a", TAG, target, "-m", message)
        return self.git("rev-parse", TAG), self.git("rev-parse", f"{TAG}^{{commit}}")

    def refs(self) -> str:
        return self.git(
            "ls-remote",
            "--tags",
            str(self.repository),
            f"refs/tags/{TAG}",
            f"refs/tags/{TAG}^{{}}",
        )


def verified_metadata(tag_object_sha: str, candidate_commit: str) -> dict[str, object]:
    return {
        "tag": TAG,
        "sha": tag_object_sha,
        "object": {"type": "commit", "sha": candidate_commit},
        "verification": {
            "verified": True,
            "reason": "valid",
            "signature": "-----BEGIN PGP SIGNATURE-----\ntest\n-----END PGP SIGNATURE-----",
        },
    }


class ReleaseIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = GitFixture(Path(self.temporary.name))

    def prepare(self, **overrides):
        tag_object_sha = self.fixture.git("rev-parse", TAG)
        candidate_commit = self.fixture.git("rev-parse", f"{TAG}^{{commit}}")
        arguments = {
            "repository": self.fixture.repository,
            "tag": TAG,
            "event_ref": f"refs/tags/{TAG}",
            "event_sha": tag_object_sha,
            "checkout_sha": candidate_commit,
            "package_version": VERSION,
            "main_ref": "refs/heads/main",
            "tag_api_metadata": verified_metadata(tag_object_sha, candidate_commit),
        }
        arguments.update(overrides)
        return prepare_identity(**arguments)

    def test_prepare_captures_annotated_tag_candidate_and_protected_main(self):
        tag_object_sha, candidate_commit = self.fixture.annotated_tag()
        main_sha = self.fixture.commit("later.txt", "later\n", "main advanced")

        identity = self.prepare()

        self.assertEqual(
            {
                "candidate_commit": candidate_commit,
                "main_sha": main_sha,
                "tag": TAG,
                "tag_object_sha": tag_object_sha,
                "version": VERSION,
            },
            identity,
        )

    def test_prepare_rejects_tagged_commit_not_on_protected_main(self):
        self.fixture.git("switch", "-c", "release-side")
        candidate_commit = self.fixture.commit("side.txt", "side\n", "side candidate")
        tag_object_sha, _ = self.fixture.annotated_tag()
        self.fixture.git("switch", "main")

        with self.assertRaisesRegex(IdentityError, "not an ancestor of protected main"):
            self.prepare(event_sha=tag_object_sha, checkout_sha=candidate_commit)

    def test_remote_rejects_replaced_tag_object_even_when_peeled_commit_is_same(self):
        original_object, candidate_commit = self.fixture.annotated_tag(message="first object")
        self.fixture.git("tag", "-d", TAG)
        replacement_object, replacement_commit = self.fixture.annotated_tag(message="second object")
        self.assertNotEqual(original_object, replacement_object)
        self.assertEqual(candidate_commit, replacement_commit)

        with self.assertRaisesRegex(IdentityError, "tag object changed"):
            validate_remote_refs(
                self.fixture.refs(),
                tag=TAG,
                tag_object_sha=original_object,
                candidate_commit=candidate_commit,
            )

    def test_remote_rejects_tag_moved_to_another_commit(self):
        original_object, candidate_commit = self.fixture.annotated_tag()
        moved_commit = self.fixture.commit("moved.txt", "moved\n", "different target")
        self.fixture.git("tag", "-d", TAG)
        self.fixture.annotated_tag(target=moved_commit, message="moved object")

        with self.assertRaisesRegex(IdentityError, "tag object changed"):
            validate_remote_refs(
                self.fixture.refs(),
                tag=TAG,
                tag_object_sha=original_object,
                candidate_commit=candidate_commit,
            )

    def test_remote_rejects_deleted_tag(self):
        tag_object_sha, candidate_commit = self.fixture.annotated_tag()
        self.fixture.git("tag", "-d", TAG)

        with self.assertRaisesRegex(IdentityError, "exactly one direct ref"):
            validate_remote_refs(
                self.fixture.refs(),
                tag=TAG,
                tag_object_sha=tag_object_sha,
                candidate_commit=candidate_commit,
            )

    def test_lightweight_tag_is_rejected_locally_and_remotely(self):
        self.fixture.git("tag", TAG)
        candidate_commit = self.fixture.git("rev-parse", TAG)
        metadata = verified_metadata(candidate_commit, candidate_commit)
        with self.assertRaisesRegex(IdentityError, "annotated tag object"):
            self.prepare(tag_api_metadata=metadata)
        with self.assertRaisesRegex(IdentityError, "peeled annotated ref"):
            validate_remote_refs(
                self.fixture.refs(),
                tag=TAG,
                tag_object_sha=candidate_commit,
                candidate_commit=candidate_commit,
            )

    def test_version_mismatch_and_noncanonical_tag_are_rejected(self):
        self.fixture.annotated_tag()
        with self.assertRaisesRegex(IdentityError, "does not match package version"):
            self.prepare(package_version="2.0.0rc5")
        with self.assertRaisesRegex(IdentityError, "invalid release tag"):
            self.prepare(tag="release-2.0.0rc6")

    def test_event_and_checkout_identity_mismatches_are_rejected(self):
        tag_object_sha, candidate_commit = self.fixture.annotated_tag()
        other_commit = self.fixture.commit("other.txt", "other\n", "other")
        with self.assertRaisesRegex(IdentityError, "event ref"):
            self.prepare(event_ref="refs/tags/v2.0.0rc5")
        with self.assertRaisesRegex(IdentityError, "event SHA"):
            self.prepare(event_sha=other_commit)
        with self.assertRaisesRegex(IdentityError, "checkout"):
            self.prepare(checkout_sha=other_commit)
        self.assertNotEqual(tag_object_sha, candidate_commit)

    def test_release_uses_exact_tag_and_resolved_refs_not_raw_target_commitish(self):
        tag_object_sha, candidate_commit = self.fixture.annotated_tag()
        remote_refs = self.fixture.refs()
        # GitHub reports the default branch for Releases created from an existing tag;
        # target_commitish is not the authoritative target in that API shape.
        existing_tag_release = {
            "tag_name": TAG,
            "target_commitish": "main",
            "draft": True,
        }
        self.assertEqual(
            {
                "candidate_commit": candidate_commit,
                "tag": TAG,
                "tag_object_sha": tag_object_sha,
            },
            validate_release_metadata(
                existing_tag_release,
                remote_refs,
                tag=TAG,
                tag_object_sha=tag_object_sha,
                candidate_commit=candidate_commit,
            ),
        )
        with self.assertRaisesRegex(IdentityError, "tag_name"):
            validate_release_metadata(
                {**existing_tag_release, "tag_name": "v2.0.0rc5"},
                remote_refs,
                tag=TAG,
                tag_object_sha=tag_object_sha,
                candidate_commit=candidate_commit,
            )
        with self.assertRaisesRegex(IdentityError, "candidate commit changed"):
            validate_release_metadata(
                existing_tag_release,
                remote_refs.replace(candidate_commit, "0" * 40),
                tag=TAG,
                tag_object_sha=tag_object_sha,
                candidate_commit=candidate_commit,
            )

    def test_release_qualification_state_must_be_published_prerelease_and_immutable(self):
        tag_object_sha, candidate_commit = self.fixture.annotated_tag()
        remote_refs = self.fixture.refs()
        valid = {
            "tag_name": TAG,
            "target_commitish": "main",
            "draft": False,
            "prerelease": True,
            "immutable": True,
        }
        self.assertEqual(
            {
                "candidate_commit": candidate_commit,
                "tag": TAG,
                "tag_object_sha": tag_object_sha,
                "release_state": {
                    "draft": False,
                    "prerelease": True,
                    "immutable": True,
                },
            },
            validate_release_metadata(
                valid,
                remote_refs,
                tag=TAG,
                tag_object_sha=tag_object_sha,
                candidate_commit=candidate_commit,
                require_immutable_prerelease=True,
            ),
        )
        for field, value in (("draft", True), ("prerelease", False), ("immutable", False)):
            with self.subTest(field=field):
                with self.assertRaisesRegex(IdentityError, "published immutable prerelease"):
                    validate_release_metadata(
                        {**valid, field: value},
                        remote_refs,
                        tag=TAG,
                        tag_object_sha=tag_object_sha,
                        candidate_commit=candidate_commit,
                        require_immutable_prerelease=True,
                    )

    def test_tag_api_embedded_name_must_match_release_ref(self):
        tag_object_sha, candidate_commit = self.fixture.annotated_tag()
        wrong_name = verified_metadata(tag_object_sha, candidate_commit)
        wrong_name["tag"] = "v2.0.0rc5"

        with self.assertRaisesRegex(IdentityError, "embedded name"):
            self.prepare(tag_api_metadata=wrong_name)

    def test_unsigned_and_unverified_tag_api_metadata_are_rejected(self):
        tag_object_sha, candidate_commit = self.fixture.annotated_tag()
        unsigned = verified_metadata(tag_object_sha, candidate_commit)
        unsigned["verification"] = {
            "verified": False,
            "reason": "unsigned",
            "signature": None,
        }
        with self.assertRaisesRegex(IdentityError, "not verified: unsigned"):
            self.prepare(tag_api_metadata=unsigned)

        missing_signature = verified_metadata(tag_object_sha, candidate_commit)
        missing_signature["verification"] = {
            "verified": True,
            "reason": "valid",
            "signature": None,
        }
        with self.assertRaisesRegex(IdentityError, "missing its signature"):
            self.prepare(tag_api_metadata=missing_signature)

    def test_cli_emits_canonical_json_and_fails_without_traceback(self):
        tag_object_sha, candidate_commit = self.fixture.annotated_tag()
        refs_file = Path(self.temporary.name) / "refs.txt"
        refs_file.write_text(self.fixture.refs() + "\n", encoding="utf-8")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = main(
                [
                    "remote",
                    "--refs-file",
                    str(refs_file),
                    "--tag",
                    TAG,
                    "--tag-object-sha",
                    tag_object_sha,
                    "--candidate-commit",
                    candidate_commit,
                ]
            )
        self.assertEqual(0, result)
        self.assertEqual(
            json.dumps(
                {
                    "candidate_commit": candidate_commit,
                    "tag": TAG,
                    "tag_object_sha": tag_object_sha,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            stdout.getvalue(),
        )

        self.fixture.git("tag", "-d", TAG)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = main(
                [
                    "remote",
                    "--refs-file",
                    str(refs_file.with_name("missing.txt")),
                    "--tag",
                    TAG,
                    "--tag-object-sha",
                    tag_object_sha,
                    "--candidate-commit",
                    candidate_commit,
                ]
            )
        self.assertEqual(1, result)
        self.assertIn("release identity rejected", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


class TagRulesetPolicyTests(unittest.TestCase):
    @staticmethod
    def ruleset(
        ruleset_id: int,
        *,
        include: list[str],
        exclude: list[str] | None = None,
        rules: tuple[str, ...] = (),
        bypass_actors: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        return {
            "id": ruleset_id,
            "target": "tag",
            "enforcement": "active",
            "conditions": {
                "ref_name": {"include": include, "exclude": exclude or []},
            },
            "rules": [{"type": rule_type} for rule_type in rules],
            "bypass_actors": bypass_actors or [],
        }

    @staticmethod
    def admin_bypass() -> dict[str, object]:
        return {
            "actor_type": "RepositoryRole",
            "actor_id": 5,
            "bypass_mode": "always",
        }

    def test_github_pathname_matching_does_not_let_star_cross_slash(self):
        release_ref = "refs/tags/v2.0.0"
        self.assertFalse(github_ref_pattern_matches("refs/*", release_ref))
        self.assertTrue(github_ref_pattern_matches("refs/tags/v*", release_ref))
        self.assertTrue(github_ref_pattern_matches("refs/**/v*", release_ref))
        self.assertTrue(github_ref_pattern_matches("refs/**/v*", "refs/a/b/v2.0.0"))
        self.assertTrue(github_ref_pattern_matches("refs/**/v*", "refs/v2.0.0"))
        self.assertFalse(github_ref_pattern_matches("refs/**", release_ref))
        self.assertTrue(github_ref_pattern_matches("refs/**", "refs/tags"))
        self.assertTrue(github_ref_pattern_matches("refs/tags/**", release_ref))
        self.assertFalse(github_ref_pattern_matches("refs/tags/**", "refs/tags/nested/v2.0.0"))
        self.assertTrue(github_ref_pattern_matches("refs/tags/**/*", release_ref))
        self.assertTrue(github_ref_pattern_matches("refs/tags/**/*", "refs/tags/nested/v2.0.0"))

    def test_unsupported_and_malformed_patterns_fail_closed(self):
        for pattern in ("tags/v*", "refs/tags/[v*", "refs/tags/v***", "refs/tags/"):
            with self.subTest(pattern=pattern):
                with self.assertRaises(IdentityError):
                    github_ref_pattern_matches(pattern, "refs/tags/v2.0.0")

    def test_excludes_win_even_when_include_matches(self):
        ruleset = self.ruleset(
            7,
            include=["refs/tags/v*"],
            exclude=["refs/tags/v2.*"],
            rules=("creation", "update", "deletion", "required_signatures"),
            bypass_actors=[self.admin_bypass()],
        )
        with self.assertRaisesRegex(IdentityError, "no active tag ruleset matches"):
            validate_tag_rulesets([ruleset], release_ref="refs/tags/v2.0.0")

    def test_required_rules_are_aggregated_across_all_matching_rulesets(self):
        rulesets = [
            self.ruleset(
                22,
                include=["refs/tags/v*"],
                rules=("creation", "required_signatures"),
                bypass_actors=[self.admin_bypass()],
            ),
            self.ruleset(
                11,
                include=["refs/**/v*"],
                rules=("update", "deletion"),
            ),
        ]
        self.assertEqual(
            {
                "matched_ruleset_ids": [11, 22],
                "approved_bypass_actors": [self.admin_bypass()],
            },
            validate_tag_rulesets(rulesets, release_ref="refs/tags/v2.0.0"),
        )

    def test_unapproved_or_malformed_bypass_actor_fails_closed(self):
        base = {
            "include": ["refs/tags/v*"],
            "rules": ("creation", "update", "deletion", "required_signatures"),
        }
        actors = (
            {"actor_type": "RepositoryRole", "actor_id": 4, "bypass_mode": "always"},
            {"actor_type": "RepositoryRole", "actor_id": 2, "bypass_mode": "always"},
            {"actor_type": "Team", "actor_id": 5, "bypass_mode": "always"},
            {"actor_type": "Integration", "actor_id": 5, "bypass_mode": "always"},
            {"actor_type": "RepositoryRole", "actor_id": 5, "bypass_mode": "pull_request"},
            {"actor_type": "RepositoryRole", "actor_id": 5},
        )
        for actor in actors:
            with self.subTest(actor=actor):
                ruleset = self.ruleset(7, bypass_actors=[actor], **base)
                with self.assertRaisesRegex(IdentityError, "unapproved bypass actor"):
                    validate_tag_rulesets([ruleset], release_ref="refs/tags/v2.0.0")

    def test_creation_restriction_requires_approved_admin_bypass(self):
        ruleset = self.ruleset(
            7,
            include=["refs/tags/v*"],
            rules=("creation", "update", "deletion", "required_signatures"),
        )
        with self.assertRaisesRegex(IdentityError, "repository-admin bypass"):
            validate_tag_rulesets([ruleset], release_ref="refs/tags/v2.0.0")

    def test_every_layered_creation_restriction_requires_approved_admin_bypass(self):
        rulesets = [
            self.ruleset(
                7,
                include=["refs/tags/v*"],
                rules=("creation", "update", "required_signatures"),
                bypass_actors=[self.admin_bypass()],
            ),
            self.ruleset(
                8,
                include=["refs/**/v*"],
                rules=("creation", "deletion"),
            ),
        ]
        with self.assertRaisesRegex(
            IdentityError,
            "creation-restriction ruleset 8.*exactly.*repository-admin bypass",
        ):
            validate_tag_rulesets(rulesets, release_ref="refs/tags/v2.0.0")


if __name__ == "__main__":
    unittest.main()
