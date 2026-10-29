from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DependencyMaintenanceTests(unittest.TestCase):
    def test_dependabot_leaves_generated_locks_to_the_refresh_workflow(self):
        dependabot = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")

        self.assertNotIn("/requirements/locks", dependabot)
        self.assertIn("package-ecosystem: pip", dependabot)
        self.assertIn("package-ecosystem: docker", dependabot)
        docker_updates = dependabot.split("package-ecosystem: docker", 1)[1].split(
            "package-ecosystem: github-actions", 1
        )[0]
        self.assertIn("interval: weekly", docker_updates)
        self.assertIn("docker-base-image:", docker_updates)

    def test_refresh_workflow_compiles_native_locks_and_runs_required_checks(self):
        workflow = (ROOT / ".github" / "workflows" / "refresh-dependency-locks.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("schedule:", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        for runner, platform in (
            ("ubuntu-latest", "linux"),
            ("macos-latest", "macos"),
            ("windows-latest", "windows"),
        ):
            self.assertIn(f"os: {runner}", workflow)
            self.assertIn(f"platform: {platform}", workflow)
        self.assertIn("piptools compile --upgrade --allow-unsafe --generate-hashes", workflow)
        self.assertIn(
            "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333",
            workflow,
        )
        self.assertIn(
            "peter-evans/create-pull-request@5f6978faf089d4d20b00c7766989d076bb2fc7f1",
            workflow,
        )
        self.assertIn("gh workflow run tests.yml --ref", workflow)

    def test_ci_audits_each_committed_lockfile(self):
        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

        self.assertIn("dependency-lock-audit:", workflow)
        self.assertIn('case "${RUNNER_OS}" in', workflow)
        self.assertIn('for lock in "${locks[@]}"; do', workflow)
        self.assertIn('"${audit_python}" -m pip_audit -r "${lock}"', workflow)
        for lock in (ROOT / "requirements" / "locks").glob("*.txt"):
            self.assertIn(lock.name, workflow)

    def test_clean_install_smoke_uses_hashed_runtime_or_base_lock(self):
        script = (ROOT / "scripts" / "clean_install_smoke.py").read_text(encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

        self.assertIn('"--require-hashes"', script)
        self.assertIn('"--no-deps"', script)
        self.assertIn("--lock requirements/locks/runtime-${{ matrix.lock }}-x86_64.txt", workflow)
        self.assertIn("--lock requirements/locks/base-${{ matrix.lock }}-x86_64.txt", workflow)


if __name__ == "__main__":
    unittest.main()
