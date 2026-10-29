import argparse
import unittest

from comic_sol_product import cli
from comic_sol_product.cli_command_spec import (
    SHARED_COMMAND_LEAVES,
    STARTER_IDS,
)
from comic_sol_product.config import default_output_root
from scripts import comic_sol
from scripts.starter_templates import STARTER_IDS as ENGINE_STARTER_IDS


def _subparsers(parser):
    return next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )


def _leaf_parser(parser, path):
    for name in path.split("."):
        parser = _subparsers(parser).choices[name]
    return parser


def _argument_signature(parser, *, init=False):
    signature = {}
    installed_only_init_arguments = {
        "interactive",
        "image_capability_status",
        "image_capability_name",
        "supports_reference_images",
        "supports_dimensions",
    }
    for action in parser._actions:
        if action.dest in {"help", "as_json"}:
            continue
        if init and action.dest in installed_only_init_arguments:
            continue
        required = action.required
        if init and action.dest in {"output_root", "title"}:
            required = False
        choices = tuple(action.choices) if action.choices is not None else None
        signature[action.dest] = (
            tuple(action.option_strings),
            required,
            getattr(action.type, "__name__", None),
            choices,
            action.nargs,
        )
    return signature


class SharedCliCommandSpecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.installed = cli.build_parser()
        cls.source = comic_sol._build_parser()

    def test_all_shared_leaf_commands_exist_in_both_parsers(self):
        self.assertEqual(15, len(SHARED_COMMAND_LEAVES))
        for command in SHARED_COMMAND_LEAVES:
            with self.subTest(command=command):
                self.assertIn(command.split(".")[0], _subparsers(self.installed).choices)
                self.assertIn(command.split(".")[0], _subparsers(self.source).choices)
                _leaf_parser(self.installed, command)
                _leaf_parser(self.source, command)

    def test_shared_leaf_arguments_match_except_documented_surface_deltas(self):
        for command in SHARED_COMMAND_LEAVES:
            with self.subTest(command=command):
                installed = _leaf_parser(self.installed, command)
                source = _leaf_parser(self.source, command)
                self.assertEqual(
                    _argument_signature(installed, init=command == "init"),
                    _argument_signature(source, init=command == "init"),
                )

    def test_init_differences_are_limited_to_interactive_and_source_requiredness(self):
        installed = _leaf_parser(self.installed, "init")
        source = _leaf_parser(self.source, "init")
        installed_actions = {action.dest: action for action in installed._actions}
        source_actions = {action.dest: action for action in source._actions}

        self.assertIn("interactive", installed_actions)
        self.assertNotIn("interactive", source_actions)
        self.assertFalse(installed_actions["output_root"].required)
        self.assertTrue(source_actions["output_root"].required)
        self.assertFalse(installed_actions["title"].required)
        self.assertTrue(source_actions["title"].required)
        for dest in (
            "image_capability_status",
            "image_capability_name",
            "supports_reference_images",
            "supports_dimensions",
        ):
            self.assertIn(dest, installed_actions)
            self.assertNotIn(dest, source_actions)

    def test_json_and_doctor_defaults_remain_entrypoint_specific(self):
        installed_doctor = self.installed.parse_args(["--json", "doctor"])
        source_doctor = self.source.parse_args(["doctor"])
        self.assertTrue(installed_doctor.as_json)
        self.assertEqual(default_output_root(), installed_doctor.output_root)
        self.assertEqual(default_output_root(), source_doctor.output_root)

        installed_status = self.installed.parse_args(["--json", "status", "/tmp/project"])
        source_status = self.source.parse_args(["status", "/tmp/project", "--json"])
        self.assertTrue(installed_status.as_json)
        self.assertTrue(source_status.as_json)
        with self.assertRaises(SystemExit):
            self.source.parse_args(["--json", "doctor"])

    def test_shared_starter_choices_match_the_engine_catalog(self):
        self.assertEqual(ENGINE_STARTER_IDS, STARTER_IDS)


if __name__ == "__main__":
    unittest.main()
