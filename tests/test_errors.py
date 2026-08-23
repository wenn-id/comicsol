import json
import unittest
from pathlib import Path

from comic_sol_product.errors import (
    REQUIRED_NAMESPACES,
    ERROR_DEFINITIONS,
    CliUsageError,
    ValidationFailureError,
    classify_exception,
    error_payload,
    format_human_error,
)

from scripts.export_pdf import PdfExportError  # noqa: E402
from scripts.page_quality import PageQualityMigrationError  # noqa: E402
from scripts.pdf_quality import PdfQualityError  # noqa: E402
from scripts.typography import TypographyPreflightError  # noqa: E402
from scripts.validate_project import ProjectValidationError, ValidationIssue  # noqa: E402


class StructuredErrorContractTests(unittest.TestCase):
    def test_registry_covers_public_error_namespaces(self):
        namespaces = {definition.code.split("-", 2)[1] for definition in ERROR_DEFINITIONS.values()}
        self.assertTrue(REQUIRED_NAMESPACES <= namespaces)

    def test_registry_definitions_have_stable_contract_fields(self):
        self.assertGreaterEqual(len(ERROR_DEFINITIONS), len(REQUIRED_NAMESPACES))
        for code, definition in ERROR_DEFINITIONS.items():
            with self.subTest(code=code):
                self.assertEqual(code, definition.code)
                self.assertRegex(code, r"^CS-[A-Z]+-[0-9]{3}$")
                for field in ("category", "message", "reason", "recovery"):
                    self.assertTrue(getattr(definition, field).strip())

    def test_same_failure_classifies_identically_for_cli_and_mcp(self):
        error = FileNotFoundError("/private/secret/project.json")
        cli_error = classify_exception(error, command="status", surface="cli")
        mcp_error = classify_exception(error, command="comic_status", surface="mcp")

        self.assertEqual("CS-PROJ-002", cli_error.code)
        self.assertEqual(cli_error.code, mcp_error.code)
        self.assertEqual(cli_error.category, mcp_error.category)
        self.assertNotIn("secret", json.dumps(error_payload(error, command="status")))

    def test_payload_has_machine_fields_and_safe_human_rendering(self):
        error = ValueError("invalid project data")
        payload = error_payload(error, command="validate")

        self.assertEqual(
            {"code", "category", "message", "reason", "recovery", "command"},
            set(payload),
        )
        self.assertEqual("CS-PROJ-001", payload["code"])
        self.assertEqual("validate", payload["command"])
        rendered = format_human_error(error, command="validate")
        self.assertIn("CS-PROJ-001", rendered)
        self.assertIn("Recovery:", rendered)

    def test_human_rendering_preserves_safe_actionable_detail(self):
        error = ValueError("source file must use .txt or .md: '/tmp/Comic Sol/story.png'")
        rendered = format_human_error(error, command="init")

        self.assertIn("source file must use .txt or .md", rendered)
        self.assertIn("<path>", rendered)
        self.assertNotIn("/tmp/Comic Sol/story.png", rendered)

    def test_generic_runtime_error_is_not_missing_extra(self):
        classified = classify_exception(RuntimeError("finalize operation failed"), surface="cli")
        self.assertNotEqual("CS-INSTALL-001", classified.code)

    def test_specific_missing_extra_runtime_error_uses_install_namespace(self):
        classified = classify_exception(
            RuntimeError("MCP support is not installed; run: pip install 'comic-sol[mcp]'"),
            surface="cli",
        )
        self.assertEqual("CS-INSTALL-001", classified.code)

    def test_mcp_request_errors_use_mcp_namespace(self):
        error = ValueError("invalid project ID")
        classified = classify_exception(error, command="comic_status", surface="mcp", request=True)
        self.assertEqual("CS-MCP-001", classified.code)
        self.assertEqual("invalid-request", classified.category)

    def test_registry_documentation_requires_append_only_code_process(self):
        documentation = (Path(__file__).parents[1] / "docs" / "structured-errors.md").read_text()
        for namespace in REQUIRED_NAMESPACES:
            self.assertIn(f"CS-{namespace}", documentation)
        self.assertIn("Never reuse an old identifier", documentation)
        self.assertIn("ERROR_DEFINITIONS", documentation)

    def test_every_registered_code_is_reachable_through_the_classifier(self):
        cases = (
            ("CS-CLI-001", CliUsageError("the following arguments are required: command"), {}),
            ("CS-PROJ-001", ValueError("invalid project data"), {}),
            ("CS-PROJ-002", FileNotFoundError("/private/project.json"), {}),
            ("CS-PROJ-003", PermissionError("/private/project.json"), {}),
            ("CS-PROJ-004", OSError("storage unavailable"), {}),
            ("CS-PROJ-005", RuntimeError("engine failed"), {}),
            ("CS-SEC-001", ValueError("security-error: project contains a symlink"), {}),
            (
                "CS-SEC-002",
                ValueError("security-error: input exceeds the JSON size limit"),
                {},
            ),
            ("CS-IMG-001", ValueError("panel is not a readable image (PIL): somewhere"), {}),
            ("CS-QA-001", ValueError("page_qa_required: qa/pages/page-001.json is stale"), {}),
            ("CS-FONT-001", ValueError("font policy requires regular"), {}),
            ("CS-EXPORT-001", PdfExportError("no composed page PNGs exist"), {}),
            (
                "CS-INSTALL-001",
                RuntimeError("MCP support is not installed; run: pip install 'comic-sol[mcp]'"),
                {},
            ),
            ("CS-MCP-001", ValueError("invalid project ID"), {"request": True}),
            ("CS-MCP-002", RuntimeError("tool failed"), {"surface": "mcp"}),
        )
        seen = {code for code, _, _ in cases}
        self.assertEqual(set(ERROR_DEFINITIONS), seen)
        for code, error, options in cases:
            with self.subTest(code=code):
                self.assertEqual(code, classify_exception(error, **options).code)

    def test_owning_boundaries_reach_img_qa_font_and_export_for_both_surfaces(self):
        validation_error = ProjectValidationError(
            [ValidationIssue("project.json", "status", "unknown manifest status")]
        )
        boundary_signals = (
            ("CS-IMG-001", ValueError("source is not a readable image: panels/raw/p01-01.png")),
            ("CS-IMG-001", ValueError("source image format must be PNG, JPEG, or WEBP")),
            ("CS-IMG-001", ValueError("source image exceeds the decoded pixel limit")),
            ("CS-IMG-001", FileNotFoundError("missing required lettered panel image: p01-01")),
            (
                "CS-IMG-001",
                ValueError(r"panel is not a readable image (PIL): C:\Users\x\p01-01.png"),
            ),
            ("CS-QA-001", ValueError("page_qa_required: qa/pages/page-001.json is missing")),
            (
                "CS-QA-001",
                ValueError("page_qa_required: /var/lib/qa/pages/page-001.json is missing"),
            ),
            ("CS-QA-001", PdfQualityError("pdf quality check failed")),
            ("CS-QA-001", PageQualityMigrationError("record cannot be migrated")),
            ("CS-QA-001", validation_error),
            ("CS-QA-001", ValidationFailureError(3)),
            ("CS-FONT-001", ValueError("font policy bold is unavailable: face.ttf")),
            ("CS-FONT-001", ValueError("font script override is duplicated: han")),
            ("CS-FONT-001", TypographyPreflightError(())),
            ("CS-EXPORT-001", PdfExportError("invalid project manifest: broken")),
        )
        for expected_code, error in boundary_signals:
            for surface in ("cli", "mcp"):
                with self.subTest(code=expected_code, surface=surface, error=error):
                    classified = classify_exception(error, surface=surface)
                    self.assertEqual(expected_code, classified.code)

    def test_cli_surface_does_not_reuse_mcp_request_codes(self):
        classified = classify_exception(ValueError("unknown validation stage: bogus"))
        self.assertEqual("CS-PROJ-001", classified.code)
        mcp_classified = classify_exception(
            ValueError("unknown validation stage: bogus"), surface="mcp"
        )
        self.assertEqual("CS-MCP-001", mcp_classified.code)

    def test_unexpected_exception_types_classify_as_internal_error(self):
        for error in (KeyError("missing"), AttributeError("no attribute")):
            for surface, expected in (("cli", "CS-PROJ-005"), ("mcp", "CS-MCP-002")):
                with self.subTest(type=type(error).__name__, surface=surface):
                    self.assertEqual(expected, classify_exception(error, surface=surface).code)

    def test_classifier_lookup_surfaces_reference_registered_codes_only(self):
        import comic_sol_product.errors as errors_module

        self.assertFalse(hasattr(errors_module, "_BY_CATEGORY"))
        self.assertFalse(hasattr(errors_module, "_safe_raw_message"))
        for code in errors_module._BOUNDARY_TYPE_NAMES.values():
            self.assertIn(code, ERROR_DEFINITIONS)
        for _, code in errors_module._BOUNDARY_MESSAGE_PREFIXES:
            self.assertIn(code, ERROR_DEFINITIONS)

    def test_error_payload_optional_detail_is_carried_verbatim(self):
        payload = error_payload(KeyError("boom"), command="status", detail="boom at <path>")
        self.assertEqual("CS-PROJ-005", payload["code"])
        self.assertEqual("boom at <path>", payload["detail"])

    def test_payload_without_detail_keeps_the_stable_field_set(self):
        payload = error_payload(ValueError("invalid project data"), command="validate")
        self.assertEqual(
            {"code", "category", "message", "reason", "recovery", "command"}, set(payload)
        )


if __name__ == "__main__":
    unittest.main()
