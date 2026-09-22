"""Seeing what is in a file must not cost a turn per page of it.

Measured on privacy r1 (attempt-8c8461af46e74cef952e99ab1dd50a04): the read
window is 24,000 characters, `core/state_commands.py` is 28,420 characters over
681 lines, and it was read TWELVE times in a 100-turn budget — 10 of those
twelve re-serving lines the run already had, 891 lines in total. Two more of
its files, `core/state_service.py` at 57 KB and `core/state_graph.py` at 41 KB,
are over the same line.

The window itself is not the thing to move. Raising it moves the same line
further out and spends the context budget instead of the turn budget, and the
turn-per-page cost comes back at the next file size. What is wrong is that
learning WHERE something is in a file was only purchasable by transporting the
file, one window at a time.

So two claims, each with its pole:

  1. Finding out what is in a file costs ONE call whatever its size. `outline`
     returns every definition and the lines it spans, so the read that follows
     it is a read of the range that matters rather than of page 1.
  2. When a window IS truncated, the cost of the rest is stated — before the
     caller has spent a turn discovering it, and in `list` before the first
     read of the file at all. Never a silently short window.
"""
from pathlib import Path

import pytest

from skillflow import citations, read_accounting
from skillflow.read_tools import (_MAX_READ_CHARS, unified_list, unified_read)

RUN = "run-read-window"
# The real file the measurement came from. Present on the host this card was
# worked on; the synthetic case below carries the same claims everywhere else.
STATE_COMMANDS = Path.home() / "AItelier" / "core" / "state_commands.py"


class Ignition:
    def __init__(self):
        self.count = 0

    def bump_if(self, condition):
        if condition:
            self.count += 1
        return condition


def smap_for(root):
    return {"working_tree": [("repo", str(root))], "named": {}, "allowed": set()}


def read(root, path, **kw):
    return unified_read(smap_for(root), path, run_id=RUN, **kw)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "small.py").write_text("def a():\n    return 1\n")
    # Comfortably over one window, with definitions spread the whole way down.
    body = []
    for n in range(1, 61):
        body.append(f"class Thing{n}:")
        body.append(f'    """{"padding " * 40}"""')
        body.append(f"    def method_{n}(self):")
        body.append(f'        return {n}  # {"x" * 60}')
        body.append("")
    (tmp_path / "large.py").write_text("\n".join(body) + "\n")
    citations.forget_run(RUN)
    read_accounting.forget_run(RUN)
    yield tmp_path
    citations.forget_run(RUN)
    read_accounting.forget_run(RUN)


# ===========================================================================
# pole 1: a file under the window comes back whole, in one call
# ===========================================================================

def test_a_file_under_the_window_is_served_whole_in_one_read(repo):
    out = read(repo, "small.py")
    assert out["truncated"] is False
    assert out["returned_lines"] == out["total_lines"] == 2
    assert "pages_remaining" not in out
    assert read_accounting.summary(RUN)["reads"] == 1


# ===========================================================================
# pole 2: a file over the window is mapped in one call, not paged into view
# ===========================================================================

def test_the_whole_structure_of_a_large_file_comes_back_in_one_call(repo):
    size = (repo / "large.py").stat().st_size
    assert size > _MAX_READ_CHARS, "the fixture must exceed one window"
    paged = read(repo, "large.py")
    assert paged["truncated"] is True
    assert paged["pages_remaining"] >= 1

    mapped = read(repo, "large.py", outline=True)
    assert mapped["definitions"] == 120           # 60 classes + 60 methods
    assert mapped["outline_method"] == "python-ast"
    assert mapped["total_lines"] == paged["total_lines"]
    # The LAST definition in the file is in this one call's answer — the part
    # a paged reader would have reached last, or never.
    last = mapped["outline"][-1]
    assert last["name"] == "method_60"
    assert last["end_line"] > paged["start_line"] + paged["returned_lines"]

    # ...and it takes one further call to hold that definition's text.
    body = read(repo, "large.py", start_line=last["start_line"] - 1,
                end_line=last["end_line"])
    assert "def method_60" in body["content"]
    assert body["returned_lines"] == last["end_line"] - last["start_line"] + 1
    assert read_accounting.summary(RUN)["reads"] == 3


def test_the_cost_of_the_rest_is_stated_rather_than_discovered(repo):
    out = read(repo, "large.py")
    assert out["truncated"] is True
    assert out["truncated_by"] == "characters"
    assert out["next_start_line"] == out["start_line"] + out["returned_lines"]
    assert out["pages_remaining"] >= 1
    assert "outline=true" in out["hint"]

    # Walking the pages must cost exactly what was quoted.
    quoted = out["pages_remaining"]
    spent, at = 0, out["next_start_line"]
    while True:
        page = read(repo, "large.py", start_line=at)
        spent += 1
        if not page["truncated"]:
            break
        at = page["next_start_line"]
    assert spent == quoted
    # ...and paging is never charged as re-reading.
    assert read_accounting.summary(RUN)["repaid_reads"] == 0


def test_the_price_is_on_the_listing_before_the_first_read(repo):
    import json
    files = {e["name"]: e for e in json.loads(unified_list(smap_for(repo)))["files"]}
    assert files["large.py"]["over_read_window"] is True
    assert files["large.py"]["read_pages"] >= 2
    assert "read_pages" not in files["small.py"]


def test_a_truncated_window_never_looks_complete(repo):
    """Reading short while letting the caller believe it read everything is
    worse than any number of extra turns."""
    out = read(repo, "large.py")
    assert out["truncated"] is True
    assert out["returned_lines"] < out["total_lines"]
    assert len(out["content"]) <= _MAX_READ_CHARS
    # the citation covers exactly what was served, not what was asked for
    assert out["citation"]["end_line"] == out["start_line"] + out["returned_lines"]


# ===========================================================================
# the real file the criterion names
# ===========================================================================

@pytest.mark.skipif(not STATE_COMMANDS.is_file(),
                    reason="AItelier's core/state_commands.py is not on this host")
def test_the_file_that_was_read_twelve_times_is_mapped_in_one_call():
    root = STATE_COMMANDS.parent.parent
    size = STATE_COMMANDS.stat().st_size
    assert size > _MAX_READ_CHARS

    # What one window actually reaches, measured on its own ledger so it does
    # not become a read this test then has to explain away.
    first_window = unified_read(smap_for(root), "core/state_commands.py",
                                run_id="ruler")
    read_accounting.forget_run("ruler")
    assert first_window["truncated"] is True

    mapped = read(root, "core/state_commands.py", outline=True)
    assert mapped["outline_method"] == "python-ast"
    assert mapped["definitions"] > 20
    assert mapped["pages_if_read_whole"] >= 2
    # Every definition in the file, including ones a first window never reaches.
    assert max(d["end_line"] for d in mapped["outline"]) > \
        first_window["returned_lines"]

    # One call to map it, one to fetch a definition past the first window:
    # two, against the twelve reads that file cost privacy r1.
    last = max(mapped["outline"], key=lambda d: d["end_line"])
    body = read(root, "core/state_commands.py",
                start_line=last["start_line"] - 1, end_line=last["end_line"])
    assert body["returned_lines"] == last["end_line"] - last["start_line"] + 1
    assert read_accounting.summary(RUN)["reads"] == 2
    assert read_accounting.summary(RUN)["repaid_reads"] == 0


# ===========================================================================
# mutation: without the outline, the structure costs a page at a time again
# ===========================================================================

def test_without_the_outline_the_last_definition_needs_paging_to(repo,
                                                                 monkeypatch):
    import skillflow.read_tools as module
    fired = Ignition()
    real = module.outline

    def blinded(text):
        answer = real(text)
        # Fires only when there WAS a structure to return, so a mutation that
        # never runs cannot pass for one that ran and found nothing.
        fired.bump_if(bool(answer["outline"]))
        return {"outline": [], "outline_method": "disabled",
                "total_lines": answer["total_lines"], "definitions": 0,
                "pages_if_read_whole": answer["pages_if_read_whole"]}

    monkeypatch.setattr(module, "outline", blinded)
    mapped = read(repo, "large.py", outline=True)
    assert fired.count > 0, "the mutation never fired; this proves nothing"
    assert mapped["definitions"] == 0

    # With no map, the only way to the last definition is page by page.
    pages, at, found = 0, 0, False
    while True:
        page = read(repo, "large.py", start_line=at)
        pages += 1
        found = found or "def method_60" in page["content"]
        if not page["truncated"]:
            break
        at = page["next_start_line"]
    assert found and pages > 1, (
        "the fixture must take more than one page, or the mutation is untested")
