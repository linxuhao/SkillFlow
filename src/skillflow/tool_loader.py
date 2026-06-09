"""Tool loader — dynamic import of tool schemas and implementations.

Tools live in ``tools/{name}/`` directories under one or more tool paths:
- ``tool.yaml``: name, description, parameters schema
- ``impl.py``: Python function matching the tool name

Supports multiple tool directories — native tools (skillflow built-in) and
custom tools (host application).  First match wins on name conflict.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Callable


class ToolLoader:
    """Loads tool schemas and implementations from one or more tool directories.

    Usage::

        loader = ToolLoader(Path("skillflow/tools"))
        loader.add_tools_dir(Path("aitelier/tools"))  # custom
        schema = loader.load_schema("read_file")
        fn = loader.load_fn("read_file")
    """

    def __init__(self, *tools_dirs: Path):
        self._tools_dirs: list[Path] = [Path(d) for d in tools_dirs]
        self._cache: dict[str, tuple[dict, Callable]] = {}
        self._tool_dir_cache: dict[str, Path] = {}  # name → which dir

    def add_tools_dir(self, path: Path):
        """Register an additional tools directory (searched last)."""
        p = Path(path)
        if p not in self._tools_dirs:
            self._tools_dirs.append(p)
        self._cache.clear()
        self._tool_dir_cache.clear()

    def _find_tool_dir(self, name: str) -> Path | None:
        if name in self._tool_dir_cache:
            return self._tool_dir_cache[name]
        for d in self._tools_dirs:
            if (d / name / "tool.yaml").exists():
                self._tool_dir_cache[name] = d
                return d
        return None

    def is_native(self, name: str) -> bool:
        """True if the tool lives in the first (native) tools directory."""
        if not self._tools_dirs:
            return False
        tool_dir = self._find_tool_dir(name)
        # Dynamic tools (registered via register_dynamic_tool) are also native
        if tool_dir is None and name in self._cache:
            return True
        return tool_dir is not None and tool_dir == self._tools_dirs[0]

    def register_dynamic_tool(self, name: str, schema: dict, fn: Callable) -> None:
        """Register a tool that isn't backed by a tool.yaml on disk.

        Dynamically generated tools (e.g. read_step_1_sota from context specs)
        are registered here so load_schema/load_fn work without file I/O.
        """
        self._cache[name] = (schema, fn)

    def is_dynamic(self, name: str) -> bool:
        """True if the tool was registered via register_dynamic_tool."""
        if name not in self._cache:
            return False
        # Dynamic tools have no tool_dir on disk — we check by trying to find one
        return self._find_tool_dir(name) is None

    def load_schema(self, name: str) -> dict:
        """Load tool.yaml for a tool. Returns parsed dict."""
        if name not in self._cache or self._cache[name][0] is None:
            tool_dir = self._find_tool_dir(name)
            if not tool_dir:
                searched = ", ".join(str(d) for d in self._tools_dirs)
                raise ImportError(
                    f"Tool '{name}' not found in any tools directory: [{searched}]"
                )
            import yaml

            yaml_path = tool_dir / name / "tool.yaml"
            schema = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            existing = self._cache.get(name, (None, None))
            self._cache[name] = (schema, existing[1])
        return self._cache[name][0]

    def load_fn(self, name: str) -> Callable:
        """Dynamic import of tool implementation.

        Returns the function named ``{name}`` from ``impl.py``.
        """
        if name in self._cache and self._cache[name][1] is not None:
            return self._cache[name][1]

        tool_dir = self._find_tool_dir(name)
        if not tool_dir:
            searched = ", ".join(str(d) for d in self._tools_dirs)
            raise ImportError(
                f"Tool '{name}': not found in any tools directory: [{searched}]"
            )

        impl_path = tool_dir / name / "impl.py"
        if not impl_path.exists():
            raise ImportError(
                f"Tool '{name}': impl.py not found at {impl_path}"
            )

        spec = importlib.util.spec_from_file_location(name, impl_path)
        if spec is None or spec.loader is None:
            raise ImportError(
                f"Tool '{name}': could not create module spec from {impl_path}"
            )

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        fn = getattr(module, name, None)
        if fn is None:
            raise ImportError(
                f"Tool '{name}': impl.py must export function '{name}'"
            )

        schema = self._cache[name][0] if name in self._cache else {}
        self._cache[name] = (schema, fn)
        return fn

    def list_tools(self) -> list[str]:
        """List available tool names across all directories (deduplicated)."""
        names: set[str] = set()
        for d in self._tools_dirs:
            if d.exists():
                for sub in d.iterdir():
                    if sub.is_dir() and (sub / "tool.yaml").exists():
                        names.add(sub.name)
        return sorted(names)
