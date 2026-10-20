"""Targeted fork discovery protocol, also used by the companion Progress publisher.

The caller supplies an accounted read transport. Only complete, error-free pages
can establish absence; a repository name is never ancestry evidence.
"""

import json
import re


class ForkUnknown(RuntimeError):
    pass


def discover(read, upstream):
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", upstream):
        raise ForkUnknown("fork upstream must be an explicit owner/repository")
    owner, name = upstream.split("/")
    ancestry = "id nameWithOwner"
    for _ in range(8):
        ancestry = "id nameWithOwner parent{" + ancestry + "}"
    # Every repository the actor owns, not only forks, because a name collision has to be read from
    # the same listing. Asking `viewer{repository(name:)}` directly does not work: when no such
    # repository exists GitHub answers `null` together with a NOT_FOUND error, so the query fails in
    # exactly the case this exists for, an actor with no fork yet. What the listing cannot see is a
    # repository hidden from a token restricted to selected repositories, or a redirect left by a
    # rename; either way GitHub still refuses to overwrite a repository, and forks under a suffixed
    # name, which the ancestry check then finds.
    query = (
        "query($owner:String!,$name:String!,$endCursor:String){"
        "viewer{login "
        "repositories(first:100,after:$endCursor,ownerAffiliations:[OWNER]){"
        "nodes{id name nameWithOwner isFork isArchived isLocked viewerPermission owner{login} parent{" + ancestry + "}}"
        "pageInfo{hasNextPage endCursor}}}"
        "repository(owner:$owner,name:$name){id nameWithOwner isPrivate}}"
    )
    try:
        pages = json.loads(
            read(
                [
                    "api",
                    "graphql",
                    "--paginate",
                    "--slurp",
                    "-f",
                    "query=" + query,
                    "-f",
                    "owner=" + owner,
                    "-f",
                    "name=" + name,
                ]
            )
        )
        if not isinstance(pages, list) or not pages:
            raise ForkUnknown("fork discovery returned no pages")
        identity = None
        matches = []
        collision = False
        uncertain = False
        for page in pages:
            if page.get("errors"):
                raise ForkUnknown("partial fork discovery response")
            viewer = page["data"]["viewer"]
            repo = page["data"]["repository"]
            current = (viewer["login"].lower(), repo["id"], repo["nameWithOwner"].lower(), repo["isPrivate"])
            if not current[0] or not current[1] or current[2] != upstream.lower() or type(current[3]) is not bool:
                raise ForkUnknown("upstream or authenticated actor unavailable")
            if identity is not None and identity != current:
                raise ForkUnknown("fork discovery identity changed between pages")
            identity = current
            connection = viewer["repositories"]
            if not isinstance(connection["nodes"], list) or type(connection["pageInfo"]["hasNextPage"]) is not bool:
                raise ForkUnknown("incomplete fork connection")
            for node in connection["nodes"]:
                if node["owner"]["login"].lower() != current[0]:
                    raise ForkUnknown("fork discovery returned an unexpected owner")
                if type(node["isFork"]) is not bool or not isinstance(node["name"], str):
                    raise ForkUnknown("incomplete repository listing")
                collision = collision or node["name"].lower() == name.lower()
                if not node["isFork"]:
                    continue
                parent = node["parent"]
                found = False
                while parent is not None:
                    if parent["id"] == current[1] and parent["nameWithOwner"].lower() == current[2]:
                        found = True
                        break
                    if "parent" not in parent:
                        uncertain = True
                        break
                    parent = parent["parent"]
                if found:
                    if (
                        node["viewerPermission"] not in ("WRITE", "MAINTAIN", "ADMIN")
                        or node["isArchived"] is not False
                        or node["isLocked"] is not False
                    ):
                        uncertain = True
                        continue
                    if node["nameWithOwner"].split("/")[0].lower() != current[0]:
                        raise ForkUnknown("fork name and owner disagree")
                    matches.append(node["nameWithOwner"])
        if connection["pageInfo"]["hasNextPage"] is not False:
            raise ForkUnknown("fork discovery stopped before the final page")
        if not matches and uncertain:
            raise ForkUnknown("fork ancestry, push permission or usability remains uncertain")
        return {
            "actor": identity[0],
            "upstream_id": identity[1],
            "upstream": upstream,
            "fork": sorted(set(matches))[0] if matches else None,
            # Private visibility and name collisions cannot authorize a creation retry.
            "absent": not matches and not collision and identity[3] is False,
        }
    except (ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        raise ForkUnknown("incomplete fork discovery response") from exc
