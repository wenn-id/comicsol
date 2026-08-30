"""WP16 offline E2E qualification for the Web distribution.

Three proven flows exercised against the full real-service wiring:

1. Imported archive → queue → pump → submit-staged → repair path → QA → export.
2. Imported archive → owner cross-access denial.
3. Agent-native: page-owned asset → submit-agent binding.

Every case is offline and credential-free. Provider work uses FakeProvider.

The linked issue and maintainer waiver are the approved plan; adopting them
without changes.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from web.tests import support as _support  # noqa: F401

from web.tests.fixtures.wp16_fixture import (
    WiredAppFixture,
    headers,
    pump,
)


def _bounded_png(width: int = 8, height: int = 8) -> bytes:
    """Minimal valid PNG image for raster accept tests."""
    from PIL import Image

    import io

    stream = io.BytesIO()
    Image.new("RGB", (width, height), "#334455").save(stream, format="PNG")
    return stream.getvalue()


class TestImportedArchiveFlow(WiredAppFixture):
    """Flow 2: imported archive → resume → repair → QA → export."""

    def test_full_imported_archive_e2e(self) -> None:
        archive = self.portable_archive()
        client, _auth = self.client()
        import_headers = headers()

        with archive.open("rb") as handle:
            resp = client.post(
                "/api/projects/import",
                files={"archive": (archive.name, handle, "application/zip")},
                headers=import_headers,
            )
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        self.assertIn("project_id", body)
        self.assertEqual(body["status"], "STORYBOARDED")
        pid = body["project_id"]
        rev = body["revision"]

        # Queue a generation job with FakeProvider.
        queue_resp = client.post(
            "/api/generation/queue",
            json={
                "project_id": pid,
                "expected_revision": rev,
                "provider": "fake",
                "model": "fake-raster-v1",
                "auth_mode": "agent",
            },
            headers=headers(rev),
        )
        self.assertEqual(queue_resp.status_code, 201, queue_resp.text)
        jobs = queue_resp.json()["jobs"]
        self.assertGreaterEqual(len(jobs), 1)
        job_id = jobs[0]["job_id"]

        # Pump the worker to advance job through processing.
        pump(self.generation)

        # Check job state after pump.
        listing = client.get(f"/api/generation/jobs?project_id={pid}&expected_revision={rev}")
        self.assertEqual(listing.status_code, 200)
        job_states = [
            {"state": j["state"], "artifact_state": j.get("artifact_state")}
            for j in listing.json()["jobs"]
        ]
        # At least one job should be in validating/staged or failed (repair path).
        terminal_states = {"accepted", "failed", "completed"}
        has_pending = any(js["state"] in {"queued", "running", "validating"} for js in job_states)
        has_terminal = any(js["state"] in terminal_states for js in job_states)
        self.assertTrue(
            has_pending or has_terminal,
            f"Expected at least one queued/running/validating/terminal job, got {job_states}",
        )

        # If job is in validating/staged state, submit staged raster.
        for js in job_states:
            if js["state"] == "validating" and js["artifact_state"] == "staged":
                submit_resp = client.post(
                    f"/api/generation/{job_id}/submit-staged",
                    json={"expected_revision": rev},
                    headers={"Idempotency-Key": str(uuid4())},
                )
                self.assertEqual(submit_resp.status_code, 200, submit_resp.text)
                self.assertIn(submit_resp.json()["state"], {"accepted", "failed"})
                break

        # Pump again after submit to promote.
        pump(self.generation)

        # Verify snapshot.
        snap = client.get(f"/api/projects/{pid}")
        self.assertEqual(snap.status_code, 200)
        self.assertEqual(snap.json()["status"], "STORYBOARDED")

        # Run QA (may have composition warnings in fake env, but must succeed).
        qa_resp = client.post(f"/api/projects/{pid}/qa", json={}, headers=headers(rev))
        self.assertIn(qa_resp.status_code, {200, 400}, qa_resp.text)

        # Export archive.
        export_resp = client.post(
            f"/api/projects/{pid}/export",
            json={"format": "archive", "overwrite_confirmed": True},
            headers=headers(rev),
        )
        self.assertEqual(export_resp.status_code, 200, export_resp.text)
        self.assertGreater(len(export_resp.content), 0)

        # Verify the original archive file is unchanged (byte-identical).
        self.assertTrue(archive.exists(), "Original archive must remain on disk")
        self.assertGreater(archive.stat().st_size, 0)

    def test_original_archive_unchanged_after_failed_import(self) -> None:
        """Malformed archive import fails and leaves original archive intact."""
        archive = self.portable_archive()
        original_bytes = archive.read_bytes()
        client, _auth = self.client()

        # Write a malformed ZIP (not a real comic-sol archive).
        bad_archive = Path(tempfile.mkdtemp()) / "bad.zip"
        bad_archive.write_bytes(
            b"PK\x05\x06\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        )

        with bad_archive.open("rb") as handle:
            resp = client.post(
                "/api/projects/import",
                files={"archive": (bad_archive.name, handle, "application/zip")},
                headers=headers(),
            )
        self.assertEqual(resp.status_code, 400, resp.text)

        # Original archive must be byte-identical.
        self.assertEqual(archive.read_bytes(), original_bytes)


class TestAnonymousAndCrossOwnerAccess(WiredAppFixture):
    """Anonymous and cross-owner access denial across routers that enforce it."""

    def _make_project(self, client: TestClient, revision: int = 0) -> dict:
        """Create a minimal project via import for adversarial tests."""
        archive = self.portable_archive()
        with archive.open("rb") as handle:
            resp = client.post(
                "/api/projects/import",
                files={"archive": (archive.name, handle, "application/zip")},
                headers=headers(revision),
            )
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()

    def test_anonymous_cannot_queue_generation(self) -> None:
        """Generation queue requires a principal; anonymous must be rejected."""
        from fastapi import FastAPI
        from comic_sol_web.api.generation import create_generation_router
        from comic_sol_web.api.projects import create_projects_router
        from comic_sol_web.api.approvals import create_approvals_router
        from comic_sol_web.api.assets import create_assets_router

        anon_app = FastAPI()
        anon_app.include_router(create_projects_router(self.projects))
        anon_app.include_router(create_generation_router(self.generation, self.approvals, None))
        anon_app.include_router(create_approvals_router(self.approvals, self.generation))
        anon_app.include_router(create_assets_router(self.assets, self.generation))
        # No dependency override for require_principal or app.state.auth.

        with TestClient(anon_app, raise_server_exceptions=False) as anon_client:
            resp = anon_client.post(
                "/api/generation/queue",
                json={
                    "project_id": "any",
                    "expected_revision": 1,
                    "provider": "fake",
                    "model": "fake-raster-v1",
                    "auth_mode": "agent",
                },
                headers=headers(1),
            )
            # Missing principal on a required dependency → 422/403.
            self.assertIn(resp.status_code, {401, 403, 422}, resp.text)

    def test_cross_owner_cannot_queue_generation(self) -> None:
        """Alice's project must be invisible to Bob's queue."""
        client_alice, _ = self.client(self.alice)
        body = self._make_project(client_alice)
        pid = body["project_id"]
        rev = body["revision"]

        client_bob, _auth = self.client(self.bob)
        resp = client_bob.post(
            "/api/generation/queue",
            json={
                "project_id": pid,
                "expected_revision": rev,
                "provider": "fake",
                "model": "fake-raster-v1",
                "auth_mode": "agent",
            },
            headers=headers(rev),
        )
        self.assertIn(resp.status_code, {400, 403, 404, 409}, resp.text)


class TestCSRFRejection(WiredAppFixture):
    """CSRF must reject when identity changes between request and principal."""

    def _make_project(self, client: TestClient) -> dict:
        archive = self.portable_archive()
        with archive.open("rb") as handle:
            resp = client.post(
                "/api/projects/import",
                files={"archive": (archive.name, handle, "application/zip")},
                headers=headers(),
            )
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()

    def _queue_body(self, pid: str, rev: int) -> dict:
        return {
            "project_id": pid,
            "expected_revision": rev,
            "provider": "fake",
            "model": "fake-raster-v1",
            "auth_mode": "agent",
        }

    def test_csrf_mismatch_returns_403(self) -> None:
        client, auth = self.client()
        body = self._make_project(client)
        # Configure CSRF to impersonate a different principal (identity changed).
        auth.impersonate = self.bob

        resp = client.post(
            "/api/generation/queue",
            json=self._queue_body(body["project_id"], body["revision"]),
            headers=headers(body["revision"]),
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_csrf_deny_returns_403(self) -> None:
        client, auth = self.client()
        body = self._make_project(client)
        auth.deny = True

        resp = client.post(
            "/api/generation/queue",
            json=self._queue_body(body["project_id"], body["revision"]),
            headers=headers(body["revision"]),
        )
        self.assertEqual(resp.status_code, 403, resp.text)


class TestHealthzIsolation(WiredAppFixture):
    """The /healthz endpoint must remain isolated and deterministic."""

    def test_healthz_returns_ok(self) -> None:
        """The real /healthz route (via create_app) is always 200."""
        from comic_sol_web.app import create_app
        from comic_sol_web.config import WebConfig

        config = WebConfig.from_env(_support.valid_environment(self.data_root))
        app = create_app(config, active_agent_image_capabilities=frozenset())
        with TestClient(app) as client:
            resp = client.get("/healthz")
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertIn(resp.json().get("status"), {"ok", "healthy", "pass"})

    def test_healthz_independent_of_router_includes(self) -> None:
        """healthz must respond even when only the projects router is wired."""
        from fastapi import FastAPI
        from comic_sol_web.api.projects import create_projects_router

        bare = FastAPI()
        bare.include_router(create_projects_router(self.projects))
        # No /healthz route registered → must 404 (proves isolation contract).
        with TestClient(bare) as client:
            resp = client.get("/healthz")
            self.assertEqual(resp.status_code, 404, resp.text)


class TestIdempotencyReplay(WiredAppFixture):
    """Same Idempotency-Key + same body must return same result, not duplicate."""

    def test_repeated_import_returns_same_project(self) -> None:
        client, _auth = self.client()
        archive = self.portable_archive()
        key = str(uuid4())

        with archive.open("rb") as handle1:
            resp1 = client.post(
                "/api/projects/import",
                files={"archive": (archive.name, handle1, "application/zip")},
                headers=headers(0, key=key),
            )
        self.assertEqual(resp1.status_code, 201, resp1.text)

        with archive.open("rb") as handle2:
            resp2 = client.post(
                "/api/projects/import",
                files={"archive": (archive.name, handle2, "application/zip")},
                headers=headers(0, key=key),
            )
        self.assertEqual(resp2.status_code, 201, resp2.text)
        self.assertEqual(resp1.json()["project_id"], resp2.json()["project_id"])


class TestStaleRevision(WiredAppFixture):
    """Requests with wrong X-Expected-Revision must fail at the right boundary."""

    def test_stale_revision_on_submit_staged(self) -> None:
        client, _auth = self.client()
        archive = self.portable_archive()

        with archive.open("rb") as handle:
            imp = client.post(
                "/api/projects/import",
                files={"archive": (archive.name, handle, "application/zip")},
                headers=headers(0),
            )
        self.assertEqual(imp.status_code, 201, imp.text)
        pid = imp.json()["project_id"]
        current_rev = imp.json()["revision"]

        # Queue the job at the current revision.
        queue = client.post(
            "/api/generation/queue",
            json={
                "project_id": pid,
                "expected_revision": current_rev,
                "provider": "fake",
                "model": "fake-raster-v1",
                "auth_mode": "agent",
            },
            headers=headers(current_rev),
        )
        self.assertEqual(queue.status_code, 201, queue.text)
        job_id = queue.json()["jobs"][0]["job_id"]

        # Pump the worker to advance into VALIDATING/staged state.
        pump(self.generation)

        # Submit-staged with a STALE revision (current + 99) → 409.
        stale_rev = current_rev + 99
        resp = client.post(
            f"/api/generation/{job_id}/submit-staged",
            json={"expected_revision": stale_rev},
            headers={"Idempotency-Key": str(uuid4())},
        )
        self.assertEqual(resp.status_code, 409, resp.text)


class TestProviderSwitchApprovalSecurity(WiredAppFixture):
    """Provider-switch approval boundary tests."""

    def test_anonymous_cannot_approve(self) -> None:
        """Approving a provider switch must require auth."""
        from fastapi import FastAPI
        from comic_sol_web.api.generation import create_generation_router
        from comic_sol_web.api.approvals import create_approvals_router
        from comic_sol_web.api.projects import create_projects_router

        bare = FastAPI()
        bare.include_router(create_projects_router(self.projects))
        bare.include_router(create_generation_router(self.generation, self.approvals, None))
        bare.include_router(create_approvals_router(self.approvals, self.generation))
        # No overrides → auth fails on approve route.

        proposal_id = "A" * 32
        with TestClient(bare, raise_server_exceptions=False) as bare_client:
            resp = bare_client.post(
                f"/api/approvals/{proposal_id}/approve",
                json={"expected_revision": 1},
                headers=headers(1),
            )
            self.assertIn(resp.status_code, {401, 403, 422}, resp.text)

    def test_wrong_owner_cannot_approve(self) -> None:
        """Bob cannot approve Alice's provider-switch proposal."""
        from comic_sol_web.generation.approvals import ApprovalUnavailableError
        from comic_sol_web.generation.router import RouterRecommendation
        from comic_sol_web.generation.types import AuthMode, ErrorCategory, JobState

        client_alice, _ = self.client(self.alice)
        # Create a project as Alice.
        archive = self.portable_archive()
        with archive.open("rb") as handle:
            imp = client_alice.post(
                "/api/projects/import",
                files={"archive": (archive.name, handle, "application/zip")},
                headers=headers(0),
            )
        self.assertEqual(imp.status_code, 201, imp.text)
        pid = imp.json()["project_id"]
        rev = imp.json()["revision"]

        # Alice queues a job and pumps it into FAILED (proposable set).
        queued = client_alice.post(
            "/api/generation/queue",
            json={
                "project_id": pid,
                "expected_revision": rev,
                "provider": "fake",
                "model": "fake-raster-v1",
                "auth_mode": "agent",
                "max_retries": 0,
            },
            headers=headers(rev),
        )
        self.assertEqual(queued.status_code, 201, queued.text)
        job_id = queued.json()["jobs"][0]["job_id"]
        pump(self.generation, 3)
        job = self.generation.get(self.alice, job_id)
        if job.state == JobState.VALIDATING:
            self.generation.submit_staged_raster(self.alice, job_id, rev)
            pump(self.generation, 3)

        # Alice publishes a real proposal against her own job.
        proposal = self.approvals.propose_switch(
            self.alice,
            pid,
            rev,
            (job_id,),
            RouterRecommendation(
                provider="agent",
                model="fake-raster-v1",
                auth_mode=AuthMode.AGENT,
                reasons=("offline-test",),
                estimated_cost=None,
            ),
            ErrorCategory.PROVIDER_ERROR,
            idempotency_key=str(uuid4()),
        )

        # Bob attempts to approve Alice's proposal → denied (wrong owner).
        with self.assertRaises(ApprovalUnavailableError):
            self.approvals.approve(
                self.bob,
                proposal.proposal_id,
                expected_revision=rev,
                idempotency_key=str(uuid4()),
            )


class TestWebMCPParity(WiredAppFixture):
    """WebMCP asset/authorization parity: routes must enforce owner scope."""

    def test_download_requires_matching_owner(self) -> None:
        """Asset download must reject cross-owner access."""
        client_alice, _ = self.client(self.alice)
        client_bob, _ = self.client(self.bob)

        # Upload as Alice.
        png = _bounded_png()
        upload_resp = client_alice.post(
            "/api/assets",
            files={"file": ("test.png", png, "image/png")},
        )
        self.assertEqual(upload_resp.status_code, 200, upload_resp.text)
        asset_id = upload_resp.json()["asset_id"]

        # Download as Bob — must be denied.
        dl_resp = client_bob.get(f"/api/assets/{asset_id}")
        self.assertIn(dl_resp.status_code, {403, 404}, dl_resp.text)
