import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import project_io
from scripts.comic_sol import atomic_write_json, read_json, record_generation_attempt
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
            ("p01-01", "navy"), ("p01-02", "blue"), ("p01-03", "green"),
            ("p01-04", "red"), ("p01-05", "yellow"), ("p01-06", "magenta"),
            ("p01-07", "cyan"), ("p01-08", "white"),
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
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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
        self.old_accepted_bytes = (
            self.project_dir / "panels/raw/p01-01.png"
        ).read_bytes()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_promotion_archive_race(self):
        scripts_root = os.fspath(Path(__file__).resolve().parents[1])
        children = []
        for _ in range(2):
            proc = subprocess.Popen(
                [sys.executable, "-c", _PROMOTION_RACE_CHILD, os.fspath(self.project_dir), scripts_root],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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
        Image.new("RGB", (1600, 2400), "red").save(
            self.project / "pages/page-001.png"
        )
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
