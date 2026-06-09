"""Context resolution from step config context specs.

Resolves ``context`` entries from a step node's config into assembled
content for prompt injection. Supports five source types:

- ``{config: "name", output: "file"}`` — cross-config read
- ``{config: "name", step: "id", output: "file"}`` — cross-config from specific step
- ``{step: "id", file: "name", mode: "full"|"summary"|"interfaces"}`` — same-config read
- ``{step: "id"}`` — all files from that step's directory
- ``{tool: "name"}`` — dynamic tool call (e.g. dir_tree)
"""

from __future__ import annotations

import logging
from pathlib import Path


class ContextResolver:
    """Resolves context sources into assembled content."""

    def __init__(self, workspace_root: Path, tool_loader=None):
        self._workspace_root = Path(workspace_root)
        self._tool_loader = tool_loader

    def resolve(self, specs: list[dict],
                current_config: str = "",
                loop_context: dict[str, str] | None = None) -> dict[str, str]:
        """Resolve a list of context specs into a dict of label→content.

        Returns a dict keyed by human-readable labels (e.g. "Project Brief",
        "Architecture Design") suitable for prompt assembly.

        If *loop_context* is provided, ``$variable`` references in ``file:``
        fields are substituted with the corresponding loop variable values.
        """
        result: dict[str, str] = {}
        for spec in specs:
            source = spec.get("source", spec)
            label, content = self._resolve_one(source, current_config, loop_context)
            if content:
                result[label] = content
        return result

    def _resolve_one(self, source: dict, current_config: str,
                     loop_context: dict[str, str] | None = None) -> tuple[str, str]:
        source_type = source.get("source_type", "")
        if source_type == "config" or "config" in source:
            return self._resolve_cross_config(source, current_config)
        if source_type == "step" or "step" in source:
            return self._resolve_step_output(source, current_config, loop_context)
        if source_type == "workspace":
            return self._resolve_workspace(source)
        if source_type == "repository":
            return self._resolve_repository(source)
        if "tool" in source:
            return self._resolve_tool(source)
        return "", ""

    def _resolve_cross_config(self, source: dict, current_config: str) -> tuple[str, str]:
        config_name = source["config"]
        output_file = source["output"]
        cfg_dir = self._workspace_root / config_name
        if not cfg_dir.exists():
            return "", ""

        # If step is specified, read from that step's directory
        if "step" in source:
            step_dir = cfg_dir / source["step"]
            if step_dir.exists() and step_dir.is_dir():
                file_path = step_dir / output_file
                if file_path.exists():
                    try:
                        content = file_path.read_text(encoding="utf-8", errors="replace")
                        label = f"{config_name}/{source['step']}/{output_file}"
                        return label, content
                    except Exception:
                        logger = logging.getLogger("skillflow.context")
                        logger.debug("Failed to read %s", file_path, exc_info=True)
                        return "", ""

        # Otherwise scan all step dirs (new-style) and legacy Outbox_Final_* dirs
        for d in sorted(cfg_dir.glob("*")):
            if d.name.endswith(".tmp") or d.name.startswith("Outbox_Draft"):
                continue
            if not d.is_dir():
                continue
            file_path = d / output_file
            if file_path.exists():
                try:
                    content = file_path.read_text(encoding="utf-8", errors="replace")
                    label = f"{config_name}/{output_file}"
                    return label, content
                except Exception:
                    continue

        return "", ""

    def _resolve_step_output(self, source: dict, current_config: str,
                              loop_context: dict[str, str] | None = None) -> tuple[str, str]:
        import re

        step_id = source["step"]
        output_file = source.get("output") or source.get("file")
        mode = source.get("mode", "full")
        cfg = current_config or "dpe_default"

        # Substitute $variable references from loop context
        if output_file and "$" in output_file and loop_context:
            def _sub(m):
                var_name = m.group(1)
                # Look up both [var_name] and plain var_name keys
                return loop_context.get(f"[{var_name}]",
                       loop_context.get(var_name, m.group(0)))
            output_file = re.sub(r'\$(\w+)', _sub, output_file)

        # New path: workspace/{project}/{config}/{step_id}/
        step_dir = self._workspace_root / cfg / step_id

        if not step_dir.exists() or not step_dir.is_dir():
            return "", ""

        # No specific file requested — return all files concatenated
        if not output_file:
            parts: list[str] = []
            for f in sorted(step_dir.rglob("*")):
                if f.is_file() and f.name != ".gitkeep":
                    try:
                        content = f.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        continue
                    rel = f.relative_to(step_dir)
                    parts.append(f"### {rel}\n{content}")
            if not parts:
                return "", ""
            label = f"Step {step_id}"
            return label, "\n\n".join(parts)

        # Specific file: glob for patterns like "tasks/*.json"
        if "*" in output_file:
            parts = []
            for f in sorted(step_dir.glob(output_file)):
                if f.is_file():
                    try:
                        content = f.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        continue
                    parts.append(f"### {f.name}\n{content}")
            if not parts:
                return "", ""
            label = f"Step {step_id} — {output_file}"
            return label, "\n\n".join(parts)

        file_path = step_dir / output_file
        if not file_path.exists():
            return "", ""

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return "", ""

        if mode == "summary":
            lines = content.splitlines()
            if len(lines) > 100:
                content = "\n".join(lines[:100]) + "\n... [summary truncated]"
        elif mode == "interfaces":
            content = self._extract_interfaces(content)

        label = f"Step {step_id} — {output_file}"
        return label, content

    def _resolve_workspace(self, source: dict) -> tuple[str, str]:
        """Resolve from: workspace source — read from project workspace root."""
        rel_path = source.get("path", "")
        abs_path = self._workspace_root / rel_path
        if abs_path.is_file():
            try:
                content = abs_path.read_text(encoding="utf-8", errors="replace")
                label = f"Workspace — {rel_path}"
                return label, content
            except Exception:
                return "", ""
        elif abs_path.is_dir():
            # Directory: concatenate all files (like step dir)
            parts: list[str] = []
            for f in sorted(abs_path.rglob("*")):
                if f.is_file() and f.name != ".gitkeep":
                    try:
                        content = f.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        continue
                    rel = f.relative_to(abs_path)
                    parts.append(f"### {rel}\n{content}")
            if not parts:
                return "", ""
            return f"Workspace — {rel_path}", "\n\n".join(parts)
        return "", ""

    def _resolve_repository(self, source: dict) -> tuple[str, str]:
        """Resolve from: repository source — read from code repository root.

        For mode=tool, returns empty (repos are large, tool-only).
        For mode=inline/both, concatenates matching files.
        """
        if source.get("mode") == "tool":
            return "", ""  # tool-only, no inline injection

        rel_path = source.get("path", "")
        abs_path = self._workspace_root / "project" / rel_path
        if not abs_path.exists():
            return "", ""
        if abs_path.is_file():
            try:
                content = abs_path.read_text(encoding="utf-8", errors="replace")
                return f"Repository — {rel_path}", content
            except Exception:
                return "", ""
        elif abs_path.is_dir():
            parts: list[str] = []
            for f in sorted(abs_path.rglob("*")):
                if f.is_file() and f.name != ".gitkeep":
                    # Skip binary-looking files
                    if f.suffix in (".pyc", ".pyo", ".so", ".o", ".bin"):
                        continue
                    try:
                        content = f.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        continue
                    rel = f.relative_to(abs_path)
                    parts.append(f"### {rel}\n{content}")
            if not parts:
                return "", ""
            return f"Repository — {rel_path}", "\n\n".join(parts)
        return "", ""

    def _resolve_tool(self, source: dict) -> tuple[str, str]:
        tool_name = source["tool"]
        if not self._tool_loader:
            return f"[{tool_name}]", ""

        try:
            fn = self._tool_loader.load_fn(tool_name)
            result = fn(
                workspace_root=str(self._workspace_root),
                project_root=str(self._workspace_root / "project"),
            )
            if isinstance(result, dict):
                content = result.get("tree", result.get("content", str(result)))
            else:
                content = str(result)
            label = f"[{tool_name}]"
            return label, content
        except Exception:
            return f"[{tool_name}]", ""

    @staticmethod
    def _extract_interfaces(content: str) -> str:
        """Extract API/interface sections from architecture docs."""
        import re
        lines = content.splitlines()
        result: list[str] = []
        interface_keywords = {
            "interface", "api", "contract", "endpoint", "module boundary",
            "component", "data flow", "interaction"
        }
        in_section = False
        section_depth = 0

        for line in lines:
            m = re.match(r'^(#{1,4})\s+(.*)', line)
            if m:
                header_text = m.group(2).lower()
                depth = len(m.group(1))
                if any(kw in header_text for kw in interface_keywords):
                    in_section = True
                    section_depth = depth
                    result.append(line)
                elif in_section and depth <= section_depth:
                    in_section = False
                    if any(kw in header_text for kw in interface_keywords):
                        in_section = True
                        section_depth = depth
                        result.append(line)
                elif in_section:
                    result.append(line)
            elif in_section:
                result.append(line)

        if not result:
            return "\n".join(lines[:150]) + "\n... [no interface sections found]"

        extracted = "\n".join(result)
        return extracted[:8000]
