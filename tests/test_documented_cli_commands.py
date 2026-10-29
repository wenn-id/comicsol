import argparse
import re
import shlex
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from comic_sol_product import cli
from scripts import comic_sol

ROOT = Path(__file__).resolve().parents[1]
DOC_FILES = (
    ROOT / "README.md",
    ROOT / "SUPPORT.md",
    ROOT / "SKILL.md",
    *sorted((ROOT / "docs").rglob("*.md")),
    *sorted((ROOT / "skills" / "comic-sol").rglob("*.md")),
)


def _fenced_blocks(path):
    active = False
    start_line = 0
    lines = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("```"):
            if active:
                yield start_line, lines
                active = False
                lines = []
            else:
                active = True
                start_line = number + 1
            continue
        if active:
            lines.append(line)


def _logical_commands(lines):
    pending = ""
    start_line = 0
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        stripped = re.sub(r"\[[^\]]*\]", "", stripped)
        if not stripped:
            continue
        if not pending:
            start_line = number
        continuation = stripped.endswith(("\\", "`"))
        if continuation:
            stripped = stripped[:-1].rstrip()
        pending = f"{pending} {stripped}".strip()
        if not continuation:
            yield start_line, pending
            pending = ""
    if pending:
        yield start_line, pending


def _cli_invocation(command):
    normalized = command.replace("scripts\\comic_sol.py", "scripts/comic_sol.py")
    docker = re.search(
        r"--entrypoint\s+comic-sol\s+comic-sol:[^\s]+\s+(.+)$",
        normalized,
    )
    if docker:
        return "installed", shlex.split(docker.group(1))

    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError:
        return None
    for index, token in enumerate(tokens):
        normalized_token = token.replace("\\", "/")
        if normalized_token.endswith("scripts/comic_sol.py"):
            return "source", tokens[index + 1 :]
        if token in {"comic-sol", "./comic-sol/comic-sol", "comic-sol/comic-sol"}:
            return "installed", tokens[index + 1 :]
    return None


def _subparser_action(parser):
    return next(
        (action for action in parser._actions if isinstance(action, argparse._SubParsersAction)),
        None,
    )


def _selected_parser(parser, arguments):
    current = parser
    cursor = 0
    while True:
        action = _subparser_action(current)
        if action is None:
            return current, cursor
        found = None
        for index in range(cursor, len(arguments)):
            command = arguments[index].split("=", 1)[0]
            if command in action.choices:
                found = index
                current = action.choices[command]
                cursor = index + 1
                break
        if found is None:
            return current, cursor


def _normalize_value(action, value):
    choices = action.choices
    if choices is not None and value not in choices:
        if value.isupper() or "|" in value or value.startswith("$"):
            return str(next(iter(choices)))
    action_type = action.type
    type_name = getattr(action_type, "__name__", "")
    placeholder = value.startswith("$") or value.isupper()
    if action_type is int and placeholder:
        return "1"
    if type_name in {"dogfood_minutes", "dogfood_failed_attempts"} and placeholder:
        return "1"
    if action_type is Path and placeholder:
        return "/tmp/docs-cli-placeholder"
    return value


def _normalize_arguments(parser, arguments):
    normalized = list(arguments)
    leaf, cursor = _selected_parser(parser, normalized)
    options = {option: action for action in leaf._actions for option in action.option_strings}
    positionals = [action for action in leaf._actions if not action.option_strings]
    positional_index = 0
    index = cursor
    while index < len(normalized):
        token = normalized[index]
        if token.startswith("-") and token != "-":
            option, separator, value = token.partition("=")
            action = options.get(option)
            if action is None:
                index += 1
                continue
            if separator:
                normalized[index] = f"{option}={_normalize_value(action, value)}"
                index += 1
                continue
            nargs = action.nargs
            if nargs == 0:
                index += 1
                continue
            count = nargs if isinstance(nargs, int) else 1
            for _ in range(count):
                if index + 1 < len(normalized):
                    normalized[index + 1] = _normalize_value(action, normalized[index + 1])
                    index += 1
            index += 1
            continue
        if positional_index < len(positionals):
            action = positionals[positional_index]
            normalized[index] = _normalize_value(action, token)
            if action.nargs in (None, 1):
                positional_index += 1
        index += 1
    return normalized


class DocumentedCliCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.installed_parser = cli.build_parser()
        cls.source_parser = comic_sol._build_parser()

    def test_every_fenced_comic_sol_cli_invocation_parses(self):
        examples = []
        for path in DOC_FILES:
            if not path.is_file():
                continue
            for block_start, block in _fenced_blocks(path):
                for relative_line, command in _logical_commands(block):
                    invocation = _cli_invocation(command)
                    if invocation is None:
                        continue
                    surface, arguments = invocation
                    parser = self.installed_parser if surface == "installed" else self.source_parser
                    examples.append(
                        (path, block_start + relative_line - 1, command, parser, arguments)
                    )

        self.assertTrue(examples, "no fenced Comic Sol CLI examples were discovered")
        failures = []
        for path, line, command, parser, arguments in examples:
            normalized = _normalize_arguments(parser, arguments)
            stdout = StringIO()
            stderr = StringIO()
            try:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    parser.parse_args(normalized)
            except SystemExit as error:
                if error.code == 0:
                    continue
                failures.append(f"{path.relative_to(ROOT)}:{line}: {command} ({error})")
            except argparse.ArgumentError as error:
                failures.append(f"{path.relative_to(ROOT)}:{line}: {command} ({error})")
        self.assertFalse(
            failures,
            "documented CLI command(s) no longer parse:\n" + "\n".join(failures),
        )


if __name__ == "__main__":
    unittest.main()
