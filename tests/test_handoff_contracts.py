import hashlib
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

import scripts.handoff as handoff

from scripts.core_primitives import canonical_artifact_bytes, canonical_json_bytes
from scripts.handoff import (
    HANDOFF_CONTRACT_VERSION,
    HandoffContractError,
    StaleLockedScopeError,
    assert_locked_scope,
    build_generation_batches,
    build_generation_job,
    build_generation_receipt,
    build_handoff_manifest,
    generation_job_sha256,
    locked_scope_sha256,
    rank_executors,
    validate_generation_batches,
    validate_generation_job,
    validate_generation_receipt,
    validate_handoff_manifest,
)
from scripts.project_io import ProjectLock


PROMPT_SHA256 = "a" * 64
REFERENCE_SHA256 = "b" * 64
RASTER_SHA256 = "c" * 64
BATCHES_SHA256 = "d" * 64
JOB_PATH_SHA256 = "e" * 64


def valid_job():
    return build_generation_job(
        subject_kind="panel",
        subject_id="p01-01",
        prompt_path="prompts/panels/p01-01.txt",
        prompt_sha256=PROMPT_SHA256,
        references=[
            {
                "path": "references/characters/mira.png",
                "sha256": REFERENCE_SHA256,
            }
        ],
        requested_dimensions={"width": 736, "height": 1136},
        requested_aspect_ratio="46:71",
        attempt_kind="initial",
        retry_limit=2,
        batch_id="panels-001",
        target_path="panels/attempts/p01-01/initial-001.png",
    )


def valid_receipt():
    job = valid_job()
    return build_generation_receipt(
        attempt_id="p01-01-initial-001",
        job_id=job["job_id"],
        job_sha256=generation_job_sha256(job),
        raster_path="panels/attempts/p01-01/initial-001.png",
        raster_sha256=RASTER_SHA256,
        executor_kind="native-tool",
        executor_id="session-image-tool",
        provider="host-reported",
        model=None,
        capabilities_used={
            "reference_images": True,
            "dimensions": True,
            "localized_edit": False,
        },
        outcome="success",
        category="accepted",
    )


def valid_handoff_manifest():
    job = valid_job()
    return build_handoff_manifest(
        project_id="sunlight-courier",
        project_schema_version="1.1",
        stage="STORYBOARDED",
        locked_scope_sha256="f" * 64,
        batches_path="generation/batches.json",
        batches_sha256=BATCHES_SHA256,
        jobs=[
            {
                "job_id": job["job_id"],
                "path": f"generation/jobs/{job['job_id']}.json",
                "sha256": JOB_PATH_SHA256,
                "status": "ready",
            }
        ],
        required_artifacts=[
            {"path": "plan/story-plan.json", "sha256": "1" * 64},
            {"path": "plan/storyboard.json", "sha256": "2" * 64},
        ],
    )


class GenerationContractTests(unittest.TestCase):
    def assert_invalid(self, validator, value, fragment):
        issues = validator(value)
        self.assertTrue(any(fragment in issue for issue in issues), issues)
        self.assertEqual(sorted(issues), issues)

    def test_builders_emit_exact_versioned_shapes_accepted_by_validators(self):
        job = valid_job()
        batches = build_generation_batches(
            [{"batch_id": "panels-001", "kind": "panel", "job_ids": [job["job_id"]]}]
        )
        receipt = valid_receipt()
        manifest = valid_handoff_manifest()

        self.assertEqual(HANDOFF_CONTRACT_VERSION, "1.0")
        self.assertEqual(
            {"schema_version", "batches"},
            set(batches),
        )
        self.assertEqual(
            {
                "schema_version",
                "job_id",
                "subject_kind",
                "subject_id",
                "prompt_path",
                "prompt_sha256",
                "references",
                "requested_dimensions",
                "requested_aspect_ratio",
                "attempt_kind",
                "retry_limit",
                "batch_id",
                "target_path",
            },
            set(job),
        )
        self.assertEqual(
            {
                "schema_version",
                "attempt_id",
                "job_id",
                "job_sha256",
                "raster_path",
                "raster_sha256",
                "executor_kind",
                "executor_id",
                "provider",
                "model",
                "capabilities_used",
                "outcome",
                "category",
            },
            set(receipt),
        )
        self.assertEqual(
            {
                "schema_version",
                "project_schema_version",
                "project_id",
                "stage",
                "locked_scope_sha256",
                "batches",
                "jobs",
                "required_artifacts",
            },
            set(manifest),
        )
        self.assertEqual([], validate_generation_batches(batches))
        self.assertEqual([], validate_generation_job(job))
        self.assertEqual([], validate_generation_receipt(receipt))
        self.assertEqual([], validate_handoff_manifest(manifest))

    def test_every_contract_rejects_unknown_and_missing_keys(self):
        job = valid_job()
        values = (
            (validate_generation_batches, build_generation_batches([]), "batches"),
            (validate_generation_job, job, "prompt_path"),
            (validate_generation_receipt, valid_receipt(), "executor_kind"),
            (validate_handoff_manifest, valid_handoff_manifest(), "stage"),
        )
        for validator, value, required in values:
            with self.subTest(validator=validator.__name__):
                unknown = deepcopy(value)
                unknown["raw_provider_response"] = {"private": True}
                self.assert_invalid(validator, unknown, "raw_provider_response")
                missing = deepcopy(value)
                missing.pop(required)
                self.assert_invalid(validator, missing, required)

    def test_job_bytes_and_id_are_canonical_and_deterministic(self):
        first = valid_job()
        second = valid_job()
        identity = {
            key: value for key, value in first.items() if key not in {"schema_version", "job_id"}
        }
        expected_id = hashlib.sha256(
            canonical_json_bytes({"contract_version": HANDOFF_CONTRACT_VERSION, "job": identity})
        ).hexdigest()

        self.assertEqual(expected_id, first["job_id"])
        self.assertEqual(first, second)
        self.assertEqual(canonical_artifact_bytes(first), canonical_artifact_bytes(second))
        self.assertEqual(
            hashlib.sha256(canonical_artifact_bytes(first)).hexdigest(),
            generation_job_sha256(first),
        )

    def test_reference_order_is_an_explicit_job_identity_input(self):
        first = valid_job()
        second = build_generation_job(
            subject_kind="panel",
            subject_id="p01-01",
            prompt_path="prompts/panels/p01-01.txt",
            prompt_sha256=PROMPT_SHA256,
            references=[
                {"path": "references/scenes/hall.png", "sha256": "3" * 64},
                {"path": "references/characters/mira.png", "sha256": REFERENCE_SHA256},
            ],
            requested_dimensions={"height": 1136, "width": 736},
            requested_aspect_ratio="46:71",
            attempt_kind="initial",
            retry_limit=2,
            batch_id="panels-001",
            target_path="panels/attempts/p01-01/initial-001.png",
        )
        reversed_references = build_generation_job(
            **{
                "subject_kind": "panel",
                "subject_id": "p01-01",
                "prompt_path": "prompts/panels/p01-01.txt",
                "prompt_sha256": PROMPT_SHA256,
                "references": list(reversed(second["references"])),
                "requested_dimensions": {"width": 736, "height": 1136},
                "requested_aspect_ratio": "46:71",
                "attempt_kind": "initial",
                "retry_limit": 2,
                "batch_id": "panels-001",
                "target_path": "panels/attempts/p01-01/initial-001.png",
            }
        )

        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertNotEqual(second["job_id"], reversed_references["job_id"])

    def test_job_references_are_canonical_local_character_or_scene_pngs(self):
        for path in (
            "project.json",
            "prompts/panels/p01-01.txt",
            "references/characters/mira.txt",
        ):
            with self.subTest(path=path):
                job = valid_job()
                job["references"][0]["path"] = path
                self.assert_invalid(validate_generation_job, job, "references[0].path")

        scene_job = build_generation_job(
            subject_kind="panel",
            subject_id="p01-01",
            prompt_path="prompts/panels/p01-01.txt",
            prompt_sha256=PROMPT_SHA256,
            references=[{"path": "references/scenes/hall.png", "sha256": REFERENCE_SHA256}],
            requested_dimensions={"width": 736, "height": 1136},
            requested_aspect_ratio="46:71",
            attempt_kind="initial",
            retry_limit=2,
            batch_id="panels-001",
            target_path="panels/attempts/p01-01/initial-001.png",
        )
        self.assertEqual([], validate_generation_job(valid_job()))
        self.assertEqual([], validate_generation_job(scene_job))

    def test_contracts_reject_unsafe_paths_secrets_and_raw_payload_fields(self):
        job = valid_job()
        job["prompt_path"] = "../private/prompt.txt"
        self.assert_invalid(validate_generation_job, job, "prompt_path")

        receipt = valid_receipt()
        receipt["raster_path"] = "/private/output.png"
        self.assert_invalid(validate_generation_receipt, receipt, "raster_path")

        for field, value in (
            ("provider", "token=super-secret-value"),
            ("model", "/home/creator/private/model.safetensors"),
            ("provider", "https://creator:hunter2@example.invalid"),
            ("provider", "https:api.example.com/v1"),
            ("model", "file:///home/creator/private/model.safetensors"),
            ("model", "file:private-data"),
            ("provider", "https://[malformed"),
        ):
            with self.subTest(field=field, value=value):
                receipt = valid_receipt()
                receipt[field] = value
                self.assert_invalid(validate_generation_receipt, receipt, field)

        receipt = valid_receipt()
        receipt["raster_path"] = "project.json"
        self.assert_invalid(validate_generation_receipt, receipt, "raster_path")

        receipt = valid_receipt()
        receipt["category"] = "provider refusal with raw detail"
        self.assert_invalid(validate_generation_receipt, receipt, "category")

        receipt = valid_receipt()
        receipt["category"] = "ghp_abcdefghijklmnopqrstuvwxyz"
        self.assert_invalid(validate_generation_receipt, receipt, "category")

        declaration = {
            "capability_id": "native-image",
            "executor_kind": "native-tool",
            "text_to_image": True,
            "local_raster": True,
            "supports_reference_images": True,
            "supports_dimensions": True,
            "supports_localized_edit": False,
            "credential": "token=super-secret-value",
        }
        with self.assertRaisesRegex(HandoffContractError, "credential"):
            rank_executors([declaration])

    def test_noncanonical_path_aliases_are_rejected_across_contracts(self):
        job = valid_job()
        job["prompt_path"] = "prompts//panels/p01-01.txt"
        self.assert_invalid(validate_generation_job, job, "prompt_path")

        job = valid_job()
        job["references"][0]["path"] = "references/characters/./mira.png"
        self.assert_invalid(validate_generation_job, job, "references[0].path")

        receipt = valid_receipt()
        receipt["raster_path"] = "panels/attempts/p01-01//initial-001.png"
        self.assert_invalid(validate_generation_receipt, receipt, "raster_path")

        manifest = valid_handoff_manifest()
        manifest["required_artifacts"][0]["path"] = "plan//story-plan.json"
        self.assert_invalid(validate_handoff_manifest, manifest, "required_artifacts[0].path")

    def test_reference_job_uses_its_own_retained_attempt_namespace(self):
        job = build_generation_job(
            subject_kind="reference",
            subject_id="mira",
            prompt_path="prompts/references/mira.txt",
            prompt_sha256=PROMPT_SHA256,
            references=[],
            requested_dimensions={"width": 1024, "height": 1024},
            requested_aspect_ratio="1:1",
            attempt_kind="initial",
            retry_limit=2,
            batch_id="references-001",
            target_path="references/attempts/mira/initial-001.png",
        )
        self.assertEqual([], validate_generation_job(job))

    def test_job_target_is_bound_to_the_subject_attempt_namespace(self):
        job = valid_job()
        job["target_path"] = "project.json"
        self.assert_invalid(validate_generation_job, job, "target_path")

        job = valid_job()
        job["target_path"] = "panels/attempts/p01-02/initial-001.png"
        self.assert_invalid(validate_generation_job, job, "target_path")

    def test_builders_reject_invalid_contracts_instead_of_normalizing_them(self):
        with self.assertRaises(HandoffContractError):
            build_generation_job(
                subject_kind="panel",
                subject_id="p01-01",
                prompt_path="C:/private/prompt.txt",
                prompt_sha256=PROMPT_SHA256,
                references=[],
                requested_dimensions=None,
                requested_aspect_ratio=None,
                attempt_kind="initial",
                retry_limit=2,
                batch_id="panels-001",
                target_path="panels/attempts/p01-01/initial-001.png",
            )


class LockedScopeTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary_directory.name) / "project"
        files = {
            "plan/story-plan.json": {"schema_version": "1.0", "title": "Story"},
            "plan/character-bible.json": {"characters": [], "schema_version": "1.0"},
            "plan/storyboard.json": {"pages": [], "schema_version": "1.0"},
            "logs/reference-selection.json": {
                "panels": [
                    {
                        "panel_id": "p01-01",
                        "selected": [
                            {
                                "path": "references/characters/mira.png",
                            }
                        ],
                    }
                ],
                "schema_version": "1.0",
            },
            "generation/batches.json": build_generation_batches([]),
        }
        for relative, value in files.items():
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(canonical_artifact_bytes(value))
        for relative, payload in {
            "prompts/panels/p01-02.txt": b"second prompt\n",
            "prompts/panels/p01-01.txt": b"first prompt\n",
            "references/characters/mira.png": b"local-raster-bytes",
        }.items():
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        (self.project / "prompts/references").mkdir(parents=True)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_locked_scope_hash_sorts_paths_before_hashing(self):
        first = locked_scope_sha256(
            self.project,
            prompt_paths=["prompts/panels/p01-02.txt", "prompts/panels/p01-01.txt"],
            reference_paths=["references/characters/mira.png"],
        )
        second = locked_scope_sha256(
            self.project,
            prompt_paths=["prompts/panels/p01-01.txt", "prompts/panels/p01-02.txt"],
            reference_paths=["references/characters/mira.png"],
        )
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{64}$")

    def test_json_formatting_does_not_change_the_canonical_scope_hash(self):
        expected = locked_scope_sha256(
            self.project,
            prompt_paths=["prompts/panels/p01-01.txt", "prompts/panels/p01-02.txt"],
            reference_paths=["references/characters/mira.png"],
        )
        story_path = self.project / "plan/story-plan.json"
        story = json.loads(story_path.read_text("utf-8"))
        story_path.write_text(json.dumps(story, separators=(",", ":")), encoding="utf-8")

        self.assertEqual(
            expected,
            locked_scope_sha256(
                self.project,
                prompt_paths=["prompts/panels/p01-02.txt", "prompts/panels/p01-01.txt"],
                reference_paths=["references/characters/mira.png"],
            ),
        )

    def test_stale_scope_digest_is_rejected_after_any_covered_change(self):
        expected = locked_scope_sha256(
            self.project,
            prompt_paths=["prompts/panels/p01-01.txt", "prompts/panels/p01-02.txt"],
            reference_paths=["references/characters/mira.png"],
        )
        assert_locked_scope(
            self.project,
            expected,
            prompt_paths=["prompts/panels/p01-02.txt", "prompts/panels/p01-01.txt"],
            reference_paths=["references/characters/mira.png"],
        )
        (self.project / "prompts/panels/p01-01.txt").write_bytes(b"changed prompt\n")

        with self.assertRaisesRegex(StaleLockedScopeError, "stale"):
            assert_locked_scope(
                self.project,
                expected,
                prompt_paths=["prompts/panels/p01-01.txt", "prompts/panels/p01-02.txt"],
                reference_paths=["references/characters/mira.png"],
            )

    def test_locked_scope_holds_project_lock_through_discovery_and_comparison(self):
        expected = locked_scope_sha256(
            self.project,
            prompt_paths=["prompts/panels/p01-01.txt", "prompts/panels/p01-02.txt"],
            reference_paths=["references/characters/mira.png"],
        )
        discovery_entered = threading.Event()
        release_discovery = threading.Event()
        comparison_entered = threading.Event()
        release_comparison = threading.Event()
        contender_started = threading.Event()
        contender_acquired = threading.Event()
        verification_errors = []
        real_complete_scope_paths = handoff._complete_scope_paths
        real_compare_digest = handoff.hmac.compare_digest

        def paused_complete_scope_paths(*args, **kwargs):
            discovery_entered.set()
            if not release_discovery.wait(5):
                raise TimeoutError("test did not release locked-scope discovery")
            return real_complete_scope_paths(*args, **kwargs)

        def paused_compare_digest(*args, **kwargs):
            comparison_entered.set()
            if not release_comparison.wait(5):
                raise TimeoutError("test did not release locked-scope comparison")
            return real_compare_digest(*args, **kwargs)

        def verify_scope():
            try:
                assert_locked_scope(
                    self.project,
                    expected,
                    prompt_paths=[
                        "prompts/panels/p01-01.txt",
                        "prompts/panels/p01-02.txt",
                    ],
                    reference_paths=["references/characters/mira.png"],
                )
            except BaseException as error:
                verification_errors.append(error)

        def contend_for_project():
            contender_started.set()
            with ProjectLock(self.project):
                contender_acquired.set()

        verifier = threading.Thread(target=verify_scope)
        contender = threading.Thread(target=contend_for_project)
        blocked_during_discovery = False
        blocked_during_comparison = False
        discovery_observed = False
        comparison_observed = False
        with (
            mock.patch.object(
                handoff, "_complete_scope_paths", side_effect=paused_complete_scope_paths
            ),
            mock.patch.object(handoff.hmac, "compare_digest", side_effect=paused_compare_digest),
        ):
            verifier.start()
            try:
                discovery_observed = discovery_entered.wait(5)
                if discovery_observed:
                    contender.start()
                    self.assertTrue(contender_started.wait(5))
                    blocked_during_discovery = not contender_acquired.wait(0.25)
                    release_discovery.set()
                    comparison_observed = comparison_entered.wait(5)
                    if comparison_observed:
                        blocked_during_comparison = not contender_acquired.wait(0.25)
            finally:
                release_discovery.set()
                release_comparison.set()
                verifier.join(5)
                if contender.ident is not None:
                    contender.join(5)

        self.assertTrue(discovery_observed)
        self.assertTrue(comparison_observed)
        self.assertFalse(verifier.is_alive())
        self.assertFalse(contender.is_alive())
        self.assertEqual([], verification_errors)
        self.assertTrue(blocked_during_discovery)
        self.assertTrue(blocked_during_comparison)
        self.assertTrue(contender_acquired.is_set())

    def test_scope_hash_rejects_omitted_authoritative_inputs(self):
        with self.assertRaisesRegex(HandoffContractError, "all generation prompts"):
            locked_scope_sha256(
                self.project,
                prompt_paths=["prompts/panels/p01-01.txt"],
                reference_paths=["references/characters/mira.png"],
            )
        with self.assertRaisesRegex(HandoffContractError, "all selected references"):
            locked_scope_sha256(
                self.project,
                prompt_paths=["prompts/panels/p01-01.txt", "prompts/panels/p01-02.txt"],
                reference_paths=[],
            )

    def test_scope_hash_rejects_escape_and_noncanonical_paths(self):
        for path in ("../private/prompt.txt", "prompts//panels/p01-01.txt"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "relative project path"):
                    locked_scope_sha256(
                        self.project,
                        prompt_paths=[path],
                        reference_paths=["references/characters/mira.png"],
                    )


class ExecutorRankingTests(unittest.TestCase):
    @staticmethod
    def declaration(
        capability_id,
        executor_kind,
        *,
        text_to_image=True,
        local_raster=True,
        references=False,
        dimensions=False,
        localized_edit=False,
    ):
        return {
            "capability_id": capability_id,
            "executor_kind": executor_kind,
            "text_to_image": text_to_image,
            "local_raster": local_raster,
            "supports_reference_images": references,
            "supports_dimensions": dimensions,
            "supports_localized_edit": localized_edit,
        }

    def test_ranking_uses_declared_features_and_stable_id_only(self):
        declarations = [
            self.declaration(
                "external-complete",
                "external-tool",
                references=True,
                dimensions=True,
                localized_edit=True,
            ),
            self.declaration("native-basic", "native-tool"),
            self.declaration("external-beta", "external-tool", references=True),
            self.declaration("external-alpha", "external-tool", references=True),
            self.declaration("no-raster", "native-tool", local_raster=False),
        ]

        ranked = rank_executors(declarations)

        self.assertEqual(
            ["native-basic", "external-complete", "external-alpha", "external-beta"],
            [item["capability_id"] for item in ranked],
        )

    def test_ranking_rejects_unknown_keys_and_invalid_declarations(self):
        invalid = self.declaration("native-image", "native-tool")
        invalid["provider_name"] = "not-a-capability"
        with self.assertRaisesRegex(HandoffContractError, "provider_name"):
            rank_executors([invalid])

        invalid = self.declaration("Bad ID", "native-tool")
        with self.assertRaisesRegex(HandoffContractError, "capability_id"):
            rank_executors([invalid])


if __name__ == "__main__":
    unittest.main()
