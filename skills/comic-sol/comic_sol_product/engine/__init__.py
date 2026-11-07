"""Source-checkout bridge for the namespaced Comic Sol engine."""

from pathlib import Path

_source_scripts = Path(__file__).resolve().parents[2] / "scripts"
if not (_source_scripts / "comic_sol.py").is_file():
    raise ImportError("Comic Sol source engine files are missing.")

__path__ = [str(_source_scripts)]
