"""A traced prompt keeps its TAIL, because the tail is where the host puts
what the model must read last (feedback, validation error, turn budget).

Head-only clipping hid all of it: a 225K PM prompt traced as its first 20K
read as "the validation error never reached the agent" when it sat at line
1517 of 1519 (jinyong-numbers, 2026-09-01).
"""
from skillflow.core import SkillFlow


def _clip(s):
    return SkillFlow._clip(SkillFlow.__new__(SkillFlow), s)


def test_short_values_pass_through_untouched():
    assert _clip("x" * 100) == "x" * 100
    assert _clip(42) == 42


def test_the_tail_survives_the_clip():
    body = "H" * 30000 + "TAIL-MARKER-FEEDBACK-BLOCK" + "T" * 1000
    out = _clip(body)
    assert "TAIL-MARKER-FEEDBACK-BLOCK" in out
    assert out.endswith("T" * 1000)
    assert out.startswith("H" * 100)


def test_the_marker_names_what_was_dropped_and_what_was_kept():
    body = "a" * 50000
    out = _clip(body)
    assert "[clipped 30000 chars" in out
    assert "head 16000 + tail 4000 of 50000 kept" in out


def test_total_stays_bounded():
    out = _clip("z" * 1_000_000)
    assert len(out) < SkillFlow._TRACE_MAX_FIELD + 200   # kept text + marker


def test_exactly_at_the_limit_is_not_clipped():
    s = "q" * SkillFlow._TRACE_MAX_FIELD
    assert _clip(s) == s
