"""Validate JSON files against an inline JSON Schema."""

import json
from pathlib import Path


def json_schema(files: list[str], inline_schema: dict, *,
                workspace_root: str = "") -> dict:
    try:
        import jsonschema as _jsonschema
        _validate = _jsonschema.validate
    except ImportError:
        # Fallback: basic required-field check
        def _validate(instance, schema):
            required = schema.get("required", [])
            for field in required:
                if field not in instance:
                    raise ValueError(f"Missing required field: {field}")

    root = Path(workspace_root)
    results = []
    all_passed = True

    for pattern in files:
        matches = list(root.rglob(pattern)) if "*" in pattern else [root / pattern]
        for f in matches:
            if not f.exists():
                results.append({
                    "file": str(f.relative_to(root)),
                    "passed": False,
                    "error_message": "File not found"
                })
                all_passed = False
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                _validate(data, inline_schema)
                results.append({"file": str(f.relative_to(root)), "passed": True, "error_message": ""})
            except Exception as e:
                results.append({
                    "file": str(f.relative_to(root)),
                    "passed": False,
                    "error_message": str(e)
                })
                all_passed = False

    return {"all_passed": all_passed, "results": results}
