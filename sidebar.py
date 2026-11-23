"""Read desktop sidebar preferences and match them to official local projects."""

from collections import defaultdict
import json
from pathlib import Path

from backend import codex_environment


def desktop_preferences():
    home = Path(codex_environment().get("CODEX_HOME") or Path.home() / ".codex")
    try:
        state = json.loads((home / ".codex-global-state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}, {}, [], {}
    owner = state.get("electron-renderer-content-owner")
    atoms = state.get(f"electron-owner-atoms:{owner}", {})
    layouts = atoms.get("sidebar-custom-sections-v3", {})
    layout = next(iter(layouts.values()), {}) if len(layouts) == 1 else {}
    sections = {f"custom:{section['id']}": section for section in layout.get("sections", [])}
    order = layout.get("sectionOrder", []) + list(sections)
    sections = [sections[key] for key in dict.fromkeys(order) if key in sections]
    accounts = atoms.get("chatgpt-sidebar-state-v2", {})
    names = {
        (account_id, project["id"]): project["name"]
        for account_id, account in accounts.items()
        for project in account.get("projects", [])
        if project.get("id") and project.get("name")
    }
    bridges = state.get("app-server-project-id-by-legacy-project-id-by-host", {})
    aliases = next((v for k, v in bridges.items()
                    if k.startswith("local:") and Path(k[6:]) == home), {})
    assignments = {
        key: value["projectId"]
        for key, value in state.get("thread-project-assignments", {}).items()
        if value.get("projectKind") == "local" and value.get("projectId")
        and state.get("thread-project-membership-host-ids", {}).get(key, "local") == "local"
    }
    return names, aliases, assignments, sections, state.get("sidebar-project-thread-orders", {})


def catalog(projects, rows, preferences):
    names, aliases, assignments, sections, orders = preferences
    by_id = {}
    by_key = {}
    roots = []
    for data in projects:
        project_id = data["id"]
        metadata = data.get("metadata") or {}
        local_id = metadata.get("codex.localProjectId") or next(
            (key for key, value in aliases.items() if value == project_id), project_id
        )
        try:
            backing = json.loads(metadata.get("codex.chatGptBacking") or "{}") or {}
        except (TypeError, ValueError):
            backing = {}
        name = names.get((backing.get("accountId"), backing.get("projectId")), data["name"])
        project_roots = [entry["path"] for entry in data.get("roots", []) if entry.get("path")]
        project = {"id": project_id, "legacy": local_id, "name": name, "roots": project_roots, "rows": []}
        by_id[project_id] = project
        by_key[f"codex:project:{local_id}"] = project
        if backing.get("projectId"):
            by_key[f"chatgpt:project:{backing['projectId']}"] = project
        roots.extend((Path(root), project) for root in project_roots)
    roots.sort(key=lambda pair: len(pair[0].parts), reverse=True)

    other = defaultdict(list)
    for row in rows:
        assigned = assignments.get(row["id"])
        project = by_id.get(aliases.get(assigned, assigned)) or by_id.get(row.get("projectId"))
        cwd = row.get("cwd") or ""
        if project is None and cwd:
            path = Path(cwd)
            project = next((candidate for root, candidate in roots if path.is_relative_to(root)), None)
        if project is None:
            other[cwd].append(row)
        else:
            project["rows"].append(row)
    for project in by_id.values():
        order = {thread_id: i for i, thread_id in enumerate(orders.get(project["legacy"], []))
                 if isinstance(thread_id, str)}
        project["rows"].sort(key=lambda row: order.get(row["id"], len(order)))
    grouped = []
    used = set()
    for section in sections:
        entries = []
        for key in section.get("itemKeys", []):
            project = by_key.get(key)
            if project and project["id"] not in used:
                entries.append(project)
                used.add(project["id"])
        if entries:
            grouped.append((section["id"], section["name"], entries))
    rest = [
        project for key, project in by_id.items()
        if key not in used and (project["rows"] or not project["legacy"].startswith("g-p-"))
    ]
    if rest:
        grouped.append(("projects", "项目", rest))
    if other:
        grouped.append(("other", "其他本地聊天", [
            {"id": cwd, "name": Path(cwd).name or cwd or "未指定目录",
             "roots": [cwd] if cwd else [], "rows": chats}
            for cwd, chats in other.items()
        ]))
    return grouped
