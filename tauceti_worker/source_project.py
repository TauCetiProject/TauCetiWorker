"""Bindings for shared GitHub tools alongside a native content repository."""

from __future__ import annotations

import copy

from tauceti_tools.forge import GITHUB
from tauceti_tools.project import Project


def shared_settings(project):
    """Resolve legacy template/tool names while retaining the real source in Project."""
    if project is None:
        return project
    references = [project.repository_ref(role) for role in project.raw["repositories"] if role != "content"]
    references.extend(project.tool_pin(name)[0] for name in project.raw.get("tools", {}))
    if any(reference.forge != GITHUB for reference in references):
        raise ValueError("Stage 1B requires GitHub bindings for shared roadmap, tool and archive repositories")
    if project.repository_ref("content").forge == GITHUB:
        return project
    raw = copy.deepcopy(project.raw)
    raw["repositories"]["content"] = project.repository_ref("content").name
    return Project(raw, project.source)
