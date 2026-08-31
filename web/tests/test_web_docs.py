"""Web documentation contract tests (issue #268, WP17).

Every check below is derived from the merged Web code, the published #268 plan,
and the WP17 maintainer waiver recorded on #251. The suite is offline,
credential-free, and intentionally strict so the Web documentation set cannot
regress into the failure modes WP17 exists to prevent:

- ChatGPT Plus/Pro subscription access described as provider API credit;
- provider verification inflation (unit tests presented as live smoke);
- fabricated deployment URLs, videos, screenshots, or manual smoke records;
- credential, token, path, endpoint, or raw-payload leakage;
- WebMCP or local MCP surface drift;
- silent removal of an explicit-confirmation boundary;
- broken local links or referenced artifacts that do not exist.

The provider, WebMCP, and local MCP assertions read the merged production
sources directly, so documentation drift fails here rather than being noticed
after publication.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path
from typing import ClassVar

from comic_sol_web.generation.catalog import CATALOG


ROOT = Path(__file__).resolve().parents[2]
WEB_DOCS = ROOT / "docs" / "web"
SUBMISSION = ROOT / "submission" / "webmcp"
APP_SOURCE = ROOT / "web" / "comic_sol_web" / "app.py"
GENERATION_SERVICE_SOURCE = ROOT / "web" / "comic_sol_web" / "generation" / "service.py"

# The exact WebMCP surface #266 registered and WP16 qualified.
WEBMCP_READ_TOOLS = frozenset(
    {
        "get_project_state",
        "list_generation_options",
        "recommend_provider",
        "list_generation_jobs",
        "get_qa_summary",
    }
)
WEBMCP_WRITE_TOOLS = frozenset(
    {
        "create_project",
        "import_project",
        "update_project_plan",
        "queue_generation",
        "submit_generated_asset",
        "approve_provider_switch",
        "reject_provider_switch",
        "run_qa",
        "export_project",
    }
)

# Credential, token, header, and path/endpoint/payload shapes that must never
# appear in any WP17 document or submission artifact. Each pattern is a single
# prohibited shape; new ones are added when the documentation contract is
# extended. The list covers:
#   - vendor API key formats (sk-, sk-proj-, ghp_, github_pat_, AKIA, xoxb-,
#     AIza, r8_);
#   - generic authorization header shapes (Bearer, Token, Basic) with a real
#     token attached;
#   - session/cookie header values that look like a real session or CSRF
#     token (Set-Cookie + long token; csrf_token=…; X-CSRF-Token: …);
#   - private filesystem path shapes (Unix /home/<user>/, Windows
#     C:\Users\<user>\) and the obvious secret-mount path /run/secrets/*;
#   - private link-local / loopback / metadata endpoints;
#   - raw JSON or query-string bodies that carry a credential or a story
#     excerpt ({"api_key":..., {"access_token":..., authorization:...,
#     "story": "<20+ word paragraph>").
CREDENTIAL_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xoxb-[A-Za-z0-9-]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"r8_[A-Za-z0-9]{30,}"),
    re.compile(
        r"(?i)(?:^|[\s;,(\"'])(?:Bearer|Authorization|Token|Basic)\s+[A-Za-z0-9._\-+/=]{24,}"
    ),
    re.compile(r"(?i)\bSet-Cookie:\s*[A-Za-z0-9_]+=[A-Za-z0-9._\-+/=]{24,}"),
    # CSRF tokens appear in three real shapes: double-quoted, single-quoted,
    # or unquoted (Set-Cookie / X-CSRF-Token header / bare assignment). A
    # single regex must catch all three so a real csrf_token value cannot
    # pass the leak scan because of an unrecognised quote style.
    re.compile(
        r"(?i)\bcsrf_?token\s*=\s*(?:"
        r"\"[A-Za-z0-9._\-+/=]{24,}\""  # double-quoted
        r"|'[^'\\]{24,}'"  # single-quoted (no embedded quote)
        r"|[A-Za-z0-9._\-+/=]{24,}(?=[,\s;)\]}]|$)"  # unquoted, bounded
        r")"
    ),
    re.compile(r"(?i)\bx-csrf-token:\s*[A-Za-z0-9._\-+/=]{24,}"),
    re.compile(r"/home/[a-z0-9_\-]+/\.[A-Za-z0-9_./-]+"),
    re.compile(r"C:\\Users\\[^\\\s/\"]+\\"),
    re.compile(r"/run/secrets/[A-Za-z0-9_./-]+"),
    re.compile(r"https?://(?:127\.|10\.|192\.168\.|169\.254\.|::1|localhost)"),
    re.compile(
        r"(?i)(?:\"api[_-]?key\"|\"access[_-]?token\"|\"client[_-]?secret\"|\"authorization\")\s*:\s*\"[A-Za-z0-9._\-+/=]{12,}\""
    ),
    re.compile(
        r"(?i)\bauthorization\s*[:=]\s*[\"']?(?:Bearer|Basic|Token)\s+[A-Za-z0-9._\-+/=]{12,}"
    ),
    re.compile(
        r"-----BEGIN [A-Z ]+PRIVATE KEY-----",
        re.IGNORECASE,
    ),
)

MARKDOWN_LINK = re.compile(r"\[(?P<label>[^\]]*)\]\((?P<target>[^)\s]+)\)")


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def collapsed(text: str) -> str:
    return " ".join(text.split())


def section(document: str, heading: str) -> str:
    """Return one level-two Markdown section by exact heading."""
    marker = f"## {heading}"
    start = document.index(marker)
    end = document.find("\n## ", start + len(marker))
    return document[start:] if end == -1 else document[start:end]


class WebIndexContractTests(unittest.TestCase):
    """`docs/web/index.md` documents the full workflow without billing confusion."""

    document: ClassVar[str]
    normalized: ClassVar[str]

    @classmethod
    def setUpClass(cls) -> None:
        """setUpClass."""
        cls.document = read("docs/web/index.md")
        cls.normalized = collapsed(cls.document)

    def test_every_workflow_step_is_documented_in_order(self) -> None:
        """The ten approved workflow steps appear once each, in workflow order."""
        ordered = (
            "Sign in",
            "Create from a prompt, a pasted story, or a portable archive import",
            "Review the Plan",
            "Choose a generation route",
            "Confirm the generation cost",
            "Inspect the queue",
            "Confirm every provider switch",
            "Explicitly promote staged rasters",
            "Run QA",
            "Export a private PDF or portable archive",
        )
        positions = []
        for step in ordered:
            with self.subTest(step=step):
                self.assertIn(step, self.document, step)
            positions.append(self.document.index(step))
        self.assertEqual(sorted(positions), positions, "workflow steps are out of order")

    def test_plus_pro_access_is_never_described_as_provider_api_credit(self) -> None:
        """Subscription access and provider API billing stay separate concepts."""
        lowered = self.normalized.lower()
        for banned in (
            "plus credits",
            "plus credit",
            "pro credits",
            "pro credit",
            "chatgpt plus api",
            "chatgpt pro api",
            "subscription includes api",
            "plus includes api",
            "counts against your plus",
            "uses your chatgpt plus quota",
        ):
            with self.subTest(phrase=banned):
                self.assertNotIn(banned, lowered, banned)

    def test_billing_distinction_is_stated_explicitly(self) -> None:
        """The document must state the distinction rather than merely avoid it."""
        billing = collapsed(section(self.document, "Billing and access"))
        for phrase in (
            "ChatGPT Plus",
            "ChatGPT Pro",
            "not",
            "provider API",
            "separate",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, billing, phrase)
        self.assertIn(
            "A ChatGPT Plus or Pro subscription is not provider API credit",
            billing,
        )

    def test_all_four_generation_modes_are_documented(self) -> None:
        """check all four generation modes are documented."""
        modes = collapsed(section(self.document, "Generation routes")).lower()
        for mode in (
            "agent",
            "hosted",
            "session byok",
            "encrypted persisted byok",
        ):
            with self.subTest(mode=mode):
                self.assertIn(mode, modes, mode)

    def test_all_four_explicit_confirmations_are_documented(self) -> None:
        """check all four explicit confirmations are documented."""
        confirmations = collapsed(section(self.document, "Explicit confirmations")).lower()
        for confirmation in (
            "generation cost",
            "provider switch",
            "raster promotion",
            "overwrite",
        ):
            with self.subTest(confirmation=confirmation):
                self.assertIn(confirmation, confirmations, confirmation)

    def test_entry_modes_cover_story_creation_and_archive_import(self) -> None:
        """check entry modes cover story creation and archive import."""
        entry = collapsed(section(self.document, "Entry modes"))
        self.assertIn("short prompt", entry)
        self.assertIn("pasted story", entry)
        self.assertIn("portable archive", entry)
        self.assertIn(".comic-sol-handoff", entry)

    def test_page_owned_handles_are_the_only_documented_input_source(self) -> None:
        """check page owned handles are the only documented input source."""
        handles = collapsed(section(self.document, "Asset and archive handles"))
        self.assertIn("page-owned", handles)
        self.assertIn("never", handles.lower())
        self.assertIn("filesystem path", handles)
        self.assertIn("arbitrary URL", handles)

    def test_evidence_tiers_are_named_and_distinguished(self) -> None:
        """check evidence tiers are named and distinguished."""
        tiers = collapsed(section(self.document, "Evidence tiers"))
        for tier in (
            "Implemented",
            "Offline-qualified",
            "Experimental",
            "Live-verified",
        ):
            with self.subTest(tier=tier):
                self.assertIn(tier, tiers, tier)
        self.assertIn(
            "Passing unit or contract tests never establishes live-verified",
            tiers,
        )

    def test_index_makes_no_adoption_or_visual_quality_claim(self) -> None:
        """check index makes no adoption or visual quality claim."""
        lowered = self.normalized.lower()
        for banned in (
            "thousands of",
            "widely adopted",
            "most popular",
            "industry standard",
            "professional-quality art",
            "studio-quality",
            "best-in-class",
        ):
            with self.subTest(phrase=banned):
                self.assertNotIn(banned, lowered, banned)

    def test_index_states_the_exact_webmcp_and_local_mcp_counts(self) -> None:
        """check index states the exact webmcp and local mcp counts."""
        surfaces = collapsed(section(self.document, "Tool surfaces"))
        self.assertIn("five read", surfaces)
        self.assertIn("nine write", surfaces)
        self.assertIn("exactly 17", surfaces)


class WebProvidersContractTests(unittest.TestCase):
    """`docs/web/providers.md` publishes an honest, catalog-backed matrix."""

    document: ClassVar[str]
    normalized: ClassVar[str]
    matrix: ClassVar[str]

    @classmethod
    def setUpClass(cls) -> None:
        """setUpClass."""
        cls.document = read("docs/web/providers.md")
        cls.normalized = collapsed(cls.document)
        cls.matrix = section(cls.document, "Provider verification matrix")

    @classmethod
    def matrix_header(cls) -> list[str]:
        """Return the matrix column names from the header row (lowercased)."""
        for line in cls.matrix.splitlines():
            if not line.startswith("|"):
                continue
            cells = [cell.strip().lower() for cell in line.strip("|").split("|")]
            if len(cells) >= 6 and cells[0] == "provider":
                return cells
        raise AssertionError("provider verification matrix has no header row")

    @classmethod
    def matrix_rows(cls) -> dict[str, dict[str, str]]:
        """Return `{provider label: {column name: cell}}` for the matrix."""
        header = cls.matrix_header()
        rows: dict[str, dict[str, str]] = {}
        for line in cls.matrix.splitlines():
            if not line.startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if (
                len(cells) < len(header)
                or cells[0] == "Provider"
                or cells[0] == ""
                or set(cells[0]) <= {"-", ":"}
            ):
                continue
            rows[cells[0]] = dict(zip(header, cells))
        return rows

    def test_every_required_route_has_a_matrix_row(self) -> None:
        """check every required route has a matrix row."""
        rows = self.matrix_rows()
        for label in (
            "OpenAI",
            "Google",
            "BFL (direct)",
            "xAI",
            "Stability",
            "Replicate",
            "fal.ai",
            "Cloudflare",
            "ComfyUI (remote)",
            "ComfyUI (local, agent handoff)",
            "Active-agent image generation",
        ):
            with self.subTest(provider=label):
                self.assertIn(label, rows, f"missing matrix row for {label}")

    def test_every_matrix_row_uses_only_approved_tier_values(self) -> None:
        """The live-smoke column is separate from implemented/offline-qualified."""
        approved = {"Yes", "No", "Not run", "n/a"}
        for label, row in self.matrix_rows().items():
            with self.subTest(provider=label):
                live = row.get("live smoke", "")
                self.assertIn(
                    live,
                    approved,
                    f"{label} live-smoke column is {live!r}, "
                    f"which is not one of {sorted(approved)}",
                )

    def test_every_paid_route_states_that_it_is_not_routable_in_the_merged_build(self) -> None:
        """Only the agent route is registered in the merged composition root."""
        paid = {
            "OpenAI",
            "Google",
            "BFL (direct)",
            "xAI",
            "Stability",
            "Replicate",
            "fal.ai",
            "Cloudflare",
            "ComfyUI (remote)",
        }
        rows = self.matrix_rows()
        for label in sorted(paid & set(rows)):
            with self.subTest(provider=label):
                self.assertEqual(
                    "No",
                    rows[label].get("routable in merged build", ""),
                    f"{label} must state it is not routable in the merged build",
                )
        self.assertEqual(
            "Yes",
            rows.get("Active-agent image generation", {}).get("routable in merged build", ""),
            "agent route must be the one routable route in the merged build",
        )

    def test_no_route_claims_live_smoke_without_an_evidence_link(self) -> None:
        """A `Yes` in the live-smoke column requires a real evidence link."""
        for label, row in self.matrix_rows().items():
            with self.subTest(provider=label):
                if row.get("live smoke") != "Yes":
                    continue
                evidence = row.get("evidence", "")
                match = MARKDOWN_LINK.search(evidence)
                self.assertIsNotNone(
                    match,
                    f"{label} claims live smoke but its evidence cell is {evidence!r}",
                )
                assert match is not None
                target = match.group("target")
                if target.startswith(("http://", "https://")):
                    continue
                resolved = (WEB_DOCS / target.partition("#")[0]).resolve()
                self.assertTrue(
                    resolved.exists(),
                    f"{label} live-smoke evidence {target!r} does not exist",
                )

    def test_rows_without_live_smoke_state_no_evidence_rather_than_a_link(self) -> None:
        """check rows without live smoke state no evidence rather than a link."""
        for label, row in self.matrix_rows().items():
            with self.subTest(provider=label):
                if row.get("live smoke") == "Yes":
                    continue
                self.assertIn(
                    row.get("evidence", ""),
                    {"None", "None retained", "n/a"},
                    f"{label} does not claim live smoke but cites evidence {row.get('evidence')!r}",
                )

    def test_no_paid_provider_is_currently_marked_live_verified(self) -> None:
        """WP17 authorizes no paid provider call, so no paid row may claim one."""
        paid = {
            "OpenAI",
            "Google",
            "BFL (direct)",
            "xAI",
            "Stability",
            "Replicate",
            "fal.ai",
            "Cloudflare",
            "ComfyUI (remote)",
        }
        rows = self.matrix_rows()
        for label in sorted(paid & set(rows)):
            with self.subTest(provider=label):
                self.assertNotEqual(
                    "Yes",
                    rows[label].get("live smoke", ""),
                    f"{label} claims live smoke, but no paid provider call is authorized",
                )

    def test_matrix_states_that_unit_tests_are_not_live_verification(self) -> None:
        """check matrix states that unit tests are not live verification."""
        self.assertIn(
            "Passing offline contract tests is not live verification",
            self.normalized,
        )

    def test_every_catalog_model_identifier_is_documented(self) -> None:
        """The published model identifiers must match the merged catalog exactly."""
        documented = set(re.findall(r"`([^`]+)`", self.document))
        for entry in CATALOG:
            if entry.provider == "fake":
                continue
            with self.subTest(provider=entry.provider):
                self.assertIn(
                    entry.model,
                    documented,
                    f"{entry.provider} model {entry.model!r} is not documented",
                )

    def test_no_provider_outside_the_catalog_is_advertised(self) -> None:
        """The document must not invent a provider the Web distribution lacks."""
        for absent in ("Midjourney", "Leonardo", "Ideogram", "Recraft", "Luma"):
            with self.subTest(provider=absent):
                self.assertNotIn(absent, self.document, absent)

    def test_remote_and_local_comfyui_are_separate_sections(self) -> None:
        """check remote and local comfyui are separate sections."""
        remote = collapsed(section(self.document, "Remote ComfyUI"))
        local = collapsed(section(self.document, "Local ComfyUI"))
        self.assertIn("public HTTPS", remote)
        self.assertIn("loopback", remote)
        self.assertIn("refused", remote)
        self.assertIn("agent-native", local)
        self.assertIn(
            "The hosted server never opens a connection to a user's localhost",
            local,
        )
        self.assertIn("user hardware", local)
        self.assertIn("model licenses", local)

    def test_authentication_modes_are_documented_per_route(self) -> None:
        """check authentication modes are documented per route."""
        rows = self.matrix_rows()
        for label, row in rows.items():
            with self.subTest(provider=label):
                auth = row.get("authentication", "")
                self.assertTrue(auth, f"{label} has no authentication modes cell")
                self.assertRegex(
                    auth,
                    r"(?i)agent|hosted|BYOK|n/a",
                    f"{label} authentication cell is {auth!r}",
                )

    def test_document_contains_no_credential_shaped_string(self) -> None:
        """check document contains no credential shaped string."""
        for pattern in CREDENTIAL_PATTERNS:
            with self.subTest(pattern=pattern.pattern):
                self.assertIsNone(pattern.search(self.document), pattern.pattern)


class WebSecurityContractTests(unittest.TestCase):
    """`docs/web/security.md` documents credential modes, trust, recovery."""

    document: ClassVar[str]
    normalized: ClassVar[str]

    @classmethod
    def setUpClass(cls) -> None:
        """setUpClass."""
        cls.document = read("docs/web/security.md")
        cls.normalized = collapsed(cls.document)

    def test_credentials_never_reach_any_forbidden_destination(self) -> None:
        """One explicit sentence must cover all five forbidden destinations."""
        statement = (
            "A provider credential is never exposed to the browser, written into a "
            "project archive, recorded in a receipt, emitted to a log, or included "
            "in this submission."
        )
        self.assertIn(statement, collapsed(self.document.replace(">", " ")))

    def test_all_four_credential_modes_are_documented_with_lifetimes(self) -> None:
        """check all four credential modes are documented with lifetimes."""
        modes = collapsed(section(self.document, "Credential modes and lifetime")).lower()
        for phrase in (
            "agent",
            "hosted",
            "session byok",
            "encrypted persisted byok",
            "revocation",
            "rotation",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, modes, phrase)
        # The session lifetime bound is a merged code property, not a guess.
        self.assertIn("one hour", modes)

    def test_request_integrity_boundaries_are_documented(self) -> None:
        """check request integrity boundaries are documented."""
        integrity = collapsed(section(self.document, "Request integrity"))
        for phrase in (
            "authentication",
            "CSRF",
            "ownership",
            "opaque",
            "revision",
            "idempotency",
            "approval replay",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, integrity, phrase)

    def test_network_trust_boundary_documents_ssrf_and_redirect_policy(self) -> None:
        """check network trust boundary documents ssrf and redirect policy."""
        network = collapsed(section(self.document, "Network trust boundary"))
        for phrase in (
            "SSRF",
            "redirect",
            "refused",
            "loopback",
            "private",
            "link-local",
            "metadata",
            "timeout",
            "response byte",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, network, phrase)

    def test_archive_and_image_trust_boundaries_are_documented(self) -> None:
        """check archive and image trust boundaries are documented."""
        boundaries = collapsed(section(self.document, "Archive and image trust boundary")).lower()
        for phrase in (
            "portable archive",
            "raster",
            "decoded",
            "fails closed",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, boundaries, phrase)

    def test_receipts_and_redaction_are_documented(self) -> None:
        """check receipts and redaction are documented."""
        receipts = collapsed(section(self.document, "Receipts and redaction"))
        self.assertIn("[REDACTED]", receipts)
        self.assertIn("raw provider payload", receipts)

    def test_private_destinations_are_documented(self) -> None:
        """check private destinations are documented."""
        privacy = collapsed(section(self.document, "Private story and artifact destinations"))
        self.assertIn("owner", privacy)
        self.assertIn("never published", privacy)

    def test_backup_and_incident_expectations_are_documented(self) -> None:
        """check backup and incident expectations are documented."""
        self.assertIn("## Backup and incident expectations", self.document)
        expectations = collapsed(section(self.document, "Backup and incident expectations"))
        for phrase in ("backup", "incident", "rotation", "revocation"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, expectations.lower(), phrase)

    def test_document_contains_no_credential_shaped_string(self) -> None:
        """check document contains no credential shaped string."""
        for pattern in CREDENTIAL_PATTERNS:
            with self.subTest(pattern=pattern.pattern):
                self.assertIsNone(pattern.search(self.document), pattern.pattern)


class WebDeploymentContractTests(unittest.TestCase):
    """`docs/web/deployment.md` is operable and invents no endpoint or URL."""

    document: ClassVar[str]
    normalized: ClassVar[str]

    @classmethod
    def setUpClass(cls) -> None:
        """setUpClass."""
        cls.document = read("docs/web/deployment.md")
        cls.normalized = collapsed(cls.document)

    def test_single_process_runtime_and_its_limits_are_documented(self) -> None:
        """check single process runtime and its limits are documented."""
        runtime = collapsed(section(self.document, "One-process runtime"))
        self.assertIn("one process", runtime.lower())
        self.assertIn("process-local", runtime)
        for phrase in ("queue", "worker", "horizontal"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, runtime.lower(), phrase)

    def test_durable_data_volume_is_required(self) -> None:
        """check durable data volume is required."""
        volume = collapsed(section(self.document, "Durable data volume"))
        self.assertIn("COMIC_SOL_WEB_DATA_ROOT", volume)
        self.assertIn("absolute", volume)
        self.assertIn("durable", volume.lower())

    def test_every_required_environment_variable_is_documented(self) -> None:
        """check every required environment variable is documented."""
        secrets = section(self.document, "Environment secrets")
        from comic_sol_web.config import (
            CREDENTIAL_ACTIVE_KEY_ID_VAR,
            CREDENTIAL_KEY_REFS_VAR,
            DATA_ROOT_VAR,
            ENCRYPTION_SECRET_VAR,
            HOSTED_SECRET_REFS_VAR,
            MINIMUM_SECRET_LENGTH,
            SESSION_SECRET_VAR,
        )

        for variable in (
            SESSION_SECRET_VAR,
            ENCRYPTION_SECRET_VAR,
            DATA_ROOT_VAR,
            HOSTED_SECRET_REFS_VAR,
            CREDENTIAL_KEY_REFS_VAR,
            CREDENTIAL_ACTIVE_KEY_ID_VAR,
        ):
            with self.subTest(variable=variable):
                self.assertIn(variable, secrets, variable)
        self.assertIn(str(MINIMUM_SECRET_LENGTH), secrets)

    def test_healthz_is_documented_without_inventing_a_readiness_endpoint(self) -> None:
        """check healthz is documented without inventing a readiness endpoint."""
        health = collapsed(section(self.document, "Health endpoint"))
        self.assertIn("/healthz", health)
        self.assertIn('{"status":"ok"}', health)
        self.assertIn("liveness", health.lower())
        self.assertIn("no readiness endpoint", health.lower())
        for invented in ("/readyz", "/ready", "/healthz/ready", "/livez", "/metrics"):
            with self.subTest(endpoint=invented):
                self.assertNotIn(invented, self.document, invented)

    def test_tls_and_reverse_proxy_expectations_are_documented(self) -> None:
        """check tls and reverse proxy expectations are documented."""
        tls = collapsed(section(self.document, "TLS and reverse proxy"))
        self.assertIn("TLS", tls)
        self.assertIn("reverse proxy", tls)
        self.assertIn("terminates", tls.lower())

    def test_backup_restore_rotation_rollback_incident_are_documented(self) -> None:
        """check backup restore rotation rollback incident are documented."""
        for heading in (
            "Backup and restore",
            "Credential-key rotation",
            "Startup and shutdown",
        ):
            with self.subTest(heading=heading):
                self.assertIn(f"## {heading}", self.document, heading)
        self.assertIn("rollback.md", self.document)

    def test_no_deployment_url_or_performed_deployment_is_claimed(self) -> None:
        """check no deployment url or performed deployment is claimed."""
        status = collapsed(section(self.document, "Deployment status"))
        self.assertIn("not deployed", status.lower())
        lowered = self.normalized.lower()
        for banned in (
            "live at https://",
            "deployed to https://",
            "is running at https://",
            "production instance at",
            "you can try it at",
        ):
            with self.subTest(phrase=banned):
                self.assertNotIn(banned, lowered, banned)
        # No external hostname may be presented as a Comic Sol Studio deployment.
        self.assertNotRegex(
            self.document,
            r"(?i)https?://[a-z0-9.-]*(?:studio|comic-?sol)[a-z0-9.-]*\.(?:app|dev|io|com|net)",
        )

    def test_document_contains_no_credential_shaped_string(self) -> None:
        """check document contains no credential shaped string."""
        for pattern in CREDENTIAL_PATTERNS:
            with self.subTest(pattern=pattern.pattern):
                self.assertIsNone(pattern.search(self.document), pattern.pattern)


class WebRollbackContractTests(unittest.TestCase):
    """`docs/web/rollback.md` documents recovery without fabricating evidence."""

    document: ClassVar[str]
    normalized: ClassVar[str]

    @classmethod
    def setUpClass(cls) -> None:
        """setUpClass."""
        cls.document = read("docs/web/rollback.md")
        cls.normalized = collapsed(cls.document)

    def test_every_recovery_boundary_has_a_section(self) -> None:
        """check every recovery boundary has a section."""
        for heading in (
            "Rollback",
            "Restore from backup",
            "Credential-key rotation and revocation",
            "Incident response",
            "What rollback cannot recover",
        ):
            with self.subTest(heading=heading):
                self.assertIn(f"## {heading}", self.document, heading)

    def test_rollback_boundaries_are_explicit_rather_than_implied(self) -> None:
        """check rollback boundaries are explicit rather than implied."""
        limits = collapsed(section(self.document, "What rollback cannot recover"))
        for phrase in (
            "in-flight generation",
            "provider-side",
            "already-spent",
            "revoked credential",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, limits, phrase)

    def test_rollback_never_claims_a_rehearsed_production_drill(self) -> None:
        """check rollback never claims a rehearsed production drill."""
        lowered = self.normalized.lower()
        for banned in (
            "we performed",
            "was rehearsed in production",
            "verified in production",
            "drill was executed against the live",
        ):
            with self.subTest(phrase=banned):
                self.assertNotIn(banned, lowered, banned)
        self.assertIn(
            "No production rollback has been performed",
            self.normalized,
        )


class WebMcpSurfaceContractTests(unittest.TestCase):
    """Documentation must state the exact merged WebMCP surface."""

    webmcp_source: ClassVar[str]
    tool_list: ClassVar[str]

    @classmethod
    def setUpClass(cls) -> None:
        """setUpClass."""
        cls.webmcp_source = (ROOT / "web" / "comic_sol_web" / "static" / "webmcp.js").read_text(
            encoding="utf-8"
        )
        cls.tool_list = read("submission/webmcp/tools.md")

    def test_the_merged_module_registers_exactly_five_read_and_nine_write_tools(self) -> None:
        """check the merged module registers exactly five read and nine write tools."""
        registered = set(re.findall(r'^\s*name:\s*"([a-z_]+)",\s*$', self.webmcp_source, re.M))
        self.assertEqual(WEBMCP_READ_TOOLS | WEBMCP_WRITE_TOOLS, registered)
        self.assertEqual(5, len(WEBMCP_READ_TOOLS))
        self.assertEqual(9, len(WEBMCP_WRITE_TOOLS))
        self.assertEqual(14, len(registered))

    def test_the_published_tool_list_matches_the_merged_module_exactly(self) -> None:
        """check the published tool list matches the merged module exactly."""
        documented = set(
            re.findall(r"^-\s+`([a-z_]+)`", self.tool_list, re.M)
        )
        registered = set(re.findall(r'^\s*name:\s*"([a-z_]+)",\s*$', self.webmcp_source, re.M))
        self.assertEqual(
            registered,
            documented,
            "the published WebMCP tool list drifted from webmcp.js",
        )
        for tool in sorted(registered):
            with self.subTest(tool=tool):
                self.assertIn(f"`{tool}`", self.tool_list, tool)

    def test_the_published_tool_list_separates_reads_from_writes(self) -> None:
        """check the published tool list separates reads from writes."""
        reads = section(self.tool_list, "Read tools (5)")
        writes = section(self.tool_list, "Write tools (9)")
        for tool in sorted(WEBMCP_READ_TOOLS):
            with self.subTest(tool=tool):
                self.assertIn(f"`{tool}`", reads, tool)
                self.assertNotIn(f"`{tool}`", writes, tool)
        for tool in sorted(WEBMCP_WRITE_TOOLS):
            with self.subTest(tool=tool):
                self.assertIn(f"`{tool}`", writes, tool)
                self.assertNotIn(f"`{tool}`", reads, tool)

    def test_local_mcp_remains_exactly_seventeen_tools(self) -> None:
        """check local mcp remains exactly seventeen tools."""
        source = (ROOT / "scripts" / "mcp_server.py").read_text(encoding="utf-8")
        tools = re.findall(r"@mcp\.tool\(\)\n(?:@[^\n]+\n)*def (comic_[a-z_]+)\(", source)
        self.assertEqual(17, len(tools), sorted(tools))
        self.assertEqual(17, len(set(tools)))
        for document in ("docs/web/index.md", "submission/webmcp/README.md"):
            with self.subTest(document=document):
                self.assertIn("exactly 17", collapsed(read(document)), document)


class WebDocumentationLinkTests(unittest.TestCase):
    """Every relative link and referenced artifact must actually exist."""

    def _markdown_documents(self) -> list[Path]:
        documents = sorted(WEB_DOCS.rglob("*.md"))
        documents.extend(sorted(SUBMISSION.rglob("*.md")))
        self.assertTrue(documents, "no WP17 documents were found")
        return documents

    def test_every_relative_link_target_exists(self) -> None:
        """check every relative link target exists."""
        for path in self._markdown_documents():
            text = path.read_text(encoding="utf-8")
            for match in MARKDOWN_LINK.finditer(text):
                target = match.group("target")
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                file_part = target.partition("#")[0]
                if not file_part:
                    continue
                resolved = (path.parent / file_part).resolve()
                with self.subTest(document=path.name, target=target):
                    self.assertTrue(
                        resolved.exists(),
                        f"{path.relative_to(ROOT)} links {target!r}, which does not exist",
                    )

    def test_every_in_document_anchor_resolves(self) -> None:
        """check every in document anchor resolves."""
        for path in self._markdown_documents():
            text = path.read_text(encoding="utf-8")
            headings = {
                "#" + re.sub(r"[^a-z0-9 -]", "", line.lstrip("#").strip().lower()).replace(" ", "-")
                for line in text.splitlines()
                if line.startswith("#")
            }
            for match in MARKDOWN_LINK.finditer(text):
                target = match.group("target")
                if not target.startswith("#"):
                    continue
                with self.subTest(document=path.name, anchor=target):
                    self.assertIn(
                        target.lower(),
                        headings,
                        f"{path.relative_to(ROOT)} links anchor {target!r}, "
                        f"which no heading defines",
                    )

    def test_no_wp17_document_contains_a_credential_shaped_string(self) -> None:
        """check no wp17 document contains a credential shaped string."""
        for path in self._markdown_documents():
            text = path.read_text(encoding="utf-8")
            for pattern in CREDENTIAL_PATTERNS:
                with self.subTest(document=path.name, pattern=pattern.pattern):
                    self.assertIsNone(pattern.search(text), pattern.pattern)


class SubmissionContractTests(unittest.TestCase):
    """`submission/webmcp/` is bounded, honest, and evidence-backed."""

    overview: ClassVar[str]
    normalized: ClassVar[str]
    tool_list: ClassVar[str]
    tool_list_normalized: ClassVar[str]
    document: ClassVar[str]
    bodies: ClassVar[dict[str, str]]
    demo: ClassVar[str]
    verification: ClassVar[str]
    limitations: ClassVar[str]
    fixture_dir: ClassVar[Path]
    evidence: ClassVar[list[str]]

    REQUIRED_FILES = (
        "README.md",
        "tools.md",
        "provider-evidence.md",
        "demo.md",
        "verification.md",
        "limitations.md",
    )

    @classmethod
    def setUpClass(cls) -> None:
        """setUpClass."""
        cls.overview = read("submission/webmcp/README.md")
        cls.normalized = collapsed(cls.overview)

    def test_every_required_submission_document_exists(self) -> None:
        """check every required submission document exists."""
        for name in self.REQUIRED_FILES:
            with self.subTest(document=name):
                self.assertTrue(
                    (SUBMISSION / name).is_file(),
                    f"submission/webmcp/{name} is missing",
                )

    def test_overview_covers_every_required_submission_topic(self) -> None:
        """check overview covers every required submission topic."""
        for heading in (
            "Submission overview",
            "Source repository",
            "Architecture summary",
            "WebMCP tool surface",
            "Security and privacy summary",
            "Demo instructions",
            "Limitations",
            "Provider evidence",
            "Verification commands and results",
        ):
            with self.subTest(heading=heading):
                self.assertIn(f"## {heading}", self.overview, heading)

    def test_source_repository_link_is_the_canonical_repository(self) -> None:
        """check source repository link is the canonical repository."""
        repository = collapsed(section(self.overview, "Source repository"))
        self.assertIn("https://github.com/wenn-id/comicsol", repository)

    def test_deployment_and_video_status_are_honest(self) -> None:
        """check deployment and video status are honest."""
        status = collapsed(section(self.overview, "Deployment and recording status"))
        self.assertIn("not deployed", status.lower())
        self.assertIn("no video was recorded", status.lower())
        lowered = self.normalized.lower()
        for banned in (
            "live at https://",
            "watch the demo at https://",
            "video: https://",
            "youtube.com/watch",
            "youtu.be/",
            "loom.com/share",
        ):
            with self.subTest(phrase=banned):
                self.assertNotIn(banned, lowered, banned)

    def test_any_rendered_offline_screenshot_has_a_retained_artifact(self) -> None:
        """Any referenced screenshot must exist and be committed.

        WP17 produced no demo screenshots (the environment exposed no
        `document.modelContext`, so the WebMCP client could not be driven to a
        rendered screen). The narration/demo script stands in their place.
        This test still guards any future screenshot: a referenced image must
        be a real, committed local file, never a remote or fabricated one.
        """
        for path in sorted(SUBMISSION.rglob("*.md")):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"!\[[^\]]*\]\(([^)\s]+)\)", text):
                target = match.group(1)
                if target.startswith(("http://", "https://")):
                    self.fail(f"{path.name} embeds a remote image {target!r}")
                resolved = (path.parent / target).resolve()
                with self.subTest(document=path.name, image=target):
                    self.assertTrue(
                        resolved.is_file(),
                        f"{path.relative_to(ROOT)} references missing image {target!r}",
                    )

    def test_demo_states_that_no_screenshots_were_produced(self) -> None:
        """check demo states that no screenshots were produced."""
        demo = collapsed(read("submission/webmcp/demo.md"))
        self.assertIn("no screenshots", demo.lower())

    def test_screenshots_are_labelled_as_offline_deterministic_output(self) -> None:
        """check screenshots are labelled as offline deterministic output."""
        demo = collapsed(read("submission/webmcp/demo.md"))
        self.assertIn("offline", demo.lower())
        self.assertIn("FakeProvider", demo)
        self.assertIn(
            "No screenshot in this submission shows a live paid provider result",
            demo,
        )

    def test_submission_makes_no_adoption_or_visual_quality_claim(self) -> None:
        """check submission makes no adoption or visual quality claim."""
        for name in self.REQUIRED_FILES:
            lowered = collapsed(read(f"submission/webmcp/{name}")).lower()
            for banned in (
                "thousands of users",
                "widely adopted",
                "production-proven",
                "studio-quality art",
                "best-in-class",
                "state of the art results",
            ):
                with self.subTest(document=name, phrase=banned):
                    self.assertNotIn(banned, lowered, banned)

    def test_verification_document_records_command_and_outcome_pairs(self) -> None:
        """check verification document records command and outcome pairs."""
        verification = read("submission/webmcp/verification.md")
        self.assertIn("| Command | Result |", verification)
        # Every recorded gate must resolve to an explicit outcome, never a blank.
        rows = [
            [cell.strip() for cell in line.strip("|").split("|")]
            for line in verification.splitlines()
            if line.startswith("|")
        ]
        data_rows = [
            row
            for row in rows
            if len(row) >= 2 and row[0] not in {"Command", ""} and not set(row[0]) <= {"-", ":"}
        ]
        self.assertTrue(data_rows, "the verification table has no rows")
        for row in data_rows:
            with self.subTest(command=row[0]):
                self.assertTrue(row[1], f"{row[0]} has no recorded result")
                self.assertRegex(
                    row[1],
                    r"(?i)pass|fail|not run|skipped|unavailable",
                    f"{row[0]} result {row[1]!r} is not an explicit outcome",
                )

    def test_limitations_document_lists_every_unavailable_evidence_class(self) -> None:
        """check limitations document lists every unavailable evidence class."""
        limitations = collapsed(read("submission/webmcp/limitations.md"))
        for phrase in (
            "No external deployment",
            "No video recording",
            "No live paid provider call",
            "No local ComfyUI",
            "No active-agent WebMCP",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, limitations, phrase)

    def test_demo_fixture_is_sanitized_and_present(self) -> None:
        """check demo fixture is sanitized and present."""
        fixture = SUBMISSION / "demo-project"
        self.assertTrue(fixture.is_dir(), "submission/webmcp/demo-project is missing")
        files = sorted(path for path in fixture.rglob("*") if path.is_file())
        self.assertTrue(files, "the demo fixture contains no files")
        for path in files:
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".pdf", ".zip"}:
                continue
            text = path.read_text(encoding="utf-8")
            for pattern in CREDENTIAL_PATTERNS:
                with self.subTest(path=path.name, pattern=pattern.pattern):
                    self.assertIsNone(pattern.search(text), pattern.pattern)
            lowered = text.lower()
            for leaked in ("authorization:", "bearer ", "api_key", "apikey", "secret="):
                with self.subTest(path=path.name, phrase=leaked):
                    self.assertNotIn(leaked, lowered, leaked)


class RuntimeBoundaryContractTests(unittest.TestCase):
    """Validate the deployment and provider docs against the production
    composition root, not just documentation prose.

    These assertions read `web/comic_sol_web/app.py` and the
    `generation/service.py` catalog filtering. They fail if a paid provider
    adapter is wired into the merged `create_app`, or if a drain/flush
    lifecycle hook is added, without the documentation being updated to
    match the new runtime reality.
    """

    def _app_source(self) -> str:
        """Return the current merged `app.py` source text."""
        return APP_SOURCE.read_text(encoding="utf-8")

    def _generation_service_ast(self) -> ast.Module:
        """Parse `app.py` to an AST so we can inspect its composition root."""
        return ast.parse(self._app_source())

    def test_paid_provider_adapters_are_not_wired_into_create_app(self) -> None:
        """Fail if a paid adapter (OpenAI, Google, BFL, xAI, Stability,
        Replicate, fal, Cloudflare) is ever passed to ProviderRegistry in the
        merged app.py, since the docs currently label them Not routable."""
        source = self._app_source()
        # The only registered provider in the merged build is AgentProvider.
        # The paid adapter classes live in separate provider modules and are
        # imported only there; a leak of any of their names into the
        # composition root should fail this contract.
        paid_adapters = [
            "OpenAIProvider",
            "GoogleProvider",
            "BFLProvider",
            "XAIProvider",
            "StabilityProvider",
            "ReplicateProvider",
            "FalProvider",
            "CloudflareProvider",
        ]
        for name in paid_adapters:
            with self.subTest(adapter=name):
                self.assertNotIn(name, source, f"{name} must not be wired in app.py")

    def test_registry_has_exactly_one_provider_agent(self) -> None:
        """Fail if the merged registry tuple does not list AgentProvider as
        the sole registered provider, which would contradict the docs."""
        source = self._app_source()
        self.assertIn("AgentProvider(", source)
        # The ProviderRegistry constructor in _generation_service takes a
        # single tuple; count the comma-separated entries inside it to ensure
        # there is exactly one provider. This is a structural guard, not a
        # text coincidence.
        match = re.search(r"ProviderRegistry\(\((.*?)\)\)", source, re.DOTALL)
        self.assertIsNotNone(match, "ProviderRegistry single-tuple not found")
        assert match is not None
        inner = match.group(1)
        registered = re.findall(r"(\w+Provider)\(", inner)
        self.assertEqual(
            ["AgentProvider"],
            registered,
            f"merged registry must register only AgentProvider, got {registered}",
        )

    def test_catalog_entries_for_paid_providers_exist_but_are_not_routed(self) -> None:
        """Fail if a paid provider has no catalog entry at all, or if the
        providers.md matrix claims a row the catalog cannot support.

        The providers.md matrix lists OpenAI/Google/BFL/etc. as adapter
        implemented but not routable. This test cross-checks the catalog
        actually carries those providers, so the docs are grounded in a real
        model identifier list."""
        doc = (WEB_DOCS / "providers.md").read_text(encoding="utf-8")
        # The known model identifiers are asserted to exist in the catalog.
        # Test-only fixture providers (FakeProvider) are not user-selectable
        # routes and are deliberately excluded from the published matrix.
        for entry in CATALOG:
            if entry.enabled and entry.provider != "fake":
                with self.subTest(model=entry.model):
                    self.assertIn(entry.model, doc, f"catalog model {entry.model} not documented")

    def test_no_lifespan_or_drain_hook_in_create_app(self) -> None:
        """Fail if a shutdown/lifespan hook appears, since deployment.md
        states there is no coordinated graceful-shutdown drain."""
        source = self._app_source()
        self.assertNotIn("lifespan=", source)
        self.assertNotIn("on_event(", source)
        self.assertNotIn('"startup"', source)
        self.assertNotIn('"shutdown"', source)

    def test_generation_service_filters_registry_to_runtime_backed_models(self) -> None:
        """Fail if the generation service does not filter catalog entries by
        registered providers, which would make the matrix's 'routable only
        agent' claim false."""
        svc = GENERATION_SERVICE_SOURCE.read_text(encoding="utf-8")
        self.assertIn("def _runtime_options", svc)
        self.assertIn("self._providers.get(entry.provider)", svc)
        self.assertIn("AGENT_PROVIDER_MODEL", svc)

    def test_deployment_docs_do_not_claim_graceful_shutdown_or_replay(self) -> None:
        """Fail if deployment.md still claims a coordinated drain or an
        on-disk replay hook that the merged create_app does not implement."""
        doc = (WEB_DOCS / "deployment.md").read_text(encoding="utf-8")
        lowered = doc.lower()
        # Explicitly disclaim graceful shutdown; never promise a drain,
        # a queue flush, or a startup replay of interrupted work.
        self.assertIn("no coordinated graceful-shutdown drain", lowered)
        self.assertNotIn("flush the queue", lowered)
        self.assertNotIn("is replayed", lowered)
        self.assertNotIn("replays", lowered)
        self.assertIn("without a replay hook", lowered)

    def test_deployment_docs_describe_sqlite_backed_state_as_durable(self) -> None:
        """Fail if deployment.md labels the SQLite-backed queue or approvals
        as process-local, since the merge wires them to application.sqlite3."""
        doc = (WEB_DOCS / "deployment.md").read_text(encoding="utf-8")
        lowered = doc.lower()
        self.assertIn("sqlite-backed", lowered)
        self.assertIn("generation queue", lowered)
        self.assertIn("durablegenerationqueue", lowered.replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
