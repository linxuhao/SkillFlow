"""A tool gate's feedback banner must carry information.

The banner is the most prominent thing in a retried step's prompt — it is headed
"⚠️ Reviewer / User Feedback — MUST ADDRESS before resubmitting". For any tool that
reports its outcome WITHOUT an `error` key it read, verbatim, "Tool failed".

`run_tests` is exactly that shape: it returns `{"written": "test_report.json",
"passed": false}` and puts the pytest output in the report file. So a maker looping
back from a failed test gate was told, in the loudest place available, nothing at all
— and was told a TOOL had failed when what had failed was the TESTS. Observed live in
`gen_mcp_server_builder`: three consecutive fix rounds, the same banner every time,
then the loop exhausted. (The full report DID reach the step as ordinary context; the
banner was actively competing with it.)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skillflow.core import _describe_tool_failure as describe


class TestItPrefersARealError:
    def test_an_error_string_is_used_as_is(self):
        assert describe({"error": "graph not found: x.yaml"}) == "graph not found: x.yaml"

    def test_an_empty_error_is_not_used(self):
        out = describe({"error": "   ", "passed": False})
        assert out.strip() and "   " != out


class TestItDescribesTheResultWhenThereIsNoError:
    def test_run_tests_shape_names_the_report(self):
        out = describe({"written": "test_report.json", "passed": False})
        assert "test_report.json" in out
        assert "passed=False" in out
        assert out != "Tool failed"

    def test_a_summary_field_leads(self):
        out = describe({"summary": "3 failed, 1 passed", "passed": False})
        assert out.startswith("3 failed, 1 passed")

    def test_nested_values_do_not_bloat_the_banner(self):
        """A banner is a pointer, not a dump — the detail is in the artifact."""
        out = describe({"passed": False, "issues": [{"a": 1}] * 50,
                        "written": "report.json"})
        assert "issues" not in out
        assert len(out) < 300

    def test_it_degrades_gracefully(self):
        assert describe({}) == "Tool failed"
        assert describe(None) == "Tool failed"

    def test_a_passing_result_is_still_described_sensibly(self):
        """Feedback only fires on a rejecting edge, but the helper must not
        misbehave if a graph routes feedback on a pass."""
        assert "passed=True" in describe({"passed": True, "written": "r.json"})
