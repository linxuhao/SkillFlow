"""skillflow.docs — search/read skillflow's own documentation + schema source.

Backs the native ``skillflow_docs_{list,search,read}`` tools. Serves skillflow's
bundled docs plus the authoritative config schema/runtime source (``graph.py`` /
``core.py``) from THIS installed package, so any host's agents can design and edit
skillflow graphs against the real spec instead of guessing. Because it lives inside
skillflow, the topic map is maintained alongside the files it points at.
"""
from __future__ import annotations

from pathlib import Path


def pkg_dir() -> Path:
    """The installed skillflow package directory (this file's parent)."""
    return Path(__file__).resolve().parent


# topic key → (relative path under the skillflow package, one-line description).
# graph.py / core.py are SOURCE — the authoritative field list lives in the
# StepNode/PipelineGraph dataclasses + _from_dict; the .md are prose + examples.
TOPICS: dict[str, tuple[str, str]] = {
    "schema-source": ("graph.py",
        "AUTHORITATIVE: StepNode/PipelineGraph dataclasses + _from_dict — every valid "
        "config field, defaulting rules, transition/loop/output structs"),
    "engine-source": ("core.py",
        "Engine internals: lifecycle hooks (step_commit/repo_apply/on_failure), loop "
        "crediting (_credit_loop_current_item), tool-param injection (task_name), "
        "from_file transition resolution, checkpoints"),
    "yaml-structure": ("plugins/skill_converter/AGENT.md",
        "SkillFlow YAML structure reference"),
    "graph-design": ("plugins/skill_converter/prompts/design_graph.md",
        "Step types (agent/tool/gate/loop), transition matching, path variables "
        "($STEP_DIR/$CONFIG_DIR/...), a minimal worked example"),
    "overlay-design": ("plugins/skill_converter/prompts/design_overlay.md",
        "Addon/overlay authoring: anchors + splice ops onto a base graph"),
    "runner": ("plugins/skill_runner/AGENT.md",
        "Runner mode: SkillTool + RunnerService + skillflow-mcp"),
}


def _topic_path(topic: str) -> Path | None:
    t = TOPICS.get(topic)
    if not t:
        return None
    p = pkg_dir() / t[0]
    return p if p.exists() else None


def list_topics() -> dict:
    out = []
    for key, (rel, desc) in TOPICS.items():
        entry = {"topic": key, "path": rel, "desc": desc}
        if not (pkg_dir() / rel).exists():
            entry["missing"] = True
        out.append(entry)
    return {"topics": out,
            "hint": "skillflow_docs_read(topic=...) to read; skillflow_docs_search("
                    "query=...) to grep. `schema-source` (graph.py) is the authoritative "
                    "field list."}


def search_docs(query: str, max_hits: int = 40) -> dict:
    q = (query or "").strip()
    if not q:
        return {"error": "query is required"}
    hits = []
    for key, (rel, _desc) in TOPICS.items():
        p = pkg_dir() / rel
        if not p.exists():
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines):
            if q.lower() in line.lower():
                lo, hi = max(0, i - 1), min(len(lines), i + 2)
                snippet = "\n".join(f"{n + 1}: {lines[n]}" for n in range(lo, hi))
                hits.append({"topic": key, "path": rel, "line": i + 1, "snippet": snippet})
                if len(hits) >= max_hits:
                    return {"query": q, "hits": hits, "count": len(hits), "truncated": True,
                            "hint": "narrow the query, or skillflow_docs_read(topic, "
                                    "start_line, end_line) around a hit."}
    return {"query": q, "hits": hits, "count": len(hits),
            "hint": "skillflow_docs_read(topic, start_line, end_line) around a hit line "
                    "for full context."}


def read_doc(topic: str, start_line: int = 0, end_line: int | None = None,
             max_lines: int = 400) -> dict:
    """Read a topic WITH LINE NUMBERS (so it couples with search's line hits).
    Give start_line/end_line (1-based, inclusive) to read a region around a hit."""
    p = _topic_path(topic)
    if p is None:
        return {"error": f"unknown/absent topic '{topic}'", "topics": list(TOPICS.keys())}
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    start = max(1, int(start_line or 1))
    end = int(end_line) if end_line else start + max_lines - 1
    end = min(end, total, start + max_lines - 1)
    body = "\n".join(f"{n}: {lines[n - 1]}" for n in range(start, end + 1))
    res = {"topic": topic, "path": TOPICS[topic][0], "total_lines": total,
           "start_line": start, "end_line": end, "content": body}
    if end < total:
        res["note"] = (f"showing lines {start}-{end} of {total}; call again with "
                       f"start_line={end + 1} to continue, or narrow via skillflow_docs_search.")
    return res
