"""The claim-identity comments must keep matching the SQL they describe.

`_step_tools` is keyed by (run_id, step_id) and identified by the PAIR
(step_instance_id, claim_epoch), and both `_release_step_tools` and the
declaration site argue for that pair from a fact about the SQL in this file:
every statement that resets a step row to 'pending' leaves `claim_epoch` alone,
so successive claims of one ROW are told apart only by the epoch.

The comment enumerated SIX such statements and there are seven —
`_reopen_tool_step_in_tx` was missing, i.e. exactly the tool-step path this round
touches. A miscount is harmless on its own; what is not harmless is that the
enumeration is the evidence for the identity scheme, and a reader who trusts it
believes a smaller set of paths can produce a re-claim than actually can.

So: count them here, and check the load-bearing half mechanically.
"""
import re
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "src" / "skillflow" / "core.py"

# The statements as they read today. Named by the method they live in, so a
# failure says which one moved rather than "the number changed".
EXPECTED_RESETTERS = {
    "reactivate_run",
    "_handle_validation_failure",
    "_handle_lifecycle_retry",
    "_fail_step_in_tx",
    "_reopen_tool_step_in_tx",
    "reject_checkpoint",
    "recover_stale_claims",
    # Hands a claim back when the EXECUTOR went away rather than the step
    # failing (a cancelled driver). Like the others it leaves claim_epoch
    # alone — the next claim bumps it, which is what fences the dead executor.
    "release_claim",
}


def _method_at(lines: list[str], index: int) -> str:
    for i in range(index, -1, -1):
        m = re.match(r"    def (\w+)", lines[i])
        if m:
            return m.group(1)
    return "<module>"


def _pending_resets() -> dict[str, list[int]]:
    lines = CORE.read_text(encoding="utf-8").splitlines()
    found: dict[str, list[int]] = {}
    for i, line in enumerate(lines):
        # An UPDATE that WRITES 'pending', not one that reads it in a WHERE.
        if not re.search(r"SET\s+status\s*=\s*'pending'|status = 'pending',",
                         line):
            continue
        if "WHERE" in line.split("status = 'pending'")[0]:
            continue
        found.setdefault(_method_at(lines, i), []).append(i + 1)
    return found


def test_exactly_the_enumerated_statements_reset_a_row_to_pending():
    found = _pending_resets()
    assert set(found) == EXPECTED_RESETTERS, (
        "the set of statements that reset a step row to 'pending' changed.\n"
        f"found: {found}\n"
        "Update EXPECTED_RESETTERS *and* the enumeration in the `_step_tools` "
        "comment in core.py — that list is the argument for identifying a claim "
        "by (step_instance_id, claim_epoch).")


# There was a test here that grepped core.py for the spelled-out count ("EIGHT
# UPDATEs…"). It gave false confidence: it passed while the sentence it guarded
# said "eight sites" and "none of the SEVEN writes" in the same breath, because
# it only ever compared two fixed substrings. Prose is checked by reading it.


def test_no_pending_reset_writes_claim_epoch():
    """The load-bearing half: a re-claim keeps the row id and only bumps epoch."""
    lines = CORE.read_text(encoding="utf-8").splitlines()
    offenders = []
    for method, line_nos in _pending_resets().items():
        for n in line_nos:
            # The statement is one SQL string; scan forward to its end.
            body = "\n".join(lines[n - 1:n + 12])
            body = body.split('"""')[0] if body.startswith('"""') else body
            stmt = body.split(")\n")[0]
            if "claim_epoch" in stmt:
                offenders.append((method, n))
    assert not offenders, (
        f"a 'pending' reset now writes claim_epoch: {offenders}. If that is "
        "deliberate, the (instance id, epoch) pair no longer identifies a claim "
        "and both `_release_step_tools` and `_evict_ended_step_tools` need "
        "revisiting.")


def test_only_the_two_claim_paths_bump_the_epoch():
    lines = CORE.read_text(encoding="utf-8").splitlines()
    bumpers = set()
    for i, line in enumerate(lines):
        if "claim_epoch = COALESCE(claim_epoch, 0) + 1" in line:
            bumpers.add(_method_at(lines, i))
    assert bumpers == {"claim_next_step", "_claim_tool_step_in_tx"}, bumpers
