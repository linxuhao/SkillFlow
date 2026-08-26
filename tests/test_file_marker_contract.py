"""The file-boundary marker is a CROSS-REPO contract, so it needs a test here.

AItelier's context clipper parses these headers to decide which files it had to
cut and to name them in a manifest. Its parser (`core/prompt_assembler.py`,
`_FILE_HEADER_RE`) only matches `### FILE: <relpath>`.

Until this file existed, nothing on the skillflow side bound the marker at all:
setting `_FILE_MARKER` back to the bare `"### "` kept the whole suite green, and
the only assertion holding the contract lived in the other repo — where it reads
the INSTALLED skillflow, so it passes against an editable checkout on a dev box
while the deployed container emits something else. That skew shipped once
(1.5.42 emitting bare, AItelier already parsing `FILE:`), and the symptom was
silent: no manifest, files dropped whole with nothing saying so.

So these assert on what the resolver EMITS, not on the constant. Changing the
constant alone cannot make them pass.
"""

import re

from skillflow.context import ContextResolver

# Byte-for-byte AItelier's `_FILE_HEADER_RE`. If you change one, change both.
AITELIER_HEADER_RE = re.compile(r"^###\s+FILE:\s+(\S.*?)\s*$")


def _step(ws, config, step_id, files):
    d = ws / config / step_id
    d.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        f = d / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
    return d


def _headers(text):
    return [m.group(1) for line in text.splitlines()
            if (m := AITELIER_HEADER_RE.match(line))]


def test_whole_directory_headers_parse_as_the_consumer_parses_them(tmp_path):
    ws = tmp_path / "workspace"
    _step(ws, "c", "1", {
        "a.md": "# Heading in content\nbody",
        "design/nested.md": "nested body",
        ".gitignore": "*.pyc",
        "Dockerfile": "FROM python",
    })
    resolver = ContextResolver(ws)
    result = resolver.resolve([{"source": {"step": "1"}}], current_config="c")
    body = list(result.values())[0]

    # Every file is announced, and by its path relative to the step dir.
    assert _headers(body) == [".gitignore", "Dockerfile", "a.md",
                              "design/nested.md"]
    # A markdown heading inside a file's CONTENT is not mistaken for one.
    assert "# Heading in content" in body


def test_glob_and_all_items_headers_carry_the_marker_too(tmp_path):
    """Three emitters, three shapes. A partial conversion is the failure this
    catches: the whole-dir branch is the one people test by hand."""
    ws = tmp_path / "workspace"
    _step(ws, "c", "t", {"one/out.json": "{}", "two/out.json": "{}"})
    resolver = ContextResolver(ws)

    glob = resolver.resolve(
        [{"source": {"step": "t", "output": "*/out.json"}}], current_config="c")
    assert _headers(list(glob.values())[0]) == ["one/out.json", "two/out.json"]

    # The aggregate-across-loop-items branch: reached only from OUTSIDE the
    # loop that produced `t`, which is what `_loop_of` without a matching item
    # spells.
    every = resolver.resolve(
        [{"source": {"step": "t", "output": "out.json"}}], current_config="c",
        loop_context={"_loop_of": {"t": "task_name"}})
    assert _headers(list(every.values())[0]) == ["one/out.json", "two/out.json"]


def test_no_emitter_writes_a_bare_marker(tmp_path):
    """A structural backstop for the two emitters that spell the path inline.

    AItelier's own guard greps for `f"### {rel}` only, so a reintroduction using
    `f"### {f.relative_to(step_dir)}"` slips past it. Match the shape instead of
    one spelling.
    """
    import inspect

    from skillflow import context

    src = inspect.getsource(context)
    bare = re.findall(r'f"### \{', src)
    assert not bare, (
        f"{len(bare)} bare-marker emitter(s) in skillflow.context — use "
        f"_FILE_MARKER, or AItelier's clipper silently stops seeing file "
        f"boundaries")
