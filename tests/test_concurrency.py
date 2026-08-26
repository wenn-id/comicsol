import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from scripts import comic_sol, project_io
from scripts.comic_sol import (
    atomic_write_json,
    canonical_artifact_bytes,
    canonical_event_record,
    finalize_project,
    read_json,
    read_project_status,
    record_generation_attempt,
    resume_project,
)
from scripts.export_pdf import guarded_export

ROOT = Path(__file__).resolve().parents[1]


CHILD_LOCK_SCRIPT = r"""
import sys
from pathlib import Path
from scripts.project_io import ProjectLock

try:
    with ProjectLock(Path(sys.argv[1]), timeout=0.2):
        pass
except TimeoutError as error:
    print(error, file=sys.stderr)
    raise SystemExit(2)
"""

CONTENDER_AT_EMPTY_FILE_SCRIPT = r"""
import sys
from pathlib import Path
from scripts.project_io import ProjectLock

original_open = Path.open
original_lock = ProjectLock._lock
paused = False

def pause():
    global paused
    if not paused:
        paused = True
        print("READY", flush=True)
        if sys.stdin.readline().strip() != "GO":
            raise RuntimeError("missing synchronization signal")

class PausedHandle:
    def __init__(self, handle):
        self.handle = handle

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def tell(self):
        position = self.handle.tell()
        pause()
        return position

Path.open = lambda path, *args, **kwargs: PausedHandle(
    original_open(path, *args, **kwargs)
)
ProjectLock._lock = staticmethod(lambda handle: (pause(), original_lock(handle))[1])
try:
    with ProjectLock(Path(sys.argv[1]), timeout=0):
        pass
except TimeoutError:
    raise SystemExit(2)
"""


class ProjectLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary_directory.name) / "project"
        self.project.mkdir()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def run_child(self):
        return subprocess.run(
            [sys.executable, "-c", CHILD_LOCK_SCRIPT, os.fspath(self.project)],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_child_times_out_while_parent_holds_lock(self):
        with project_io.ProjectLock(self.project, timeout=1.0):
            result = self.run_child()
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertIn("project is locked", result.stderr)

    def test_child_succeeds_after_parent_releases_lock(self):
        with project_io.ProjectLock(self.project, timeout=1.0):
            pass
        result = self.run_child()
        self.assertEqual(0, result.returncode, result.stderr)

    def test_nested_lock_keeps_outer_lock_held_until_outer_exit(self):
        with project_io.ProjectLock(self.project, timeout=1.0):
            with project_io.ProjectLock(self.project, timeout=0):
                result = self.run_child()
                self.assertEqual(2, result.returncode, result.stderr)
            result = self.run_child()
            self.assertEqual(2, result.returncode, result.stderr)
        result = self.run_child()
        self.assertEqual(0, result.returncode, result.stderr)

    def test_same_lock_instance_releases_after_outer_exit(self):
        lock = project_io.ProjectLock(self.project, timeout=1.0)
        with lock:
            with lock:
                result = self.run_child()
                self.assertEqual(2, result.returncode, result.stderr)
            result = self.run_child()
            self.assertEqual(2, result.returncode, result.stderr)
        result = self.run_child()
        self.assertEqual(0, result.returncode, result.stderr)

    def test_contender_never_mutates_owner_metadata_before_acquiring(self):
        contender = subprocess.Popen(
            [
                sys.executable,
                "-c",
                CONTENDER_AT_EMPTY_FILE_SCRIPT,
                os.fspath(self.project),
            ],
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual("READY\n", contender.stdout.readline())
        expected = f"{os.getpid()}\n".encode("ascii")
        try:
            with project_io.ProjectLock(self.project, timeout=1.0):
                contender.stdin.write("GO\n")
                contender.stdin.flush()
                self.assertEqual(2, contender.wait(timeout=5), contender.stderr.read())
                self.assertEqual(
                    expected,
                    (self.project / ".comic-sol.lock").read_bytes(),
                )
        finally:
            if contender.poll() is None:
                contender.kill()
                contender.wait()
            contender.stdin.close()
            contender.stdout.close()
            contender.stderr.close()
        self.assertEqual(expected, (self.project / ".comic-sol.lock").read_bytes())

    def test_failure_after_acquisition_unlocks_before_close(self):
        for failed_operation in ("truncate", "write", "flush"):
            with self.subTest(operation=failed_operation):
                events = []
                original = OSError(f"{failed_operation} failed")

                class Handle:
                    def seek(self, *args):
                        pass

                    def tell(self):
                        return 1

                    def truncate(self):
                        events.append("truncate")
                        if failed_operation == "truncate":
                            raise original

                    def write(self, payload):
                        events.append("write")
                        if failed_operation == "write":
                            raise original

                    def flush(self):
                        events.append("flush")
                        if failed_operation == "flush":
                            raise original

                    def close(self):
                        events.append("close")

                lock = project_io.ProjectLock(self.project, timeout=0)
                handle = Handle()
                (self.project / ".comic-sol.lock").write_bytes(b"\0")

                def fail_unlock(unused):
                    events.append("unlock")
                    raise OSError("unlock failed")

                with (
                    mock.patch.object(
                        project_io.ProjectLock, "_open_retained", return_value=handle
                    ),
                    mock.patch.object(
                        project_io.ProjectLock,
                        "_lock",
                        side_effect=lambda unused: events.append("lock"),
                    ),
                    mock.patch.object(
                        project_io.ProjectLock,
                        "_unlock",
                        create=True,
                        side_effect=fail_unlock,
                    ),
                ):
                    with self.assertRaises(OSError) as raised:
                        lock.__enter__()
                self.assertIs(original, raised.exception)
                self.assertEqual(["unlock", "close"], events[-2:])
                self.assertIsNone(lock._handle)

    def test_stale_empty_lock_file_is_recovered(self):
        """Crash between truncate and PID write must not permanently block project."""
        lock_path = self.project / ".comic-sol.lock"
        lock_path.write_bytes(b"")
        with project_io.ProjectLock(self.project, timeout=0.1):
            self.assertEqual(f"{os.getpid()}\n", lock_path.read_text("ascii"))

    def test_lock_file_is_retained_with_sanitized_pid_metadata(self):
        with project_io.ProjectLock(self.project, timeout=1.0):
            metadata = (self.project / ".comic-sol.lock").read_text(encoding="ascii")
        self.assertEqual(f"{os.getpid()}\n", metadata)
        self.assertTrue((self.project / ".comic-sol.lock").is_file())


_BUDGET_RACE_CHILD = r"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, os.fspath(Path(sys.argv[2])))
from scripts.comic_sol import record_generation_attempt
project_dir = Path(sys.argv[1])
record_generation_attempt(project_dir, sys.argv[3], sys.argv[4], project_dir / sys.argv[5])
""".strip()


class RetryCounterProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.project_dir = self.root / "project"
        self.project_dir.mkdir()
        for path in ("panels/raw", "logs"):
            (self.project_dir / path).mkdir(parents=True, exist_ok=True)
        self._seed_project()
        self._barrier = self.project_dir / "budget-barrier"

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _seed_project(self):
        from PIL import Image as PILImage

        for panel_id, color in [
            ("p01-01", "navy"),
            ("p01-02", "blue"),
            ("p01-03", "green"),
            ("p01-04", "red"),
            ("p01-05", "yellow"),
            ("p01-06", "magenta"),
            ("p01-07", "cyan"),
            ("p01-08", "white"),
        ]:
            attempt = self.project_dir / f"panels/raw/{panel_id}.initial.png"
            PILImage.new("RGB", (512, 512), color).save(attempt)
            record_generation_attempt(self.project_dir, panel_id, "initial", attempt)

    def _launch_budget_children(self):
        scripts_root = os.fspath(Path(__file__).resolve().parents[1])
        children = []
        pairs = [
            # 8 distinct successes
            ("p01-01", "visual_retry", "p01-01.visual-1.png"),
            ("p01-02", "visual_retry", "p01-02.visual-1.png"),
            ("p01-03", "visual_retry", "p01-03.visual-1.png"),
            ("p01-04", "visual_retry", "p01-04.visual-1.png"),
            ("p01-05", "visual_retry", "p01-05.visual-1.png"),
            ("p01-06", "visual_retry", "p01-06.visual-1.png"),
            ("p01-07", "visual_retry", "p01-07.visual-1.png"),
            ("p01-08", "visual_retry", "p01-08.visual-1.png"),
            # 4 transient repeats — will conflict
            ("p01-01", "transient_repeat", "p01-01.transient-1.png"),
            ("p01-02", "transient_repeat", "p01-02.transient-1.png"),
            ("p01-03", "transient_repeat", "p01-03.transient-1.png"),
            ("p01-04", "transient_repeat", "p01-04.transient-1.png"),
            # 4 third visual retries — will fail
            ("p01-01", "visual_retry", "p01-01.visual-3.png"),
            ("p01-02", "visual_retry", "p01-02.visual-3.png"),
            ("p01-03", "visual_retry", "p01-03.visual-3.png"),
            ("p01-04", "visual_retry", "p01-04.visual-3.png"),
            # 4 ninth global calls — will fail
            ("p01-05", "visual_retry", "p01-05.visual-9.png"),
            ("p01-06", "visual_retry", "p01-06.visual-9.png"),
            ("p01-07", "visual_retry", "p01-07.visual-9.png"),
            ("p01-08", "visual_retry", "p01-08.visual-9.png"),
        ]
        for panel_id, kind, attempt_name in pairs:
            attempt_path = self.project_dir / f"panels/raw/{attempt_name}"
            from PIL import Image as _PILImage

            _PILImage.new("RGB", (512, 512), (0, 0, 0)).save(attempt_path)
            child_script = (
                f"import json, os, sys, time\n"
                f"from pathlib import Path\n"
                f"_sr={scripts_root!r}\n"
                f"sys.path.insert(0, _sr)\n"
                f"from scripts.comic_sol import record_generation_attempt\n"
                f"project = Path(sys.argv[1])\n"
                f"barrier = project / 'budget-barrier'\n"
                f"for _ in range(500):\n"
                f"    if barrier.exists():\n"
                f"        break\n"
                f"    time.sleep(0.01)\n"
                f"else:\n"
                f"    raise SystemExit('barrier timeout')\n"
                f"try:\n"
                f"    record_generation_attempt(project, {panel_id!r}, {kind!r}, project / 'panels/raw/{attempt_name}')\n"
                f"    print('SUCCESS:' + {panel_id!r}, flush=True)\n"
                f"except ValueError as e:\n"
                f"    print('FAILURE:' + str(e), flush=True)\n"
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", child_script, os.fspath(self.project_dir), scripts_root],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            children.append(proc)
        self._barrier.write_text("go", encoding="ascii")
        return children

    def test_20_process_budget_race(self):
        children = self._launch_budget_children()
        successes = 0
        failures = 0
        for proc in children:
            out, err = proc.communicate(timeout=60)
            if "SUCCESS:" in out:
                successes += 1
            else:
                self.assertIn("FAILURE:", out or err)
                failures += 1
        self.assertEqual(8, successes)
        self.assertEqual(12, failures)
        counters_path = self.project_dir / "logs/generation-counters.json"
        counters = json.loads(counters_path.read_text("utf-8"))
        self.assertEqual(8, counters["global_extra_calls"])


_PROMOTION_RACE_CHILD = r"""
import os, sys, time
from pathlib import Path
sys.path.insert(0, os.fspath(Path(sys.argv[2])))
from scripts.comic_sol import promote_attempt
project = Path(sys.argv[1])
barrier = project / "promotion-barrier"
for _ in range(500):
    if barrier.exists():
        break
    time.sleep(0.01)
else:
    raise SystemExit("barrier timeout")
try:
    promote_attempt(project, "p01-01", project / "panels/raw/p01-01.new.png")
    print("SUCCESS", flush=True)
except Exception as e:
    print("CONFLICT:" + str(e), flush=True)
""".strip()


class PromotionArchiveRaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.project_dir = self.root / "project"
        self.project_dir.mkdir()
        (self.project_dir / "panels/raw").mkdir(parents=True)
        (self.project_dir / "logs").mkdir(parents=True)
        from PIL import Image as PILImage

        PILImage.new("RGB", (512, 512), "navy").save(self.project_dir / "panels/raw/p01-01.png")
        PILImage.new("RGB", (512, 512), "blue").save(self.project_dir / "panels/raw/p01-01.new.png")
        (self.project_dir / "project.json").write_text(
            '{"schema_version":"1.0","status":"PANELS_READY"}', "utf-8"
        )
        self._barrier = self.project_dir / "promotion-barrier"
        self.old_accepted_bytes = (self.project_dir / "panels/raw/p01-01.png").read_bytes()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_promotion_archive_race(self):
        scripts_root = os.fspath(Path(__file__).resolve().parents[1])
        children = []
        for _ in range(2):
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _PROMOTION_RACE_CHILD,
                    os.fspath(self.project_dir),
                    scripts_root,
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            children.append(proc)
        self._barrier.write_text("go", encoding="ascii")
        successes = 0
        conflicts = 0
        for proc in children:
            out, err = proc.communicate(timeout=60)
            self.assertIn("SUCCESS" if "SUCCESS" in out else "CONFLICT:", out or err)
            if "SUCCESS" in out:
                successes += 1
            else:
                conflicts += 1
        self.assertEqual(2, successes)
        self.assertEqual(0, conflicts)

        raw_p01 = self.project_dir / "panels/raw/p01-01.png"
        self.assertTrue(raw_p01.is_file())
        archives = list((self.project_dir / "panels/raw").glob("p01-01.attempt-*.png"))
        self.assertEqual(1, len(archives))
        self.assertEqual(self.old_accepted_bytes, archives[0].read_bytes())
        self.assertNotEqual(raw_p01.read_bytes(), archives[0].read_bytes())
        events = [
            json.loads(line)
            for line in (self.project_dir / "logs/events.jsonl").read_text("utf-8").splitlines()
        ]
        promoted = [event for event in events if event["event"] == "generation.attempt-promoted"]
        self.assertEqual(1, len(promoted))
        self.assertEqual("p01-01", promoted[0]["details"]["panel_id"])
        self.assertEqual("panels/raw/p01-01.new.png", promoted[0]["details"]["attempt_path"])


class HandoffConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.project = self._planner_project()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _planner_project(self):
        from scripts.character_identity import IDENTITY_PACK_PATH, derive_identity_pack
        from tests.test_validation import (
            valid_characters,
            valid_manifest,
            valid_story,
            valid_storyboard,
        )

        project = comic_sol.init_project(
            self.root,
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
        return project

    @staticmethod
    def _manifest_jobs(project):
        manifest = comic_sol.read_json(project / "handoff/manifest.json")
        jobs = {}
        for descriptor in manifest["jobs"]:
            job = comic_sol.read_json(project / descriptor["path"])
            jobs[job["subject_kind"], job["subject_id"]] = job
        return manifest, jobs

    @staticmethod
    def _success_arguments(job, raster_path, *, approve_reference=False):
        return {
            "job_id": job["job_id"],
            "attempt": 1,
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
    def _events(project):
        return [
            json.loads(line)
            for line in (project / "logs/events.jsonl").read_text("utf-8").splitlines()
        ]

    @staticmethod
    def _transaction_entries(project):
        transactions = project / "logs/transactions"
        return list(transactions.iterdir()) if transactions.is_dir() else []

    def _prepared_reference(self):
        comic_sol.prepare_handoff(self.project)
        _manifest, jobs = self._manifest_jobs(self.project)
        return jobs["reference", "mira"]

    def _prepared_panel(self):
        from PIL import Image

        reference_job = self._prepared_reference()
        reference_raster = self.root / "prepared-reference.png"
        Image.new("RGB", (512, 512), (220, 180, 80)).save(reference_raster, format="PNG")
        comic_sol.accept_handoff_result(
            self.project,
            **self._success_arguments(
                reference_job,
                reference_raster,
                approve_reference=True,
            ),
        )
        comic_sol.prepare_handoff(self.project)
        _manifest, jobs = self._manifest_jobs(self.project)
        return jobs["panel", "p01-01"]

    def test_identical_concurrent_accept_results_publish_one_attempt(self):
        from PIL import Image

        panel_job = self._prepared_panel()
        dimensions = panel_job["requested_dimensions"]
        raster = self.root / "concurrent-panel.png"
        Image.new(
            "RGB",
            (dimensions["width"], dimensions["height"]),
            (20, 30, 40),
        ).save(raster, format="PNG")
        raster_bytes = raster.read_bytes()
        arguments = self._success_arguments(panel_job, raster)
        receipts_before = set((self.project / "generation/receipts").glob("*.json"))
        events_before = self._events(self.project)

        owner_validated = threading.Event()
        release_owner = threading.Event()
        contender_attempted = threading.Event()
        contender_done = threading.Event()
        results = {}
        errors = []
        validate_raster = comic_sol._validate_handoff_raster
        lock_primitive = project_io.ProjectLock._lock
        owner_thread = None
        contender_thread = None

        def pause_owner_after_validation(payload, job):
            validated = validate_raster(payload, job)
            if threading.current_thread() is owner_thread:
                owner_validated.set()
                if not release_owner.wait(timeout=5):
                    raise AssertionError("accept-result synchronization timed out")
            return validated

        def observe_contender_lock(handle):
            if threading.current_thread() is contender_thread:
                contender_attempted.set()
            return lock_primitive(handle)

        def accept(name, done=None):
            try:
                results[name] = comic_sol.accept_handoff_result(self.project, **arguments)
            except BaseException as error:
                errors.append((name, error))
            finally:
                if done is not None:
                    done.set()

        with (
            mock.patch(
                "scripts.comic_sol._validate_handoff_raster",
                side_effect=pause_owner_after_validation,
            ),
            mock.patch.object(
                project_io.ProjectLock,
                "_lock",
                new=staticmethod(observe_contender_lock),
            ),
        ):
            owner_thread = threading.Thread(target=accept, args=("owner",))
            owner_thread.start()
            self.assertTrue(
                owner_validated.wait(timeout=5),
                "owner did not validate while holding the project lock",
            )
            contender_thread = threading.Thread(
                target=accept,
                args=("contender", contender_done),
            )
            contender_thread.start()
            try:
                self.assertTrue(
                    contender_attempted.wait(timeout=5),
                    "contender did not attempt the project lock",
                )
                self.assertFalse(contender_done.is_set())
            finally:
                release_owner.set()
                owner_thread.join(timeout=5)
                contender_thread.join(timeout=5)

        self.assertFalse(owner_thread.is_alive())
        self.assertFalse(contender_thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual({"owner", "contender"}, set(results))
        self.assertEqual([False, True], sorted(result["duplicate"] for result in results.values()))
        self.assertEqual(
            1,
            len({result["attempt_id"] for result in results.values()}),
        )
        self.assertEqual(
            1,
            len({result["raster_sha256"] for result in results.values()}),
        )

        receipts_after = set((self.project / "generation/receipts").glob("*.json"))
        new_receipts = receipts_after - receipts_before
        self.assertEqual(1, len(new_receipts))
        receipt = comic_sol.read_json(new_receipts.pop())
        self.assertEqual(panel_job["job_id"], receipt["job_id"])
        self.assertEqual(panel_job["target_path"], receipt["raster_path"])

        retained = self.project / panel_job["target_path"]
        self.assertEqual(raster_bytes, retained.read_bytes())
        self.assertEqual(
            [retained],
            list(retained.parent.glob("*.png")),
        )
        counters = comic_sol.read_json(self.project / "logs/generation-counters.json")
        self.assertEqual(0, counters["global_extra_calls"])
        self.assertEqual(
            {"initial": 1, "transient_repeats": 0, "visual_retries": 0},
            counters["panels"]["p01-01"],
        )

        new_events = self._events(self.project)[len(events_before) :]
        self.assertEqual(
            ["handoff.result-accepted"],
            [event["event"] for event in new_events],
        )
        accepted_events = [
            event
            for event in new_events
            if event["event"] == "handoff.result-accepted"
            and event["details"]["job_id"] == panel_job["job_id"]
        ]
        self.assertEqual(1, len(accepted_events))
        self.assertEqual(panel_job["target_path"], accepted_events[0]["details"]["attempt_path"])
        self.assertEqual([], self._transaction_entries(self.project))

    def test_reference_intake_blocks_prepare_then_prepare_reads_activation(self):
        from PIL import Image

        reference_job = self._prepared_reference()
        raster = self.root / "concurrent-reference.png"
        Image.new("RGB", (512, 512), (220, 180, 80)).save(raster, format="PNG")
        raster_bytes = raster.read_bytes()
        arguments = self._success_arguments(
            reference_job,
            raster,
            approve_reference=True,
        )
        events_before = self._events(self.project)

        intake_validated = threading.Event()
        release_intake = threading.Event()
        prepare_attempted = threading.Event()
        prepare_done = threading.Event()
        intake_results = []
        prepare_results = []
        prepare_observations = []
        errors = []
        validate_raster = comic_sol._validate_handoff_raster
        validate_activation = comic_sol._validate_reference_activation
        lock_primitive = project_io.ProjectLock._lock
        intake_thread = None
        prepare_thread = None

        def pause_intake_after_validation(payload, job):
            validated = validate_raster(payload, job)
            if threading.current_thread() is intake_thread:
                intake_validated.set()
                if not release_intake.wait(timeout=5):
                    raise AssertionError("reference intake synchronization timed out")
            return validated

        def observe_prepare_lock(handle):
            if threading.current_thread() is prepare_thread:
                prepare_attempted.set()
            return lock_primitive(handle)

        def observe_committed_activation(project_dir, snapshot):
            if threading.current_thread() is prepare_thread:
                state = next(
                    item
                    for item in snapshot["effective_jobs"]
                    if item["job_id"] == reference_job["job_id"]
                )
                receipts = [
                    receipt
                    for receipt in snapshot["receipts"]
                    if receipt["job_id"] == reference_job["job_id"]
                    and receipt["outcome"] == "success"
                ]
                receipt = receipts[0]
                receipt_path = self.project / f"generation/receipts/{receipt['attempt_id']}.json"
                prepare_observations.append(
                    {
                        "canonical": (self.project / "references/characters/mira.png").read_bytes(),
                        "phase": snapshot["phase"],
                        "receipt_bytes": receipt_path.read_bytes(),
                        "receipt_count": len(receipts),
                        "retained": (self.project / receipt["raster_path"]).read_bytes(),
                        "scope_state": snapshot["scope_state"],
                        "status": state["status"],
                    }
                )
            return validate_activation(project_dir, snapshot)

        def run_intake():
            try:
                intake_results.append(comic_sol.accept_handoff_result(self.project, **arguments))
            except BaseException as error:
                errors.append(("intake", error))

        def run_prepare():
            try:
                prepare_results.append(comic_sol.prepare_handoff(self.project))
            except BaseException as error:
                errors.append(("prepare", error))
            finally:
                prepare_done.set()

        with (
            mock.patch(
                "scripts.comic_sol._validate_handoff_raster",
                side_effect=pause_intake_after_validation,
            ),
            mock.patch(
                "scripts.comic_sol._validate_reference_activation",
                side_effect=observe_committed_activation,
            ),
            mock.patch.object(
                project_io.ProjectLock,
                "_lock",
                new=staticmethod(observe_prepare_lock),
            ),
        ):
            intake_thread = threading.Thread(target=run_intake)
            intake_thread.start()
            self.assertTrue(
                intake_validated.wait(timeout=5),
                "reference intake did not validate while holding the project lock",
            )
            prepare_thread = threading.Thread(target=run_prepare)
            prepare_thread.start()
            try:
                self.assertTrue(
                    prepare_attempted.wait(timeout=5),
                    "prepare did not attempt the project lock",
                )
                self.assertFalse(prepare_done.is_set())
            finally:
                release_intake.set()
                intake_thread.join(timeout=5)
                prepare_thread.join(timeout=5)

        self.assertFalse(intake_thread.is_alive())
        self.assertFalse(prepare_thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(1, len(intake_results))
        self.assertFalse(intake_results[0]["duplicate"])
        self.assertEqual(
            "references/characters/mira.png", intake_results[0]["activated_reference_path"]
        )
        self.assertEqual(1, len(prepare_results))
        self.assertTrue(prepare_results[0]["changed"])
        self.assertEqual("panel", prepare_results[0]["phase"])
        self.assertEqual("render-panels", prepare_results[0]["next_action"])
        self.assertEqual(1, len(prepare_observations))
        observation = prepare_observations[0]
        self.assertEqual(raster_bytes, observation["canonical"])
        self.assertEqual("reference", observation["phase"])
        self.assertEqual(1, observation["receipt_count"])
        self.assertEqual(raster_bytes, observation["retained"])
        self.assertEqual("current", observation["scope_state"])
        self.assertEqual("completed", observation["status"])

        receipt_path = self.project / intake_results[0]["receipt_path"]
        self.assertEqual(
            observation["receipt_bytes"],
            receipt_path.read_bytes(),
        )
        handoff_manifest, jobs = self._manifest_jobs(self.project)
        self.assertEqual({("reference", "mira"), ("panel", "p01-01")}, set(jobs))
        panel_job = jobs["panel", "p01-01"]
        self.assertEqual(
            [
                {
                    "path": "references/characters/mira.png",
                    "sha256": intake_results[0]["raster_sha256"],
                }
            ],
            panel_job["references"],
        )
        inspection = comic_sol.inspect_handoff(self.project)
        effective = {job["job_id"]: job for job in inspection["jobs"]}
        self.assertEqual("panel", inspection["phase"])
        self.assertEqual("completed", effective[reference_job["job_id"]]["status"])
        self.assertEqual("ready", effective[panel_job["job_id"]]["status"])
        project_manifest = comic_sol.read_json(self.project / "project.json")
        self.assertEqual(
            handoff_manifest["locked_scope_sha256"],
            project_manifest["handoff"]["locked_scope_sha256"],
        )
        self.assertFalse((self.project / "logs/generation-counters.json").exists())
        self.assertEqual(
            ["handoff.result-accepted", "handoff.prepared"],
            [event["event"] for event in self._events(self.project)[len(events_before) :]],
        )
        self.assertEqual([], self._transaction_entries(self.project))


class FinalizeLockRaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary_directory.name) / "project"
        self.project.mkdir()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _leave_interrupted_manifest_publication(self):
        manifest = read_json(ROOT / "templates/manifest.json")
        manifest["project_id"] = "project"
        manifest["status"] = "BLOCKED"
        manifest["blocked_from"] = "INIT"
        manifest["blocked_reason"] = "image-capability-unavailable"
        atomic_write_json(self.project / "project.json", manifest)
        original = (self.project / "project.json").read_bytes()
        (self.project / "logs").mkdir()
        baseline_event = canonical_event_record(
            "artifact.reused",
            {"artifact_path": "plan/story-plan.json", "reused": True},
        )
        (self.project / "logs/events.jsonl").write_bytes(baseline_event)
        self.expected_generation = {
            "project.json": original,
            "logs/events.jsonl": baseline_event,
        }
        published = dict(manifest)
        published["status"] = "PLANNED"
        published["blocked_from"] = None
        published["blocked_reason"] = None

        transaction = project_io.ProjectTransaction(self.project, "interrupted-status")
        transaction.__enter__()
        transaction.append_bytes(
            "logs/events.jsonl",
            baseline_event,
            repair_torn_jsonl=True,
        )
        transaction.stage_bytes("project.json", canonical_artifact_bytes(published))
        transaction._phase = "publishing"
        transaction._write_journal()
        for entry in transaction._journal:
            if entry.get("operation") == "append":
                transaction._apply_append(entry)
            else:
                project_io.replace_contained(self.project, entry["staged"], entry["path"])
        lock = transaction._lock
        if lock is None:
            self.fail("interrupted transaction did not acquire its lock")
        lock.__exit__(None, None, None)
        transaction._lock = None
        self.assertEqual("PLANNED", read_json(self.project / "project.json")["status"])
        self.assertEqual(
            baseline_event + baseline_event,
            (self.project / "logs/events.jsonl").read_bytes(),
        )
        return original

    def _assert_expected_generation(self, project):
        for relative, payload in self.expected_generation.items():
            self.assertEqual(payload, (project / relative).read_bytes(), relative)
        manifest = read_json(project / "project.json")
        events = [
            json.loads(line)
            for line in (project / "logs/events.jsonl").read_text("utf-8").splitlines()
        ]
        self.assertEqual("BLOCKED", manifest["status"])
        self.assertEqual("INIT", manifest["blocked_from"])
        self.assertEqual("image-capability-unavailable", manifest["blocked_reason"])
        self.assertEqual(1, len(events))
        self.assertEqual("artifact.reused", events[0]["event"])

    def test_public_operations_wait_for_recovery_and_observe_one_generation(self):
        root = self.project.parent
        operations = ("read_project_status", "resume_project", "finalize_project")

        for operation in operations:
            with self.subTest(operation=operation):
                self.project = root / operation
                self.project.mkdir()
                self._leave_interrupted_manifest_publication()
                owner_entered = threading.Event()
                release_owner = threading.Event()
                contender_attempted = threading.Event()
                contender_done = threading.Event()
                owner_results = []
                contender_results = []
                observed_generations = []
                errors = []
                recover = project_io.ProjectTransaction.recover
                lock_primitive = project_io.ProjectLock._lock
                owner_thread = None
                contender_thread = None

                def pause_owner_recovery(project_dir):
                    if threading.current_thread() is owner_thread:
                        owner_entered.set()
                        if not release_owner.wait(timeout=5):
                            raise AssertionError("owner recovery synchronization timed out")
                    return recover(project_dir)

                def observe_contender_lock(handle):
                    if threading.current_thread() is contender_thread:
                        contender_attempted.set()
                    return lock_primitive(handle)

                def run_owner():
                    try:
                        owner_results.append(read_project_status(self.project))
                    except BaseException as error:
                        errors.append(("owner", error))

                def resumed_state(project_dir, manifest_path):
                    return {"status": read_json(manifest_path)["status"]}

                def finalized_state(project_dir, caller_project_dir, progress):
                    return {"status": read_json(project_dir / "project.json")["status"]}

                def run_contender():
                    try:
                        if operation == "read_project_status":
                            result = read_project_status(self.project)
                        elif operation == "resume_project":
                            result = resume_project(self.project)
                        else:
                            result = finalize_project(self.project)
                        contender_results.append(result)
                        observed_generations.append(
                            {
                                relative: (self.project / relative).read_bytes()
                                for relative in self.expected_generation
                            }
                        )
                    except BaseException as error:
                        errors.append(("contender", error))
                    finally:
                        contender_done.set()

                with (
                    mock.patch(
                        "scripts.comic_sol.ProjectTransaction.recover",
                        side_effect=pause_owner_recovery,
                    ),
                    mock.patch.object(
                        project_io.ProjectLock,
                        "_lock",
                        new=staticmethod(observe_contender_lock),
                    ),
                    mock.patch(
                        "scripts.comic_sol._resume_project_locked",
                        side_effect=resumed_state,
                    ),
                    mock.patch(
                        "scripts.comic_sol._finalize_project_locked",
                        side_effect=finalized_state,
                    ),
                ):
                    owner_thread = threading.Thread(target=run_owner)
                    owner_thread.start()
                    self.assertTrue(
                        owner_entered.wait(timeout=5),
                        "owner did not pause while holding the project lock",
                    )
                    contender_thread = threading.Thread(target=run_contender)
                    contender_thread.start()
                    try:
                        self.assertTrue(
                            contender_attempted.wait(timeout=5),
                            f"{operation} did not attempt the project lock",
                        )
                        self.assertFalse(contender_done.is_set())
                    finally:
                        release_owner.set()
                        owner_thread.join(timeout=5)
                        contender_thread.join(timeout=5)

                self.assertFalse(owner_thread.is_alive())
                self.assertFalse(contender_thread.is_alive())
                self.assertEqual([], errors)
                self.assertEqual("BLOCKED", owner_results[0]["status"])
                self.assertEqual("BLOCKED", contender_results[0]["status"])
                self.assertEqual([self.expected_generation], observed_generations)
                self._assert_expected_generation(self.project)
                self.assertEqual([], list((self.project / "logs/transactions").iterdir()))

    def test_status_recovers_before_read_while_holding_project_lock(self):
        original = self._leave_interrupted_manifest_publication()
        entered = threading.Event()
        release = threading.Event()
        status_result = []
        status_errors = []
        contender_errors = []
        recover = project_io.ProjectTransaction.recover

        def pause_recovery(project_dir):
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("status recovery synchronization timed out")
            return recover(project_dir)

        def run_status():
            try:
                status_result.append(read_project_status(self.project))
            except BaseException as error:
                status_errors.append(error)

        def contend():
            try:
                with project_io.ProjectLock(self.project, timeout=0.1):
                    pass
            except BaseException as error:
                contender_errors.append(error)

        with mock.patch(
            "scripts.comic_sol.ProjectTransaction.recover",
            side_effect=pause_recovery,
        ):
            worker = threading.Thread(target=run_status)
            worker.start()
            self.assertTrue(entered.wait(timeout=5), "status did not enter recovery")
            contender = threading.Thread(target=contend)
            contender.start()
            contender.join(timeout=2)
            release.set()
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertFalse(contender.is_alive())
        self.assertEqual([], status_errors)
        self.assertEqual(1, len(contender_errors))
        self.assertIsInstance(contender_errors[0], TimeoutError)
        self.assertEqual("BLOCKED", status_result[0]["status"])
        self.assertEqual(original, (self.project / "project.json").read_bytes())
        self.assertEqual([], list((self.project / "logs/transactions").iterdir()))

    def test_finalize_recovers_interrupted_publication_before_workflow(self):
        original = self._leave_interrupted_manifest_publication()
        observed = []

        def inspect_recovered_state(project_dir, caller_project_dir, progress):
            observed.append((project_dir / "project.json").read_bytes())
            self.assertEqual([], list((project_dir / "logs/transactions").iterdir()))
            return {"status": "stub"}

        with mock.patch(
            "scripts.comic_sol._finalize_project_locked",
            side_effect=inspect_recovered_state,
        ):
            self.assertEqual({"status": "stub"}, finalize_project(self.project))

        self.assertEqual([original], observed)
        self.assertEqual(original, (self.project / "project.json").read_bytes())

    def test_finalize_waits_for_existing_project_lock(self):
        lock = project_io.ProjectLock(self.project, timeout=1.0)
        lock.__enter__()
        finished = threading.Event()
        errors = []

        def run_finalize():
            try:
                finalize_project(self.project)
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()

        try:
            with mock.patch(
                "scripts.comic_sol._finalize_project_locked",
                return_value={"status": "stub"},
            ):
                worker = threading.Thread(target=run_finalize)
                worker.start()
                self.assertFalse(finished.wait(timeout=0.2))
                lock.__exit__(None, None, None)
                self.assertTrue(finished.wait(timeout=5))
                worker.join(timeout=1)
        finally:
            if lock._lock_key is not None:
                lock.__exit__(None, None, None)

        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)

    def test_project_transaction_waits_for_existing_project_lock(self):
        lock = project_io.ProjectLock(self.project, timeout=1.0)
        lock.__enter__()
        finished = threading.Event()
        errors = []

        def run_transaction():
            try:
                with project_io.ProjectTransaction(self.project, "wait-test"):
                    pass
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()

        try:
            worker = threading.Thread(target=run_transaction)
            worker.start()
            self.assertFalse(finished.wait(timeout=0.2))
            lock.__exit__(None, None, None)
            self.assertTrue(finished.wait(timeout=5))
            worker.join(timeout=1)
        finally:
            if lock._lock_key is not None:
                lock.__exit__(None, None, None)

        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)

    def test_project_transaction_honors_custom_finite_lock_timeout(self):
        lock = project_io.ProjectLock(self.project, timeout=1.0)
        lock.__enter__()
        errors = []

        def run_transaction():
            try:
                with project_io.ProjectTransaction(
                    self.project,
                    "bounded-test",
                    lock_timeout=0.1,
                ):
                    pass
            except BaseException as error:
                errors.append(error)

        try:
            worker = threading.Thread(target=run_transaction)
            worker.start()
            worker.join(timeout=2)
        finally:
            lock.__exit__(None, None, None)

        self.assertFalse(worker.is_alive())
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], TimeoutError)

    def test_finalize_holds_project_lock_against_mutating_transactions(self):
        entered = threading.Event()
        release = threading.Event()
        errors = []

        def blocked_finalize(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("finalize synchronization timed out")
            return {"status": "stub"}

        def run_finalize():
            try:
                finalize_project(self.project)
            except BaseException as error:
                errors.append(error)

        with mock.patch(
            "scripts.comic_sol._finalize_project_locked",
            side_effect=blocked_finalize,
        ):
            worker = threading.Thread(target=run_finalize)
            worker.start()
            self.assertTrue(entered.wait(timeout=5), "finalize did not acquire its lock")
            try:
                with self.assertRaises(TimeoutError):
                    with project_io.ProjectLock(self.project, timeout=0.1):
                        pass
            finally:
                release.set()
                worker.join(timeout=5)

        self.assertFalse(worker.is_alive(), "finalize worker did not exit")
        self.assertEqual([], errors)
        self.assertFalse(
            self.project.parent.joinpath(f".{self.project.name}.finalize-lock").exists()
        )


class PdfExportTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary_directory.name) / "project"
        (self.project / "pages").mkdir(parents=True)
        manifest = read_json(ROOT / "templates/manifest.json")
        manifest["project_id"] = "project"
        manifest["input"]["source_sha256"] = "a" * 64
        manifest["settings"]["page_count"] = 1
        manifest["settings"]["panel_count"] = 1
        manifest["panels"] = ["p01-01"]
        atomic_write_json(self.project / "project.json", manifest)
        from PIL import Image

        Image.new("RGB", (1600, 2400), "red").save(self.project / "pages/page-001.png")
        page_qa = self.project / "qa/pages"
        page_qa.mkdir(parents=True)
        (page_qa / "page-001.json").write_text("{}", "utf-8")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_guarded_export_preserves_manifest_change_made_before_lock(self):
        def mutate_manifest(*args, **kwargs):
            manifest = read_json(self.project / "project.json")
            manifest["warnings"] = ["concurrent warning"]
            atomic_write_json(self.project / "project.json", manifest)
            return b"%PDF-1.4\n%%EOF\n", {"page_count": 1}

        with (
            mock.patch("scripts.export_pdf.require_valid_project"),
            mock.patch(
                "scripts.export_pdf._render_verified_payload",
                side_effect=mutate_manifest,
            ),
        ):
            guarded_export(self.project)

        self.assertEqual(
            ["concurrent warning"],
            read_json(self.project / "project.json")["warnings"],
        )


if __name__ == "__main__":
    unittest.main()
