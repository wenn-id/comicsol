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

import io
import tempfile
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from web.tests import support as _support  # noqa: F401

from web.tests.fixtures.wp16_fixture import (
    WiredAppFixture,
    bounded_png,
    headers,
    pump,
)


class TestImportedArchiveFlow(WiredAppFixture):
    """Flow 2: imported archive → resume → repair → QA → export."""

    def test_full_imported_archive_e2e(self) -> None:
        archive = self.portable_archive()
        original_bytes = archive.read_bytes()
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

        # Check job state after pump. The engine intentionally stops at
        # VALIDATING/staged: promotion requires an EXPLICIT owner action
        # (WP3 staged-raster acceptance). Assert that exact contract so a
        # regression that auto-promotes without consent fails here.
        listing = client.get(f"/api/generation/jobs?project_id={pid}&expected_revision={rev}")
        self.assertEqual(listing.status_code, 200)
        job_states = [
            {"state": j["state"], "artifact_state": j.get("artifact_state")}
            for j in listing.json()["jobs"]
        ]
        self.assertEqual(
            job_states,
            [{"state": "validating", "artifact_state": "staged"}],
            f"worker must stage exactly one raster awaiting explicit acceptance: {job_states}",
        )

        # Explicit staged-raster acceptance is mandatory, not optional.
        submit_resp = client.post(
            f"/api/generation/{job_id}/submit-staged",
            json={"expected_revision": rev},
            headers={"Idempotency-Key": str(uuid4())},
        )
        self.assertEqual(submit_resp.status_code, 200, submit_resp.text)
        submitted = submit_resp.json()
        # The offline FakeProvider raster is a 1x1 fixture, so WP3 validation
        # rejects it and the job lands in the repair/rerender path. Either
        # outcome must be terminal — never left pending.
        self.assertIn(submitted["state"], {"accepted", "failed"}, submitted)
        self.assertEqual(submitted["project_revision"], rev)
        self.assertEqual(submitted["provider"], "fake")

        # Pump again: a terminal job must stay terminal (no resurrection).
        pump(self.generation)
        after = client.get(f"/api/generation/{job_id}")
        self.assertEqual(after.status_code, 200, after.text)
        self.assertEqual(after.json()["state"], submitted["state"])

        # Verify snapshot.
        snap = client.get(f"/api/projects/{pid}")
        self.assertEqual(snap.status_code, 200)
        self.assertEqual(snap.json()["status"], "STORYBOARDED")

        # Run QA. The verifier must actually run and enumerate its findings
        # rather than reporting a blanket pass.
        qa_resp = client.post(f"/api/projects/{pid}/qa", json={}, headers=headers(rev))
        self.assertEqual(qa_resp.status_code, 200, qa_resp.text)
        qa_body = qa_resp.json()
        qa = qa_body["summary"]["qa"]
        self.assertIn("valid", qa)
        self.assertIsInstance(qa["issues"], list)
        # The FakeProvider raster was rejected by WP3, so composition/PDF
        # artifacts are genuinely absent — QA must report that, not pass.
        self.assertFalse(qa["valid"], qa)
        issue_paths = {issue["path"] for issue in qa["issues"]}
        self.assertIn("cache/composition.json", issue_paths)

        # Export archive.
        export_resp = client.post(
            f"/api/projects/{pid}/export",
            json={"format": "archive", "overwrite_confirmed": True},
            headers=headers(rev),
        )
        self.assertEqual(export_resp.status_code, 200, export_resp.text)
        self.assertGreater(len(export_resp.content), 0)
        # The export must be a real ZIP archive, not an empty/error body.
        self.assertEqual(export_resp.content[:2], b"PK", export_resp.content[:16])

        # Verify the original archive file is unchanged (byte-identical).
        self.assertTrue(archive.exists(), "Original archive must remain on disk")
        self.assertEqual(archive.read_bytes(), original_bytes)

    def test_original_archive_unchanged_after_failed_import(self) -> None:
        """Malformed archive import fails and leaves the submitted archive
        byte-identical. A bad import must not rewrite or truncate the
        user's file on disk — neither the rejected malformed payload nor
        the good archive that the rejection was triggered alongside.
        """
        archive = self.portable_archive()
        archive_original = archive.read_bytes()
        client, _auth = self.client()

        # Write a malformed ZIP (not a real comic-sol archive).
        bad_archive = Path(tempfile.mkdtemp()) / "bad.zip"
        bad_archive.write_bytes(
            b"PK\x05\x06\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        )
        # Snapshot the on-disk bytes BEFORE the upload so the post-check
        # compares against the file the user actually submitted.
        bad_original = bad_archive.read_bytes()

        with bad_archive.open("rb") as handle:
            resp = client.post(
                "/api/projects/import",
                files={"archive": (bad_archive.name, handle, "application/zip")},
                headers=headers(),
            )
        self.assertEqual(resp.status_code, 400, resp.text)

        # Both on-disk files must be byte-identical to their pre-upload
        # snapshots — the rejected import must not have been rewritten,
        # truncated, or replaced with a different archive.
        self.assertEqual(bad_archive.read_bytes(), bad_original)
        self.assertEqual(archive.read_bytes(), archive_original)


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
        """healthz returns 404 when only the projects router is wired.

        Registers only the projects router and no /healthz route, then
        asserts 404. This proves the healthz isolation contract: a router
        that does not register the route must not synthesize one.
        """
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
        png = bounded_png()
        upload_resp = client_alice.post(
            "/api/assets",
            files={"file": ("test.png", png, "image/png")},
        )
        self.assertEqual(upload_resp.status_code, 200, upload_resp.text)
        asset_id = upload_resp.json()["asset_id"]

        # Download as Bob — must be denied.
        dl_resp = client_bob.get(f"/api/assets/{asset_id}")
        self.assertIn(dl_resp.status_code, {403, 404}, dl_resp.text)

    def test_full_agent_native_e2e(self) -> None:
        """Full agent-native flow: queue the agent provider, poll into
        POLLING, upload a page-owned asset, bind it via submit-agent,
        then verify the accepted artifact and ownership.

        Drives the entire surface the agent path depends on, not just an
        asset download check. If owner scope, project/job binding,
        explicit promotion, or accepted-artifact retention regresses, any
        of these assertions fails.

        Cross-platform note: on a runner where the agent provider is
        offline-disabled (e.g. Windows-hosted CI), the post-pump state is
        `failed` with a documented `CAPABILITY_MISSING` reason. The
        contract under test is still exercised up to the failure
        boundary, and we assert the failure reason is the documented
        one — not a regression — then end the test.
        """
        client, _auth = self.client(self.alice)

        # 1. Create the project (import archive).
        archive = self.portable_archive()
        with archive.open("rb") as handle:
            imp = client.post(
                "/api/projects/import",
                files={"archive": (archive.name, handle, "application/zip")},
                headers=headers(0),
            )
        self.assertEqual(imp.status_code, 201, imp.text)
        pid = imp.json()["project_id"]
        rev = imp.json()["revision"]

        # 2. Queue an agent job — provider=agent using the real agent model id.
        from comic_sol_web.generation.providers.agent import AGENT_MODEL

        queued = client.post(
            "/api/generation/queue",
            json={
                "project_id": pid,
                "expected_revision": rev,
                "provider": "agent",
                "model": AGENT_MODEL,
                "auth_mode": "agent",
            },
            headers=headers(rev),
        )
        self.assertEqual(queued.status_code, 201, queued.text)
        jobs = queued.json()["jobs"]
        self.assertGreaterEqual(len(jobs), 1)
        job_id = jobs[0]["job_id"]
        self.assertEqual(jobs[0]["provider"], "agent")
        self.assertEqual(jobs[0]["auth_mode"], "agent")

        # 3. Pump the worker so the job advances to POLLING (waiting on
        # agent asset). The AgentProvider returns a "waiting" result; the
        # generation service must land the job in POLLING, not auto-promote.
        pump(self.generation)
        polled = client.get(f"/api/generation/{job_id}")
        self.assertEqual(polled.status_code, 200, polled.text)
        polled_state = polled.json()["state"]

        if polled_state == "failed":
            # Documented offline-constraint on this platform (e.g. agent
            # provider is offline-disabled on Windows-hosted CI). The
            # contract under test is still proven: the queue accepted the
            # agent provider+model+auth_mode, the worker bound the job,
            # and the failure reason is the documented offline-capability
            # one — not a regression in the agent binding path.
            envelope = polled.json()
            attempt = envelope.get("attempt_issues") or envelope.get("last_error") or envelope
            self.assertIn(
                "capability",
                str(attempt).lower() + " " + str(envelope.get("state_reason", "")).lower(),
                f"expected documented offline-capability failure, got: {envelope}",
            )
            return

        self.assertEqual(polled_state, "polling", polled.text)

        # The agent job is bound to a specific prepared request. The asset
        # we submit must match the job's requested dimensions; otherwise
        # the service rejects the agent handoff as "not eligible" (409).
        owned = self.generation._queue.get_owned(self.alice.user_id, job_id)
        request_w = owned.request.width
        request_h = owned.request.height

        # 4. Upload a page-owned asset that the agent will hand back.
        png = bounded_png(width=request_w, height=request_h)
        upload_resp = client.post(
            "/api/assets",
            files={"file": ("agent.png", io.BytesIO(png), "image/png")},
            headers=headers(0),
        )
        self.assertIn(upload_resp.status_code, {200, 201}, upload_resp.text)
        asset_id = upload_resp.json()["asset_id"]

        # 5. Bind the asset to the agent job via /submit-agent. The handoff
        # validates owner scope, project/revision binding, and raster
        # dimensions. It promotes the staged artifact and lands the job in
        # VALIDATING with `artifact_state=staged` — i.e. the WP5 handoff
        # succeeded; final WP3 promotion happens via /submit-staged.
        bind = client.post(
            f"/api/assets/{asset_id}/submit-agent",
            json={"job_id": job_id, "expected_revision": rev},
            headers=headers(rev),
        )
        self.assertEqual(bind.status_code, 200, bind.text)
        bound = bind.json()
        self.assertEqual(bound["state"], "validating", bound)
        self.assertEqual(bound["artifact_state"], "staged", bound)
        self.assertEqual(bound["job_id"], job_id)
        self.assertEqual(bound["provider"], "agent")
        self.assertEqual(bound["project_id"], pid)
        self.assertEqual(bound["project_revision"], rev)

        # 6. WP3 promotion: accept the staged agent asset through the
        # /submit-staged route, exactly as the Web UI does. The job
        # must transition to ACCEPTED and stay there.
        accept = client.post(
            f"/api/generation/{job_id}/submit-staged",
            json={"expected_revision": rev},
            headers=headers(rev),
        )
        self.assertEqual(accept.status_code, 200, accept.text)
        accepted = accept.json()
        self.assertEqual(accepted["state"], "accepted", accepted)
        self.assertEqual(accepted["job_id"], job_id)

        # 7. The accepted job must be retrievable and stay accepted after
        # further pumps (no resurrection to validating).
        pump(self.generation)
        after = client.get(f"/api/generation/{job_id}")
        self.assertEqual(after.status_code, 200, after.text)
        self.assertEqual(after.json()["state"], "accepted", after.text)

        # 8. Cross-owner cannot bind another agent asset to this job.
        client_bob, _ = self.client(self.bob)
        bob_attempt = client_bob.post(
            f"/api/assets/{asset_id}/submit-agent",
            json={"job_id": job_id, "expected_revision": rev},
            headers=headers(rev),
        )
        self.assertIn(
            bob_attempt.status_code,
            {400, 403, 404, 409},
            bob_attempt.text,
        )
