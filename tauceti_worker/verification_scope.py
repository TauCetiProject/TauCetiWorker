"""Shared offline scope calculation with the selected source's routing context."""

import os

from tauceti_tools.local_scope import CANONICAL, MODULE, PINS, POLICIES, git, scope, untracked
from tauceti_tools.local_scope import local_main as shared_local_main

from .config import environment

__all__ = ["CANONICAL", "MODULE", "PINS", "POLICIES", "git", "local_main", "scope", "untracked"]


def local_main(root):
    """Reuse canonical-ref discovery with native content routing when selected."""
    configuration = os.environ.get("TAUCETI_SOURCE_GH_CONF")
    if not configuration:
        return shared_local_main(root)
    # The human-owned project settings remain unchanged for other consumers.
    # Only this offline lookup uses the native content configuration already
    # supplied to the source CLI, rather than defaulting to GitHub's origin.
    with environment(TAUCETI_CONF=configuration, TAUCETI_CONF_SOURCE=None, TAUCETI_CONF_SNAPSHOT=None):
        return shared_local_main(root)
