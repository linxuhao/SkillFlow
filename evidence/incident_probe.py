#!/usr/bin/env python3
"""Reproduce the 2026-09-05 incident against WHICHEVER skillflow is importable.

Uses only API that exists before the fix, so the same file runs against the
unpatched library (where it fails, printing the commit that should not exist)
and the patched one (where it passes). Nothing here imports TerminalRunFenced.

    PYTHONPATH=<tree>/src python evidence/incident_probe.py

Three properties, each one an observed fact from the incident:
  P1  a run stopped before its claimed step confirms must not commit
  P2  the claim the stop closed must not be left `claimed` forever
  P3  two concurrent get_or_create_run for one (project, graph) yield one run
"""
import subprocess, sys, tempfile, threading
from pathlib import Path

import skillflow
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.tool_loader import ToolLoader

TOOLS = Path(skillflow.__file__).parent / "tools"


def git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, capture_output=True,
                          text=True, check=True).stdout.strip()


def build(root):
    repo = root / "projects" / "p1"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("base\n")
    git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "base")
    node = StepNode(id="work", step_type="agent", agent_config="worker",
                    output_mode="write", transitions=[Transition(to=None)],
                    lifecycle={"on_deliver": [
                        {"tool": "repo_apply",
                         "params": {"source_dir": "$STEP_DIR"}}]})
    sf = SkillFlow(":memory:", tool_loader=ToolLoader(TOOLS),
                   workspace_base=str(root / "ws"),
                   projects_base=str(root / "projects"))
    sf.register_agent_config("worker", tools=["read_file"])
    sf.register_graph(PipelineGraph(name="g", begin="work", steps=[node]))
    return sf, repo


results = []


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        results.append((name, "FAIL", str(e)))
    except Exception as e:                                   # noqa: BLE001
        results.append((name, "ERROR", f"{type(e).__name__}: {e}"))
    else:
        results.append((name, "PASS", ""))


def p1():
    with tempfile.TemporaryDirectory() as d:
        sf, repo = build(Path(d))
        rid = sf.create_run("g", {"project_id": "p1"}, project_id="p1")
        sf.start_run(rid); sf.advance_run(rid)
        token = sf.claim_next_step(rid).token
        tmp = sf._workspace.get_step_tmp_dir("p1", "g", "work")
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / "DIAGNOSIS.md").write_text("appended by a stopped run\n")
        before = git(repo, "rev-parse", "HEAD")
        n = int(git(repo, "rev-list", "--count", "HEAD"))
        sf.fail_run(rid, "stopped by the operator")          # the stop
        try:
            sf.confirm_step(token, StepResult(outputs={}))   # 40s later, live
        except Exception:
            pass                                             # fenced = the fix
        after = git(repo, "rev-parse", "HEAD")
        n2 = int(git(repo, "rev-list", "--count", "HEAD"))
        assert after == before and n2 == n, (
            f"a stopped run committed: {before[:7]} -> {after[:7]} "
            f"({n} -> {n2} commits); log: "
            + git(repo, "log", "--oneline", "-1"))


def p2():
    with tempfile.TemporaryDirectory() as d:
        sf, _ = build(Path(d))
        rid = sf.create_run("g", {"project_id": "p1"}, project_id="p1")
        sf.start_run(rid); sf.advance_run(rid)
        sf.claim_next_step(rid)
        sf.fail_run(rid, "stopped")
        left = [dict(r) for r in sf._conn.execute(
            "SELECT step_id, status FROM skillflow_steps "
            "WHERE run_id = ? AND status = 'claimed'", (rid,)).fetchall()]
        assert not left, f"claim stranded by the stop: {left}"


def p3():
    with tempfile.TemporaryDirectory() as d:
        sf, _ = build(Path(d))
        ids, lock, go = [], threading.Lock(), threading.Barrier(6)

        def make():
            go.wait()
            r = sf.get_or_create_run("g", "p1", {"project_id": "p1"})
            with lock:
                ids.append(r)
        ts = [threading.Thread(target=make, daemon=True) for _ in range(6)]
        for t in ts: t.start()
        for t in ts: t.join(20)
        assert len(set(ids)) == 1, f"competing runs created: {sorted(set(ids))}"


print(f"skillflow under test: {skillflow.__file__}")
check("P1 stopped run does not commit", p1)
check("P2 stop leaves no stranded claim", p2)
check("P3 one run per (project, graph)", p3)
width = max(len(n) for n, _, _ in results)
for name, verdict, detail in results:
    print(f"{name.ljust(width)}  {verdict}" + (f"  {detail}" if detail else ""))
sys.exit(0 if all(v == "PASS" for _, v, _ in results) else 1)
