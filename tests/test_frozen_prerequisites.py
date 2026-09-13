import pytest

from skillflow.core import SkillFlow
from skillflow.prerequisites import (
    FrozenPrerequisiteError,
    materialize_frozen_prerequisites,
    require_available_capability_identity,
)


def spec(*checks):
    return {"version": 1, "checks": list(checks)}


def check(cid, probe, expected, **arguments):
    return {"id": cid, "probe": probe, "arguments": arguments,
            "expected": expected}


def test_mismatch_traces_required_and_actual_then_stops_before_later_probe():
    calls, trace = [], []
    probes = {
        "source_head": lambda args: calls.append("source") or "c6446bd6146a70704a94a57356c6f4dabbad4298",
        "expensive_gate": lambda args: calls.append("gate") or True,
    }
    with pytest.raises(FrozenPrerequisiteError) as caught:
        materialize_frozen_prerequisites(spec(
            check("source-base", "source_head", "91621acd58b7cf16f20aaae15c35a34001e826c5"),
            check("gate", "expensive_gate", True),
        ), probes, trace=trace.append)
    assert calls == ["source"]
    assert trace == [{
        "id": "source-base", "probe": "source_head",
        "required": "91621acd58b7cf16f20aaae15c35a34001e826c5",
        "actual": "c6446bd6146a70704a94a57356c6f4dabbad4298",
        "passed": False, "error": "required and actual identities differ",
    }]
    assert caught.value.report["checks"] == trace


@pytest.mark.parametrize("bad", [
    {},
    {"version": 2, "checks": []},
    {"version": 1, "checks": [{"id": "x", "probe": "p", "arguments": {},
                                  "expected": 1, "extra": True}]},
    {"version": 1, "checks": [{"id": "x", "probe": "p", "arguments": [],
                                  "expected": 1}]},
])
def test_unknown_or_malformed_shapes_fail_before_any_probe(bad):
    called = []
    with pytest.raises(FrozenPrerequisiteError):
        materialize_frozen_prerequisites(bad, {"p": lambda args: called.append(args)})
    assert called == []


def test_missing_and_raising_probes_fail_closed_with_trace():
    for probes in ({}, {"capability": lambda args: (_ for _ in ()).throw(OSError("offline"))}):
        trace = []
        with pytest.raises(FrozenPrerequisiteError):
            materialize_frozen_prerequisites(
                spec(check("runtime", "capability", {"name": "x"}, name="x")),
                probes, trace=trace.append)
        assert trace[0]["actual"] is None and trace[0]["passed"] is False


def test_success_returns_complete_exact_identity_report():
    trace = []
    report = materialize_frozen_prerequisites(spec(
        check("input", "sha256_file", "a" * 64, path="/tmp/report"),
        check("runtime", "capability", {"name": "review", "tools": ["read"]}, name="review"),
    ), {
        "sha256_file": lambda args: "a" * 64,
        "capability": lambda args: {"name": args["name"], "tools": ["read"]},
    }, trace=trace.append)
    assert report["passed"] is True
    assert len(report["checks"]) == len(trace) == 2


def test_runtime_capability_identity_includes_resolved_tool_schema(tmp_path):
    class Loader:
        @staticmethod
        def load_schema(name):
            if name == "missing":
                raise FileNotFoundError(name)
            return {"name": name, "parameters": {"type": "object"}}

    sf = SkillFlow(str(tmp_path / "engine.db"), tool_loader=Loader())
    sf.register_capability("ready", tools=["inspect"], owner="host")
    sf.register_capability("broken", tools=["missing"], owner="host")
    ready = sf.capability_identity("ready")
    assert ready["available"] is True
    assert len(ready["tool_schema_sha256"]["inspect"]) == 64
    assert sf.capability_identity("broken") == {
        "name": "broken", "tools": ["missing"], "briefing": "", "owner": "host",
        "available": False, "tool_schema_sha256": {"missing": None},
    }
    assert sf.capability_identity("absent") is None


@pytest.mark.parametrize("identity", [
    None,
    {"name": "review", "tools": ["inspect"], "briefing": "", "owner": "host",
     "available": False, "tool_schema_sha256": {"inspect": None}},
    "review",
    {"name": "review", "available": True},
    {"name": "wrong", "tools": [], "briefing": "", "owner": "host",
     "available": True, "tool_schema_sha256": {}},
    {"name": "review", "tools": ["inspect"], "briefing": "", "owner": "host",
     "available": True, "tool_schema_sha256": {"inspect": "not-a-digest"}},
    {"name": "review", "tools": ["inspect", "inspect"], "briefing": "", "owner": "host",
     "available": True, "tool_schema_sha256": {"inspect": "a" * 64}},
])
def test_invalid_capability_identity_is_never_made_valid_by_mirroring(identity):
    with pytest.raises(FrozenPrerequisiteError):
        require_available_capability_identity(identity, "review")


def test_available_capability_identity_is_canonical_and_accepted():
    identity = {"name": "review", "tools": ["inspect"], "briefing": "read only",
                "owner": "host", "available": True,
                "tool_schema_sha256": {"inspect": "a" * 64}}
    assert require_available_capability_identity(identity, "review") is identity


def test_semantic_validator_retains_mirrored_invalid_actual_in_refusal_trace():
    invalid = {"name": "review", "tools": ["inspect"], "briefing": "",
               "owner": "host", "available": False,
               "tool_schema_sha256": {"inspect": None}}
    trace = []
    with pytest.raises(FrozenPrerequisiteError):
        materialize_frozen_prerequisites(
            spec(check("runtime", "capability", invalid, name="review")),
            {"capability": lambda arguments: invalid},
            validators={"capability": lambda actual, arguments:
                        require_available_capability_identity(actual, arguments["name"])},
            trace=trace.append,
        )
    assert trace[0]["required"] == invalid
    assert trace[0]["actual"] == invalid
    assert trace[0]["passed"] is False
