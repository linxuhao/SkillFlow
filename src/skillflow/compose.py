"""Graph composition — overlays / addons.

Lets a *base* graph stay generic while *addon* fragments inject steps at named
extension points, instead of forking the whole graph or cramming every optional
concern into one config. Composition happens at the parsed-dict layer, BEFORE
``PipelineGraph._from_dict``, so the injected result goes through the normal
validation (reachability, cycle-safety, agent_config existence) in
``register_graph`` — a malformed overlay is rejected there, not silently merged.

Design
------
A base graph may declare named **anchors** mapping a stable public name to one of
its internal step ids::

    name: dpe_default
    anchors:
      post_verify_tests: 5_test      # "the edge out of the test step"
    steps: [...]

An **addon** declares a list of overlay operations::

    name: game_harness
    overlay:
      - insert_after: "@post_verify_tests"   # or a raw step id like "5_test"
        steps:
          - id: 5_compile
            step_type: tool
            tool_name: godot_compile
            tool_params: { out_dir: $STEP_DIR }
            # no transitions → auto-wired into the anchor's original target(s)

``insert_after`` splices a chain of steps immediately after the anchor node,
preserving the anchor's original outgoing edge on the LAST injected step. So
``A -> X`` with ``insert_after A [S1, S2]`` becomes ``A -> S1 -> S2 -> X``.
Injected steps that declare their own ``transitions`` keep them (e.g. a gate that
loops back); only steps left without transitions are auto-chained.

The one primitive ``insert_after`` covers the game-harness case (attach compile /
play-test gates into the verification stretch) and any "add a stage here" need.
"""
from __future__ import annotations

import copy


class ComposeError(ValueError):
    """Raised when an overlay cannot be applied to a base graph."""


def _resolve_anchor(ref: str, anchors: dict) -> str:
    """Map an ``@name`` anchor reference to a base step id; pass ids through."""
    if isinstance(ref, str) and ref.startswith("@"):
        name = ref[1:]
        if name not in anchors:
            raise ComposeError(f"unknown anchor '@{name}' (base declares: {sorted(anchors)})")
        return anchors[name]
    return ref


def _apply_insert_after(base: dict, anchor_id: str, new_steps: list[dict],
                        after_match: dict | None) -> None:
    steps = base["steps"]
    by_id = {s["id"]: s for s in steps}
    if anchor_id not in by_id:
        raise ComposeError(f"insert_after target '{anchor_id}' not found in base graph")
    if not new_steps:
        raise ComposeError(f"insert_after '{anchor_id}' has no steps to insert")

    new_ids = [s["id"] for s in new_steps]
    if len(new_ids) != len(set(new_ids)):
        raise ComposeError(f"duplicate ids within inserted chain: {new_ids}")
    for nid in new_ids:
        if nid in by_id:
            raise ComposeError(f"inserted step id '{nid}' collides with an existing step")

    anchor = by_id[anchor_id]
    trans = anchor.get("transitions", [])
    # Pick the single edge to reroute through the injected chain.
    if after_match is not None:
        candidates = [t for t in trans if t.get("match") == after_match]
        if len(candidates) != 1:
            raise ComposeError(
                f"insert_after '{anchor_id}': after_match matched {len(candidates)} "
                "transitions, need exactly 1")
        target = candidates[0]
    elif len(trans) == 0:
        target = None  # anchor was terminal; the chain becomes the new tail
    elif len(trans) == 1:
        target = trans[0]
    else:
        raise ComposeError(
            f"insert_after '{anchor_id}': anchor has {len(trans)} transitions — "
            "disambiguate with after_match")

    first_id = new_steps[0]["id"]
    # What the tail of the chain should point at = the anchor's original target.
    if target is None:
        tail_transitions = [{"to": None}]
        anchor["transitions"] = [{"to": first_id}]
    else:
        tail_transitions = [{"to": target["to"]}]
        target["to"] = first_id  # reroute the anchor's edge into the chain head

    chain = copy.deepcopy(new_steps)
    for i, s in enumerate(chain):
        if s.get("transitions"):
            continue  # honour explicit wiring (e.g. a loop-back gate)
        s["transitions"] = ([{"to": chain[i + 1]["id"]}] if i < len(chain) - 1
                            else tail_transitions)
    steps.extend(chain)


def _apply_add_context(base: dict, to_id: str, source: dict) -> None:
    """Append a context ``source`` to an existing step (so an injected step's
    output is visible to, e.g., a downstream reviewer). The source is wrapped in
    the ``{source: ...}`` context-entry convention skillflow graphs use."""
    by_id = {s["id"]: s for s in base["steps"]}
    if to_id not in by_id:
        raise ComposeError(f"add_context target '{to_id}' not found in base graph")
    step = by_id[to_id]
    ctx = step.setdefault("context", [])
    entry = {"source": source}
    if entry not in ctx:
        ctx.append(entry)


_OPS = {"insert_after", "add_context"}


def compose_graph(base: dict, overlays: list[dict]) -> dict:
    """Return a new graph dict = ``base`` with every overlay applied in order.

    ``base`` may carry an ``anchors`` map (stripped from the result — it is
    composition metadata, not part of the graph schema). Each overlay dict has an
    ``overlay: [op, ...]`` list. The input dicts are not mutated.
    """
    merged = copy.deepcopy(base)
    anchors = merged.pop("anchors", {}) or {}
    merged.setdefault("steps", [])

    for ov in overlays:
        ops = ov.get("overlay", [])
        if not isinstance(ops, list):
            raise ComposeError(f"overlay '{ov.get('name', '?')}': 'overlay' must be a list")
        for op in ops:
            action = next((k for k in op if k in _OPS), None)
            if action is None:
                raise ComposeError(
                    f"overlay '{ov.get('name', '?')}': op has no known action "
                    f"(supported: {sorted(_OPS)}) — got {sorted(op)}")
            if action == "insert_after":
                _apply_insert_after(
                    merged,
                    _resolve_anchor(op["insert_after"], anchors),
                    op.get("steps", []),
                    op.get("after_match"),
                )
            elif action == "add_context":
                _apply_add_context(
                    merged,
                    _resolve_anchor(op["add_context"], anchors),
                    op["source"],
                )
    return merged
