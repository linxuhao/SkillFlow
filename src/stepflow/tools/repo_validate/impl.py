"""Run validation tools against the project repo."""

from pathlib import Path


def repo_validate(validations: list[dict], *, workspace_root: str = "",
                  project_root: str = "") -> dict:
    # Lazy-import ToolLoader to avoid circular imports
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "tool_loader",
        Path(__file__).parent.parent.parent / "tool_loader.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ToolLoader = module.ToolLoader

    tools_dir = Path(__file__).parent.parent
    loader = ToolLoader(tools_dir)

    proj = Path(project_root)
    results = []
    all_passed = True

    for v in validations:
        tool_name = v.get("tool", "")
        file_globs = v.get("files", [])
        if not tool_name:
            continue

        try:
            fn = loader.load_fn(tool_name)
        except ImportError as e:
            all_passed = False
            results.append(
                {"tool": tool_name, "file": "", "passed": False,
                 "error_message": f"Tool not found: {e}"}
            )
            continue

        for pattern in file_globs:
            matches = list(proj.rglob(pattern)) if "*" in pattern else [proj / pattern]
            if not any(m.exists() for m in matches):
                continue
            for f in matches:
                if not f.is_file():
                    continue
                try:
                    rel = str(f.relative_to(proj))
                    kwargs = {"file": rel, "workspace_root": str(proj)}
                    r = fn(**kwargs)
                    passed = r.get("verdict") == "passed" or r.get("passed", False)
                    if not passed:
                        all_passed = False
                    results.append({
                        "tool": tool_name, "file": rel, "passed": passed,
                        "error_message": r.get("feedback", r.get("error", ""))
                    })
                except Exception as e:
                    all_passed = False
                    results.append({
                        "tool": tool_name,
                        "file": str(f.relative_to(proj)),
                        "passed": False,
                        "error_message": str(e)
                    })

    return {"all_passed": all_passed, "results": results}
