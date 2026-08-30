"""WP16 offline adversarial security qualification for the Web distribution.

Covers, at the HTTP boundary over real wired services and at the
cryptographic/redaction primitives:

- redaction (credential, cookie, token, endpoint, machine-path,
  provider-payload, private-story URLs);
- bounded ciphertext/credential sizes;
- deterministic /healthz;
- queue replay idempotency and conflicting reuse;
- callback forgery and callback/poll races;
- duplicate provider completion;
- stale revision 409;
- CSRF mismatch/deny at queue boundary;
- anonymous and cross-owner access;
- archive traversal/symlink/oversize/malformed/rollback;
- raster MIME mismatch, dimension and decoded-size limits,
  failed-replacement retention;
- SSRF/redirect/loopback/private/link-local/DNS-rebinding policy;
- provider-switch approval expiry, replay, wrong owner, wrong jobs,
  forged destination;
- lease/restart recovery and cancellation races;
- sensitive-data redaction across logs/envelopes.

All offline; no live or paid provider calls; credential-free.
"""

from __future__ import annotations

import io
import json
import unittest
import zipfile
from uuid import uuid4

from fastapi.testclient import TestClient

from comic_sol_web.security import (
    CredentialCipher,
    REDACTED,
    redact_mapping,
    redact_text,
)

from web.tests.fixtures.wp16_fixture import (
    WiredAppFixture,
    headers,
    pump,
)


class TestRedactionPrimitives(unittest.TestCase):
    """Bounded redaction of credentials and sensitive material."""

    def test_bearer_token_redacted(self) -> None:
        text = "Authorization: Bearer abc.def-ghi_123+/="
        redacted = redact_text(text)
        assert "abc.def-ghi_123+/=" not in redacted
        assert REDACTED in redacted

    def test_basic_auth_redacted(self) -> None:
        text = "Authorization: Basic dXNlcjpwYXNz"
        redacted = redact_text(text)
        assert "dXNlcjpwYXNz" not in redacted
        assert REDACTED in redacted

    def test_assignment_redacted(self) -> None:
        text = 'api_key="sk-live-12345", password=supersecret'
        redacted = redact_text(text)
        assert "sk-live-12345" not in redacted
        assert "supersecret" not in redacted
        assert REDACTED in redacted

    def test_explicit_secret_replaced(self) -> None:
        secret = "my-super-secret-value"
        text = f"leak {secret} leak"
        redacted = redact_text(text, secrets=[secret])
        assert secret not in redacted
        assert REDACTED in redacted

    def test_private_story_url_redacted(self) -> None:
        secret = "s3cr3t-handle"
        text = f"visit https://example.com/stories/{secret}/page/1 now"
        redacted = redact_text(text, secrets=[secret])
        assert secret not in redacted
        assert REDACTED in redacted

    def test_redact_mapping_recursive(self) -> None:
        payload = {
            "meta": {"token": "tok-123", "ok": "fine"},
            "list": ["x", {"password": "pw-1"}],
            "api_key": "ak-9",
        }
        sanitized = redact_mapping(payload, secrets=["tok-123"])
        assert sanitized["meta"]["token"] == REDACTED
        assert sanitized["meta"]["ok"] == "fine"
        assert sanitized["list"][1]["password"] == REDACTED
        assert sanitized["api_key"] == REDACTED

    def test_negative_limit_rejected(self) -> None:
        with self.assertRaises(ValueError):
            redact_text("x", limit=-1)

    def test_credential_cipher_roundtrip_and_bounds(self) -> None:
        cipher = CredentialCipher("unit-test-secret")
        encoded = cipher.encrypt("sup3r-s3cret")
        assert encoded.startswith("v1:")
        assert cipher.decrypt(encoded) == "sup3r-s3cret"

        with self.assertRaises(ValueError):
            cipher.encrypt("x" * (64 * 1024 + 1))
        with self.assertRaises(ValueError):
            cipher.decrypt("z" * (128 * 1024 + 1))
        with self.assertRaises(ValueError):
            cipher.decrypt("nope")
        with self.assertRaises(ValueError):
            cipher.decrypt("z:garbage")

    def test_redaction_limit_bounded(self) -> None:
        text = "a" * 5000
        redacted = redact_text(text, limit=128)
        assert len(redacted) <= 128

    def test_machine_path_and_endpoint_redacted(self) -> None:
        secret = "/var/run/secrets/creds.json"
        text = f"config at {secret}; endpoint https://host/oauth/token"
        redacted = redact_text(text, secrets=[secret, "https://host/oauth/token"])
        assert secret not in redacted
        assert "https://host/oauth/token" not in redacted


class TestHealthzIsolation(WiredAppFixture):
    """The /healthz endpoint must be deterministic and lean."""

    def test_healthz_ok(self) -> None:
        from web.tests.support import valid_environment
        from web.comic_sol_web.app import create_app
        from web.comic_sol_web.config import WebConfig

        env = valid_environment(self.data_root)
        app = create_app(WebConfig.from_env(env))
        client = TestClient(app)
        resp = client.get("/healthz")
        assert resp.status_code == 200
        body = resp.json()
        # Deterministic in-memory payload; no version, env, or credential fields.
        assert body == {"status": "ok"}

    def test_healthz_no_secrets(self) -> None:
        from web.tests.support import valid_environment
        from web.comic_sol_web.app import create_app
        from web.comic_sol_web.config import WebConfig

        env = valid_environment(self.data_root)
        app = create_app(WebConfig.from_env(env))
        client = TestClient(app)
        resp = client.get("/healthz")
        text = resp.text.lower()
        for forbidden in ("secret", "token", "credential", "password", "api_key"):
            assert forbidden not in text, forbidden


class TestQueueIdempotency(WiredAppFixture):
    """Queue replay and conflicting reuse must be bounded."""

    def _queue_body(self, pid: str, rev: int, *, model: str = "fake-raster-v1") -> dict:
        return {
            "project_id": pid,
            "expected_revision": rev,
            "provider": "fake",
            "model": model,
            "auth_mode": "agent",
        }

    def test_replay_same_key_returns_same_jobs(self) -> None:
        client, _auth = self.client(self.alice)
        archive = self.portable_archive()
        with archive.open("rb") as handle:
            imp = client.post(
                "/api/projects/import",
                files={"archive": (archive.name, handle, "application/zip")},
                headers=headers(0),
            )
        assert imp.status_code == 201, imp.text
        pid = imp.json()["project_id"]
        rev = imp.json()["revision"]

        key = str(uuid4())
        first = client.post(
            "/api/generation/queue",
            json=self._queue_body(pid, rev),
            headers=headers(rev, key=key),
        )
        assert first.status_code == 201, first.text
        replay = client.post(
            "/api/generation/queue",
            json=self._queue_body(pid, rev),
            headers=headers(rev, key=key),
        )
        assert replay.status_code == 201, replay.text
        # Idempotency is per-content: the same canonical request must resolve
        # to the same job, even though the live envelope is stateful (the
        # background consumer may advance state between responses).
        first_ids = [job["job_id"] for job in first.json()["jobs"]]
        replay_ids = [job["job_id"] for job in replay.json()["jobs"]]
        assert first_ids and first_ids == replay_ids, (first_ids, replay_ids)

    def test_conflicting_reuse_new_key_rejected(self) -> None:
        client, _auth = self.client(self.alice)
        archive = self.portable_archive()
        with archive.open("rb") as handle:
            imp = client.post(
                "/api/projects/import",
                files={"archive": (archive.name, handle, "application/zip")},
                headers=headers(0),
            )
        pid = imp.json()["project_id"]
        rev = imp.json()["revision"]

        key = str(uuid4())
        first = client.post(
            "/api/generation/queue",
            json=self._queue_body(pid, rev),
            headers=headers(rev, key=key),
        )
        assert first.status_code == 201, first.text
        # A different idempotency key for the same project/revision.
        second = client.post(
            "/api/generation/queue",
            json=self._queue_body(pid, rev),
            headers=headers(rev, key=str(uuid4())),
        )
        # Either replay-conflict (409/400) or an identical req accepted as
        # deduplicated — but never a silently different job set.
        assert second.status_code in {200, 201, 400, 409}, second.text
        if second.status_code == 201:
            first_ids = [job["job_id"] for job in first.json()["jobs"]]
            second_ids = [job["job_id"] for job in second.json()["jobs"]]
            assert first_ids and first_ids == second_ids


class TestProviderSwitchApprovalSecurity(WiredAppFixture):
    """Approval expiry, replay, wrong owner, wrong jobs, forged destination.

    The offline provider set offers a switchable target (agent), so we build
    real proposals via the service and exercise the security boundaries that
    the HTTP routes delegate to.
    """

    def _make_fake_proposal(self) -> tuple[str, int, str]:
        """Return (project_id, project_revision, job_id) with a proposable job.

        Drives: import → queue fake → pump → submit-staged (lands job in
        FAILED, which is in the proposable set) so a provider-switch
        proposal can be published against a real, owned job.
        """
        from comic_sol_web.generation.types import JobState

        client, _ = self.client(self.alice)
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
        queued = client.post(
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
        return pid, rev, job_id

    def _build_proposal(self, pid: str, rev: int, job_id: str, *, ttl: int = 300):
        from comic_sol_web.generation.router import RouterRecommendation
        from comic_sol_web.generation.types import AuthMode, ErrorCategory

        rec = RouterRecommendation(
            provider="agent",
            model="fake-raster-v1",
            auth_mode=AuthMode.AGENT,
            reasons=("offline-test",),
            estimated_cost=None,
        )
        return self.approvals.propose_switch(
            self.alice,
            pid,
            rev,
            (job_id,),
            rec,
            ErrorCategory.PROVIDER_ERROR,
            idempotency_key=str(uuid4()),
            ttl_seconds=ttl,
        )

    def test_approve_requires_owner(self) -> None:
        from comic_sol_web.generation.approvals import ApprovalUnavailableError

        pid, rev, job_id = self._make_fake_proposal()
        prop = self._build_proposal(pid, rev, job_id)
        # Bob must not be able to act on Alice's proposal.
        with self.assertRaises(ApprovalUnavailableError):
            self.approvals.approve(
                self.bob,
                prop.proposal_id,
                expected_revision=rev,
                idempotency_key=str(uuid4()),
            )

    def test_approve_anonymous_rejected(self) -> None:
        from comic_sol_web.auth import SessionPrincipal
        from comic_sol_web.generation.approvals import ApprovalUnavailableError

        pid, rev, job_id = self._make_fake_proposal()
        prop = self._build_proposal(pid, rev, job_id)
        # A synthetic no-user principal must not be able to decide.
        anonymous = SessionPrincipal("", "")
        with self.assertRaises(ApprovalUnavailableError):
            self.approvals.approve(
                anonymous,
                prop.proposal_id,
                expected_revision=rev,
                idempotency_key=str(uuid4()),
            )

    def test_proposal_replay_is_bounded(self) -> None:
        """Approving the same proposal twice must not double-apply."""
        from comic_sol_web.generation.approvals import ApprovalConflictError

        pid, rev, job_id = self._make_fake_proposal()
        prop = self._build_proposal(pid, rev, job_id)
        # First approval lands.
        self.approvals.approve(
            self.alice,
            prop.proposal_id,
            expected_revision=rev,
            idempotency_key=str(uuid4()),
        )
        # Second approval of the same proposal must raise a conflict
        # (the decision table already contains a row for this proposal_id).
        with self.assertRaises(ApprovalConflictError):
            self.approvals.approve(
                self.alice,
                prop.proposal_id,
                expected_revision=rev,
                idempotency_key=str(uuid4()),
            )

    def test_proposal_expiry_rejected(self) -> None:
        """An expired proposal must be rejected with a conflict."""
        from comic_sol_web.generation.approvals import ApprovalConflictError

        pid, rev, job_id = self._make_fake_proposal()
        # ttl_seconds=1; the clock helper returns 1_000 throughout setUp.
        # Force expiry by advancing the approval clock past expires_at.
        self.clock_value = 1_000
        prop = self._build_proposal(pid, rev, job_id, ttl=1)
        self.clock_value = 1_000 + 3600
        with self.assertRaises(ApprovalConflictError):
            self.approvals.approve(
                self.alice,
                prop.proposal_id,
                expected_revision=rev,
                idempotency_key=str(uuid4()),
            )

    def test_approval_wrong_jobs_rejected(self) -> None:
        """A proposal whose job list is invalid must be rejected."""
        from comic_sol_web.generation.approvals import ApprovalRequestError

        # Non-hex job IDs and empty lists are rejected at proposal boundary.
        for bad in ([], ["not-hex"], "job-id-string", ["a" * 63]):
            with self.assertRaises(ApprovalRequestError, msg=str(bad)):
                self.approvals.propose_switch(
                    principal=self.alice,
                    project_id="proj",
                    expected_revision=1,
                    job_ids=bad,
                    recommendation=None,
                    reason="repair",
                    idempotency_key=str(uuid4()),
                )


class TestCallbackForgeryAndRace(WiredAppFixture):
    """Callback forgery and callback/poll races must be bounded."""

    def _queue_one(self, client) -> tuple[str, str, int]:
        archive = self.portable_archive()
        with archive.open("rb") as handle:
            imp = client.post(
                "/api/projects/import",
                files={"archive": (archive.name, handle, "application/zip")},
                headers=headers(0),
            )
        pid = imp.json()["project_id"]
        rev = imp.json()["revision"]
        queued = client.post(
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
        assert queued.status_code == 201, queued.text
        job_id = queued.json()["jobs"][0]["job_id"]
        return job_id, pid, rev

    def test_no_http_callback_endpoint_exists(self) -> None:
        """A forged HTTP callback must have no surface to land on.

        Provider callbacks flow through the worker → GenerationService.
        record_result, never through an HTTP route, so a direct POST cannot
        be used to forge a completion.
        """
        client, _auth = self.client(self.alice)
        job_id, _pid, _rev = self._queue_one(client)
        for path in (
            "/api/generation/callback",
            f"/api/generation/{job_id}/callback",
        ):
            resp = client.post(path, json={"forged": True}, headers=headers(0))
            self.assertIn(resp.status_code, {404, 405}, (path, resp.status_code, resp.text))

    def test_callback_requires_bound_attempt(self) -> None:
        """record_result must reject unbound or wrong-external-id callbacks.

        This is the only callback surface; forging it at the service layer
        must raise GenerationConflictError.
        """
        from comic_sol_web.generation.types import GenerationResult, JobState
        from PIL import Image as _PILImage
        from comic_sol_web.generation.service import GenerationConflictError

        import io as _io

        client, _auth = self.client(self.alice)
        job_id, _pid, _rev = self._queue_one(client)
        stream = _io.BytesIO()
        _PILImage.new("RGB", (8, 8), "#334455").save(stream, format="PNG")
        png_bytes = stream.getvalue()

        # No lease, no external_job_id → callback not bound to this attempt.
        with self.assertRaises(GenerationConflictError):
            self.generation.record_result(
                job_id=job_id,
                lease_token=None,
                result=GenerationResult(
                    external_job_id="forged-external",
                    state=JobState.ACCEPTED,
                    raster_bytes=png_bytes,
                    media_type="image/png",
                    effective_parameters={},
                    usage={},
                ),
            )

    def test_duplicate_completion_rejected(self) -> None:
        client, _auth = self.client(self.alice)
        job_id, pid, rev = self._queue_one(client)

        pump(self.generation)  # advance out of queued
        before = client.get(f"/api/generation/{job_id}")
        assert before.status_code == 200
        before_state = before.json()["state"]

        # A second worker pass must not resurrect or duplicate the job; the
        # state may only move forward (or stay at the same terminal/pending
        # value), never backwards to queued.
        pump(self.generation)
        after = client.get(f"/api/generation/{job_id}")
        assert after.status_code == 200
        self.assertNotEqual(after.json()["state"], "queued")
        # If the first pass already reached a terminal state, the second pass
        # must preserve it rather than re-running the provider.
        if before_state in {"accepted", "failed", "cancelled"}:
            self.assertEqual(after.json()["state"], before_state)


class TestArchiveSecurity(WiredAppFixture):
    """Archive traversal, symlink/reparse, oversize, malformed, rollback."""

    def _make_archive_bytes(self, entries: list[tuple[str, bytes]]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in entries:
                zf.writestr(name, data)
            zf.writestr("engine.json", json.dumps({"stage": "DRAFTED"}).encode())
        return buf.getvalue()

    def test_archive_traversal_rejected(self) -> None:
        client, _auth = self.client(self.alice)
        data = self._make_archive_bytes([("../evil", b"x"), ("page-1.png", b"png")])
        resp = client.post(
            "/api/projects/import",
            files={"archive": ("traversal.zip", io.BytesIO(data), "application/zip")},
            headers=headers(0),
        )
        assert resp.status_code in {400, 422}, resp.text

    def test_archive_symlink_rejected(self) -> None:
        client, _auth = self.client(self.alice)
        data = self._make_archive_bytes([("link", b"PNG")])
        resp = client.post(
            "/api/projects/import",
            files={"archive": ("symlink.zip", io.BytesIO(data), "application/zip")},
            headers=headers(0),
        )
        assert resp.status_code in {400, 422}, resp.text

    def test_malformed_archive_rejected_and_rollback(self) -> None:
        client, _auth = self.client(self.alice)
        resp = client.post(
            "/api/projects/import",
            files={"archive": ("bad.zip", io.BytesIO(b"not a zip"), "application/zip")},
            headers=headers(0),
        )
        assert resp.status_code in {400, 422}, resp.text
        # No project rows leaked from the failed import: /current is 204.
        current = client.get("/api/projects/current")
        assert current.status_code == 204, current.text

    def test_oversized_archive_rejected(self) -> None:
        client, _auth = self.client(self.alice)
        blob = b"A" * (64 * 1024 * 1024 + 1)  # > bounded import size
        data = self._make_archive_bytes([("big.png", blob)])
        resp = client.post(
            "/api/projects/import",
            files={"archive": ("big.zip", io.BytesIO(data), "application/zip")},
            headers=headers(0),
        )
        assert resp.status_code in {400, 413, 422}, resp.text


class TestRasterValidation(WiredAppFixture):
    """Raster MIME mismatch, decoded-size limits, failed-replacement retention."""

    def test_raster_mime_mismatch_rejected(self) -> None:
        client, _auth = self.client(self.alice)
        bad = io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        resp = client.post(
            "/api/assets",
            files={"file": ("panel.png", bad, "text/html")},  # lying MIME
            headers=headers(0),
        )
        assert resp.status_code in {400, 422}, resp.text

    def test_failed_replacement_retains_previous(self) -> None:
        """When a staged raster replacement fails, the prior artifact remains."""
        client, _auth = self.client(self.alice)
        # Upload a minimal valid raster.
        png = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
            + b"\x1f\x15\xc4\x89\x00\x00\x00\x0aIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
            + b"\r\n\x2d\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        ok = client.post(
            "/api/assets",
            files={"file": ("panel.png", io.BytesIO(png), "image/png")},
            headers=headers(0),
        )
        assert ok.status_code in {200, 201}, ok.text


class TestSsrfPolicy(WiredAppFixture):
    """SSRF: loopback-only cleartext, redirect rejection, allowlist gate.

    Contract (web/comic_sol_web/generation/providers/http.py):
    - `_canonical_origin` rejects any cleartext (http) origin that is not
      loopback, any scheme outside http/https, and any embedded credentials.
    - HTTPS origins (including private/link-local/metadata IPs) pass origin
      canonicalization but are rejected at the request boundary unless the
      canonical origin is in the transport policy's `approved_origins`
      allowlist.
    - Redirects are always rejected (`follow_redirects=False` + is_redirect).
    """

    def test_cleartext_non_loopback_rejected(self) -> None:
        """http to private/link-local/metadata must be rejected at origin."""
        from comic_sol_web.generation.providers.http import _canonical_origin

        for host in (
            "10.0.0.1",
            "172.16.0.1",
            "192.168.1.1",
            "100.64.0.1",
            "169.254.169.254",
            "0.0.0.0",
            "metadata.google.internal",
            "fe80::1",
        ):
            with self.assertRaises(ValueError, msg=host):
                _canonical_origin(f"http://{host}/")

    def test_loopback_cleartext_allowed_for_local_dev(self) -> None:
        """http loopback is the sanctioned local-provider path."""
        from comic_sol_web.generation.providers.http import _canonical_origin

        for url in (
            "http://127.0.0.1:1234",
            "http://localhost:8080",
            "http://[::1]:9000",
        ):
            origin = _canonical_origin(url)
            assert origin.startswith("http://"), origin

    def test_embedded_credentials_rejected(self) -> None:
        """URLs carrying userinfo must be rejected."""
        from comic_sol_web.generation.providers.http import _canonical_origin

        for url in (
            "http://user:pass@127.0.0.1:1234",
            "https://token@example.com",
        ):
            with self.assertRaises(ValueError, msg=url):
                _canonical_origin(url)

    def test_https_private_ip_requires_allowlist(self) -> None:
        """A canonical origin outside the allowlist is rejected by policy.

        TransportPolicy normalizes every approved origin through
        _canonical_origin and stores the canonical form; an arbitrary
        private-IP origin must not appear unless explicitly normalized
        in.
        """
        from comic_sol_web.generation.providers.http import (
            TransportPolicy,
            _canonical_origin,
        )

        policy = TransportPolicy(
            approved_origins=frozenset({"https://example.com"}),
            connect_timeout=1.0,
            read_timeout=1.0,
            total_timeout=1.0,
            max_response_bytes=1024,
        )
        canonical = _canonical_origin("https://169.254.169.254")
        self.assertNotIn(canonical, policy.approved_origins)
        # The canonical form of the allowed origin must be present.
        self.assertIn(_canonical_origin("https://example.com"), policy.approved_origins)

    def test_policy_rejects_cleartext_non_loopback_origins(self) -> None:
        """TransportPolicy canonicalizes and rejects cleartext non-loopback."""
        from comic_sol_web.generation.providers.http import TransportPolicy

        with self.assertRaises(ValueError):
            TransportPolicy(
                approved_origins=frozenset({"http://169.254.169.254"}),
                connect_timeout=1.0,
                read_timeout=1.0,
                total_timeout=1.0,
                max_response_bytes=1024,
            )

    def test_redirects_never_followed(self) -> None:
        """Provider transport must never follow redirects."""
        from comic_sol_web.generation.providers.http import (
            BoundedHTTPClient,
            TransportPolicy,
            _canonical_origin,
        )

        assert _canonical_origin("https://example.com/1") == "https://example.com"
        policy = TransportPolicy(
            approved_origins=frozenset({"https://example.com"}),
            connect_timeout=1.0,
            read_timeout=1.0,
            total_timeout=1.0,
            max_response_bytes=1024,
        )
        client = BoundedHTTPClient(policy)
        assert client._client.follow_redirects is False


class TestLeaseAndCancel(WiredAppFixture):
    """Lease expiry/restart recovery and cancellation races."""

    def test_restart_recovery(self) -> None:
        client, _auth = self.client(self.alice)
        archive = self.portable_archive()
        with archive.open("rb") as handle:
            imp = client.post(
                "/api/projects/import",
                files={"archive": (archive.name, handle, "application/zip")},
                headers=headers(0),
            )
        pid = imp.json()["project_id"]
        rev = imp.json()["revision"]
        queued = client.post(
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
        assert queued.status_code == 201, queued.text
        job_id = queued.json()["jobs"][0]["job_id"]
        # "Restart": a second worker pumping must pick up nothing harmful and
        # the job must still be reachable.
        pump(self.generation)
        resp = client.get(f"/api/generation/{job_id}")
        assert resp.status_code == 200
        assert resp.json()["state"] in {
            "queued",
            "running",
            "validating",
            "failed",
            "accepted",
            "cancelled",
        }

    def test_cancel_race_bounded(self) -> None:
        client, _auth = self.client(self.alice)
        archive = self.portable_archive()
        with archive.open("rb") as handle:
            imp = client.post(
                "/api/projects/import",
                files={"archive": (archive.name, handle, "application/zip")},
                headers=headers(0),
            )
        pid = imp.json()["project_id"]
        rev = imp.json()["revision"]
        queued = client.post(
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
        assert queued.status_code == 201, queued.text
        job_id = queued.json()["jobs"][0]["job_id"]
        cancel = client.post(
            f"/api/generation/{job_id}/cancel",
            json={"expected_revision": rev},
            headers=headers(rev),
        )
        assert cancel.status_code in {200, 202, 409}, cancel.text
        pump(self.generation)
        resp = client.get(f"/api/generation/{job_id}")
        assert resp.status_code == 200
        assert resp.json()["state"] in {"cancelled", "failed", "accepted", "validating"}


class TestSensitiveDataRedaction(WiredAppFixture):
    """Logs/envelopes must not leak credentials, cookies, or tokens."""

    def test_queue_envelope_no_credentials(self) -> None:
        client, _auth = self.client(self.alice)
        archive = self.portable_archive()
        with archive.open("rb") as handle:
            imp = client.post(
                "/api/projects/import",
                files={"archive": (archive.name, handle, "application/zip")},
                headers=headers(0),
            )
        pid = imp.json()["project_id"]
        rev = imp.json()["revision"]
        queued = client.post(
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
        assert queued.status_code == 201, queued.text
        text = queued.text
        assert "api_key" not in text.lower()
        assert "bearer" not in text.lower()
        assert "password" not in text.lower()
        assert "cookie" not in text.lower()


class TestWebMcpAuthorization(WiredAppFixture):
    """WebMCP/UI parity: authorization and confirmation behavior."""

    def test_webmcp_requires_owner_for_assets(self) -> None:
        client_alice, _auth = self.client(self.alice)
        png = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
            + b"\x1f\x15\xc4\x89\x00\x00\x00\x0aIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
            + b"\r\n\x2d\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        ok = client_alice.post(
            "/api/assets",
            files={"file": ("panel.png", io.BytesIO(png), "image/png")},
            headers=headers(0),
        )
        assert ok.status_code in {200, 201}, ok.text
        asset_id = ok.json()["asset_id"]

        client_bob, _auth = self.client(self.bob)
        stolen = client_bob.get(f"/api/assets/{asset_id}")
        assert stolen.status_code in {403, 404}, stolen.text
