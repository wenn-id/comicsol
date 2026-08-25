import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from PIL import Image

from scripts import comic_sol, handoff, schema
from scripts.character_identity import IDENTITY_PACK_PATH, derive_identity_pack
from scripts.core_primitives import canonical_artifact_bytes, canonical_json_bytes
from scripts.handoff import (
    HANDOFF_CONTRACT_VERSION,
    HandoffContractError,
    HandoffResultError,
    build_generation_batches,
    build_generation_job,
    build_generation_receipt,
    generation_job_sha256,
    validate_generation_job,
    validate_generation_receipt,
    validate_handoff_manifest,
)
from scripts.input_limits import InputResourceLimitError
from scripts.validate_project import validate_project
from tests.support import make_symlink
from tests.test_validation import valid_characters, valid_manifest, valid_story, valid_storyboard


def _require_api(testcase, module, name):
    api = getattr(module, name, None)
    testcase.assertTrue(
        callable(api),
        f"required WP2 API {module.__name__}.{name} is not implemented",
    )
    return api


def _expected_attempt_id(job_id, attempt):
    preimage = {"attempt": attempt, "job_id": job_id}
    digest = hashlib.sha256(canonical_json_bytes(preimage)).hexdigest()
    return "attempt-" + digest[:40]


def _contract_job(*, retry_limit=2):
    return build_generation_job(
        subject_kind="panel",
        subject_id="p01-01",
        prompt_path="prompts/panels/p01-01.txt",
        prompt_sha256="a" * 64,
        references=[
            {
                "path": "references/characters/mira.png",
                "sha256": "b" * 64,
            }
        ],
        requested_dimensions={"width": 736, "height": 1136},
        requested_aspect_ratio="46:71",
        attempt_kind="initial",
        retry_limit=retry_limit,
        batch_id="panels-001",
        target_path="panels/attempts/p01-01/initial-001.png",
    )


def _contract_receipt(job, attempt, *, outcome="failure", category="transient-tool-error"):
    success = outcome == "success"
    return build_generation_receipt(
        attempt_id=_expected_attempt_id(job["job_id"], attempt),
        job_id=job["job_id"],
        job_sha256=generation_job_sha256(job),
        raster_path=(job["target_path"] if success else None),
        raster_sha256=("c" * 64 if success else None),
        executor_kind="external-tool",
        executor_id="fixture-renderer",
        provider="fixture-provider",
        model="fixture-model",
        capabilities_used={
            "reference_images": True,
            "dimensions": True,
            "localized_edit": False,
        },
        outcome=outcome,
        category=category,
    )


class HandoffLifecycleGoldenTests(unittest.TestCase):
    def _planner_project(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        project = comic_sol.init_project(
            root,
            "Sunlight Courier",
            b"A courier carries the last light.",
            {"mode": "short_prompt", "language": "en"},
            page_count=1,
        )

        manifest = valid_manifest()
        manifest["project_id"] = project.name
        manifest["status"] = "STORYBOARDED"
        manifest["input"]["source_sha256"] = comic_sol.sha256_file(project / "source/input.txt")
        comic_sol.atomic_write_json(project / "project.json", manifest)
        comic_sol.atomic_write_json(project / "plan/story-plan.json", valid_story())
        comic_sol.atomic_write_json(project / "plan/character-bible.json", valid_characters())
        comic_sol.atomic_write_json(project / "plan/storyboard.json", valid_storyboard())
        comic_sol.atomic_write_json(
            project / IDENTITY_PACK_PATH,
            derive_identity_pack(valid_characters()),
        )
        (project / "prompts/references/mira.txt").write_text(
            "Mira identity reference, neutral pose, plain background.",
            encoding="utf-8",
        )
        (project / "prompts/panels/p01-01.txt").write_text(
            "Mira catches the last vial of sunlight in the dispatch hall.",
            encoding="utf-8",
        )
        return root, project

    @staticmethod
    def _manifest_jobs(project):
        manifest = comic_sol.read_json(project / "handoff/manifest.json")
        jobs = {}
        for descriptor in manifest["jobs"]:
            job = comic_sol.read_json(project / descriptor["path"])
            jobs[job["subject_kind"], job["subject_id"]] = job
        return manifest, jobs

    @staticmethod
    def _effective_job(snapshot, job_id):
        return next(item for item in snapshot["jobs"] if item["job_id"] == job_id)

    def test_provider_free_reference_then_panel_lifecycle_is_project_complete(self):
        prepare = _require_api(self, comic_sol, "prepare_handoff")
        inspect = _require_api(self, comic_sol, "inspect_handoff")
        accept = _require_api(self, comic_sol, "accept_handoff_result")
        attempt_identifier = _require_api(self, handoff, "attempt_id")
        root, project = self._planner_project()

        reference_preparation = prepare(project)

        self.assertEqual("reference", reference_preparation["phase"])
        reference_batches = comic_sol.read_json(project / "generation/batches.json")
        self.assertEqual([], handoff.validate_generation_batches(reference_batches))
        self.assertEqual(
            [{"batch_id": "references-001", "kind": "reference"}],
            [
                {"batch_id": batch["batch_id"], "kind": batch["kind"]}
                for batch in reference_batches["batches"]
            ],
        )
        reference_manifest, jobs = self._manifest_jobs(project)
        self.assertEqual([], validate_handoff_manifest(reference_manifest))
        self.assertEqual({("reference", "mira")}, set(jobs))
        reference_job = jobs["reference", "mira"]
        self.assertEqual([], validate_generation_job(reference_job))
        self.assertEqual([], reference_job["references"])
        self.assertEqual(
            "references/attempts/mira/initial-001.png",
            reference_job["target_path"],
        )
        project_manifest = comic_sol.read_json(project / "project.json")
        self.assertEqual(
            {
                "contract_version": HANDOFF_CONTRACT_VERSION,
                "locked_scope_sha256": reference_manifest["locked_scope_sha256"],
                "manifest_path": "handoff/manifest.json",
            },
            project_manifest["handoff"],
        )
        reference_inspection = inspect(project)
        self.assertTrue(reference_inspection["prepared"])
        self.assertEqual("reference", reference_inspection["phase"])
        self.assertEqual(
            {
                "status": "ready",
                "attempts_used": 0,
                "attempts_remaining": 3,
                "next_attempt": 1,
            },
            {
                key: self._effective_job(reference_inspection, reference_job["job_id"])[key]
                for key in (
                    "status",
                    "attempts_used",
                    "attempts_remaining",
                    "next_attempt",
                )
            },
        )

        rendered_reference = root / "renderer-reference.png"
        Image.new("RGB", (512, 512), (220, 180, 80)).save(rendered_reference, format="PNG")
        accept(
            project,
            job_id=reference_job["job_id"],
            attempt=1,
            raster_path=rendered_reference,
            executor_kind="external-tool",
            executor_id="fixture-renderer",
            provider="fixture-provider",
            model="fixture-model",
            capabilities_used={
                "reference_images": False,
                "dimensions": False,
                "localized_edit": False,
            },
            approve_reference=True,
        )

        reference_attempt = project / reference_job["target_path"]
        canonical_reference = project / "references/characters/mira.png"
        self.assertEqual(rendered_reference.read_bytes(), reference_attempt.read_bytes())
        self.assertEqual(reference_attempt.read_bytes(), canonical_reference.read_bytes())
        reference_attempt_id = attempt_identifier(job_id=reference_job["job_id"], attempt=1)
        reference_receipt_path = project / f"generation/receipts/{reference_attempt_id}.json"
        reference_receipt = comic_sol.read_json(reference_receipt_path)
        self.assertEqual([], validate_generation_receipt(reference_receipt))
        self.assertEqual(reference_job["job_id"], reference_receipt["job_id"])
        reference_receipt_bytes = reference_receipt_path.read_bytes()
        after_reference = inspect(project)
        self.assertEqual(
            "completed",
            self._effective_job(after_reference, reference_job["job_id"])["status"],
        )

        panel_preparation = prepare(project)

        self.assertEqual("panel", panel_preparation["phase"])
        self.assertEqual(reference_receipt_bytes, reference_receipt_path.read_bytes())
        panel_batches = comic_sol.read_json(project / "generation/batches.json")
        self.assertEqual(
            [("references-001", "reference"), ("panels-001", "panel")],
            [(batch["batch_id"], batch["kind"]) for batch in panel_batches["batches"]],
        )
        panel_manifest, jobs = self._manifest_jobs(project)
        self.assertEqual([], validate_handoff_manifest(panel_manifest))
        self.assertEqual({("reference", "mira"), ("panel", "p01-01")}, set(jobs))
        panel_job = jobs["panel", "p01-01"]
        self.assertEqual(
            ["references/characters/mira.png"],
            [item["path"] for item in panel_job["references"]],
        )
        panel_inspection = inspect(project)
        self.assertEqual("panel", panel_inspection["phase"])
        self.assertEqual(
            "completed",
            self._effective_job(panel_inspection, reference_job["job_id"])["status"],
        )
        self.assertEqual(
            "ready",
            self._effective_job(panel_inspection, panel_job["job_id"])["status"],
        )

        dimensions = panel_job["requested_dimensions"]
        rendered_panel = root / "renderer-panel.png"
        Image.new(
            "RGB",
            (dimensions["width"], dimensions["height"]),
            (20, 30, 40),
        ).save(rendered_panel, format="PNG")
        accept(
            project,
            job_id=panel_job["job_id"],
            attempt=1,
            raster_path=rendered_panel,
            executor_kind="external-tool",
            executor_id="fixture-renderer",
            provider="fixture-provider",
            model="fixture-model",
            capabilities_used={
                "reference_images": True,
                "dimensions": True,
                "localized_edit": False,
            },
            approve_reference=False,
        )

        panel_attempt = project / panel_job["target_path"]
        self.assertEqual(rendered_panel.read_bytes(), panel_attempt.read_bytes())
        self.assertFalse((project / "panels/raw/p01-01.png").exists())
        panel_attempt_id = attempt_identifier(job_id=panel_job["job_id"], attempt=1)
        panel_receipt = comic_sol.read_json(
            project / f"generation/receipts/{panel_attempt_id}.json"
        )
        self.assertEqual([], validate_generation_receipt(panel_receipt))
        final_inspection = inspect(project)
        self.assertEqual(
            "completed",
            self._effective_job(final_inspection, panel_job["job_id"])["status"],
        )
        final_manifest = comic_sol.read_json(project / "handoff/manifest.json")
        final_project = comic_sol.read_json(project / "project.json")
        self.assertEqual(
            final_manifest["locked_scope_sha256"],
            final_project["handoff"]["locked_scope_sha256"],
        )


class HandoffPureContractTests(unittest.TestCase):
    def test_required_wp2_api_surface_is_present_in_approved_modules(self):
        required = {
            comic_sol: (
                "prepare_handoff",
                "inspect_handoff",
                "accept_handoff_result",
                "record_handoff_failure",
            ),
            handoff: (
                "attempt_id",
                "locked_scope_sha256_from_content",
                "reconcile_job_receipts",
            ),
            schema: ("migrate_project_manifest_in_memory",),
        }
        missing = [
            f"{module.__name__}.{name}"
            for module, names in required.items()
            for name in names
            if not callable(getattr(module, name, None))
        ]
        self.assertEqual([], missing, f"required WP2 APIs are absent: {', '.join(missing)}")

    def test_staged_content_locked_scope_hash_is_canonical_and_order_independent(self):
        digest_scope = _require_api(self, handoff, "locked_scope_sha256_from_content")
        contents = {
            "generation/batches.json": canonical_artifact_bytes(build_generation_batches([])),
            "logs/reference-selection.json": canonical_artifact_bytes(
                {"panels": [], "schema_version": "1.0"}
            ),
            "plan/character-bible.json": canonical_artifact_bytes(
                {"characters": [], "schema_version": "1.0"}
            ),
            "plan/story-plan.json": canonical_artifact_bytes(
                {"schema_version": "1.0", "title": "Fixture"}
            ),
            "plan/storyboard.json": canonical_artifact_bytes(
                {"pages": [], "schema_version": "1.0"}
            ),
            "prompts/panels/p01-01.txt": b"deterministic panel prompt\n",
        }
        files = [
            {
                "path": path,
                "sha256": hashlib.sha256(
                    canonical_json_bytes(json.loads(payload)) if path.endswith(".json") else payload
                ).hexdigest(),
            }
            for path, payload in sorted(contents.items())
        ]
        expected = hashlib.sha256(
            canonical_json_bytes({"contract_version": HANDOFF_CONTRACT_VERSION, "files": files})
        ).hexdigest()

        self.assertEqual(expected, digest_scope(contents))
        self.assertEqual(expected, digest_scope(dict(reversed(list(contents.items())))))
        changed = dict(contents)
        changed["prompts/panels/p01-01.txt"] += b"changed"
        self.assertNotEqual(expected, digest_scope(changed))

    def test_attempt_id_uses_the_authoritative_canonical_formula(self):
        make_attempt_id = _require_api(self, handoff, "attempt_id")
        job_id = "a" * 64

        for attempt in (1, 2, 3):
            with self.subTest(attempt=attempt):
                expected = _expected_attempt_id(job_id, attempt)
                self.assertEqual(
                    expected,
                    make_attempt_id(job_id=job_id, attempt=attempt),
                )
                self.assertRegex(expected, r"^attempt-[0-9a-f]{40}$")
        self.assertNotEqual(
            make_attempt_id(job_id=job_id, attempt=1),
            make_attempt_id(job_id=job_id, attempt=2),
        )

    def test_effective_states_and_attempt_counts_come_from_jobs_and_receipts(self):
        reconcile = _require_api(self, handoff, "reconcile_job_receipts")
        job = _contract_job(retry_limit=2)
        job_digest = generation_job_sha256(job)
        failures = [_contract_receipt(job, attempt) for attempt in (1, 2, 3)]
        completed = [
            failures[0],
            _contract_receipt(job, 2, outcome="success", category="accepted"),
        ]
        cases = (
            (
                "missing",
                {
                    "job": None,
                    "job_sha256": None,
                    "receipts": [],
                    "declared_status": "missing",
                    "stale": False,
                },
                ("missing", 0, 0, None),
            ),
            (
                "ready-new",
                {
                    "job": job,
                    "job_sha256": job_digest,
                    "receipts": [],
                    "declared_status": "ready",
                    "stale": False,
                },
                ("ready", 0, 3, 1),
            ),
            (
                "ready-retry",
                {
                    "job": job,
                    "job_sha256": job_digest,
                    "receipts": failures[:2],
                    "declared_status": "ready",
                    "stale": False,
                },
                ("ready", 2, 1, 3),
            ),
            (
                "completed",
                {
                    "job": job,
                    "job_sha256": job_digest,
                    "receipts": completed,
                    "declared_status": "completed",
                    "stale": False,
                },
                ("completed", 2, 1, None),
            ),
            (
                "failed",
                {
                    "job": job,
                    "job_sha256": job_digest,
                    "receipts": failures,
                    "declared_status": "failed",
                    "stale": False,
                },
                ("failed", 3, 0, None),
            ),
            (
                "stale",
                {
                    "job": job,
                    "job_sha256": job_digest,
                    "receipts": failures[:1],
                    "declared_status": "ready",
                    "stale": True,
                },
                ("stale", 1, 2, None),
            ),
        )

        for label, arguments, expected in cases:
            with self.subTest(label=label):
                result = reconcile(**arguments)
                self.assertEqual(
                    expected,
                    (
                        result["status"],
                        result["attempts_used"],
                        result["attempts_remaining"],
                        result["next_attempt"],
                    ),
                )

    def test_receipt_reconciliation_rejects_wrong_binding_and_ordinal_conflict(self):
        reconcile = _require_api(self, handoff, "reconcile_job_receipts")
        job = _contract_job()
        job_digest = generation_job_sha256(job)
        receipt = _contract_receipt(job, 1)
        wrong_job = deepcopy(receipt)
        wrong_job["job_id"] = "f" * 64
        conflicting = deepcopy(receipt)
        conflicting["category"] = "provider-refusal"
        self.assertEqual([], validate_generation_receipt(wrong_job))
        self.assertEqual([], validate_generation_receipt(conflicting))

        with self.assertRaisesRegex(HandoffContractError, "job"):
            reconcile(
                job=job,
                job_sha256=job_digest,
                receipts=[wrong_job],
                declared_status="ready",
                stale=False,
            )
        with self.assertRaisesRegex(HandoffContractError, "conflict"):
            reconcile(
                job=job,
                job_sha256=job_digest,
                receipts=[receipt, conflicting],
                declared_status="ready",
                stale=False,
            )

    def test_receipt_reconciliation_rejects_receipts_after_terminal_success(self):
        reconcile = _require_api(self, handoff, "reconcile_job_receipts")
        job = _contract_job()
        job_digest = generation_job_sha256(job)
        success = _contract_receipt(job, 1, outcome="success", category="accepted")
        cases = (
            (
                "success-then-failure",
                _contract_receipt(job, 2),
                "after successful receipt",
            ),
            (
                "success-then-success",
                _contract_receipt(job, 2, outcome="success", category="accepted"),
                "multiple successful receipts",
            ),
        )

        for label, later_receipt, message in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(HandoffContractError, message):
                    reconcile(
                        job=job,
                        job_sha256=job_digest,
                        receipts=[success, later_receipt],
                        declared_status="completed",
                        stale=False,
                    )

    def test_schema_1_0_migration_is_composed_in_memory_without_writes(self):
        migrate = _require_api(self, schema, "migrate_project_manifest_in_memory")
        legacy = valid_manifest()
        legacy["schema_version"] = "1.0"
        legacy.pop("handoff")
        original = deepcopy(legacy)
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            manifest_path = project / "project.json"
            manifest_path.write_bytes(canonical_artifact_bytes(legacy))
            before = {path.name: path.read_bytes() for path in project.iterdir()}

            migrated = migrate(legacy)

            after = {path.name: path.read_bytes() for path in project.iterdir()}
        self.assertEqual(original, legacy)
        self.assertEqual(before, after)
        self.assertEqual("1.1", migrated["schema_version"])
        self.assertEqual(
            {
                "contract_version": HANDOFF_CONTRACT_VERSION,
                "locked_scope_sha256": None,
                "manifest_path": None,
            },
            migrated["handoff"],
        )
        expected = deepcopy(original)
        expected["schema_version"] = "1.1"
        expected["handoff"] = migrated["handoff"]
        self.assertEqual(expected, migrated)
        self.assertEqual(migrated, migrate(original))


class HandoffPrepareInspectTests(unittest.TestCase):
    """Authoritative WP2 step-4 prepare/inspect lifecycle coverage."""

    def _planner_project(self):
        return HandoffLifecycleGoldenTests._planner_project(self)

    @staticmethod
    def _manifest_jobs(project):
        return HandoffLifecycleGoldenTests._manifest_jobs(project)

    @staticmethod
    def _effective_job(snapshot, job_id):
        return HandoffLifecycleGoldenTests._effective_job(snapshot, job_id)

    def _legacy_planner_project(self):
        root, project = self._planner_project()
        manifest = comic_sol.read_json(project / "project.json")
        manifest["schema_version"] = "1.0"
        manifest.pop("handoff")
        comic_sol.atomic_write_json(project / "project.json", manifest)
        return root, project

    @staticmethod
    def _tree_snapshot(project):
        snapshot = {}
        for path in sorted(
            project.rglob("*"),
            key=lambda item: item.relative_to(project).as_posix(),
        ):
            relative = path.relative_to(project).as_posix()
            if path.is_dir():
                snapshot[relative] = ("directory", b"")
            else:
                snapshot[relative] = ("file", path.read_bytes())
        return snapshot

    @staticmethod
    def _artifact_snapshot(project):
        snapshot = {}
        for path in sorted(
            project.rglob("*"),
            key=lambda item: item.relative_to(project).as_posix(),
        ):
            if not path.is_file():
                continue
            relative = path.relative_to(project).as_posix()
            if relative == ".comic-sol.lock" or relative.startswith("logs/transactions/"):
                continue
            snapshot[relative] = path.read_bytes()
        return snapshot

    @staticmethod
    def _transaction_names(project):
        transactions = project / "logs/transactions"
        if not transactions.is_dir():
            return []
        return sorted(entry.name for entry in transactions.iterdir())

    @staticmethod
    def _write_png(path, color):
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (512, 512), color).save(path, format="PNG")
        return path.read_bytes()

    def _activate_current_reference(self, project, reference_job):
        attempt_path = project / reference_job["target_path"]
        raster_bytes = self._write_png(attempt_path, (220, 180, 80))
        canonical_path = project / "references/characters/mira.png"
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        canonical_path.write_bytes(raster_bytes)
        raster_sha256 = hashlib.sha256(raster_bytes).hexdigest()
        receipt_id = handoff.attempt_id(job_id=reference_job["job_id"], attempt=1)
        receipt = build_generation_receipt(
            attempt_id=receipt_id,
            job_id=reference_job["job_id"],
            job_sha256=generation_job_sha256(reference_job),
            raster_path=reference_job["target_path"],
            raster_sha256=raster_sha256,
            executor_kind="external-tool",
            executor_id="fixture-renderer",
            provider="fixture-provider",
            model="fixture-model",
            capabilities_used={
                "reference_images": False,
                "dimensions": False,
                "localized_edit": False,
            },
            outcome="success",
            category="accepted",
        )
        receipt_path = project / f"generation/receipts/{receipt_id}.json"
        comic_sol.atomic_write_json(receipt_path, receipt)

        manifest = comic_sol.read_json(project / "handoff/manifest.json")
        descriptor = next(
            item for item in manifest["jobs"] if item["job_id"] == reference_job["job_id"]
        )
        descriptor["status"] = "completed"
        comic_sol.atomic_write_json(project / "handoff/manifest.json", manifest)
        return canonical_path, receipt_path, raster_sha256

    def test_schema_1_0_and_unprepared_1_1_inspection_is_read_only(self):
        inspect = _require_api(self, comic_sol, "inspect_handoff")

        for project_schema in ("1.0", "1.1"):
            with self.subTest(project_schema=project_schema):
                if project_schema == "1.0":
                    _, project = self._legacy_planner_project()
                else:
                    _, project = self._planner_project()
                lock_path = project / ".comic-sol.lock"
                lock_path.unlink(missing_ok=True)
                before = self._tree_snapshot(project)

                result = inspect(project)

                self.assertFalse(result["prepared"])
                self.assertEqual("prepare", result["next_action"])
                self.assertEqual(before, self._tree_snapshot(project))
                self.assertFalse(lock_path.exists())
                self.assertFalse((project / "handoff/manifest.json").exists())
                self.assertFalse((project / "logs/reference-selection.json").exists())
                self.assertEqual([], self._transaction_names(project))

    def test_prepare_composes_migration_and_handoff_in_one_project_last_transaction(self):
        from unittest import mock

        from scripts import project_io

        prepare = _require_api(self, comic_sol, "prepare_handoff")
        _, project = self._legacy_planner_project()
        legacy_manifest = comic_sol.read_json(project / "project.json")
        expected_migration = schema.migrate_project_manifest_in_memory(legacy_manifest)
        before_events = (project / "logs/events.jsonl").read_bytes()
        calls = []
        real_stage = project_io.ProjectTransaction.stage_bytes
        real_append = project_io.ProjectTransaction.append_bytes

        def observe_stage(transaction, relative, payload):
            calls.append((id(transaction), "replace", relative))
            return real_stage(transaction, relative, payload)

        def observe_append(transaction, relative, payload, **kwargs):
            calls.append((id(transaction), "append", relative))
            return real_append(transaction, relative, payload, **kwargs)

        with (
            mock.patch.object(
                project_io.ProjectTransaction,
                "stage_bytes",
                autospec=True,
                side_effect=observe_stage,
            ),
            mock.patch.object(
                project_io.ProjectTransaction,
                "append_bytes",
                autospec=True,
                side_effect=observe_append,
            ),
        ):
            result = prepare(project)

        self.assertTrue(result["changed"])
        self.assertTrue(result["migrated"])
        self.assertEqual("reference", result["phase"])
        published_project = comic_sol.read_json(project / "project.json")
        expected_migration["handoff"] = published_project["handoff"]
        self.assertEqual(expected_migration, published_project)
        handoff_manifest, jobs = self._manifest_jobs(project)
        self.assertEqual([], validate_handoff_manifest(handoff_manifest))
        self.assertEqual({("reference", "mira")}, set(jobs))
        job_path = f"generation/jobs/{jobs['reference', 'mira']['job_id']}.json"
        paths = [relative for _transaction, _operation, relative in calls]
        self.assertEqual(
            {
                "generation/batches.json",
                job_path,
                "handoff/manifest.json",
                "logs/events.jsonl",
                "logs/reference-selection.json",
                "project.json",
            },
            set(paths),
        )
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(1, len({transaction for transaction, _operation, _path in calls}))
        self.assertEqual("project.json", paths[-1])
        self.assertGreater(len((project / "logs/events.jsonl").read_bytes()), len(before_events))
        self.assertEqual(
            handoff.locked_scope_sha256(
                project,
                prompt_paths=(
                    "prompts/panels/p01-01.txt",
                    "prompts/references/mira.txt",
                ),
                reference_paths=(),
            ),
            handoff_manifest["locked_scope_sha256"],
        )
        self.assertEqual(
            handoff_manifest["locked_scope_sha256"],
            published_project["handoff"]["locked_scope_sha256"],
        )
        self.assertEqual([], self._transaction_names(project))

    def test_repeated_prepare_is_byte_preserving_and_appends_no_event(self):
        from unittest import mock

        from scripts import project_io

        prepare = _require_api(self, comic_sol, "prepare_handoff")
        inspect = _require_api(self, comic_sol, "inspect_handoff")
        _, project = self._planner_project()
        first = prepare(project)
        self.assertTrue(first["changed"])
        before = self._tree_snapshot(project)
        before_inspection = inspect(project)

        with (
            mock.patch.object(
                project_io.ProjectTransaction,
                "stage_bytes",
                side_effect=AssertionError("no-op prepare staged a replacement"),
            ) as stage_bytes,
            mock.patch.object(
                project_io.ProjectTransaction,
                "append_bytes",
                side_effect=AssertionError("no-op prepare staged an event"),
            ) as append_bytes,
        ):
            repeated = prepare(project)

        self.assertFalse(repeated["changed"])
        self.assertFalse(repeated["migrated"])
        stage_bytes.assert_not_called()
        append_bytes.assert_not_called()
        self.assertEqual(before, self._tree_snapshot(project))
        self.assertEqual(before_inspection, inspect(project))
        self.assertEqual([], self._transaction_names(project))

    def test_reference_phase_publishes_only_budget_zero_reference_work(self):
        prepare = _require_api(self, comic_sol, "prepare_handoff")
        inspect = _require_api(self, comic_sol, "inspect_handoff")
        _, project = self._planner_project()

        result = prepare(project)

        self.assertEqual("reference", result["phase"])
        self.assertTrue(result["changed"])
        self.assertFalse(result["migrated"])
        batches = comic_sol.read_json(project / "generation/batches.json")
        self.assertEqual([], handoff.validate_generation_batches(batches))
        self.assertEqual(
            [{"batch_id": "references-001", "kind": "reference"}],
            [
                {"batch_id": batch["batch_id"], "kind": batch["kind"]}
                for batch in batches["batches"]
            ],
        )
        _manifest, jobs = self._manifest_jobs(project)
        self.assertEqual({("reference", "mira")}, set(jobs))
        job = jobs["reference", "mira"]
        self.assertEqual([], validate_generation_job(job))
        self.assertEqual([], job["references"])
        self.assertIsNone(job["requested_dimensions"])
        self.assertIsNone(job["requested_aspect_ratio"])
        self.assertEqual(2, job["retry_limit"])
        self.assertEqual(
            "references/attempts/mira/initial-001.png",
            job["target_path"],
        )
        selection = comic_sol.read_json(project / "logs/reference-selection.json")
        self.assertEqual(0, selection["panels"][0]["reference_budget"])
        self.assertEqual([], selection["panels"][0]["selected"])
        self.assertEqual(
            ["references-unsupported"],
            [item["reason"] for item in selection["panels"][0]["omitted"]],
        )
        snapshot = inspect(project)
        self.assertTrue(snapshot["prepared"])
        self.assertEqual("reference", snapshot["phase"])
        self.assertEqual("current", snapshot["scope_state"])
        self.assertEqual(
            ("ready", 0, 3, 1),
            tuple(
                self._effective_job(snapshot, job["job_id"])[key]
                for key in (
                    "status",
                    "attempts_used",
                    "attempts_remaining",
                    "next_attempt",
                )
            ),
        )

    def test_panel_phase_preserves_completed_references_and_uses_unlimited_selection(self):
        prepare = _require_api(self, comic_sol, "prepare_handoff")
        inspect = _require_api(self, comic_sol, "inspect_handoff")
        _, project = self._planner_project()
        prepare(project)
        _reference_manifest, jobs = self._manifest_jobs(project)
        reference_job = jobs["reference", "mira"]
        reference_job_path = project / f"generation/jobs/{reference_job['job_id']}.json"
        reference_job_bytes = reference_job_path.read_bytes()
        canonical_path, receipt_path, canonical_sha256 = self._activate_current_reference(
            project, reference_job
        )
        receipt_bytes = receipt_path.read_bytes()
        completed = inspect(project)
        self.assertEqual(
            "completed",
            self._effective_job(completed, reference_job["job_id"])["status"],
        )

        result = prepare(project)

        self.assertEqual("panel", result["phase"])
        self.assertEqual(reference_job_bytes, reference_job_path.read_bytes())
        self.assertEqual(receipt_bytes, receipt_path.read_bytes())
        self.assertEqual(hashlib.sha256(canonical_path.read_bytes()).hexdigest(), canonical_sha256)
        batches = comic_sol.read_json(project / "generation/batches.json")
        self.assertEqual(
            [("references-001", "reference"), ("panels-001", "panel")],
            [(batch["batch_id"], batch["kind"]) for batch in batches["batches"]],
        )
        _panel_manifest, jobs = self._manifest_jobs(project)
        self.assertEqual({("reference", "mira"), ("panel", "p01-01")}, set(jobs))
        panel_job = jobs["panel", "p01-01"]
        self.assertEqual([], validate_generation_job(panel_job))
        self.assertEqual("initial", panel_job["attempt_kind"])
        self.assertEqual(2, panel_job["retry_limit"])
        self.assertEqual(
            "panels/attempts/p01-01/initial-001.png",
            panel_job["target_path"],
        )
        self.assertEqual(
            [reference_job["job_id"]],
            batches["batches"][0]["job_ids"],
        )
        self.assertEqual(
            [panel_job["job_id"]],
            batches["batches"][1]["job_ids"],
        )
        self.assertEqual(
            [{"path": "references/characters/mira.png", "sha256": canonical_sha256}],
            panel_job["references"],
        )
        self.assertEqual({"width": 1472, "height": 2272}, panel_job["requested_dimensions"])
        self.assertEqual("46:71", panel_job["requested_aspect_ratio"])
        selection = comic_sol.read_json(project / "logs/reference-selection.json")
        self.assertIsNone(selection["panels"][0]["reference_budget"])
        self.assertEqual(
            ["references/characters/mira.png"],
            [item["path"] for item in selection["panels"][0]["selected"]],
        )
        snapshot = inspect(project)
        self.assertEqual("panel", snapshot["phase"])
        self.assertEqual(
            "completed",
            self._effective_job(snapshot, reference_job["job_id"])["status"],
        )
        self.assertEqual(
            ("ready", 0, 3, 1),
            tuple(
                self._effective_job(snapshot, panel_job["job_id"])[key]
                for key in (
                    "status",
                    "attempts_used",
                    "attempts_remaining",
                    "next_attempt",
                )
            ),
        )

    def test_planning_prompt_and_reference_drift_is_stale_and_never_mutated(self):
        prepare = _require_api(self, comic_sol, "prepare_handoff")
        inspect = _require_api(self, comic_sol, "inspect_handoff")

        for changed_input in ("planning", "prompt", "reference"):
            with self.subTest(changed_input=changed_input):
                _, project = self._planner_project()
                prepare(project)
                if changed_input == "reference":
                    _manifest, jobs = self._manifest_jobs(project)
                    self._activate_current_reference(project, jobs["reference", "mira"])
                    prepare(project)
                    self._write_png(
                        project / "references/characters/mira.png",
                        (40, 80, 120),
                    )
                elif changed_input == "planning":
                    story = comic_sol.read_json(project / "plan/story-plan.json")
                    story["theme"] = "Hope must remain shared."
                    comic_sol.atomic_write_json(project / "plan/story-plan.json", story)
                else:
                    prompt = project / "prompts/references/mira.txt"
                    prompt.write_bytes(prompt.read_bytes() + b" Maintain the amber scarf.\n")
                before = self._tree_snapshot(project)

                stale = inspect(project)

                self.assertEqual("stale", stale["scope_state"])
                self.assertTrue(stale["jobs"])
                self.assertEqual({"stale"}, {job["status"] for job in stale["jobs"]})
                self.assertEqual(before, self._tree_snapshot(project))
                with self.assertRaises(handoff.StaleLockedScopeError):
                    prepare(project)
                self.assertEqual(before, self._tree_snapshot(project))

    def test_explicit_invalidation_releases_stale_handoff_for_reprepare(self):
        prepare = _require_api(self, comic_sol, "prepare_handoff")
        inspect = _require_api(self, comic_sol, "inspect_handoff")
        _, project = self._planner_project()
        prepared = prepare(project)
        self.assertTrue(prepared["changed"])
        sentinel = project / "user-retained-note.txt"
        sentinel.write_bytes(b"retain this user artifact\n")
        retained_paths = (
            "handoff/manifest.json",
            "generation/batches.json",
            "logs/reference-selection.json",
            "user-retained-note.txt",
        )
        retained_before = {
            relative: (project / relative).read_bytes() for relative in retained_paths
        }
        prompt = project / "prompts/references/mira.txt"
        prompt.write_bytes(prompt.read_bytes() + b" Preserve the amber scarf.\n")

        stale = inspect(project)

        self.assertEqual("stale", stale["scope_state"])
        self.assertEqual("invalidate", stale["next_action"])
        comic_sol.invalidate_from(project, "generation")
        self.assertEqual(
            {
                "contract_version": HANDOFF_CONTRACT_VERSION,
                "locked_scope_sha256": None,
                "manifest_path": None,
            },
            comic_sol.read_json(project / "project.json")["handoff"],
        )
        released = inspect(project)
        self.assertFalse(released["prepared"])
        self.assertEqual("unprepared", released["scope_state"])
        self.assertEqual("prepare", released["next_action"])
        self.assertEqual([], released["jobs"])
        self.assertEqual([], released["batches"])
        self.assertEqual(
            retained_before,
            {relative: (project / relative).read_bytes() for relative in retained_paths},
        )

        reparsed = prepare(project)

        self.assertTrue(reparsed["changed"])
        self.assertEqual("current", inspect(project)["scope_state"])
        self.assertEqual(b"retain this user artifact\n", sentinel.read_bytes())

    def test_reference_to_panel_advancement_requires_current_successful_receipt(self):
        prepare = _require_api(self, comic_sol, "prepare_handoff")
        _, project = self._planner_project()
        prepare(project)
        _manifest, jobs = self._manifest_jobs(project)
        reference_job = jobs["reference", "mira"]
        attempt_path = project / reference_job["target_path"]
        raster_bytes = self._write_png(attempt_path, (220, 180, 80))
        canonical_path = project / "references/characters/mira.png"
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        canonical_path.write_bytes(raster_bytes)
        before = self._tree_snapshot(project)

        with self.assertRaises(HandoffContractError):
            prepare(project)

        self.assertEqual(before, self._tree_snapshot(project))
        batches = comic_sol.read_json(project / "generation/batches.json")
        self.assertEqual(
            [("references-001", "reference")],
            [(batch["batch_id"], batch["kind"]) for batch in batches["batches"]],
        )
        self.assertFalse(any(batch["kind"] == "panel" for batch in batches["batches"]))

    def test_stage_fsync_and_publication_failures_restore_every_artifact_byte(self):
        import os
        from unittest import mock

        from scripts import project_io

        prepare = _require_api(self, comic_sol, "prepare_handoff")
        for boundary in ("stage", "fsync", "publication"):
            with self.subTest(boundary=boundary):
                _, project = self._legacy_planner_project()
                before = self._artifact_snapshot(project)
                legacy_project_bytes = (project / "project.json").read_bytes()
                injected = {"raised": False}

                if boundary == "stage":
                    real_stage = project_io.ProjectTransaction.stage_bytes
                    stage_calls = 0

                    def fail_after_stage(transaction, relative, payload):
                        nonlocal stage_calls
                        stage_calls += 1
                        real_stage(transaction, relative, payload)
                        if stage_calls == 3:
                            injected["raised"] = True
                            raise OSError("injected stage failure")

                    patcher = mock.patch.object(
                        project_io.ProjectTransaction,
                        "stage_bytes",
                        autospec=True,
                        side_effect=fail_after_stage,
                    )
                elif boundary == "fsync":
                    real_fsync = project_io.fsync_directory
                    project_identity = project.resolve()

                    def fail_after_project_publish(path):
                        if Path(path).resolve() == project_identity and not injected["raised"]:
                            injected["raised"] = True
                            raise OSError("injected fsync failure")
                        return real_fsync(path)

                    patcher = mock.patch.object(
                        project_io,
                        "fsync_directory",
                        side_effect=fail_after_project_publish,
                    )
                else:
                    replace_calls = 0
                    project_identity = project.resolve()
                    if os.name == "nt" or not getattr(os, "O_NOFOLLOW", 0):
                        real_replace = os.replace

                        def fail_during_publication(source, destination):
                            nonlocal replace_calls
                            source_path = Path(source).resolve()
                            destination_path = Path(destination).resolve()
                            source_relative = source_path.relative_to(project_identity).as_posix()
                            destination_relative = destination_path.relative_to(
                                project_identity
                            ).as_posix()
                            is_publication = source_relative.startswith(
                                "logs/transactions/"
                            ) and not destination_relative.startswith("logs/transactions/")
                            if is_publication:
                                replace_calls += 1
                                if replace_calls == 3:
                                    injected["raised"] = True
                                    raise OSError("injected publication failure")
                            return real_replace(source, destination)

                        patcher = mock.patch.object(
                            project_io.os,
                            "replace",
                            side_effect=fail_during_publication,
                        )
                    else:
                        real_replace = project_io.replace_contained

                        def fail_during_publication(
                            project_dir,
                            source_relative,
                            destination_relative,
                        ):
                            nonlocal replace_calls
                            replace_calls += 1
                            if replace_calls == 3:
                                injected["raised"] = True
                                raise OSError("injected publication failure")
                            return real_replace(
                                project_dir,
                                source_relative,
                                destination_relative,
                            )

                        patcher = mock.patch.object(
                            project_io,
                            "replace_contained",
                            side_effect=fail_during_publication,
                        )

                with patcher, self.assertRaisesRegex(OSError, f"injected {boundary} failure"):
                    prepare(project)

                self.assertTrue(injected["raised"])
                self.assertEqual(legacy_project_bytes, (project / "project.json").read_bytes())
                self.assertEqual(before, self._artifact_snapshot(project))
                project_io.ProjectTransaction.recover(project)
                self.assertEqual(before, self._artifact_snapshot(project))
                self.assertEqual([], self._transaction_names(project))
                self.assertFalse((project / "handoff/manifest.json").exists())
                self.assertFalse((project / "logs/reference-selection.json").exists())
                self.assertEqual([], list((project / "generation/jobs").glob("*.json")))


class HandoffIntakeFailureTests(unittest.TestCase):
    """Authoritative WP2 step-6 result-intake and failure coverage."""

    def _planner_project(self):
        return HandoffLifecycleGoldenTests._planner_project(self)

    @staticmethod
    def _manifest_jobs(project):
        return HandoffLifecycleGoldenTests._manifest_jobs(project)

    @staticmethod
    def _effective_job(snapshot, job_id):
        return HandoffLifecycleGoldenTests._effective_job(snapshot, job_id)

    @staticmethod
    def _artifact_snapshot(project):
        return HandoffPrepareInspectTests._artifact_snapshot(project)

    @staticmethod
    def _transaction_names(project):
        return HandoffPrepareInspectTests._transaction_names(project)

    @staticmethod
    def _write_png(path, color):
        return HandoffPrepareInspectTests._write_png(path, color)

    @staticmethod
    def _write_raster(path, size, color, *, image_format="PNG"):
        Image.new("RGB", size, color).save(path, format=image_format)
        return path.read_bytes()

    @staticmethod
    def _receipt_path(project, job_id, attempt):
        identifier = handoff.attempt_id(job_id=job_id, attempt=attempt)
        return project / f"generation/receipts/{identifier}.json"

    @staticmethod
    def _success_arguments(job, raster_path, *, attempt=1, approve_reference=False):
        return {
            "job_id": job["job_id"],
            "attempt": attempt,
            "raster_path": raster_path,
            "executor_kind": "external-tool",
            "executor_id": "fixture-renderer",
            "provider": "fixture-provider",
            "model": "fixture-model",
            "capabilities_used": {
                "reference_images": job["subject_kind"] == "panel",
                "dimensions": job["requested_dimensions"] is not None,
                "localized_edit": False,
            },
            "approve_reference": approve_reference,
        }

    @staticmethod
    def _failure_arguments(job, *, attempt, category="transient-tool-error"):
        return {
            "job_id": job["job_id"],
            "attempt": attempt,
            "executor_kind": "external-tool",
            "executor_id": "fixture-renderer",
            "provider": "fixture-provider",
            "model": "fixture-model",
            "capabilities_used": {
                "reference_images": job["subject_kind"] == "panel",
                "dimensions": job["requested_dimensions"] is not None,
                "localized_edit": False,
            },
            "category": category,
        }

    def _prepared_reference(self):
        root, project = self._planner_project()
        comic_sol.prepare_handoff(project)
        _manifest, jobs = self._manifest_jobs(project)
        return root, project, jobs["reference", "mira"]

    def _prepared_panel(self):
        root, project, reference_job = self._prepared_reference()
        HandoffPrepareInspectTests._activate_current_reference(self, project, reference_job)
        comic_sol.prepare_handoff(project)
        _manifest, jobs = self._manifest_jobs(project)
        return root, project, jobs["panel", "p01-01"]

    def test_external_absolute_png_intake_retains_only_canonical_result_data(self):
        accept = _require_api(self, comic_sol, "accept_handoff_result")
        root, project, panel_job = self._prepared_panel()
        dimensions = panel_job["requested_dimensions"]
        self.assertEqual("46:71", panel_job["requested_aspect_ratio"])
        raster = root / "private-renderer-output.png"
        raster_bytes = self._write_raster(
            raster,
            (dimensions["width"], dimensions["height"]),
            (20, 30, 40),
        )
        self.assertTrue(raster.is_absolute())
        self.assertNotEqual(project, raster.parent)
        before_events = (project / "logs/events.jsonl").read_bytes()

        result = accept(project, **self._success_arguments(panel_job, raster))

        retained = project / panel_job["target_path"]
        receipt_path = self._receipt_path(project, panel_job["job_id"], 1)
        receipt = comic_sol.read_json(receipt_path)
        self.assertEqual(raster_bytes, retained.read_bytes())
        with Image.open(retained) as image:
            self.assertEqual("PNG", image.format)
            self.assertEqual(
                (dimensions["width"], dimensions["height"]),
                image.size,
            )
            self.assertEqual(
                image.width * 71,
                image.height * 46,
            )
        self.assertEqual([], validate_generation_receipt(receipt))
        self.assertEqual(panel_job["target_path"], receipt["raster_path"])
        self.assertEqual(hashlib.sha256(raster_bytes).hexdigest(), receipt["raster_sha256"])
        self.assertNotIn(str(raster), json.dumps(receipt, sort_keys=True))
        self.assertNotIn(str(raster), json.dumps(result, sort_keys=True))
        self.assertNotIn(str(raster.parent), json.dumps(result, sort_keys=True))
        self.assertFalse((project / "panels/raw/p01-01.png").exists())
        manifest = comic_sol.read_json(project / "handoff/manifest.json")
        descriptor = next(
            item for item in manifest["jobs"] if item["job_id"] == panel_job["job_id"]
        )
        self.assertEqual("completed", descriptor["status"])
        self.assertEqual(
            {
                "global_extra_calls": 0,
                "panels": {
                    "p01-01": {
                        "initial": 1,
                        "transient_repeats": 0,
                        "visual_retries": 0,
                    }
                },
                "schema_version": "1.0",
            },
            comic_sol.read_json(project / "logs/generation-counters.json"),
        )
        after_events = (project / "logs/events.jsonl").read_bytes()
        self.assertNotEqual(before_events, after_events)
        source_locator = str(raster).encode("utf-8")
        for relative, payload in self._artifact_snapshot(project).items():
            self.assertNotIn(source_locator, payload, relative)

    def test_relative_png_intake_is_absolutized_without_resolving_source(self):
        import os

        accept = _require_api(self, comic_sol, "accept_handoff_result")
        root, project, panel_job = self._prepared_panel()
        dimensions = panel_job["requested_dimensions"]
        relative = Path("relative-renderer-output.png")
        self._write_raster(
            root / relative,
            (dimensions["width"], dimensions["height"]),
            (25, 35, 45),
        )
        previous_cwd = Path.cwd()
        try:
            os.chdir(root)
            expected_source = relative.expanduser().absolute()
            with mock.patch.object(
                comic_sol,
                "read_bytes_nofollow",
                wraps=comic_sol.read_bytes_nofollow,
            ) as read_nofollow:
                result = accept(
                    project,
                    **self._success_arguments(panel_job, relative),
                )
        finally:
            os.chdir(previous_cwd)

        self.assertEqual(panel_job["target_path"], result["raster_path"])
        source_path = read_nofollow.call_args.args[0]
        self.assertTrue(source_path.is_absolute())
        self.assertEqual(expected_source, source_path)
        self.assertEqual(
            (root / relative).read_bytes(),
            (project / panel_job["target_path"]).read_bytes(),
        )

    def test_unexpandable_raster_path_is_invalid_input_without_mutation(self):
        import contextlib
        import io

        accept = _require_api(self, comic_sol, "accept_handoff_result")
        root, project, panel_job = self._prepared_panel()
        raster_path = Path("~comic-sol-user-that-must-not-exist/result.png")
        before = self._artifact_snapshot(project)

        with self.assertRaisesRegex(HandoffResultError, "raster.*path"):
            accept(project, **self._success_arguments(panel_job, raster_path))
        self.assertEqual(before, self._artifact_snapshot(project))

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = comic_sol.main(
                [
                    "handoff",
                    "accept-result",
                    str(project),
                    "--job",
                    panel_job["job_id"],
                    "--attempt",
                    "1",
                    "--path",
                    str(raster_path),
                    "--executor-kind",
                    "external-tool",
                    "--executor-id",
                    "fixture-renderer",
                    "--json",
                ]
            )

        self.assertEqual(2, code)
        self.assertEqual("", stdout.getvalue())
        self.assertRegex(stderr.getvalue(), r"^ERROR HandoffResultError: .*raster.*path.*\n$")
        self.assertEqual(before, self._artifact_snapshot(project))
        self.assertEqual([], self._transaction_names(project))
        self.assertFalse((root / "result.png").exists())

    def test_intake_rejects_non_png_wrong_dimensions_and_wrong_aspect_without_writes(self):
        accept = _require_api(self, comic_sol, "accept_handoff_result")
        cases = (
            ("jpeg", "JPEG", (1472, 2272), "PNG"),
            ("dimensions", "PNG", (736, 1136), "dimension"),
            ("aspect", "PNG", (1472, 1472), "dimension|aspect"),
        )
        for label, image_format, size, message in cases:
            with self.subTest(label=label):
                root, project, panel_job = self._prepared_panel()
                raster = root / f"invalid-{label}.bin"
                self._write_raster(
                    raster,
                    size,
                    (80, 90, 100),
                    image_format=image_format,
                )
                before = self._artifact_snapshot(project)

                with self.assertRaisesRegex(ValueError, message):
                    accept(project, **self._success_arguments(panel_job, raster))

                self.assertEqual(before, self._artifact_snapshot(project))
                self.assertEqual([], self._transaction_names(project))
                self.assertFalse((project / panel_job["target_path"]).exists())
                self.assertFalse(self._receipt_path(project, panel_job["job_id"], 1).exists())

    def test_ordinals_duplicates_conflicts_and_completed_jobs_fail_closed(self):
        from unittest import mock

        from scripts import project_io

        accept = _require_api(self, comic_sol, "accept_handoff_result")
        root, project, panel_job = self._prepared_panel()
        dimensions = panel_job["requested_dimensions"]
        raster = root / "ordered-result.png"
        self._write_raster(
            raster,
            (dimensions["width"], dimensions["height"]),
            (20, 30, 40),
        )
        before = self._artifact_snapshot(project)

        with self.assertRaisesRegex(HandoffContractError, "ordinal|next attempt|contiguous"):
            accept(
                project,
                **self._success_arguments(panel_job, raster, attempt=2),
            )
        self.assertEqual(before, self._artifact_snapshot(project))

        arguments = self._success_arguments(panel_job, raster)
        accept(project, **arguments)
        accepted = self._artifact_snapshot(project)
        with (
            mock.patch.object(
                project_io.ProjectTransaction,
                "stage_bytes",
                side_effect=AssertionError("exact duplicate staged a replacement"),
            ) as stage_bytes,
            mock.patch.object(
                project_io.ProjectTransaction,
                "append_bytes",
                side_effect=AssertionError("exact duplicate staged an event"),
            ) as append_bytes,
        ):
            accept(project, **arguments)
        stage_bytes.assert_not_called()
        append_bytes.assert_not_called()
        self.assertEqual(accepted, self._artifact_snapshot(project))

        conflicting = dict(arguments)
        conflicting["model"] = "different-fixture-model"
        with self.assertRaisesRegex(HandoffContractError, "conflict"):
            accept(project, **conflicting)
        self.assertEqual(accepted, self._artifact_snapshot(project))

        with self.assertRaisesRegex(HandoffContractError, "completed"):
            accept(
                project,
                **self._success_arguments(panel_job, raster, attempt=2),
            )
        self.assertEqual(accepted, self._artifact_snapshot(project))
        self.assertEqual([], self._transaction_names(project))

    def test_rasterless_failures_publish_sanitized_receipts_and_exhaust_retries(self):
        from unittest import mock

        from scripts import project_io

        record_failure = _require_api(self, comic_sol, "record_handoff_failure")
        _root, project, panel_job = self._prepared_panel()
        events_before = (project / "logs/events.jsonl").read_bytes()
        pristine = self._artifact_snapshot(project)

        with self.assertRaisesRegex(HandoffContractError, "ordinal|next attempt|contiguous"):
            record_failure(
                project,
                **self._failure_arguments(panel_job, attempt=2),
            )
        self.assertEqual(pristine, self._artifact_snapshot(project))

        for attempt, expected in (
            (1, ("ready", 1, 2, 2)),
            (2, ("ready", 2, 1, 3)),
            (3, ("failed", 3, 0, None)),
        ):
            record_failure(
                project,
                **self._failure_arguments(panel_job, attempt=attempt),
            )
            receipt_path = self._receipt_path(project, panel_job["job_id"], attempt)
            receipt = comic_sol.read_json(receipt_path)
            expected_receipt = build_generation_receipt(
                attempt_id=handoff.attempt_id(job_id=panel_job["job_id"], attempt=attempt),
                job_id=panel_job["job_id"],
                job_sha256=generation_job_sha256(panel_job),
                raster_path=None,
                raster_sha256=None,
                executor_kind="external-tool",
                executor_id="fixture-renderer",
                provider="fixture-provider",
                model="fixture-model",
                capabilities_used={
                    "reference_images": True,
                    "dimensions": True,
                    "localized_edit": False,
                },
                outcome="failure",
                category="transient-tool-error",
            )
            self.assertEqual(expected_receipt, receipt)
            self.assertEqual(canonical_artifact_bytes(expected_receipt), receipt_path.read_bytes())
            snapshot = comic_sol.inspect_handoff(project)
            effective = self._effective_job(snapshot, panel_job["job_id"])
            self.assertEqual(
                expected,
                tuple(
                    effective[key]
                    for key in (
                        "status",
                        "attempts_used",
                        "attempts_remaining",
                        "next_attempt",
                    )
                ),
            )
            self.assertFalse((project / panel_job["target_path"]).exists())
            self.assertFalse((project / "logs/generation-counters.json").exists())
            if attempt == 1:
                first_failure = self._artifact_snapshot(project)
                duplicate_arguments = self._failure_arguments(panel_job, attempt=1)
                with (
                    mock.patch.object(
                        project_io.ProjectTransaction,
                        "stage_bytes",
                        side_effect=AssertionError("exact failure duplicate staged a replacement"),
                    ) as stage_bytes,
                    mock.patch.object(
                        project_io.ProjectTransaction,
                        "append_bytes",
                        side_effect=AssertionError("exact failure duplicate staged an event"),
                    ) as append_bytes,
                ):
                    record_failure(project, **duplicate_arguments)
                stage_bytes.assert_not_called()
                append_bytes.assert_not_called()
                self.assertEqual(first_failure, self._artifact_snapshot(project))

                conflicting_arguments = dict(duplicate_arguments)
                conflicting_arguments["category"] = "provider-refusal"
                with self.assertRaisesRegex(HandoffContractError, "conflict"):
                    record_failure(project, **conflicting_arguments)
                self.assertEqual(first_failure, self._artifact_snapshot(project))
                self.assertEqual([], self._transaction_names(project))

        events_after = (project / "logs/events.jsonl").read_bytes()
        self.assertNotEqual(events_before, events_after)
        exhausted = self._artifact_snapshot(project)
        with self.assertRaisesRegex(HandoffContractError, "exhausted|failed|attempt"):
            record_failure(
                project,
                **self._failure_arguments(panel_job, attempt=4),
            )
        self.assertEqual(exhausted, self._artifact_snapshot(project))
        self.assertEqual([], self._transaction_names(project))

        root, project, panel_job = self._prepared_panel()
        record_failure(project, **self._failure_arguments(panel_job, attempt=1))
        dimensions = panel_job["requested_dimensions"]
        raster = root / "retry-success.png"
        self._write_raster(
            raster,
            (dimensions["width"], dimensions["height"]),
            (100, 110, 120),
        )
        comic_sol.accept_handoff_result(
            project,
            **self._success_arguments(panel_job, raster, attempt=2),
        )
        counters = comic_sol.read_json(project / "logs/generation-counters.json")
        self.assertEqual(1, counters["panels"]["p01-01"]["initial"])
        self.assertEqual(0, counters["global_extra_calls"])

    def test_reference_activation_requires_approval_and_never_overwrites_canonical(self):
        accept = _require_api(self, comic_sol, "accept_handoff_result")
        root, project, reference_job = self._prepared_reference()
        raster = root / "reference-result.png"
        raster_bytes = self._write_raster(raster, (512, 512), (220, 180, 80))
        before = self._artifact_snapshot(project)

        with self.assertRaisesRegex(HandoffContractError, "approve"):
            accept(
                project,
                **self._success_arguments(
                    reference_job,
                    raster,
                    approve_reference=False,
                ),
            )
        self.assertEqual(before, self._artifact_snapshot(project))

        accept(
            project,
            **self._success_arguments(
                reference_job,
                raster,
                approve_reference=True,
            ),
        )
        self.assertEqual(
            raster_bytes,
            (project / reference_job["target_path"]).read_bytes(),
        )
        self.assertEqual(
            raster_bytes,
            (project / "references/characters/mira.png").read_bytes(),
        )
        self.assertFalse((project / "logs/generation-counters.json").exists())

        root, project, reference_job = self._prepared_reference()
        canonical = project / "references/characters/mira.png"
        original_canonical = HandoffPrepareInspectTests._write_png(
            canonical,
            (15, 25, 35),
        )
        replacement = root / "replacement-reference.png"
        self._write_raster(replacement, (512, 512), (220, 180, 80))
        before = self._artifact_snapshot(project)
        with self.assertRaisesRegex(HandoffContractError, "canonical|overwrite|exists"):
            accept(
                project,
                **self._success_arguments(
                    reference_job,
                    replacement,
                    approve_reference=True,
                ),
            )
        self.assertEqual(before, self._artifact_snapshot(project))
        self.assertEqual(original_canonical, canonical.read_bytes())
        self.assertEqual([], self._transaction_names(project))

    def test_stage_fsync_and_publication_failures_restore_all_intake_bytes(self):
        import os
        from unittest import mock

        from scripts import project_io

        accept = _require_api(self, comic_sol, "accept_handoff_result")
        record_failure = _require_api(self, comic_sol, "record_handoff_failure")
        for operation in ("reference", "panel", "failure"):
            for boundary in ("stage", "fsync", "publication"):
                with self.subTest(operation=operation, boundary=boundary):
                    if operation == "reference":
                        root, project, job = self._prepared_reference()
                        raster = root / f"rollback-{operation}-{boundary}.png"
                        self._write_raster(raster, (512, 512), (40, 50, 60))
                        approve_reference = True
                    else:
                        root, project, job = self._prepared_panel()
                        approve_reference = False
                        if operation == "panel":
                            dimensions = job["requested_dimensions"]
                            raster = root / f"rollback-{operation}-{boundary}.png"
                            self._write_raster(
                                raster,
                                (dimensions["width"], dimensions["height"]),
                                (40, 50, 60),
                            )
                        else:
                            raster = None
                    canonical = project / "references/characters/mira.png"
                    canonical_before = canonical.read_bytes() if canonical.exists() else None
                    before = self._artifact_snapshot(project)
                    injected = {"raised": False}

                    if boundary == "stage":
                        real_stage = project_io.ProjectTransaction.stage_bytes
                        stage_calls = 0

                        def fail_after_stage(transaction, relative, payload):
                            nonlocal stage_calls
                            stage_calls += 1
                            real_stage(transaction, relative, payload)
                            if stage_calls == 2:
                                injected["raised"] = True
                                raise OSError("injected stage failure")

                        patcher = mock.patch.object(
                            project_io.ProjectTransaction,
                            "stage_bytes",
                            autospec=True,
                            side_effect=fail_after_stage,
                        )
                    elif boundary == "fsync":
                        real_fsync = project_io.fsync_directory
                        transactions = (project / "logs/transactions").resolve()

                        def fail_after_destination_fsync(path):
                            resolved = Path(path).resolve()
                            inside_transactions = (
                                resolved == transactions or transactions in resolved.parents
                            )
                            if not inside_transactions and not injected["raised"]:
                                injected["raised"] = True
                                raise OSError("injected fsync failure")
                            return real_fsync(path)

                        patcher = mock.patch.object(
                            project_io,
                            "fsync_directory",
                            side_effect=fail_after_destination_fsync,
                        )
                    else:
                        replace_calls = 0
                        project_identity = project.resolve()
                        if os.name == "nt" or not getattr(os, "O_NOFOLLOW", 0):
                            real_replace = os.replace

                            def fail_during_publication(source, destination):
                                nonlocal replace_calls
                                source_path = Path(source).resolve()
                                destination_path = Path(destination).resolve()
                                source_relative = source_path.relative_to(
                                    project_identity
                                ).as_posix()
                                destination_relative = destination_path.relative_to(
                                    project_identity
                                ).as_posix()
                                is_publication = source_relative.startswith(
                                    "logs/transactions/"
                                ) and not destination_relative.startswith("logs/transactions/")
                                if is_publication:
                                    replace_calls += 1
                                    if replace_calls == 2 and not injected["raised"]:
                                        injected["raised"] = True
                                        raise OSError("injected publication failure")
                                return real_replace(source, destination)

                            patcher = mock.patch.object(
                                project_io.os,
                                "replace",
                                side_effect=fail_during_publication,
                            )
                        else:
                            real_replace = project_io.replace_contained

                            def fail_during_publication(
                                project_dir,
                                source_relative,
                                destination_relative,
                            ):
                                nonlocal replace_calls
                                is_publication = source_relative.startswith(
                                    "logs/transactions/"
                                ) and not destination_relative.startswith("logs/transactions/")
                                if is_publication:
                                    replace_calls += 1
                                    if replace_calls == 2 and not injected["raised"]:
                                        injected["raised"] = True
                                        raise OSError("injected publication failure")
                                return real_replace(
                                    project_dir,
                                    source_relative,
                                    destination_relative,
                                )

                            patcher = mock.patch.object(
                                project_io,
                                "replace_contained",
                                side_effect=fail_during_publication,
                            )

                    with patcher, self.assertRaisesRegex(OSError, f"injected {boundary} failure"):
                        if operation == "failure":
                            record_failure(
                                project,
                                **self._failure_arguments(job, attempt=1),
                            )
                        else:
                            accept(
                                project,
                                **self._success_arguments(
                                    job,
                                    raster,
                                    approve_reference=approve_reference,
                                ),
                            )

                    self.assertTrue(injected["raised"])
                    self.assertEqual(before, self._artifact_snapshot(project))
                    current_canonical = canonical.read_bytes() if canonical.exists() else None
                    self.assertEqual(canonical_before, current_canonical)
                    project_io.ProjectTransaction.recover(project)
                    self.assertEqual(before, self._artifact_snapshot(project))
                    self.assertEqual([], self._transaction_names(project))


class HandoffStep10RegressionTests(unittest.TestCase):
    """Authoritative WP2 step-10 adversarial lifecycle regressions."""

    def _planner_project(self):
        return HandoffLifecycleGoldenTests._planner_project(self)

    @staticmethod
    def _manifest_jobs(project):
        return HandoffLifecycleGoldenTests._manifest_jobs(project)

    @staticmethod
    def _artifact_snapshot(project):
        return HandoffPrepareInspectTests._artifact_snapshot(project)

    @staticmethod
    def _transaction_names(project):
        return HandoffPrepareInspectTests._transaction_names(project)

    @staticmethod
    def _write_png(path, color):
        return HandoffPrepareInspectTests._write_png(path, color)

    @staticmethod
    def _write_raster(path, size, color, *, image_format="PNG"):
        return HandoffIntakeFailureTests._write_raster(
            path,
            size,
            color,
            image_format=image_format,
        )

    @staticmethod
    def _success_arguments(job, raster_path, *, attempt=1, approve_reference=False):
        return HandoffIntakeFailureTests._success_arguments(
            job,
            raster_path,
            attempt=attempt,
            approve_reference=approve_reference,
        )

    @staticmethod
    def _failure_arguments(job, *, attempt=1, category="transient-tool-error"):
        return HandoffIntakeFailureTests._failure_arguments(
            job,
            attempt=attempt,
            category=category,
        )

    def _prepared_reference(self):
        root, project = self._planner_project()
        comic_sol.prepare_handoff(project)
        _manifest, jobs = self._manifest_jobs(project)
        return root, project, jobs["reference", "mira"]

    def _prepared_panel(self):
        root, project, reference_job = self._prepared_reference()
        HandoffPrepareInspectTests._activate_current_reference(self, project, reference_job)
        comic_sol.prepare_handoff(project)
        _manifest, jobs = self._manifest_jobs(project)
        return root, project, jobs["panel", "p01-01"]

    def _assert_rejected_without_mutation(self, project, action, expected_exception):
        before = self._artifact_snapshot(project)
        transactions_before = self._transaction_names(project)
        error = None
        try:
            action()
        except Exception as caught:  # noqa: BLE001 - the assertion checks the boundary type.
            error = caught
        self.assertEqual(before, self._artifact_snapshot(project))
        self.assertEqual(transactions_before, self._transaction_names(project))
        self.assertIsNotNone(error, "unsafe handoff input was accepted")
        self.assertIsInstance(error, expected_exception)
        return error

    @staticmethod
    def _failure_receipt(job, *, job_id=None, job_sha256=None):
        bound_job_id = job["job_id"] if job_id is None else job_id
        return build_generation_receipt(
            attempt_id=handoff.attempt_id(job_id=bound_job_id, attempt=1),
            job_id=bound_job_id,
            job_sha256=(generation_job_sha256(job) if job_sha256 is None else job_sha256),
            raster_path=None,
            raster_sha256=None,
            executor_kind="external-tool",
            executor_id="fixture-renderer",
            provider="fixture-provider",
            model="fixture-model",
            capabilities_used={
                "reference_images": job["subject_kind"] == "panel",
                "dimensions": job["requested_dimensions"] is not None,
                "localized_edit": False,
            },
            outcome="failure",
            category="transient-tool-error",
        )

    def test_prepare_rejects_missing_unknown_ambiguous_and_orphan_prompt_identities(self):
        cases = (
            ("missing-reference", "required prompt is missing"),
            ("unknown-reference", "unknown reference identity"),
            ("ambiguous-reference", "identity is ambiguous"),
            ("missing-panel", "required prompt is missing"),
            ("orphan-panel", "orphan panel prompt"),
        )
        for case, message in cases:
            with self.subTest(case=case):
                _root, project = self._planner_project()
                if case == "missing-reference":
                    (project / "prompts/references/mira.txt").unlink()
                elif case == "unknown-reference":
                    (project / "prompts/references/unknown.txt").write_text(
                        "Unknown identity reference.",
                        encoding="utf-8",
                    )
                elif case == "ambiguous-reference":
                    story = comic_sol.read_json(project / "plan/story-plan.json")
                    story["scenes"][0]["id"] = "mira"
                    story["scenes"][0]["continuity_anchor"] = "amber scarf"
                    comic_sol.atomic_write_json(project / "plan/story-plan.json", story)
                    storyboard = comic_sol.read_json(project / "plan/storyboard.json")
                    storyboard["pages"][0]["panels"][0]["scene_id"] = "mira"
                    comic_sol.atomic_write_json(project / "plan/storyboard.json", storyboard)
                elif case == "missing-panel":
                    (project / "prompts/panels/p01-01.txt").unlink()
                else:
                    (project / "prompts/panels/p01-02.txt").write_text(
                        "Orphan panel prompt.",
                        encoding="utf-8",
                    )

                error = self._assert_rejected_without_mutation(
                    project,
                    lambda: comic_sol.prepare_handoff(project),
                    HandoffContractError,
                )

                self.assertRegex(str(error), message)
                self.assertFalse((project / "handoff/manifest.json").exists())
                self.assertFalse((project / "generation/batches.json").exists())
                self.assertFalse((project / "logs/reference-selection.json").exists())

    def test_prepare_rejects_symlinked_prompt_without_mutation(self):
        root, project = self._planner_project()
        prompt = project / "prompts/references/mira.txt"
        prompt.unlink()
        outside = root / "outside-reference-prompt.txt"
        outside.write_text("Mira reference prompt.", encoding="utf-8")
        make_symlink(self, prompt, outside)

        error = self._assert_rejected_without_mutation(
            project,
            lambda: comic_sol.prepare_handoff(project),
            ValueError,
        )

        self.assertRegex(str(error), "security-error.*symlink")
        self.assertFalse((project / "handoff/manifest.json").exists())

    def test_prepare_rejects_unmanaged_reference_and_panel_targets_without_mutation(self):
        for phase in ("reference", "panel"):
            with self.subTest(phase=phase):
                if phase == "reference":
                    _root, project = self._planner_project()
                    target = project / "references/attempts/mira/initial-001.png"
                else:
                    _root, project, reference_job = self._prepared_reference()
                    HandoffPrepareInspectTests._activate_current_reference(
                        self,
                        project,
                        reference_job,
                    )
                    target = project / "panels/attempts/p01-01/initial-001.png"
                sentinel = self._write_png(target, (12, 34, 56))

                error = self._assert_rejected_without_mutation(
                    project,
                    lambda: comic_sol.prepare_handoff(project),
                    HandoffContractError,
                )

                self.assertRegex(str(error), "unmanaged retained target collision")
                self.assertEqual(sentinel, target.read_bytes())

    def test_prepare_rejects_unmanaged_receipt_collision_without_any_publication(self):
        _oracle_root, oracle = self._planner_project()
        comic_sol.prepare_handoff(oracle)
        _manifest, jobs = self._manifest_jobs(oracle)
        intended_job = jobs["reference", "mira"]

        _root, project = self._planner_project()
        receipt = self._failure_receipt(intended_job)
        receipt_path = project / f"generation/receipts/{receipt['attempt_id']}.json"
        comic_sol.atomic_write_json(receipt_path, receipt)
        before = self._artifact_snapshot(project)

        error = self._assert_rejected_without_mutation(
            project,
            lambda: comic_sol.prepare_handoff(project),
            HandoffContractError,
        )

        self.assertRegex(str(error), "unmanaged receipt collision")
        self.assertEqual(before, self._artifact_snapshot(project))
        self.assertEqual(canonical_artifact_bytes(receipt), receipt_path.read_bytes())
        self.assertFalse((project / "handoff/manifest.json").exists())
        self.assertFalse((project / "generation/batches.json").exists())
        self.assertFalse((project / "logs/reference-selection.json").exists())
        self.assertEqual([], self._transaction_names(project))

    def test_prepare_rejects_malformed_and_non_png_canonical_references_without_mutation(self):
        for case in ("malformed", "jpeg"):
            with self.subTest(case=case):
                _root, project = self._planner_project()
                canonical = project / "references/characters/mira.png"
                if case == "malformed":
                    canonical.write_bytes(b"not-a-raster")
                else:
                    self._write_raster(
                        canonical,
                        (512, 512),
                        (70, 80, 90),
                        image_format="JPEG",
                    )

                error = self._assert_rejected_without_mutation(
                    project,
                    lambda: comic_sol.prepare_handoff(project),
                    (HandoffContractError, ValueError),
                )

                self.assertRegex(str(error), "raster|PNG|format")
                self.assertFalse((project / "handoff/manifest.json").exists())

    def test_stale_job_scope_and_planning_input_reject_all_intake_without_mutation(self):
        cases = (
            ("job", HandoffContractError),
            ("scope", handoff.StaleLockedScopeError),
            ("input", handoff.StaleLockedScopeError),
        )
        for case, expected_exception in cases:
            for operation in ("accept", "failure"):
                with self.subTest(case=case, operation=operation):
                    root, project, job = self._prepared_panel()
                    dimensions = job["requested_dimensions"]
                    raster = root / f"stale-{case}-{operation}.png"
                    self._write_raster(
                        raster,
                        (dimensions["width"], dimensions["height"]),
                        (20, 30, 40),
                    )
                    if case == "job":
                        job_path = project / f"generation/jobs/{job['job_id']}.json"
                        tampered = comic_sol.read_json(job_path)
                        tampered["retry_limit"] = 1
                        comic_sol.atomic_write_json(job_path, tampered)
                    elif case == "scope":
                        prompt = project / "prompts/panels/p01-01.txt"
                        prompt.write_bytes(prompt.read_bytes() + b" Scope drift.\n")
                    else:
                        story = comic_sol.read_json(project / "plan/story-plan.json")
                        story["theme"] = "Planning drift must invalidate intake."
                        comic_sol.atomic_write_json(project / "plan/story-plan.json", story)

                    def action():
                        if operation == "accept":
                            return comic_sol.accept_handoff_result(
                                project,
                                **self._success_arguments(job, raster),
                            )
                        return comic_sol.record_handoff_failure(
                            project,
                            **self._failure_arguments(job),
                        )

                    self._assert_rejected_without_mutation(
                        project,
                        action,
                        expected_exception,
                    )

    def test_missing_selected_reference_is_stale_for_all_intake_without_mutation(self):
        for operation in ("accept", "failure"):
            with self.subTest(operation=operation):
                root, project, job = self._prepared_panel()
                dimensions = job["requested_dimensions"]
                raster = root / f"missing-selected-reference-{operation}.png"
                self._write_raster(
                    raster,
                    (dimensions["width"], dimensions["height"]),
                    (20, 30, 40),
                )
                canonical = project / "references/characters/mira.png"
                canonical.unlink()

                def action():
                    if operation == "accept":
                        return comic_sol.accept_handoff_result(
                            project,
                            **self._success_arguments(job, raster),
                        )
                    return comic_sol.record_handoff_failure(
                        project,
                        **self._failure_arguments(job),
                    )

                self._assert_rejected_without_mutation(
                    project,
                    action,
                    handoff.StaleLockedScopeError,
                )

    def test_symlinked_receipt_blocks_inspection_and_all_intake_without_mutation(self):
        root, project, job = self._prepared_reference()
        receipt = self._failure_receipt(job)
        outside = root / "outside-receipt.json"
        outside.write_bytes(canonical_artifact_bytes(receipt))
        receipt_dir = project / "generation/receipts"
        receipt_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = receipt_dir / f"{receipt['attempt_id']}.json"
        make_symlink(self, receipt_path, outside)
        raster = root / "receipt-symlink-result.png"
        self._write_raster(raster, (512, 512), (20, 30, 40))
        actions = (
            ("inspect", lambda: comic_sol.inspect_handoff(project)),
            (
                "accept",
                lambda: comic_sol.accept_handoff_result(
                    project,
                    **self._success_arguments(
                        job,
                        raster,
                        approve_reference=True,
                    ),
                ),
            ),
            (
                "failure",
                lambda: comic_sol.record_handoff_failure(
                    project,
                    **self._failure_arguments(job),
                ),
            ),
        )
        for operation, action in actions:
            with self.subTest(operation=operation):
                error = self._assert_rejected_without_mutation(
                    project,
                    action,
                    ValueError,
                )
                self.assertRegex(str(error), "security-error.*symlink")

    def test_symlinked_external_raster_is_rejected_without_mutation(self):
        root, project, job = self._prepared_reference()
        outside = root / "real-external-result.png"
        self._write_raster(outside, (512, 512), (20, 30, 40))
        linked = root / "linked-external-result.png"
        make_symlink(self, linked, outside)

        error = self._assert_rejected_without_mutation(
            project,
            lambda: comic_sol.accept_handoff_result(
                project,
                **self._success_arguments(
                    job,
                    linked,
                    approve_reference=True,
                ),
            ),
            (HandoffContractError, ValueError),
        )

        self.assertRegex(str(error), "read safely|symlink|reparse")
        self.assertFalse((project / job["target_path"]).exists())

    def test_encoded_and_decoded_intake_limits_reject_without_mutation(self):
        cases = ("encoded", "decoded")
        for case in cases:
            with self.subTest(case=case):
                root, project, job = self._prepared_reference()
                raster = root / f"oversized-{case}.png"
                if case == "encoded":
                    raster.write_bytes(b"x" * 1025)
                    patcher = mock.patch.object(
                        comic_sol,
                        "MAX_ENCODED_RASTER_BYTES",
                        1024,
                    )
                else:
                    self._write_raster(raster, (512, 512), (20, 30, 40))
                    patcher = mock.patch.object(
                        comic_sol,
                        "MAX_DECODED_PIXELS",
                        512 * 512 - 1,
                    )

                with patcher:
                    error = self._assert_rejected_without_mutation(
                        project,
                        lambda: comic_sol.accept_handoff_result(
                            project,
                            **self._success_arguments(
                                job,
                                raster,
                                approve_reference=True,
                            ),
                        ),
                        InputResourceLimitError,
                    )

                self.assertRegex(str(error), "limit")
                self.assertFalse((project / job["target_path"]).exists())

    def test_reference_intake_rejects_malformed_and_non_png_results_without_mutation(self):
        for case in ("malformed", "jpeg"):
            with self.subTest(case=case):
                root, project, job = self._prepared_reference()
                raster = root / f"invalid-reference-{case}.bin"
                if case == "malformed":
                    raster.write_bytes(b"not-a-raster")
                else:
                    self._write_raster(
                        raster,
                        (512, 512),
                        (70, 80, 90),
                        image_format="JPEG",
                    )

                error = self._assert_rejected_without_mutation(
                    project,
                    lambda: comic_sol.accept_handoff_result(
                        project,
                        **self._success_arguments(
                            job,
                            raster,
                            approve_reference=True,
                        ),
                    ),
                    HandoffContractError,
                )

                self.assertRegex(str(error), "readable PNG|format must match PNG")
                self.assertFalse((project / job["target_path"]).exists())
                self.assertFalse((project / "references/characters/mira.png").exists())

    def test_executor_metadata_control_characters_reject_all_intake_without_mutation(self):
        cases = (
            ("executor_id", "fixture\nrenderer"),
            ("provider", "fixture\x7fprovider"),
            ("model", "fixture\x85model"),
        )
        for field, invalid_value in cases:
            for operation in ("accept", "failure"):
                with self.subTest(field=field, operation=operation):
                    root, project, job = self._prepared_reference()
                    raster = root / f"control-{field}-{operation}.png"
                    self._write_raster(raster, (512, 512), (20, 30, 40))
                    if operation == "accept":
                        arguments = self._success_arguments(
                            job,
                            raster,
                            approve_reference=True,
                        )
                    else:
                        arguments = self._failure_arguments(job)
                    arguments[field] = invalid_value

                    def action():
                        if operation == "accept":
                            return comic_sol.accept_handoff_result(
                                project,
                                **arguments,
                            )
                        return comic_sol.record_handoff_failure(
                            project,
                            **arguments,
                        )

                    self._assert_rejected_without_mutation(
                        project,
                        action,
                        handoff.HandoffResultError,
                    )

    def test_persisted_receipt_collisions_block_inspection_and_all_intake_without_mutation(self):
        for case in ("filename", "duplicate", "unknown-job"):
            with self.subTest(case=case):
                root, project, job = self._prepared_reference()
                receipt_dir = project / "generation/receipts"
                receipt_dir.mkdir(parents=True, exist_ok=True)
                if case == "unknown-job":
                    receipt = self._failure_receipt(
                        job,
                        job_id="f" * 64,
                        job_sha256="e" * 64,
                    )
                    comic_sol.atomic_write_json(
                        receipt_dir / f"{receipt['attempt_id']}.json",
                        receipt,
                    )
                else:
                    receipt = self._failure_receipt(job)
                    if case == "duplicate":
                        comic_sol.atomic_write_json(
                            receipt_dir / f"{receipt['attempt_id']}.json",
                            receipt,
                        )
                        filename = "duplicate-alias.json"
                    else:
                        filename = "wrong-name.json"
                    comic_sol.atomic_write_json(receipt_dir / filename, receipt)
                raster = root / f"persisted-receipt-{case}.png"
                self._write_raster(raster, (512, 512), (20, 30, 40))
                actions = (
                    ("inspect", lambda: comic_sol.inspect_handoff(project)),
                    (
                        "accept",
                        lambda: comic_sol.accept_handoff_result(
                            project,
                            **self._success_arguments(
                                job,
                                raster,
                                approve_reference=True,
                            ),
                        ),
                    ),
                    (
                        "failure",
                        lambda: comic_sol.record_handoff_failure(
                            project,
                            **self._failure_arguments(job),
                        ),
                    ),
                )
                for operation, action in actions:
                    with self.subTest(operation=operation):
                        error = self._assert_rejected_without_mutation(
                            project,
                            action,
                            HandoffContractError,
                        )
                        if case == "unknown-job":
                            self.assertRegex(str(error), "current handoff job")
                        else:
                            self.assertRegex(str(error), "filename|duplicate attempt")

    def test_receipts_after_success_block_inspection_without_mutation(self):
        cases = (
            ("failure", "transient-tool-error", "after successful receipt"),
            ("success", "accepted", "multiple successful receipts"),
        )
        for later_outcome, category, message in cases:
            with self.subTest(later_outcome=later_outcome):
                _root, project, job = self._prepared_reference()
                _canonical, _first_receipt, raster_sha256 = (
                    HandoffPrepareInspectTests._activate_current_reference(self, project, job)
                )
                success = later_outcome == "success"
                identifier = handoff.attempt_id(job_id=job["job_id"], attempt=2)
                later_receipt = build_generation_receipt(
                    attempt_id=identifier,
                    job_id=job["job_id"],
                    job_sha256=generation_job_sha256(job),
                    raster_path=job["target_path"] if success else None,
                    raster_sha256=raster_sha256 if success else None,
                    executor_kind="external-tool",
                    executor_id="fixture-renderer",
                    provider="fixture-provider",
                    model="fixture-model",
                    capabilities_used={
                        "reference_images": False,
                        "dimensions": False,
                        "localized_edit": False,
                    },
                    outcome=later_outcome,
                    category=category,
                )
                later_path = project / f"generation/receipts/{identifier}.json"
                comic_sol.atomic_write_json(later_path, later_receipt)

                error = self._assert_rejected_without_mutation(
                    project,
                    lambda: comic_sol.inspect_handoff(project),
                    HandoffContractError,
                )

                self.assertRegex(str(error), message)
                self.assertEqual(canonical_artifact_bytes(later_receipt), later_path.read_bytes())

    def test_reference_phase_locks_preexisting_canonical_scene_rasters(self):
        _root, project = self._planner_project()
        scene_reference = project / "references/scenes/delivery-hall.png"
        self._write_raster(scene_reference, (512, 512), (30, 40, 50))

        prepared = comic_sol.prepare_handoff(project)
        self.assertEqual("reference", prepared["phase"])
        self.assertEqual("current", comic_sol.inspect_handoff(project)["scope_state"])

        self._write_raster(scene_reference, (512, 512), (80, 90, 100))
        inspection = comic_sol.inspect_handoff(project)

        self.assertEqual("stale", inspection["scope_state"])
        self.assertEqual("invalidate", inspection["next_action"])
        self.assertEqual({"stale"}, {job["status"] for job in inspection["jobs"]})

    def test_deleted_approved_reference_is_stale_instead_of_reentering_reference_phase(self):
        _root, project, reference_job = self._prepared_reference()
        HandoffPrepareInspectTests._activate_current_reference(self, project, reference_job)
        (project / "references/characters/mira.png").unlink()

        inspection = comic_sol.inspect_handoff(project)

        self.assertEqual("stale", inspection["scope_state"])
        self.assertEqual("invalidate", inspection["next_action"])
        self.assertEqual("stale", inspection["jobs"][0]["status"])
        project_issues = validate_project(project, "plan")
        self.assertTrue(
            any(
                issue.path == "references/characters/mira.png"
                and issue.field == "file"
                and "successful reference receipt" in issue.message
                for issue in project_issues
            ),
            project_issues,
        )
        with self.assertRaises(handoff.StaleLockedScopeError):
            comic_sol.prepare_handoff(project)

    def test_tampered_approved_reference_fails_validation_receipt_binding(self):
        _root, project, reference_job = self._prepared_reference()
        HandoffPrepareInspectTests._activate_current_reference(self, project, reference_job)
        self._write_raster(
            project / "references/characters/mira.png",
            (512, 512),
            (90, 100, 110),
        )

        project_issues = validate_project(project, "plan")

        self.assertTrue(
            any(
                issue.path == "references/characters/mira.png"
                and issue.field == "sha256"
                and "successful reference receipt" in issue.message
                for issue in project_issues
            ),
            project_issues,
        )

    def test_successful_retained_raster_still_matches_job_geometry_on_resume(self):
        root, project, job = self._prepared_panel()
        dimensions = job["requested_dimensions"]
        rendered = root / "geometry-bound-panel.png"
        self._write_raster(
            rendered,
            (dimensions["width"], dimensions["height"]),
            (20, 30, 40),
        )
        result = comic_sol.accept_handoff_result(
            project,
            **self._success_arguments(job, rendered),
        )
        retained = project / result["raster_path"]
        self._write_raster(retained, (512, 512), (70, 80, 90))
        receipt = comic_sol.read_json(project / result["receipt_path"])
        receipt["raster_sha256"] = hashlib.sha256(retained.read_bytes()).hexdigest()
        comic_sol.atomic_write_json(project / result["receipt_path"], receipt)

        with self.assertRaisesRegex(HandoffContractError, "generation job|dimensions"):
            comic_sol.inspect_handoff(project)
        project_issues = validate_project(project, "plan")
        self.assertTrue(
            any(
                issue.path == result["raster_path"]
                and issue.field == "dimensions"
                and "generation job" in issue.message
                for issue in project_issues
            ),
            project_issues,
        )

    def test_failed_job_reports_retry_exhaustion_instead_of_render_action(self):
        _root, project, job = self._prepared_reference()
        for attempt in range(1, 4):
            comic_sol.record_handoff_failure(
                project,
                **self._failure_arguments(job, attempt=attempt),
            )

        inspection = comic_sol.inspect_handoff(project)

        self.assertEqual("retry-exhausted", inspection["next_action"])
        self.assertEqual("failed", inspection["jobs"][0]["status"])
        self.assertIsNone(inspection["jobs"][0]["next_attempt"])

    def test_invalid_attempt_ordinal_is_a_result_error_for_both_intake_routes(self):
        root, project, job = self._prepared_reference()
        raster = root / "invalid-ordinal.png"
        self._write_raster(raster, (512, 512), (20, 30, 40))
        actions = (
            lambda: comic_sol.accept_handoff_result(
                project,
                **self._success_arguments(
                    job,
                    raster,
                    attempt=0,
                    approve_reference=True,
                ),
            ),
            lambda: comic_sol.record_handoff_failure(
                project,
                **self._failure_arguments(job, attempt=0),
            ),
        )
        for action in actions:
            with self.subTest(action=action):
                self._assert_rejected_without_mutation(
                    project,
                    action,
                    handoff.HandoffResultError,
                )


if __name__ == "__main__":
    unittest.main()
