"""Shared lifecycle state and identifier contracts below the facade modules.

This module carries only dependency-free constants so that both the lifecycle
engine (``comic_sol``) and the validators (``validate_project``, ``mcp_server``)
can import the same status and identifier grammar without either importing the
other's heavier surface. Keeping these here removes the ``validate_project`` ->
``comic_sol`` static import cycle.
"""

from __future__ import annotations

import re

LINEAR_STATUSES = (
    "INIT",
    "PLANNED",
    "SCRIPTED",
    "STORYBOARDED",
    "REFERENCES_READY",
    "PANELS_READY",
    "QA_READY",
    "LETTERED",
    "COMPOSED",
    "EXPORTED",
    "COMPLETE",
)
TERMINAL_STATUSES = frozenset({"COMPLETE", "COMPLETE_WITH_WARNINGS"})
ALL_STATUSES = frozenset((*LINEAR_STATUSES, "BLOCKED", "COMPLETE_WITH_WARNINGS"))

IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
CATEGORY = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")


def allowed_transition(current: str, target: str) -> bool:
    """Return whether one lifecycle transition is valid."""
    if current not in ALL_STATUSES or target not in ALL_STATUSES:
        return False
    if current in TERMINAL_STATUSES or current == "BLOCKED":
        return False
    if target == "BLOCKED":
        return True
    if current == "EXPORTED" and target == "COMPLETE_WITH_WARNINGS":
        return True
    if current in LINEAR_STATUSES:
        index = LINEAR_STATUSES.index(current)
        return index + 1 < len(LINEAR_STATUSES) and LINEAR_STATUSES[index + 1] == target
    return False
