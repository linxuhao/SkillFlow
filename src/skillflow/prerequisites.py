"""Fail-closed materialization of frozen execution prerequisites.

The framework owns ordering: these checks run before a host admits execution.
Hosts own the read-only probes because Git repositories, report stores and
runtime registries are deliberately outside SkillFlow's domain.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from skillflow.exceptions import SkillFlowError

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class FrozenPrerequisiteError(SkillFlowError):
    """A frozen requirement was malformed, unavailable or mismatched."""

    def __init__(self, message: str, report: dict | None = None):
        super().__init__(message)
        self.report = report or {"version": 1, "checks": [], "passed": False}


def _json_value(value: Any, label: str) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise FrozenPrerequisiteError(f"{label} must be JSON-safe") from exc
    if len(encoded) > 65536:
        raise FrozenPrerequisiteError(f"{label} exceeds 65536 encoded characters")
    return value


def _shape(spec: dict) -> list[dict]:
    if not isinstance(spec, dict) or set(spec) != {"version", "checks"}:
        raise FrozenPrerequisiteError(
            "frozen prerequisites must contain exactly version and checks")
    if spec["version"] != 1 or type(spec["version"]) is not int:
        raise FrozenPrerequisiteError("unsupported frozen prerequisite version")
    checks = spec["checks"]
    if not isinstance(checks, list) or not 1 <= len(checks) <= 64:
        raise FrozenPrerequisiteError("frozen prerequisite checks must be a list of 1-64 entries")
    seen: set[str] = set()
    for index, check in enumerate(checks):
        if (not isinstance(check, dict)
                or set(check) != {"id", "probe", "arguments", "expected"}):
            raise FrozenPrerequisiteError(
                f"frozen prerequisite check {index} has an unknown shape")
        cid, probe = check["id"], check["probe"]
        if not isinstance(cid, str) or not _NAME.fullmatch(cid) or cid in seen:
            raise FrozenPrerequisiteError(
                f"frozen prerequisite check {index} has an invalid or duplicate id")
        if not isinstance(probe, str) or not _NAME.fullmatch(probe):
            raise FrozenPrerequisiteError(
                f"frozen prerequisite check {cid!r} has an invalid probe")
        if not isinstance(check["arguments"], dict):
            raise FrozenPrerequisiteError(
                f"frozen prerequisite check {cid!r} arguments must be an object")
        _json_value(check["arguments"], f"check {cid!r} arguments")
        _json_value(check["expected"], f"check {cid!r} expected identity")
        seen.add(cid)
    return checks


def materialize_frozen_prerequisites(
    spec: dict,
    probes: Mapping[str, Callable[[dict], Any]],
    *,
    trace: Callable[[dict], None] | None = None,
) -> dict:
    """Materialize and compare exact identities, stopping at first failure.

    Shape validation completes before any probe runs. Each successful probe is
    compared by canonical JSON identity. A missing/raising probe is a refusal,
    never an implicit capability. ``trace`` receives required and actual values
    before a mismatch is raised, so a host can durably record the refusal.
    """
    checks = _shape(spec)
    if not isinstance(probes, Mapping):
        raise FrozenPrerequisiteError("prerequisite probes must be a mapping")
    report = {"version": 1, "checks": [], "passed": False}
    for check in checks:
        event = {
            "id": check["id"],
            "probe": check["probe"],
            "required": check["expected"],
        }
        fn = probes.get(check["probe"])
        if not callable(fn):
            event.update(actual=None, passed=False,
                         error="runtime probe is unavailable")
        else:
            try:
                actual = _json_value(fn(dict(check["arguments"])),
                                     f"check {check['id']!r} actual identity")
                event.update(actual=actual,
                             passed=actual == check["expected"])
                if not event["passed"]:
                    event["error"] = "required and actual identities differ"
            except Exception as exc:  # noqa: BLE001 - host probes are arbitrary callables
                if isinstance(exc, FrozenPrerequisiteError):
                    message = str(exc)
                else:
                    message = f"probe failed: {type(exc).__name__}: {exc}"
                event.update(actual=None, passed=False, error=message[:1000])
        report["checks"].append(event)
        if trace is not None:
            try:
                trace(dict(event))
            except Exception as exc:
                raise FrozenPrerequisiteError(
                    f"could not record frozen prerequisite trace: {type(exc).__name__}",
                    report) from exc
        if not event["passed"]:
            raise FrozenPrerequisiteError(
                f"frozen prerequisite {check['id']!r} refused: {event['error']}",
                report)
    report["passed"] = True
    return report
