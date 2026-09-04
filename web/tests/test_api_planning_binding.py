"""Behavioral regression coverage for planning-response project/revision binding."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


STATIC = Path(__file__).resolve().parents[1] / "comic_sol_web" / "static"
API = STATIC / "api.js"


class PlanningApiBindingTests(unittest.TestCase):
    def test_queue_planning_rejects_wrong_project_or_revision(self) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for the Studio API contract")
        assert node is not None

        script = r"""
import { readFileSync } from "node:fs";

function moduleUrl(source) {
  return `data:text/javascript;base64,${Buffer.from(source, "utf8").toString("base64")}`;
}
function check(condition, message) {
  if (!condition) throw new Error(message);
}

globalThis.document = { cookie: "comic_sol_csrf=csrf-test" };
if (typeof globalThis.FormData === "undefined") globalThis.FormData = class FormData {};

let responsePayload = null;
globalThis.fetch = async () => ({
  ok: true,
  status: 201,
  async json() { return responsePayload; },
});

const source = readFileSync(new URL(__API_MODULE__), "utf8");
const api = await import(moduleUrl(source));
const selection = { provider: "openai", model: "gpt-5.4-mini" };
const idempotencyKey = "00000000-0000-4000-8000-000000000001";
const planningJob = (projectId, revision) => ({
  job_id: "planning-job-1",
  project_id: projectId,
  project_revision: revision,
  state: "queued",
  attempt_count: 0,
  provider: selection.provider,
  model: selection.model,
  published_revision: null,
  usage: {},
  error_category: null,
});

responsePayload = planningJob("project-a", 3);
const accepted = await api.queuePlanning("project-a", 3, selection, idempotencyKey);
check(accepted.job.project_id === "project-a", "matching project response was rejected");
check(accepted.job.project_revision === 3, "matching revision response was rejected");

for (const [payload, label] of [
  [planningJob("project-b", 3), "wrong project"],
  [planningJob("project-a", 2), "stale revision"],
  [planningJob("project-a", 4), "future revision"],
]) {
  responsePayload = payload;
  let rejected = false;
  try {
    await api.queuePlanning("project-a", 3, selection, idempotencyKey);
  } catch (error) {
    rejected = error instanceof api.StudioApiError;
  }
  check(rejected, `${label} planning response was accepted`);
}
""".replace("__API_MODULE__", json.dumps(API.as_uri()))

        completed = subprocess.run(
            [node, "--input-type=module", "--eval", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(
            0,
            completed.returncode,
            f"Node planning binding contract failed:\n{completed.stdout}\n{completed.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
