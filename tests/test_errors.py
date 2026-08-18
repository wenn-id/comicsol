import json
import unittest
from pathlib import Path

from comic_sol_product.errors import (
    REQUIRED_NAMESPACES,
    ERROR_DEFINITIONS,
    classify_exception,
    error_payload,
    format_human_error,
)


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


if __name__ == "__main__":
    unittest.main()
