"""Contract tests for the canonical Web-to-engine project gateway."""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import io
import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from typing import Mapping, cast, get_type_hints
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from web.tests import support as _support  # noqa: F401  # Checkout import path setup.

from comic_sol_web.api.projects import create_projects_router
from comic_sol_web.auth import SessionPrincipal, require_principal
from comic_sol_web.database import Database
from comic_sol_web.engine_gateway import (
    EngineGateway,
    GatewayInputError,
    PROJECT_MIGRATION,
    PROJECT_MIGRATIONS,
    ProjectSnapshot,
    ProjectUnavailableError,
    StaleProjectRevisionError,
)
from comic_sol_web.generation.types import GenerationRequest
from comic_sol_web.migrations import APPLICATION_MIGRATIONS, Migration, apply_migrations
from comic_sol_web.projects import ProjectService
from scripts import comic_sol
from scripts.core_primitives import canonical_artifact_bytes, canonical_json_bytes
from scripts.handoff import HandoffResultError
from scripts.handoff_archive import HandoffArchiveError, export_handoff_archive
from scripts.schema import CURRENT_PROJECT_SCHEMA_VERSION, read_project_manifest
from scripts.validate_project import validate_manifest, validate_project
from tests.test_validation import valid_story, valid_storyboard


CAPABILITIES_USED = {
    "dimensions": True,
    "localized_edit": False,
    "reference_images": False,
}
PANEL_CAPABILITIES_USED = {**CAPABILITIES_USED, "reference_images": True}


def tree_snapshot(root: Path) -> dict[str, bytes]:
    return {
        relative: path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        if (relative := path.relative_to(root).as_posix()) != ".comic-sol.lock"
        and not relative.startswith("logs/transactions/")
    }


def plan_payload(root: Path) -> dict[str, str]:
    return {
        "storyPlan": (root / "plan/story-plan.json").read_text(encoding="utf-8"),
        "storyboard": (root / "plan/storyboard.json").read_text(encoding="utf-8"),
        "visualIdentityPack": (root / "plan/character-identity-pack.json").read_text(
            encoding="utf-8"
        ),
    }


def first_plan_payload() -> dict[str, str]:
    story = valid_story()
    for scene in story["scenes"]:
        scene["characters"] = []
    storyboard = valid_storyboard()
    panel = storyboard["pages"][0]["panels"][0]
    panel["characters"] = []
    panel["continuity"] = []
    panel["text"] = []
    return {
        "storyPlan": json.dumps(story),
        "storyboard": json.dumps(storyboard),
        "visualIdentityPack": json.dumps({"characters": [], "schema_version": "1.0"}),
    }


def portable_archive(test_case: unittest.TestCase) -> Path:
    from tests.test_handoff_lifecycle import HandoffLifecycleGoldenTests

    _root, project = HandoffLifecycleGoldenTests._planner_project(test_case)
    comic_sol.prepare_handoff(project)
    archive_root = Path(tempfile.mkdtemp())
    test_case.addCleanup(shutil.rmtree, archive_root, True)
    archive = archive_root / "sunlight-courier.comic-sol-handoff"
    export_handoff_archive(project, archive)
    return archive


def archive_with_schema(archive: Path, schema_version: str) -> Path:
    mutated = archive.with_name(f"unsupported-{schema_version}.comic-sol-handoff")
    with zipfile.ZipFile(archive, "r") as source:
        entries = [(info, source.read(info)) for info in source.infolist()]
    updated: list[tuple[zipfile.ZipInfo, bytes]] = []
    for info, payload in entries:
        if info.filename == "project/project.json":
            manifest = json.loads(payload)
            manifest["schema_version"] = schema_version
            payload = canonical_json_bytes(manifest)
        updated.append((info, payload))
    checksums = json.loads(
        next(payload for info, payload in updated if info.filename == "checksums.json")
    )
    project_payloads = {info.filename: payload for info, payload in updated}
    for item in checksums["files"]:
        item["sha256"] = hashlib.sha256(project_payloads[item["path"]]).hexdigest()
    checksum_payload = canonical_json_bytes(checksums)
    updated = [
        (info, checksum_payload if info.filename == "checksums.json" else payload)
        for info, payload in updated
    ]
    with zipfile.ZipFile(mutated, "w") as output:
        for info, payload in updated:
            output.writestr(info, payload)
    return mutated


class GatewayFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.data_root = Path(self.temporary_directory.name) / "web-data"
        self.database = Database(self.data_root / "application.sqlite3")
        apply_migrations(self.database, PROJECT_MIGRATIONS)
        self.gateway = EngineGateway(self.database, self.data_root)
        self.service = ProjectService(self.gateway)
        self.alice = SessionPrincipal("alice-id", "alice")
        self.bob = SessionPrincipal("bob-id", "bob")

    def create(self, *, title: str = "Gateway Story") -> ProjectSnapshot:
        return self.service.create_project(
            self.alice,
            {
                "title": title,
                "prompt": "A courier carries the last light through a quiet city.",
                "language": "en",
                "page_count": 1,
            },
        )

    def import_prepared(self) -> ProjectSnapshot:
        return self.service.import_project(self.alice, portable_archive(self))

    def staged_raster(self, request: GenerationRequest, color: str = "navy") -> Path:
        path = self.gateway.staging_root / f"{request.job_id}-{color}.png"
        Image.new("RGB", (request.width, request.height), color).save(path)
        return path


class EngineGatewayContractTests(GatewayFixture):
    def test_snapshot_and_gateway_signatures_are_the_frozen_contract(self) -> None:
        self.assertEqual(
            ["project_id", "revision", "root", "status", "summary"],
            [field.name for field in dataclasses.fields(ProjectSnapshot)],
        )
        self.assertTrue(getattr(ProjectSnapshot, "__dataclass_params__").frozen)
        self.assertEqual(
            {
                "project_id": str,
                "revision": int,
                "root": Path,
                "status": str,
                "summary": Mapping[str, object],
            },
            get_type_hints(ProjectSnapshot),
        )
        expected = {
            "create_project": (
                ["self", "owner_id", "request"],
                {"owner_id": str, "request": Mapping[str, object], "return": ProjectSnapshot},
            ),
            "import_project": (
                ["self", "owner_id", "archive"],
                {"owner_id": str, "archive": Path, "return": ProjectSnapshot},
            ),
            "snapshot": (
                ["self", "project_id", "expected_revision"],
                {
                    "project_id": str,
                    "expected_revision": int | None,
                    "return": ProjectSnapshot,
                },
            ),
            "read_plan": (
                ["self", "project_id", "expected_revision"],
                {
                    "project_id": str,
                    "expected_revision": int | None,
                    "return": ProjectSnapshot,
                },
            ),
            "update_plan": (
                ["self", "project_id", "expected_revision", "plan"],
                {
                    "project_id": str,
                    "expected_revision": int,
                    "plan": Mapping[str, object],
                    "return": ProjectSnapshot,
                },
            ),
            "prepare_generation": (
                ["self", "project_id", "expected_revision"],
                {
                    "project_id": str,
                    "expected_revision": int,
                    "return": tuple[GenerationRequest, ...],
                },
            ),
            "submit_raster": (
                [
                    "self",
                    "project_id",
                    "expected_revision",
                    "job_id",
                    "raster",
                    "media_type",
                    "capabilities_used",
                ],
                {
                    "project_id": str,
                    "expected_revision": int,
                    "job_id": str,
                    "raster": Path,
                    "media_type": str,
                    "capabilities_used": Mapping[str, object],
                    "return": ProjectSnapshot,
                },
            ),
            "run_qa": (
                ["self", "project_id", "expected_revision"],
                {
                    "project_id": str,
                    "expected_revision": int,
                    "return": ProjectSnapshot,
                },
            ),
            "export": (
                ["self", "project_id", "expected_revision", "formats"],
                {
                    "project_id": str,
                    "expected_revision": int,
                    "formats": tuple[str, ...],
                    "return": Mapping[str, Path],
                },
            ),
        }
        for name, (parameters, hints) in expected.items():
            with self.subTest(method=name):
                method = getattr(EngineGateway, name)
                self.assertEqual(parameters, list(inspect.signature(method).parameters))
                self.assertEqual(hints, get_type_hints(method))
        self.assertIs(
            GenerationRequest,
            get_type_hints(EngineGateway.prepare_generation)["return"].__args__[0],
        )

    def test_project_schema_is_numbered_and_rolls_back_atomically(self) -> None:
        with self.database.read() as connection:
            versions = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            )
        self.assertEqual(tuple(range(1, PROJECT_MIGRATION.version + 1)), versions)

        rollback_root = Path(self.temporary_directory.name) / "migration-rollback"
        rollback_database = Database(rollback_root / "application.sqlite3")
        apply_migrations(rollback_database, APPLICATION_MIGRATIONS)
        broken = Migration(
            PROJECT_MIGRATION.version,
            PROJECT_MIGRATION.statements
            + ("CREATE TABLE web_projects (duplicate_definition TEXT)",),
        )
        with self.assertRaises(sqlite3.OperationalError):
            apply_migrations(rollback_database, (*APPLICATION_MIGRATIONS, broken))
        with rollback_database.read() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'web_projects'"
                ).fetchone()
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = ?",
                    (PROJECT_MIGRATION.version,),
                ).fetchone()
            )

    def test_create_from_scratch_publishes_a_canonical_prompt_project(self) -> None:
        snapshot = self.create()
        self.assertEqual(1, snapshot.revision)
        self.assertEqual("INIT", snapshot.status)
        self.assertRegex(snapshot.project_id, r"\A[A-Za-z0-9_-]{32}\Z")
        self.assertNotEqual(snapshot.project_id, snapshot.root.name)
        self.assertEqual("gateway-story", snapshot.root.name)
        self.assertEqual(
            b"A courier carries the last light through a quiet city.",
            (snapshot.root / "source/input.txt").read_bytes(),
        )
        self.assertEqual(
            {"language": "en", "mode": "short_prompt"},
            json.loads((snapshot.root / "source/request.json").read_bytes()),
        )
        manifest = read_project_manifest(snapshot.root / "project.json")
        self.assertEqual(CURRENT_PROJECT_SCHEMA_VERSION, manifest["schema_version"])
        self.assertEqual([], validate_manifest(manifest))
        self.assertEqual(snapshot.root, self.gateway.snapshot(snapshot.project_id).root)

    def test_create_honors_source_mode_and_rejects_page_count_above_engine_limit(self) -> None:
        story = self.service.create_project(
            self.alice,
            {
                "title": "Pasted Story",
                "prompt": "A complete story pasted by its creator.",
                "language": "en",
                "mode": "pasted_story",
                "page_count": 4,
            },
        )
        self.assertEqual(
            {"language": "en", "mode": "pasted_story"},
            json.loads((story.root / "source/request.json").read_bytes()),
        )
        before = set(self.gateway.projects_root.iterdir())
        with self.assertRaises(ValueError):
            self.service.create_project(
                self.alice,
                {
                    "title": "Too Many Pages",
                    "prompt": "This request must fail closed.",
                    "mode": "short_prompt",
                    "page_count": 5,
                },
            )
        self.assertEqual(before, set(self.gateway.projects_root.iterdir()))

    def test_supported_portable_archive_import_is_canonical(self) -> None:
        snapshot = self.import_prepared()
        self.assertEqual(1, snapshot.revision)
        self.assertEqual("STORYBOARDED", snapshot.status)
        self.assertEqual("sunlight-courier", snapshot.summary["engine_project_id"])
        self.assertEqual(CURRENT_PROJECT_SCHEMA_VERSION, snapshot.summary["schema_version"])
        self.assertTrue((snapshot.root / "handoff/manifest.json").is_file())
        self.assertEqual(
            [], validate_manifest(read_project_manifest(snapshot.root / "project.json"))
        )

    def test_imported_plan_is_loaded_from_canonical_artifacts_without_paths(self) -> None:
        snapshot = self.import_prepared()
        loaded = self.service.read_plan(self.alice, snapshot.project_id, snapshot.revision)
        plan = cast(Mapping[str, str], loaded.summary["plan"])
        self.assertEqual(plan_payload(snapshot.root), dict(plan))
        self.assertEqual(valid_story(), json.loads(plan["storyPlan"]))
        self.assertEqual(valid_storyboard(), json.loads(plan["storyboard"]))
        self.assertNotIn(str(snapshot.root), json.dumps(dict(plan), sort_keys=True))

    def test_first_plan_update_publishes_canonical_artifacts_atomically(self) -> None:
        snapshot = self.create()
        candidate = first_plan_payload()

        updated = self.service.update_plan(
            self.alice,
            snapshot.project_id,
            snapshot.revision,
            candidate,
        )

        self.assertEqual(snapshot.revision + 1, updated.revision)
        self.assertEqual(candidate.keys(), plan_payload(snapshot.root).keys())
        self.assertEqual(
            {"characters": [], "schema_version": "1.0"},
            json.loads((snapshot.root / "plan/character-bible.json").read_bytes()),
        )
        manifest = read_project_manifest(snapshot.root / "project.json")
        self.assertEqual(1, manifest["settings"]["panel_count"])
        self.assertEqual(["p01-01"], manifest["panels"])
        self.assertEqual(
            {"story_plan", "character_bible", "storyboard"},
            set(manifest["artifacts"]),
        )
        self.assertEqual([], validate_project(snapshot.root, "storyboard"))

    def test_plan_read_serializes_complete_snapshot_with_updates(self) -> None:
        snapshot = self.import_prepared()
        candidate = plan_payload(snapshot.root)
        story = json.loads(candidate["storyPlan"])
        story["theme"] = "A concurrent update remains one coherent snapshot."
        candidate["storyPlan"] = json.dumps(story)
        read_started = threading.Event()
        release_read = threading.Event()
        update_finished = threading.Event()
        errors: list[BaseException] = []
        original_summary = EngineGateway._plan_summary

        def blocking_summary(root: Path) -> dict[str, str]:
            if threading.current_thread().name == "plan-reader":
                read_started.set()
                if not release_read.wait(5):
                    raise TimeoutError("test did not release Plan read")
            return original_summary(root)

        def read() -> None:
            try:
                self.gateway.read_plan(snapshot.project_id, snapshot.revision)
            except BaseException as error:
                errors.append(error)

        def update() -> None:
            try:
                self.gateway.update_plan(snapshot.project_id, snapshot.revision, candidate)
            except BaseException as error:
                errors.append(error)
            finally:
                update_finished.set()

        with mock.patch.object(
            EngineGateway,
            "_plan_summary",
            new=staticmethod(blocking_summary),
        ):
            reader = threading.Thread(target=read, name="plan-reader")
            updater = threading.Thread(target=update, name="plan-updater")
            reader.start()
            self.assertTrue(read_started.wait(2))
            updater.start()
            self.assertFalse(update_finished.wait(0.2))
            release_read.set()
            reader.join(5)
            updater.join(5)
        self.assertFalse(reader.is_alive())
        self.assertFalse(updater.is_alive())
        self.assertEqual([], errors)

    def test_plan_update_commits_canonical_bytes_and_survives_gateway_reload(self) -> None:
        snapshot = self.import_prepared()
        candidate = plan_payload(snapshot.root)
        story = json.loads(candidate["storyPlan"])
        story["theme"] = "Hope remains shared through every delivery."
        candidate["storyPlan"] = json.dumps(story)

        updated = self.service.update_plan(
            self.alice,
            snapshot.project_id,
            snapshot.revision,
            candidate,
        )
        self.assertEqual(snapshot.revision + 1, updated.revision)
        self.assertEqual(
            canonical_artifact_bytes(story),
            (snapshot.root / "plan/story-plan.json").read_bytes(),
        )
        self.assertEqual([], validate_project(snapshot.root, "storyboard"))
        manifest = read_project_manifest(snapshot.root / "project.json")
        self.assertEqual(
            {"contract_version": "1.0", "locked_scope_sha256": None, "manifest_path": None},
            manifest["handoff"],
        )
        reopened = EngineGateway(self.database, self.data_root)
        persisted = reopened.read_plan(snapshot.project_id, updated.revision)
        self.assertEqual(
            plan_payload(snapshot.root),
            dict(cast(Mapping[str, str], persisted.summary["plan"])),
        )

    def test_plan_edits_fail_closed_until_prompts_are_rebuilt(self) -> None:
        snapshot = self.import_prepared()
        candidate = plan_payload(snapshot.root)
        storyboard = json.loads(candidate["storyboard"])
        storyboard["pages"][0]["panels"][0]["beat"] = "A newly reviewed beat."
        candidate["storyboard"] = json.dumps(storyboard)
        prompt = snapshot.root / "prompts/panels/p01-01.txt"
        prompt_before = prompt.read_bytes()

        updated = self.gateway.update_plan(
            snapshot.project_id,
            snapshot.revision,
            candidate,
        )

        self.assertEqual("INIT", updated.status)
        self.assertEqual(prompt_before, prompt.read_bytes())
        with self.assertRaisesRegex(GatewayInputError, "planning and storyboard"):
            self.gateway.prepare_generation(snapshot.project_id, updated.revision)
        self.assertEqual(prompt_before, prompt.read_bytes())

    def test_failed_plan_updates_preserve_project_bytes_and_web_revision_exactly(self) -> None:
        snapshot = self.import_prepared()
        before = tree_snapshot(snapshot.root)
        candidate = plan_payload(snapshot.root)

        operations = (
            lambda: self.gateway.update_plan(snapshot.project_id, 0, candidate),
            lambda: self.gateway.update_plan(
                snapshot.project_id,
                snapshot.revision,
                {**candidate, "storyPlan": "not json"},
            ),
            lambda: self.gateway.update_plan(
                snapshot.project_id,
                snapshot.revision,
                {**candidate, "storyboard": json.dumps({"schema_version": "1.0", "pages": []})},
            ),
            lambda: self.service.update_plan(
                self.bob,
                snapshot.project_id,
                snapshot.revision,
                candidate,
            ),
        )
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                operation()
            self.assertEqual(before, tree_snapshot(snapshot.root))
            self.assertEqual(snapshot.revision, self.gateway.snapshot(snapshot.project_id).revision)

        interrupted = dict(candidate)
        interrupted_story = json.loads(interrupted["storyPlan"])
        interrupted_story["theme"] = "A changed theme that must not partially publish."
        interrupted["storyPlan"] = json.dumps(interrupted_story)
        interrupted_storyboard = json.loads(interrupted["storyboard"])
        interrupted_storyboard["pages"][0]["panels"][0]["beat"] = (
            "A changed beat that must not partially publish."
        )
        interrupted["storyboard"] = json.dumps(interrupted_storyboard)

        original_stage = getattr(
            __import__("comic_sol_web.engine_gateway", fromlist=["ProjectTransaction"]),
            "ProjectTransaction",
        ).stage_bytes
        calls = 0

        def interrupt(transaction, relative, payload):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected interrupted Plan staging")
            return original_stage(transaction, relative, payload)

        with mock.patch(
            "comic_sol_web.engine_gateway.ProjectTransaction.stage_bytes",
            new=interrupt,
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted Plan staging"):
                self.gateway.update_plan(snapshot.project_id, snapshot.revision, interrupted)
        self.assertEqual(before, tree_snapshot(snapshot.root))
        self.assertEqual(snapshot.revision, self.gateway.snapshot(snapshot.project_id).revision)

    def test_created_and_imported_projects_converge_on_canonical_engine_state(self) -> None:
        created = self.create(title="New Canonical Project")
        imported = self.import_prepared()
        for snapshot in (created, imported):
            manifest = read_project_manifest(snapshot.root / "project.json")
            with self.subTest(project=snapshot.project_id):
                self.assertEqual(CURRENT_PROJECT_SCHEMA_VERSION, manifest["schema_version"])
                self.assertEqual([], validate_manifest(manifest))
                self.assertTrue((snapshot.root / "source/input.txt").is_file())
                self.assertTrue((snapshot.root / "source/request.json").is_file())
                self.assertNotIn(snapshot.project_id, tree_snapshot(snapshot.root))

    def test_create_and_import_roll_back_project_and_ownership_on_record_failure(self) -> None:
        archive = portable_archive(self)
        for operation in (
            lambda: self.gateway.create_project(
                self.alice.user_id,
                {"title": "Rollback", "prompt": "Rollback this project."},
            ),
            lambda: self.gateway.import_project(self.alice.user_id, archive),
        ):
            with self.subTest(operation=operation):
                with mock.patch.object(
                    self.gateway,
                    "_record_project",
                    side_effect=sqlite3.IntegrityError("record refused"),
                ):
                    with self.assertRaises(sqlite3.IntegrityError):
                        operation()
                self.assertEqual([], list(self.gateway.projects_root.iterdir()))
                with self.database.read() as connection:
                    self.assertEqual(
                        0, connection.execute("SELECT COUNT(*) FROM web_projects").fetchone()[0]
                    )
                residue = [
                    path.name
                    for path in self.data_root.rglob("*")
                    if path.name.startswith((".comic-sol-init-", ".comic-sol-handoff-"))
                    or path.name.isdigit()
                ]
                self.assertEqual([], residue)

    def test_project_ownership_is_required_for_every_application_operation(self) -> None:
        snapshot = self.create()
        with self.assertRaises(ProjectUnavailableError):
            self.service.snapshot(self.bob, snapshot.project_id)
        with self.assertRaises(ProjectUnavailableError):
            self.service.prepare_generation(self.bob, snapshot.project_id, snapshot.revision)
        self.assertEqual(snapshot, self.service.snapshot(self.alice, snapshot.project_id))

    def test_stale_revision_is_rejected_before_mutation(self) -> None:
        snapshot = self.import_prepared()
        before = tree_snapshot(snapshot.root)
        with self.assertRaises(StaleProjectRevisionError) as caught:
            self.gateway.prepare_generation(snapshot.project_id, 0)
        self.assertEqual((0, 1), (caught.exception.expected, caught.exception.actual))
        self.assertEqual(before, tree_snapshot(snapshot.root))

    def test_snapshot_is_a_strictly_read_only_operation(self) -> None:
        snapshot = self.import_prepared()
        lock = snapshot.root / ".comic-sol.lock"
        lock.unlink(missing_ok=True)
        before = tree_snapshot(snapshot.root)
        reopened = self.gateway.snapshot(snapshot.project_id, snapshot.revision)
        self.assertEqual(snapshot.project_id, reopened.project_id)
        self.assertEqual(snapshot.revision, reopened.revision)
        self.assertEqual(before, tree_snapshot(snapshot.root))
        self.assertFalse(lock.exists())

    def test_prepare_generation_binds_each_request_to_project_revision(self) -> None:
        snapshot = self.import_prepared()
        requests = self.service.prepare_generation(
            self.alice, snapshot.project_id, snapshot.revision
        )
        self.assertEqual(1, len(requests))
        request = requests[0]
        self.assertIsInstance(request, GenerationRequest)
        self.assertEqual(snapshot.project_id, request.project_id)
        self.assertEqual(snapshot.revision, request.project_revision)
        self.assertEqual("reference", request.subject_kind)
        self.assertEqual("mira", request.subject_id)
        self.assertEqual(1024, request.width)
        self.assertEqual(1024, request.height)
        self.assertEqual(frozenset({"text_to_image"}), request.required_capabilities)
        self.assertEqual("Mira identity reference, neutral pose, plain background.", request.prompt)

    def test_valid_raster_submission_uses_canonical_handoff_acceptance(self) -> None:
        snapshot = self.import_prepared()
        request = self.gateway.prepare_generation(snapshot.project_id, snapshot.revision)[0]
        raster = self.staged_raster(request)
        accepted = self.gateway.submit_raster(
            snapshot.project_id,
            snapshot.revision,
            request.job_id,
            raster,
            "image/png",
            CAPABILITIES_USED,
        )
        self.assertEqual(snapshot.revision + 1, accepted.revision)
        retained = accepted.root / "references/attempts/mira/initial-001.png"
        canonical = accepted.root / "references/characters/mira.png"
        self.assertEqual(raster.read_bytes(), retained.read_bytes())
        self.assertEqual(retained.read_bytes(), canonical.read_bytes())
        inspection = comic_sol.inspect_handoff(accepted.root)
        self.assertEqual("completed", inspection["jobs"][0]["status"])
        receipt_path = next((accepted.root / "generation/receipts").glob("*.json"))
        self.assertEqual(
            CAPABILITIES_USED,
            json.loads(receipt_path.read_text(encoding="utf-8"))["capabilities_used"],
        )

    def test_panel_submission_retains_and_promotes_the_accepted_raster(self) -> None:
        snapshot = self.import_prepared()
        reference = self.gateway.prepare_generation(snapshot.project_id, snapshot.revision)[0]
        accepted_reference = self.gateway.submit_raster(
            snapshot.project_id,
            snapshot.revision,
            reference.job_id,
            self.staged_raster(reference),
            "image/png",
            CAPABILITIES_USED,
        )
        panel_requests = self.gateway.prepare_generation(
            snapshot.project_id, accepted_reference.revision
        )
        panel = next(request for request in panel_requests if request.subject_kind == "panel")
        raster = self.staged_raster(panel, "teal")
        accepted_panel = self.gateway.submit_raster(
            snapshot.project_id,
            panel.project_revision,
            panel.job_id,
            raster,
            "image/png",
            PANEL_CAPABILITIES_USED,
        )
        retained = accepted_panel.root / f"panels/attempts/{panel.subject_id}/initial-001.png"
        canonical = accepted_panel.root / f"panels/raw/{panel.subject_id}.png"
        self.assertEqual(raster.read_bytes(), retained.read_bytes())
        self.assertEqual(retained.read_bytes(), canonical.read_bytes())
        self.assertEqual(panel.project_revision + 1, accepted_panel.revision)
        receipts = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (accepted_panel.root / "generation/receipts").glob("*.json")
        ]
        panel_receipt = next(item for item in receipts if item["job_id"] == panel.job_id)
        self.assertEqual(PANEL_CAPABILITIES_USED, panel_receipt["capabilities_used"])

    def test_restart_recovers_revision_after_an_interrupted_engine_commit(self) -> None:
        snapshot = self.import_prepared()
        request = self.gateway.prepare_generation(snapshot.project_id, snapshot.revision)[0]
        with mock.patch.object(
            self.gateway,
            "_ensure_revision",
            side_effect=RuntimeError("simulated process termination"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated process termination"):
                self.gateway.submit_raster(
                    snapshot.project_id,
                    snapshot.revision,
                    request.job_id,
                    self.staged_raster(request),
                    "image/png",
                    CAPABILITIES_USED,
                )

        reopened = EngineGateway(self.database, self.data_root)
        recovered = reopened.snapshot(snapshot.project_id)
        self.assertEqual(snapshot.revision + 1, recovered.revision)
        with self.assertRaises(StaleProjectRevisionError):
            reopened.snapshot(snapshot.project_id, snapshot.revision)

    def test_panel_promotion_failure_advances_once_and_retry_completes_acceptance(self) -> None:
        snapshot = self.import_prepared()
        reference = self.gateway.prepare_generation(snapshot.project_id, snapshot.revision)[0]
        accepted_reference = self.gateway.submit_raster(
            snapshot.project_id,
            snapshot.revision,
            reference.job_id,
            self.staged_raster(reference),
            "image/png",
            CAPABILITIES_USED,
        )
        panel = next(
            request
            for request in self.gateway.prepare_generation(
                snapshot.project_id, accepted_reference.revision
            )
            if request.subject_kind == "panel"
        )
        raster = self.staged_raster(panel, "purple")
        with mock.patch(
            "comic_sol_web.engine_gateway.comic_sol.promote_attempt",
            side_effect=RuntimeError("injected promotion failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected promotion failure"):
                self.gateway.submit_raster(
                    snapshot.project_id,
                    panel.project_revision,
                    panel.job_id,
                    raster,
                    "image/png",
                    CAPABILITIES_USED,
                )
        after_failure = self.gateway.snapshot(snapshot.project_id)
        self.assertEqual(panel.project_revision + 1, after_failure.revision)
        retained = after_failure.root / f"panels/attempts/{panel.subject_id}/initial-001.png"
        canonical = after_failure.root / f"panels/raw/{panel.subject_id}.png"
        self.assertTrue(retained.is_file())
        self.assertFalse(canonical.exists())

        recovered = self.gateway.submit_raster(
            snapshot.project_id,
            after_failure.revision,
            panel.job_id,
            raster,
            "image/png",
            CAPABILITIES_USED,
        )
        self.assertEqual(after_failure.revision, recovered.revision)
        self.assertEqual(retained.read_bytes(), canonical.read_bytes())

    def test_engine_commit_advances_revision_when_initial_sqlite_record_fails(self) -> None:
        snapshot = self.import_prepared()
        request = self.gateway.prepare_generation(snapshot.project_id, snapshot.revision)[0]
        raster = self.staged_raster(request)
        original = self.gateway._set_revision
        attempts = 0

        def fail_once(connection, project_id, revision):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise sqlite3.OperationalError("injected revision failure")
            return original(connection, project_id, revision)

        with mock.patch.object(self.gateway, "_set_revision", side_effect=fail_once):
            with self.assertRaisesRegex(sqlite3.OperationalError, "injected revision failure"):
                self.gateway.submit_raster(
                    snapshot.project_id,
                    snapshot.revision,
                    request.job_id,
                    raster,
                    "image/png",
                    CAPABILITIES_USED,
                )
        self.assertEqual(2, attempts)
        reopened = self.gateway.snapshot(snapshot.project_id)
        self.assertEqual(snapshot.revision + 1, reopened.revision)
        self.assertEqual(
            raster.read_bytes(),
            (reopened.root / "references/characters/mira.png").read_bytes(),
        )

    def test_failed_raster_replacement_preserves_last_accepted_bytes_exactly(self) -> None:
        snapshot = self.import_prepared()
        request = self.gateway.prepare_generation(snapshot.project_id, snapshot.revision)[0]
        first = self.staged_raster(request, "navy")
        accepted = self.gateway.submit_raster(
            snapshot.project_id,
            snapshot.revision,
            request.job_id,
            first,
            "image/png",
            CAPABILITIES_USED,
        )
        retained = accepted.root / "references/attempts/mira/initial-001.png"
        canonical = accepted.root / "references/characters/mira.png"
        accepted_bytes = retained.read_bytes()
        replacement = self.staged_raster(request, "maroon")
        with self.assertRaises(HandoffResultError):
            self.gateway.submit_raster(
                accepted.project_id,
                accepted.revision,
                request.job_id,
                replacement,
                "image/png",
                CAPABILITIES_USED,
            )
        self.assertEqual(accepted_bytes, retained.read_bytes())
        self.assertEqual(accepted_bytes, canonical.read_bytes())
        self.assertEqual(accepted.revision, self.gateway.snapshot(accepted.project_id).revision)
        with self.assertRaises(GatewayInputError):
            self.gateway.submit_raster(
                accepted.project_id,
                accepted.revision,
                request.job_id,
                replacement,
                "image/jpeg",
                CAPABILITIES_USED,
            )
        self.assertEqual(accepted_bytes, retained.read_bytes())

    def test_wrong_job_and_uncontained_staging_path_fail_closed(self) -> None:
        snapshot = self.import_prepared()
        request = self.gateway.prepare_generation(snapshot.project_id, snapshot.revision)[0]
        raster = self.staged_raster(request)
        before = tree_snapshot(snapshot.root)
        with self.assertRaises(HandoffResultError):
            self.gateway.submit_raster(
                snapshot.project_id,
                snapshot.revision,
                "0" * 64,
                raster,
                "image/png",
                CAPABILITIES_USED,
            )
        outside = Path(self.temporary_directory.name) / "outside.png"
        Image.new("RGB", (1024, 1024), "black").save(outside)
        with self.assertRaises(GatewayInputError):
            self.gateway.submit_raster(
                snapshot.project_id,
                snapshot.revision,
                request.job_id,
                outside,
                "image/png",
                CAPABILITIES_USED,
            )
        self.assertEqual(before, tree_snapshot(snapshot.root))

    def test_owner_and_revision_guard_every_raster_qa_and_export_operation(self) -> None:
        snapshot = self.import_prepared()
        request = self.gateway.prepare_generation(snapshot.project_id, snapshot.revision)[0]
        raster = self.staged_raster(request)
        before = tree_snapshot(snapshot.root)
        wrong_owner_calls = (
            lambda: self.service.submit_raster(
                self.bob,
                snapshot.project_id,
                snapshot.revision,
                request.job_id,
                raster,
                "image/png",
                CAPABILITIES_USED,
            ),
            lambda: self.service.run_qa(self.bob, snapshot.project_id, snapshot.revision),
            lambda: self.service.export(
                self.bob, snapshot.project_id, snapshot.revision, ("archive",)
            ),
        )
        for operation in wrong_owner_calls:
            with self.subTest(guard="owner", operation=operation):
                with self.assertRaises(ProjectUnavailableError):
                    operation()
        stale_calls = (
            lambda: self.gateway.submit_raster(
                snapshot.project_id,
                0,
                request.job_id,
                raster,
                "image/png",
                CAPABILITIES_USED,
            ),
            lambda: self.gateway.run_qa(snapshot.project_id, 0),
            lambda: self.gateway.export(snapshot.project_id, 0, ("archive",)),
        )
        for operation in stale_calls:
            with self.subTest(guard="revision", operation=operation):
                with self.assertRaises(StaleProjectRevisionError):
                    operation()
        self.assertEqual(before, tree_snapshot(snapshot.root))

    def test_run_qa_returns_structured_findings_without_fabricating_records(self) -> None:
        snapshot = self.create()
        before = tree_snapshot(snapshot.root)
        checked = self.gateway.run_qa(snapshot.project_id, snapshot.revision)
        qa = cast(Mapping[str, object], checked.summary["qa"])
        issues = cast(tuple[Mapping[str, str], ...], qa["issues"])
        self.assertFalse(qa["valid"])
        self.assertIn(
            "plan/story-plan.json",
            {issue["path"] for issue in issues},
        )
        self.assertEqual(snapshot.revision, checked.revision)
        self.assertEqual(before, tree_snapshot(snapshot.root))

    def test_portable_export_reopens_as_the_same_canonical_project(self) -> None:
        snapshot = self.import_prepared()
        outputs = self.gateway.export(snapshot.project_id, snapshot.revision, ("archive",))
        self.assertEqual({"archive"}, set(outputs))
        self.assertTrue(outputs["archive"].is_file())

        other_root = Path(self.temporary_directory.name) / "second-web-data"
        other_database = Database(other_root / "application.sqlite3")
        apply_migrations(other_database, PROJECT_MIGRATIONS)
        other = EngineGateway(other_database, other_root)
        reopened = other.import_project("other-owner", outputs["archive"])
        self.assertEqual(snapshot.status, reopened.status)
        self.assertEqual(
            snapshot.summary["engine_project_id"], reopened.summary["engine_project_id"]
        )
        self.assertEqual(tree_snapshot(snapshot.root), tree_snapshot(reopened.root))

    def test_repeated_archive_exports_publish_unique_immutable_outputs(self) -> None:
        snapshot = self.import_prepared()
        first = self.gateway.export(snapshot.project_id, snapshot.revision, ("archive",))
        first_bytes = first["archive"].read_bytes()
        second = self.gateway.export(snapshot.project_id, snapshot.revision, ("archive",))
        self.assertNotEqual(first["archive"], second["archive"])
        self.assertEqual(first_bytes, first["archive"].read_bytes())
        self.assertEqual(first_bytes, second["archive"].read_bytes())

    def test_containment_malformed_archives_and_unsupported_formats_fail_closed(self) -> None:
        with self.assertRaises(ProjectUnavailableError):
            self.gateway.snapshot("../outside")
        malicious_id = "A" * 32
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO web_projects "
                "(project_id, owner_id, storage_name, revision, engine_state) "
                "VALUES (?, ?, ?, ?, ?)",
                (malicious_id, self.alice.user_id, "../outside", 1, "0" * 64),
            )
        with self.assertRaises(ProjectUnavailableError):
            self.gateway.snapshot(malicious_id)

        malformed = Path(self.temporary_directory.name) / "malformed.comic-sol-handoff"
        malformed.write_bytes(b"not a zip archive")
        with self.assertRaises(HandoffArchiveError):
            self.gateway.import_project(self.alice.user_id, malformed)
        self.assertEqual([], list(self.gateway.projects_root.iterdir()))

        valid = portable_archive(self)
        unsupported_format = valid.with_suffix(".zip")
        shutil.copyfile(valid, unsupported_format)
        with self.assertRaises(GatewayInputError):
            self.gateway.import_project(self.alice.user_id, unsupported_format)

        unsupported_schema = archive_with_schema(valid, "9.0")
        with self.assertRaises(HandoffArchiveError):
            self.gateway.import_project(self.alice.user_id, unsupported_schema)
        self.assertEqual([], list(self.gateway.projects_root.iterdir()))


class FakeAuth:
    def __init__(self, principal: SessionPrincipal) -> None:
        self.principal = principal
        self.csrf_checks = 0

    def require_csrf(self, _request) -> SessionPrincipal:
        self.csrf_checks += 1
        return self.principal


class ProjectApiTests(GatewayFixture):
    def app_for(self, principal: SessionPrincipal) -> tuple[FastAPI, FakeAuth]:
        app = FastAPI()
        auth = FakeAuth(principal)
        app.state.auth = auth
        app.include_router(create_projects_router(self.service))
        app.dependency_overrides[require_principal] = lambda: principal
        return app, auth

    def test_create_import_and_get_return_exact_revision_envelopes(self) -> None:
        archive = portable_archive(self)
        app, auth = self.app_for(self.alice)
        with TestClient(app) as client:
            created = client.post(
                "/api/projects",
                json={"title": "API Project", "prompt": "A precise API project.", "page_count": 1},
            )
            self.assertEqual(201, created.status_code)
            self.assertEqual("private, no-store", created.headers.get("cache-control"))
            created_body = created.json()
            self.assertEqual({"project_id", "revision", "status", "summary"}, set(created_body))
            self.assertEqual(1, created_body["revision"])
            fetched = client.get(f"/api/projects/{created_body['project_id']}")
            self.assertEqual(200, fetched.status_code)
            self.assertEqual("private, no-store", fetched.headers.get("cache-control"))
            self.assertEqual(created_body, fetched.json())

            imported = client.post(
                "/api/projects/import",
                files={
                    "archive": (
                        archive.name,
                        io.BytesIO(archive.read_bytes()),
                        "application/zip",
                    )
                },
            )
            self.assertEqual(201, imported.status_code)
            self.assertEqual("private, no-store", imported.headers.get("cache-control"))
            imported_body = imported.json()
            self.assertEqual({"project_id", "revision", "status", "summary"}, set(imported_body))
            self.assertEqual(1, imported_body["revision"])
            self.assertEqual("STORYBOARDED", imported_body["status"])
            self.assertEqual(
                plan_payload(self.gateway.snapshot(imported_body["project_id"]).root),
                imported_body["summary"]["plan"],
            )
        self.assertEqual(2, auth.csrf_checks)

    def test_plan_api_requires_write_headers_and_returns_committed_canonical_plan(self) -> None:
        snapshot = self.import_prepared()
        app, auth = self.app_for(self.alice)
        plan = plan_payload(snapshot.root)
        story = json.loads(plan["storyPlan"])
        story["theme"] = "The API commits a reviewed canonical Plan."
        plan["storyPlan"] = json.dumps(story)
        request = {"project_id": snapshot.project_id, "plan": plan}
        with TestClient(app) as client:
            missing_headers = client.post("/api/projects", json=request)
            self.assertEqual(400, missing_headers.status_code)

            updated = client.post(
                "/api/projects",
                json=request,
                headers={
                    "Idempotency-Key": "b3a11f8d-8f73-48fb-9d22-09e80c9d90be",
                    "X-Expected-Revision": str(snapshot.revision),
                },
            )
        self.assertEqual(200, updated.status_code)
        self.assertEqual("private, no-store", updated.headers.get("cache-control"))
        body = updated.json()
        self.assertEqual(snapshot.revision + 1, body["revision"])
        self.assertEqual(plan_payload(snapshot.root), body["summary"]["plan"])
        self.assertEqual(2, auth.csrf_checks)

    def test_api_anonymous_or_wrong_owner_access_is_unavailable(self) -> None:
        snapshot = self.create()
        app, _auth = self.app_for(self.bob)
        with TestClient(app) as client:
            wrong_owner = client.get(f"/api/projects/{snapshot.project_id}")
        self.assertEqual(404, wrong_owner.status_code)
        self.assertEqual({"detail": "project unavailable"}, wrong_owner.json())

        anonymous = FastAPI()
        anonymous.include_router(create_projects_router(self.service))
        with TestClient(anonymous) as client:
            response = client.get(f"/api/projects/{snapshot.project_id}")
        self.assertEqual(401, response.status_code)
        self.assertEqual({"detail": "authentication required"}, response.json())

    def test_server_filesystem_failures_are_not_reported_as_client_errors(self) -> None:
        class FailingService:
            def snapshot(self, *_args, **_kwargs):
                raise OSError("injected server filesystem failure")

        app = FastAPI()
        app.include_router(create_projects_router(FailingService()))
        app.dependency_overrides[require_principal] = lambda: self.alice
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(f"/api/projects/{'A' * 32}")
        self.assertEqual(500, response.status_code)


if __name__ == "__main__":
    unittest.main()
