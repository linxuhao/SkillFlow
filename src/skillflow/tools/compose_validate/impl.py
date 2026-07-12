"""compose_validate — validate an addon overlay by actually composing it.

The acceptance test for a machine-authored overlay (see the addon_converter
pipeline) is not a static lint: it is whether the overlay, applied to its real
base graph, yields a VALID graph. This tool composes overlay onto base via
``skillflow.compose`` and runs the same ``PipelineGraph.validate()`` that
``register_graph`` gates on, so a bad anchor reference, a colliding step id, or a
resulting unreachable/cyclic graph is caught here instead of at run time.

Loadable via ToolLoader as tool_name="compose_validate". Pure over its inputs
(overlay + base graph, by path or content) so it needs no live SkillFlow — the
host seeds the base graph dict (``graph.to_dict()``, anchors intact) alongside
the overlay.
"""

from __future__ import annotations

import json
from pathlib import Path


def _load_dict(content: str | None, path: str | None, label: str) -> dict:
    """Parse YAML/JSON from content or path into a mapping."""
    if content is None and path:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"{label} not found: {path}")
        content = p.read_text(encoding="utf-8")
    if content is None:
        raise ValueError(f"{label}: provide {label.lower()}_content or {label.lower()}_path")
    import yaml
    data = yaml.safe_load(content)
    if not isinstance(data, dict):
        raise ValueError(f"{label} is not a mapping")
    return data


def compose_validate(**kwargs) -> dict:
    """Compose an overlay onto its base and validate the result.

    Parameters (all optional, but each pair needs one side):
        overlay_path / overlay_content: the overlay spec.
        base_path / base_content: the base graph dict (to_dict, anchors intact).
        out_dir: where to write compose_report.json.

    Returns:
        {passed: bool, errors: [str, ...], summary: str}
    """
    from skillflow.compose import compose_graph, ComposeError
    from skillflow.graph import PipelineGraph

    errors: list[str] = []
    overlay: dict = {}
    base: dict = {}

    # ── Load inputs ──────────────────────────────────────────────────
    try:
        overlay = _load_dict(kwargs.get("overlay_content"),
                             kwargs.get("overlay_path"), "Overlay")
    except Exception as e:
        errors.append(f"overlay: {e}")
    try:
        base = _load_dict(kwargs.get("base_content"),
                          kwargs.get("base_path"), "Base")
    except Exception as e:
        errors.append(f"base: {e}")

    passed = False
    summary = ""
    if errors:
        summary = "Could not load inputs; overlay not composed."
    else:
        # Base-binding check mirrors SkillFlow.compose_config: an overlay that
        # declares a `base:` must target THIS base, else its @anchors are
        # meaningless against it.
        declared = overlay.get("base", "")
        base_name = base.get("name", "")
        if declared and base_name and declared != base_name:
            errors.append(
                f"overlay binds to base '{declared}', not '{base_name}'")

        if not isinstance(overlay.get("overlay"), list) or not overlay["overlay"]:
            errors.append("overlay: 'overlay' must be a non-empty list of ops")

        if not errors:
            try:
                merged = compose_graph(base, [overlay])
                # Force a stable name so validation doesn't trip on a missing one.
                merged.setdefault("name", overlay.get("name") or base_name or "composed")
                graph = PipelineGraph._from_dict(merged)
                issues = graph.validate()
                if issues:
                    errors.extend(issues)
                else:
                    passed = True
            except ComposeError as e:
                errors.append(f"compose failed: {e}")
            except Exception as e:  # malformed op / _from_dict blowup
                errors.append(f"{type(e).__name__}: {e}")

        if passed:
            n_ops = len(overlay.get("overlay", []))
            summary = (f"Overlay '{overlay.get('name', '?')}' composes cleanly onto "
                       f"base '{base_name}' ({n_ops} op(s)); resulting graph is valid.")
        else:
            summary = (f"Overlay '{overlay.get('name', '?')}' failed to compose/validate "
                       f"onto base '{base_name}': {len(errors)} error(s).")

    result = {"passed": passed, "errors": errors, "summary": summary}

    # ── Persist report (downstream context) ──────────────────────────
    out_dir = kwargs.get("out_dir")
    if out_dir:
        try:
            d = Path(out_dir)
            d.mkdir(parents=True, exist_ok=True)
            (d / "compose_report.json").write_text(
                json.dumps(result, indent=2), encoding="utf-8")
        except OSError:
            pass

    return result
