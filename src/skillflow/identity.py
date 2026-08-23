"""Who holds a claim, and is that owner still alive?

``claimed_by`` used to be the string literal ``"worker"``. With no owner
recorded, "the worker crashed" and "the worker is alive but quiet" are
indistinguishable *by construction*, so the only question a reaper could ask
was "has it been silent long enough?" — one lease window of latency on every
crash, and no answer at all for a step whose node declares no timeout.

This module records a real owner and answers the other question directly:

    worker host=box pid=4711 boot=9f2c1a3e ns=4026532281 start=87342331

Two clocks, two signals, never merged (the discipline the reference system
states as "you cannot detect your own death, so someone else must; you can
detect your own tardiness, so nobody else should"):

* **death** — observed by a third party here, via the OS. Fast path.
* **tardiness** — the activity lease in ``recover_stale_claims``. Unchanged,
  and still the only answer for an owner whose liveness we cannot observe.

``owner_is_dead`` is deliberately three-valued: ``None`` means *unknown*, and
unknown always falls back to the lease. It never guesses.

Liveness observation is Linux-only (it reads ``/proc``). Elsewhere every
answer is ``None`` and behaviour is exactly what it was before this module
existed.
"""

from __future__ import annotations

import os
import socket

__all__ = ["worker_identity", "parse_identity", "owner_is_dead"]

_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"


def _read_boot_id() -> str | None:
    """The kernel's boot id, shortened.

    Stable across a *container* restart (which is the point — every pid from
    the previous container process is gone, and that is exactly the crash we
    want caught instantly) and different after a machine reboot.
    """
    try:
        with open(_BOOT_ID_PATH) as fh:
            return fh.read().strip().replace("-", "")[:12] or None
    except OSError:
        return None


def _read_pid_ns() -> str | None:
    """Inode of this process's PID namespace.

    Not used to prove death — only to refuse to conclude anything about a pid
    number that belongs to a different namespace than the one we can see.
    """
    try:
        return str(os.stat("/proc/self/ns/pid").st_ino)
    except OSError:
        return None


def _pid_starttime(pid: int) -> str | None:
    """Field 22 of ``/proc/<pid>/stat`` — ticks since boot at process start.

    The recycled-pid guard. A pid number on its own is not an identity: after a
    container restart pid 1 exists again and is a *different* process. Two
    processes sharing a pid number must also share this value to be confused,
    and it advances 100 times a second.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    # Field 2 (comm) is parenthesised and may contain spaces AND parens, so
    # split after the LAST ')' — the documented way to parse this file.
    close = data.rfind(b")")
    if close < 0:
        return None
    fields = data[close + 1:].split()
    if len(fields) < 20:      # fields[0] is state (field 3) → starttime is [19]
        return None
    return fields[19].decode("ascii", "replace")


_SELF: dict[str, str | int | None] | None = None


def _self_identity() -> dict:
    """This process's identity, resolved once (nothing in it can change)."""
    global _SELF
    if _SELF is None:
        pid = os.getpid()
        _SELF = {
            "host": socket.gethostname() or "unknown",
            "pid": pid,
            "boot": _read_boot_id(),
            "ns": _read_pid_ns(),
            "start": _pid_starttime(pid),
        }
    return _SELF


def worker_identity(role: str = "worker") -> str:
    """The string written to ``skillflow_steps.claimed_by`` at claim time.

    ``role`` keeps the old literals ("worker", "tool-inline") as the leading
    token, so existing logs and greps still read the same at a glance.
    """
    me = _self_identity()
    parts = [role, f"host={me['host']}", f"pid={me['pid']}"]
    for key in ("boot", "ns", "start"):
        if me.get(key):
            parts.append(f"{key}={me[key]}")
    return " ".join(parts)


def parse_identity(claimed_by: str | None) -> dict | None:
    """Parse a ``claimed_by`` string, or None if it carries no identity.

    Returns None for the pre-migration literals ("worker", "tool-inline"), for
    NULL, and for anything else without a ``pid=``. Those claims keep the lease
    as their only recovery path — an old row must not break the reaper.
    """
    if not claimed_by or not isinstance(claimed_by, str):
        return None
    ident: dict = {}
    tokens = claimed_by.split()
    if not tokens:
        return None
    ident["role"] = tokens[0]
    for token in tokens[1:]:
        key, sep, value = token.partition("=")
        if sep:
            ident[key] = value
    if "pid" not in ident:
        return None
    try:
        ident["pid"] = int(ident["pid"])
    except (TypeError, ValueError):
        return None
    return ident


def owner_is_dead(claimed_by: str | None) -> bool | None:
    """Is the process that made this claim gone?

    ``True``  — observed dead: reclaim now, do not wait for the lease.
    ``False`` — observed alive: it is still working, so there is nothing to
                recover and the lease does not apply. Silence is not death —
                a worker inside one long call emits nothing while it works —
                and a reaper that cannot tell a quiet worker from a gone one
                must not be what decides.
    ``None``  — not determinable: the lease decides, exactly as it always did.

    Only ``None`` reaches the lease, so this module changes nothing wherever
    liveness cannot be observed.
    """
    ident = parse_identity(claimed_by)
    if ident is None:
        return None
    me = _self_identity()

    # A different kernel boot cannot be probed: pid numbers from before a
    # reboot mean nothing here. It also costs nothing to defer — such a claim's
    # silence is at least as old as the reboot, so the lease has already fired.
    if not me.get("boot") or ident.get("boot") != me["boot"]:
        return None

    pid = ident["pid"]
    if pid <= 0:
        return None
    # No shortcut for "that pid is us": our own pid number can be a recycled
    # one too — after a container restart the new main process is pid 1 exactly
    # as the old one was. The start marker below is what tells them apart, and
    # skipping it for self would exempt the commonest case from its own guard.

    # Probed in OUR pid namespace, on purpose. A container restart gives the
    # new process a new namespace, so requiring `ns` to match first would make
    # the fast path miss the one crash that actually happens. The cost is the
    # unsupported case of two LIVE processes in different namespaces sharing
    # one skillflow DB: there, a pid absent from our namespace reads as dead.
    # That outcome is what the lease would have produced a window later anyway
    # — and the claim_epoch fence is what makes either reset safe.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True               # nothing holds that pid: dead
    except PermissionError:
        pass                      # exists, owned by someone else
    except OSError:
        return None

    # Something answers to that pid. Is it the SAME something?
    recorded = ident.get("start")
    actual = _pid_starttime(pid)
    if recorded and actual and recorded != actual:
        # Recycled. This is the container-restart case: the claim's pid exists
        # again in the new namespace and is a completely different process.
        return True

    # A pid number is only meaningful inside its own namespace. If the claim
    # came from another one, what we just probed was never the claimer.
    if ident.get("ns") and me.get("ns") and ident["ns"] != me["ns"]:
        return None
    return False
