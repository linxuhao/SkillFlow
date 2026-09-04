"""prompt_delta events keep the full message so a step can be REPLAYED.

Everything else keeps the 20K head+tail clip: a per-turn prompt is the whole
history again and is redundant with the deltas that built it.
"""
from skillflow.core import SkillFlow


def _sf():
    return SkillFlow.__new__(SkillFlow)


def test_a_full_event_keeps_a_24k_read_result_intact():
    body = "r" * 24000
    assert _sf()._clip(body, SkillFlow._TRACE_FULL_MAX_FIELD) == body


def test_a_full_event_is_still_bounded():
    out = _sf()._clip("z" * 1_000_000, SkillFlow._TRACE_FULL_MAX_FIELD)
    assert len(out) < SkillFlow._TRACE_FULL_MAX_FIELD + 200
    assert "[clipped" in out and out.endswith("z" * 4000)


def test_default_clip_is_unchanged():
    out = _sf()._clip("a" * 50000)
    assert "head 16000 + tail 4000 of 50000 kept" in out


def test_prompt_delta_is_the_full_event():
    assert "prompt_delta" in SkillFlow._TRACE_FULL_EVENTS
