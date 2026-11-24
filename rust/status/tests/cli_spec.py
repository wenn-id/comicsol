"""Capture the installed Python CLI's option and help contract for the Rust parser."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from comic_sol_product.cli import build_parser  # noqa: E402


def snapshot(parser: argparse.ArgumentParser) -> dict:
    flags = []
    positional = []
    children = {}
    subcommand = None
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            subcommand = action.dest
            children = {name: snapshot(child) for name, child in action.choices.items()}
            continue
        if not action.option_strings:
            positional.append(action.dest)
            continue
        flag = {
            "name": action.dest,
            "aliases": action.option_strings,
            "takes_value": action.nargs != 0,
        }
        if action.required:
            flag["required"] = True
        if action.choices is not None:
            flag["choices"] = list(action.choices)
        if action.type is int:
            flag["type"] = "int"
        elif action.type is not None and action.type is not Path:
            flag["type"] = action.type.__name__
        if isinstance(action, argparse._AppendAction):
            flag["repeat"] = True
        flags.append(flag)
    result = {"help": parser.format_help(), "flags": flags}
    if positional:
        result["positionals"] = positional
    if subcommand:
        result.update(subcommand=subcommand, children=children, names=list(children))
    return result


if __name__ == "__main__":
    print(json.dumps(snapshot(build_parser()), ensure_ascii=False, indent=2))
