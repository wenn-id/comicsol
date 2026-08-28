"""Contract tests for the user-facing documentation set (issue #213).

The audited gaps this module keeps closed: surfaces and output roots, the
support matrix, vendor-neutral provider guidance, README artifact examples and
accessibility limitations, the expanded SUPPORT/PRIVACY/TERMS scope with a
private sensitive-report route, and the README links that make those documents
reachable. Everything is derived from repository files, so the suite stays
offline.
"""

import re
import textwrap
import unittest
from pathlib import Path

from comic_sol_product.config import default_output_root


ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def collapsed(text: str) -> str:
    return " ".join(text.split())


class SurfacesDocumentTests(unittest.TestCase):
    """docs/surfaces.md separates every workflow and its output root."""

    @classmethod
    def setUpClass(cls):
        cls.document = read("docs/surfaces.md")

    def test_every_surface_has_a_section(self):
        """Keep every documented integration surface reachable by its current heading."""
        for surface in (
            "Codex Skill placement",
            "Codex Plugin bundle",
            "Source checkout (development)",
            "Installed CLI (wheel)",
            "Native portable archive",
            "MCP server",
            "OCI image",
        ):
            self.assertIn(f"## {surface}", self.document, surface)

    def test_platform_default_table_matches_the_engine(self):
        for platform in ("linux", "darwin", "win32"):
            expected = default_output_root(platform=platform, home=Path("/home/user"))
            self.assertIn(
                expected.as_posix().replace("/home/user", "$HOME"),
                self.document,
                platform,
            )
        self.assertIn("`%USERPROFILE%\\Documents\\Comic Sol`", self.document)

    def test_mcp_and_oci_roots_are_stated_as_non_defaults(self):
        document = collapsed(self.document)
        self.assertIn("no default output root", document)
        self.assertIn("explicit `--root` required", document)
        self.assertIn("/data", document)

    def test_native_runtime_root_is_separate_from_project_output(self):
        self.assertIn("$HOME/.local/share/comic-sol", self.document)
        self.assertIn("$HOME\\AppData\\Local\\ComicSol", self.document)
        self.assertIn("never holds user projects", collapsed(self.document))

    def test_links_the_support_matrix_and_onboarding(self):
        self.assertIn("support-matrix.md", self.document)
        self.assertIn("onboarding.md", self.document)


class SupportMatrixDocumentTests(unittest.TestCase):
    """docs/support-matrix.md publishes the platform/mode/architecture/runtime set."""

    @classmethod
    def setUpClass(cls):
        cls.document = read("docs/support-matrix.md")

    def test_states_the_distribution_contracts(self):
        document = collapsed(self.document)
        for phrase in (
            "Linux x86_64",
            "macOS arm64",
            "Windows x86_64",
            "WSL2 uses the Linux x86_64 archive; it has no separate native archive.",
            "Source installation supports Linux, macOS, Windows, and WSL2 on Python 3.11+.",
            "Intel macOS is source-install-only; it has no native archive.",
        ):
            self.assertIn(phrase, document)

    def test_matrix_covers_every_install_mode(self):
        for mode in (
            "Codex Skill / Plugin",
            "Source / development",
            "Installed CLI wheel",
            "Native portable archive",
            "MCP server",
            "OCI image",
        ):
            self.assertIn(mode, self.document, mode)

    def test_matrix_separates_runtime_and_extras(self):
        document = collapsed(self.document)
        self.assertIn("bundled Python 3.11", document)
        self.assertIn("Python 3.11+", document)
        self.assertIn("MCP SDK", document)
        self.assertIn("linux/amd64", document)

    def test_extra_table_matches_the_declared_dependencies(self):
        self.assertIn("`mcp==2.0.0`", self.document)
        self.assertIn("`Pillow==12.3.0`", self.document)


class ProviderGuideTests(unittest.TestCase):
    """Provider guidance stays capability-based, credential-safe, vendor-neutral."""

    @classmethod
    def setUpClass(cls):
        cls.root_guide = read("references/image-provider-setup.md")
        cls.bundle_guide = read("skills/comic-sol/references/image-provider-setup.md")

    def test_describes_the_capability_not_a_vendor(self):
        document = collapsed(self.root_guide)
        for phrase in (
            "create a raster image from text alone",
            "editing-only image tool",
            "never embeds credentials",
        ):
            self.assertIn(phrase, document)

    def test_no_api_key_in_prompt_pattern_survives_anywhere(self):
        for name, document in (
            ("root", self.root_guide),
            ("bundle", self.bundle_guide),
        ):
            self.assertNotIn("Your API key is set as", document, name)
            self.assertNotIn("API key is set as", document, name)

    def test_vendor_links_are_isolated_in_a_dated_non_normative_appendix(self):
        appendix = self.root_guide.split("## Non-normative vendor pointers", 1)[1]
        self.assertIn("**not normative**", appendix)
        self.assertIn("dated", appendix.lower())
        # Endorsement language and free-tier claims stay out of the guidance.
        guidance = self.root_guide.split("## Non-normative vendor pointers", 1)[0]
        self.assertNotIn("free tier", guidance)
        self.assertNotIn("recommended, free", guidance)
        self.assertNotIn("fal.ai", guidance)

    def test_credential_safety_names_the_client_not_the_prompt(self):
        document = collapsed(self.root_guide)
        self.assertIn("never in prompts", document)
        self.assertIn("Never paste an API key into a prompt", document)
        self.assertIn("revoke it before filing any report", document)

    def test_config_examples_use_placeholders_not_a_vendor_command(self):
        self.assertIn("<launcher for your chosen MCP image server>", self.root_guide)
        self.assertIn("<ITS_CREDENTIAL_VARIABLE>", self.root_guide)

    def test_bundled_guide_stays_vendor_neutral(self):
        self.assertNotIn("fal.ai", self.bundle_guide)
        self.assertNotIn("GPT-5", self.bundle_guide)


class ReadmeContractTests(unittest.TestCase):
    """README examples include the v2.2 artifacts and the audited link set."""

    @classmethod
    def setUpClass(cls):
        cls.readme = read("README.md")

    def test_artifact_listing_includes_v2_2_artifacts(self):
        for artifact in (
            "plan/character-identity-pack.json",
            "panels/*/sfx-audit.json",
            "qa/pages/page-001.json",
            "exports/pdf-verification.json",
            "logs/reference-selection.json",
            "logs/repair-plan.json",
        ):
            self.assertIn(artifact, self.readme, artifact)

    def test_artifact_listing_states_schema_and_evidence_limits(self):
        readme = collapsed(self.readme)
        self.assertIn("schema 2.1", readme)
        self.assertIn("`pdf_verification` descriptor", readme)
        self.assertIn("evidence with limits", readme)
        self.assertIn("mechanics", readme)

    def test_accessibility_limitations_are_stated_without_overclaiming(self):
        section = self.readme.split("### Accessibility and localization limitations", 1)[1].split(
            "\n## ", 1
        )[0]
        document = collapsed(section)
        for phrase in (
            "image-based",
            "untagged",
            "not PDF/UA",
            "no alt text",
            "no extractable text layer",
            "English-only",
        ):
            self.assertIn(phrase, document, phrase)
        # The limitation must not drift into an accessibility promise.
        self.assertIn("cannot read the dialogue", document)

    def test_links_the_legal_and_limitation_documents(self):
        for link in (
            "SUPPORT.md",
            "PRIVACY.md",
            "TERMS.md",
            "SECURITY.md",
            "docs/typography.md",
            "docs/surfaces.md",
            "docs/support-matrix.md",
            "#accessibility-and-localization-limitations",
        ):
            self.assertIn(f"]({link})", self.readme, link)

    def test_install_section_points_at_surfaces_and_matrix(self):
        install = self.readme.split("## Install", 1)[1].split("\n## ", 1)[0]
        self.assertIn("docs/surfaces.md", install)
        self.assertIn("docs/support-matrix.md", install)

    def test_guided_initializer_keeps_an_explicit_automation_equivalent(self):
        section = self.readme.split("### Guided project initialization", 1)[1].split("\n### ", 1)[0]
        self.assertIn("comic-sol init --interactive", section)
        self.assertIn("comic-sol --json init", section)
        self.assertIn("--page-count 2", section)
        self.assertIn("never prompts", section)


class OnboardingTests(unittest.TestCase):
    """The happy path pins an explicit output root and names its surface."""

    @classmethod
    def setUpClass(cls):
        cls.document = read("docs/onboarding.md")

    def test_doctor_happy_path_passes_an_explicit_output_root(self):
        doctor = self.document.split("## 2. Run `comic-sol doctor` now", 1)[1].split("\n## ", 1)[0]
        self.assertIn('--output-root "$HOME/Comic Sol"', doctor)
        self.assertIn('--output-root "$HOME/Documents/Comic Sol"', doctor)
        self.assertIn('doctor --output-root "$env:USERPROFILE\\Documents\\Comic Sol"', doctor)
        self.assertNotRegex(doctor, r"comic_sol\.py doctor\n")
        self.assertIn("INFO image capability: inspect in agent session", doctor)
        self.assertIn("`details.readiness` as `unknown`", doctor)

    def test_names_its_surface_and_links_the_separation(self):
        document = collapsed(self.document)
        self.assertIn("one surface", document)
        self.assertIn("docs/surfaces.md", self.document)
        self.assertIn("docs/support-matrix.md", self.document)

    def test_explains_automatic_image_capability_results_without_provider_setup(self):
        capability = self.document.split("## 3. Let the agent check image capability", 1)[1].split(
            "\n## ", 1
        )[0]
        for result in ("PASS", "partial", "unavailable", "unknown"):
            self.assertIn(result, capability)
        self.assertIn("automatically inspects", capability)
        self.assertIn("does not invoke", capability)
        self.assertIn("install anything", capability)
        self.assertIn("credentials", capability)
        self.assertIn("agent-image-generation", capability)


class SupportPrivacyTermsTests(unittest.TestCase):
    """SUPPORT/PRIVACY/TERMS cover every surface and the private report route."""

    @classmethod
    def setUpClass(cls):
        cls.support = read("SUPPORT.md")
        cls.privacy = read("PRIVACY.md")
        cls.terms = read("TERMS.md")

    def test_support_names_version_install_mode_error_code_and_json_doctor(self):
        document = collapsed(self.support)
        for phrase in (
            "comic-sol --version",
            "Install mode",
            "native portable archive",
            "OCI image",
            "CS-<NAMESPACE>-<NNN>",
            "structured-errors.md",
            "--json doctor",
            "data.ready",
        ):
            self.assertIn(phrase, document, phrase)

    def test_support_defines_a_private_route_for_sensitive_reports(self):
        document = collapsed(self.support)
        for phrase in (
            "Private route for sensitive reports",
            "private vulnerability reporting",
            "Revoke exposed credentials",
            "[REDACTED]",
            "Never open a public issue first",
        ):
            self.assertIn(phrase, document, phrase)

    def test_privacy_covers_every_surface_and_its_output_root(self):
        document = collapsed(self.privacy)
        for phrase in (
            "Codex Skill",
            "Codex Plugin",
            "installed Python CLI",
            "native portable archives",
            "MCP server",
            "OCI image",
            "`--root`",
            "`/data`",
            "surfaces.md",
        ):
            self.assertIn(phrase, document, phrase)
        self.assertIn("2026-", self.privacy)

    def test_privacy_keeps_a_private_contact_route(self):
        self.assertIn("SUPPORT.md", self.privacy)
        self.assertIn("private route", collapsed(self.privacy))

    def test_terms_cover_every_surface_and_stay_local_first(self):
        document = collapsed(self.terms)
        for phrase in (
            "Codex Skill",
            "Codex Plugin",
            "installed Python CLI wheel",
            "native portable archives",
            "MCP server",
            "OCI image",
            "No surface has a Comic Sol account, hosted API, or remote storage",
            "SUPPORT.md",
            "SECURITY.md",
        ):
            self.assertIn(phrase, document, phrase)
        self.assertIn("2026-", self.terms)

    def test_no_fixed_python_minor_version_in_the_checked_documents(self):
        fixed_minor = re.compile(r"(?i)\b(?:python3?\.\d+|py\s+-3\.\d+)\b")
        for name, document in (
            ("SUPPORT.md", self.support),
            ("PRIVACY.md", self.privacy),
            ("TERMS.md", self.terms),
            ("provider guide", read("references/image-provider-setup.md")),
        ):
            self.assertFalse(fixed_minor.search(document), name)


class CrossAgentHandoffDocumentationTests(unittest.TestCase):
    """WP5: README, SKILL.md, and CHANGELOG document cross-agent handoff integration."""

    @classmethod
    def setUpClass(cls):
        cls.readme = read("README.md")
        cls.skill = read("SKILL.md")
        cls.changelog = read("CHANGELOG.md")
        cls.workflow = read("references/workflow.md")

    @staticmethod
    def command_blocks(document: str, command: str) -> list[str]:
        blocks = re.findall(
            r"^[ \t]*```text\n(.*?)^[ \t]*```$",
            document,
            flags=re.DOTALL | re.MULTILINE,
        )
        normalized = [textwrap.dedent(block).rstrip() for block in blocks]
        return [block for block in normalized if block.startswith(command)]

    def test_readme_documents_cross_agent_handoff_workflow(self):
        readme = collapsed(self.readme)
        for phrase in (
            "handoff prepare",
            "handoff inspect",
            "accept-result",
            "record-failure",
        ):
            self.assertIn(phrase, readme, phrase)

    def test_readme_documents_portable_archive_export_import(self):
        readme = collapsed(self.readme)
        for phrase in (
            "archive export",
            "archive import",
        ):
            self.assertIn(phrase, readme, phrase)

    def test_readme_documents_comfyui_as_reference_experimental(self):
        readme = collapsed(self.readme)
        self.assertIn("ComfyUI", self.readme)
        self.assertIn("reference", readme.lower())
        self.assertIn("experimental", readme.lower())
        # Must NOT claim ComfyUI is verified or production-ready
        self.assertNotIn("ComfyUI is verified", self.readme)
        self.assertNotIn("ComfyUI verified", self.readme)

    def test_skill_documents_capability_ordering(self):
        """SKILL.md must state the executor selection order explicitly."""
        skill = collapsed(self.skill)
        # The ordering must be: native compatible tool, then external executor,
        # then handoff preparation
        for phrase in (
            "compatible native image tool",
            "external executor",
            "handoff",
        ):
            self.assertIn(phrase, skill, phrase)
        # Verify the ordering is explicitly stated (native before external before handoff)
        native_pos = skill.find("compatible native image tool")
        external_pos = skill.find("external executor")
        handoff_pos = skill.find("Prepare a handoff")
        self.assertGreater(native_pos, -1, "native capability not found")
        self.assertGreater(external_pos, -1, "external executor not found")
        self.assertGreater(handoff_pos, -1, "handoff prepare not found")
        self.assertLess(native_pos, external_pos, "native must come before external")
        self.assertLess(external_pos, handoff_pos, "external must come before handoff")

    def test_skill_documents_no_provider_name_ranking(self):
        """Capability ordering must not rank by provider name."""
        skill = collapsed(self.skill)
        # The ordering section must not contain provider-specific names as ranking logic
        self.assertNotIn("prefer OpenAI", skill)
        self.assertNotIn("prefer fal", skill)
        self.assertNotIn("prefer Stability", skill)

    def test_changelog_has_wp5_handoff_integration_entry(self):
        changelog = collapsed(self.changelog)
        self.assertIn("cross-agent", changelog)
        self.assertIn("handoff", changelog)

    def test_accept_result_command_blocks_match_panel_and_reference_contracts(self):
        """Panel and reference result intake document their exact, distinct arguments."""
        panel_command = """PYTHON scripts/comic_sol.py handoff accept-result PROJECT \\
  --job JOB_ID \\
  --attempt N \\
  --executor-kind EXECUTOR_KIND \\
  --executor-id ID \\
  --path PATH"""
        reference_command = f"{panel_command} \\\n  --approve-reference"
        blocks = self.command_blocks(
            self.workflow,
            "PYTHON scripts/comic_sol.py handoff accept-result PROJECT",
        )
        self.assertEqual([reference_command, panel_command], blocks)
        self.assertNotIn("PANEL_ID", panel_command)
        self.assertNotIn("--reason", panel_command)
        self.assertNotIn("--approve-reference", panel_command)
        self.assertNotIn("PANEL_ID", reference_command)
        self.assertNotIn("--reason", reference_command)
        self.assertIn("--approve-reference", reference_command)

    def test_record_failure_command_block_matches_contract(self):
        """Failure intake documents its exact arguments without unsupported options."""
        expected = """PYTHON scripts/comic_sol.py handoff record-failure PROJECT \\
  --job JOB_ID \\
  --attempt N \\
  --executor-kind EXECUTOR_KIND \\
  --executor-id ID \\
  --category CATEGORY"""
        blocks = self.command_blocks(
            self.workflow,
            "PYTHON scripts/comic_sol.py handoff record-failure PROJECT",
        )
        self.assertEqual([expected], blocks)
        self.assertNotIn("PANEL_ID", expected)
        self.assertNotIn("--reason", expected)
        self.assertNotIn("--approve-reference", expected)

    def test_handoff_lifecycle_uses_source_launcher(self):
        """Every handoff lifecycle command runs from source and documents the installed alias."""
        section = self.workflow[self.workflow.find("## Cross-agent handoff lifecycle") :]
        for command in (
            "prepare PROJECT",
            "inspect PROJECT",
            "export PROJECT --output PROJECT.comic-sol-handoff",
            "import PROJECT.comic-sol-handoff --output-root ROOT",
            "accept-result PROJECT",
            "record-failure PROJECT",
        ):
            self.assertIn(f"PYTHON scripts/comic_sol.py handoff {command}", section)
        self.assertIn(
            "installed package, `comic-sol handoff` is equivalent",
            section,
        )

    def test_reference_jobs_complete_before_second_prepare_and_panel_jobs(self):
        """Reference-bearing projects document the complete two-phase handoff lifecycle."""
        section = collapsed(self.workflow[self.workflow.find("## Cross-agent handoff lifecycle") :])
        first_prepare = section.find("prepare reference jobs")
        reference_execute = section.find("execute every ready reference job")
        reference_approve = section.find("--approve-reference")
        next_prepare = section.find("`next_action` is `prepare`")
        second_prepare = section.find("prepare again to create panel jobs")
        panel_execute = section.find("execute every ready panel job")
        visual_qa = section.find("Continue normal visual QA")
        positions = (
            first_prepare,
            reference_execute,
            reference_approve,
            next_prepare,
            second_prepare,
            panel_execute,
            visual_qa,
        )
        self.assertTrue(all(position >= 0 for position in positions), positions)
        self.assertEqual(tuple(sorted(positions)), positions)

    def test_fast_mode_allows_documented_handoff_subcommands(self):
        """Fast Mode keeps engine source unread while permitting the handoff CLI."""
        fast_mode = self.skill.split("## Fast Mode", 1)[1].split("### Resolve Python once", 1)[0]
        self.assertIn("Do not read, grep, or open any file under `scripts/`", fast_mode)
        self.assertIn("documented `handoff` subcommands", fast_mode)

    def test_handoff_inspection_applies_only_to_prepared_handoff_execution(self):
        """Direct native generation does not require a prepared handoff job."""
        core_step = next(
            line for line in self.skill.splitlines() if line.startswith("5. Generate canonical")
        )
        self.assertIn("For prepared-handoff execution", core_step)
        self.assertNotIn("Before invoking any executor", core_step)

    def test_no_contradictory_provider_model_ranking(self):
        """Declared capability priority must not become provider/model-specific ranking."""
        selection = self.skill.split("## Executor selection", 1)[1].split("\n## ", 1)[0]
        normalized = collapsed(selection)
        self.assertIn("declared capability priority", normalized)
        native_pos = normalized.find("compatible native image tool")
        external_pos = normalized.find("compatible declared external executor")
        handoff_pos = normalized.find("Prepare a handoff")
        blocked_pos = normalized.find("BLOCKED")
        self.assertTrue(-1 < native_pos < external_pos < handoff_pos < blocked_pos)
        self.assertIn(
            "never by provider name, model name, or provider-specific hard-coded ranking",
            normalized,
        )

    def test_archive_export_before_remote_execution(self):
        """Archive transfer precedes prepared-handoff execution and result intake."""
        section = self.workflow[self.workflow.find("## Cross-agent handoff lifecycle") :]
        export_pos = section.find("export the prepared project")
        import_pos = section.find("import at the destination")
        execute_pos = section.find("Execute via the selected executor")
        accept_pos = section.find("PYTHON scripts/comic_sol.py handoff accept-result")
        for name, position in (
            ("export", export_pos),
            ("import", import_pos),
            ("executor execution", execute_pos),
            ("result intake", accept_pos),
        ):
            self.assertGreater(position, -1, f"{name} not found in lifecycle section")
        self.assertLess(export_pos, import_pos)
        self.assertLess(import_pos, execute_pos)
        self.assertLess(execute_pos, accept_pos)

    def test_handoff_examples_use_shell_safe_executor_placeholder(self):
        """Executor-kind examples cannot be interpreted as shell pipelines."""
        section = self.workflow[self.workflow.find("## Cross-agent handoff lifecycle") :]
        self.assertNotIn("native-tool|external-tool", section)
        self.assertEqual(3, section.count("--executor-kind EXECUTOR_KIND"))
        self.assertIn("replace `EXECUTOR_KIND` with exactly one CLI value", section)

    def test_handoff_archive_examples_use_required_suffix(self):
        """Every lifecycle archive example uses the enforced portable suffix."""
        section = self.workflow[self.workflow.find("## Cross-agent handoff lifecycle") :]
        self.assertIn("must use the `.comic-sol-handoff` suffix", section)
        self.assertEqual(3, section.count("PROJECT.comic-sol-handoff"))

    def test_capability_blocked_handoff_resumes_before_visual_qa(self):
        """Capability-blocked projects record reality and resume before downstream QA."""
        section = self.workflow[self.workflow.find("## Cross-agent handoff lifecycle") :]
        capability_pos = section.find("record its real provider-neutral capability observation")
        resume_pos = section.find("PYTHON scripts/comic_sol.py resume PROJECT --json")
        qa_pos = section.find("Continue normal visual QA")
        self.assertTrue(-1 < capability_pos < resume_pos < qa_pos)
        self.assertIn("otherwise preserve `BLOCKED`", section)

    def test_readme_record_failure_uses_category_not_reason(self):
        """README handoff section uses --category for record-failure."""
        readme = collapsed(self.readme)
        # The README must mention category in connection with record-failure
        self.assertIn("category", readme)
        # Must not say "failure with a reason" in the handoff section
        handoff_section = self.readme.split("## Cross-agent handoff", 1)[1].split("\n## ", 1)[0]
        self.assertNotIn("with a reason", handoff_section)


class GoldenCreatorPathTests(unittest.TestCase):
    """WP3 keeps the approved creator path ahead of every advanced surface."""

    @classmethod
    def setUpClass(cls):
        """Load the creator documents and both canonical and shipped provider guides."""
        cls.readme = read("README.md")
        cls.support = read("docs/support-matrix.md")
        cls.providers = {
            "canonical": read("references/image-provider-setup.md"),
            "bundle": read("skills/comic-sol/references/image-provider-setup.md"),
        }

    @staticmethod
    def section(document: str, heading: str) -> str:
        """Return one level-two Markdown section by its exact heading."""
        marker = f"## {heading}"
        start = document.index(marker)
        end = document.find("\n## ", start + len(marker))
        return document[start:] if end == -1 else document[start:end]

    def test_readme_creator_path_has_approved_structural_order(self):
        """Keep every approved creator-path element in its binding order."""
        advanced = self.readme.index("## Advanced integrations")
        creator = self.readme[:advanced]
        ordered_markers = (
            "## Retained live-generated evidence",
            "samples/sunlight-courier/pages/page-001.png",
            "samples/sunlight-courier/pages/page-002.png",
            "Comic Sol is a local-first production pipeline around any compatible AI image generator. Plan anywhere. Render anywhere. Resume everywhere.",
            "comic-sol skill-install",
            "Make a 2-page manga about a courier delivering sunlight to an underground city.",
            "Codex planning → Antigravity rendering",
            "samples/sunlight-courier/exports/sunlight-courier.pdf",
            "samples/sunlight-courier/project.json",
            "samples/sunlight-courier/qa/report.md",
        )
        positions = tuple(creator.index(marker) for marker in ordered_markers)
        self.assertEqual(tuple(sorted(positions)), positions)
        for page in ("001", "002"):
            match = re.search(
                rf"!\[([^]]+)\]\(samples/sunlight-courier/pages/page-{page}\.png\)",
                creator,
            )
            self.assertIsNotNone(match, page)
            self.assertRegex(match.group(1).lower(), r"courier|sunlight|underground")

    def test_every_creator_path_element_precedes_advanced_integrations(self):
        """Keep all creator essentials above the advanced-integration boundary."""
        advanced = self.readme.index("## Advanced integrations")
        required = (
            "page-001.png",
            "page-002.png",
            "retained live-generated evidence",
            "Plan anywhere. Render anywhere. Resume everywhere.",
            "skill-install",
            "Make a 2-page manga",
            "handoff prepare",
            "handoff export",
            "handoff import",
            "handoff accept-result",
            "sunlight-courier.pdf",
            "project.json",
            "qa/report.md",
        )
        for marker in required:
            self.assertLess(self.readme.index(marker), advanced, marker)

    def test_handoff_example_preserves_the_complete_concise_contract(self):
        """Keep transfer, intake, unblock, resume, and promotion in executable order."""
        section = self.section(self.readme, "Codex planning → Antigravity rendering")
        normalized = collapsed(section)
        for phrase in (
            "experimental until linked live smoke evidence exists",
            "has not been verified",
            "handoff prepare",
            "handoff inspect",
            "handoff export",
            "handoff import",
            "exact `jobs[].path`",
            "only a `ready` job",
            "--job JOB_ID",
            "--attempt ATTEMPT",
            "--executor-kind EXECUTOR_KIND",
            "--executor-id EXECUTOR_ID",
            "--path RASTER_PATH",
            "--approve-reference",
            "prepare again",
            "inspect again before retries",
            "record its real provider-neutral capability observation",
            'comic-sol resume "$IMPORTED_PROJECT" --json',
            "visual QA and promotion",
        ):
            self.assertIn(phrase, normalized, phrase)
        self.assertIn("installed package", normalized)
        self.assertIn("source checkout", normalized)
        resume = normalized.index('comic-sol resume "$IMPORTED_PROJECT" --json')
        qa = normalized.index("visual QA and promotion")
        self.assertLess(resume, qa)

    def test_skill_install_path_requires_a_distribution_that_ships_it(self):
        """Disclose rc6 availability before advertising its installer command."""
        section = self.section(self.readme, "Install the Agent Skill")
        command = section.index("comic-sol skill-install --target codex --scope user")
        for marker in (
            "v2.0.0rc6",
            "not published",
            "v2.0.0rc4",
            "does not include `skill-install`",
        ):
            self.assertGreater(section.find(marker), -1, marker)
            self.assertLess(section.index(marker), command, marker)

    def test_codex_plugin_does_not_reintroduce_manual_skill_placement(self):
        """Keep manual Skill checkout commands out of the plugin workflow."""
        section = self.section(self.readme, "Codex Plugin — same repository")
        for command in (
            "codex plugin marketplace add",
            "codex plugin list",
            "codex plugin add",
        ):
            self.assertIn(command, section)
        self.assertNotIn("git clone", section)
        self.assertNotIn(r".codex\skills\comic-sol", section)

    def test_sample_claims_keep_the_evidence_boundary(self):
        """Limit visual-quality claims to the one retained live sample."""
        evidence = self.section(self.readme, "Retained live-generated evidence")
        normalized = collapsed(evidence).lower()
        self.assertIn("only visual-quality sample", normalized)
        self.assertIn("one retained sample does not prove broad illustration quality", normalized)
        self.assertIn("deterministic fixtures are mechanics-only evidence", normalized)
        self.assertNotIn("placeholder quality", normalized)

    def test_support_matrix_has_exact_tiers_and_separate_claims(self):
        """Publish exactly three tiers while separating host and generator claims."""
        tiers = collapsed(self.section(self.support, "Support tiers"))
        expected = (
            "### 1. Full orchestration",
            "Agent Skills plus filesystem and shell/tool execution.",
            "### 2. Handoff executor",
            "Filesystem plus a compatible native image tool or configured external adapter",
            "consumes prepared generation jobs and returns rasters/receipts.",
            "### 3. Planning only",
            "Chat without required filesystem/tool execution",
            "cannot be claimed to execute or resume the pipeline.",
        )
        for phrase in expected:
            self.assertIn(phrase, tiers)
        self.assertIn("## Host support", self.support)
        self.assertIn("## Image-generator support", self.support)
        host = self.section(self.support, "Host support")
        for name in ("Codex", "Claude", "Antigravity", "ZCode"):
            self.assertRegex(host, rf"(?i){name}[^\n]*experimental")
        self.assertIn("contract claim, not universal verification", collapsed(host))

    def test_image_routes_follow_declared_capability_order(self):
        """Enforce identical route priority in canonical and shipped provider guides."""
        markers = (
            "1. **Compatible declared native image tool.**",
            "2. **Compatible declared external adapter/API tool.**",
            "3. **Portable handoff.**",
            "4. **Actionable `BLOCKED` state preserving editable intermediates.**",
        )
        for name, provider in self.providers.items():
            routes = self.section(provider, "Image route order")
            normalized = collapsed(routes)
            positions = tuple(normalized.index(marker) for marker in markers)
            self.assertEqual(tuple(sorted(positions)), positions, name)
            self.assertIn(
                "Never infer capability from provider, model, host, or tool names.",
                normalized,
                name,
            )

    def test_native_cli_and_engine_positioning_are_explicit(self):
        """Describe the engine as product and CLI/MCP/Skills as adapters."""
        documents = "\n".join(
            read(path)
            for path in (
                "README.md",
                "docs/user/index.md",
                "docs/user/getting-started.md",
                "docs/surfaces.md",
            )
        )
        normalized = collapsed(documents).lower()
        self.assertIn("deterministic engine is the product", normalized)
        self.assertIn("agent skills, cli, and mcp are adapters", normalized)
        self.assertIn("core cli does not create artwork by itself", normalized)
        for verb in (
            "validates",
            "persists",
            "resumes",
            "repairs",
            "letters",
            "composes",
            "exports",
        ):
            self.assertIn(verb, normalized)
        self.assertIn("stores no provider credentials", normalized)

    def test_source_and_bundle_provider_links_resolve(self):
        """Resolve every relative provider-guide link in both layouts."""
        for relative in (
            "references/image-provider-setup.md",
            "skills/comic-sol/references/image-provider-setup.md",
        ):
            document_path = ROOT / relative
            for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", read(relative)):
                if target.startswith(("http://", "https://", "#")):
                    continue
                path = (document_path.parent / target.split("#", 1)[0]).resolve()
                self.assertTrue(path.is_file(), f"{relative}: {target}")

    def test_docs_make_no_fabricated_host_verification_or_adoption_claim(self):
        """Reject unsupported host-verification, rendering, and adoption claims."""
        documents = "\n".join(
            read(path)
            for path in (
                "README.md",
                "docs/onboarding.md",
                "docs/support-matrix.md",
                "references/image-provider-setup.md",
            )
        )
        banned = (
            "Antigravity has been verified",
            "verified on every host",
            "works with every AI chat product",
            "automatic rendering on every host",
            "widely adopted",
            "thousands of creators",
        )
        for claim in banned:
            self.assertNotIn(claim, documents)


if __name__ == "__main__":
    unittest.main()
