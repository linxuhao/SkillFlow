"""Closed Git: repository metadata without repository-selected programs.

Engine-side Git consumers (code output delivery, legacy repo_apply/draft
commits, artifact history) must not run a hook, filter, fsmonitor, signing,
pager, diff, credential or transport program that a repository, an inherited
``GIT_*`` variable or a global/system config selected. Disabling hooks is not
enough: ``git add``/``status``/``diff`` against working files run configured
clean filters and fsmonitor. So:

* every Git process gets ``closed_env()``: inherited ``GIT_*`` routing/config
  removed (author/committer identity kept), system config off, the global
  config replaced by a generated file holding only identity, safe.directory and
  excludes-file DATA copied from the host's global/system config, and
  ``CLOSED_CONFIG`` overriding the executable-valued keys;
* the commands used are metadata/object/ref operations that read no working
  file through Git (rev-parse, ls-tree, ls-files, check-ignore, check-attr,
  hash-object --no-filters --stdin, update-index --index-info on a private
  index, write-tree, commit-tree, update-ref, tree-to-tree diff, merge-base,
  log --pretty, init, config --get);
* working files are read here, raw and without following symlinks, and are
  compared/hashed as Git blobs in Python;
* a path whose Git attributes or config would convert its bytes (filter,
  working-tree-encoding, ident, CRLF) cannot be compared or committed without
  running that conversion, so it is refused with ``GitUnsupported``. Nothing
  falls back to native add/status/commit.

``ClosedRepo.publish`` replaces ``git add`` + ``git commit``: under an owned
``index.lock`` it captures the index, stages selected raw blobs into a private
copy of that index, writes the tree, creates a hook/signature-free commit,
compare-and-sets HEAD, then installs the private copy as the index.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

TIMEOUT = 60

# Command-line config has the highest precedence, so these override any
# local/included repository value of the same key.
CLOSED_CONFIG = (
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "core.splitIndex=false",
    "core.pager=cat",
    "core.askPass=",
    "core.sshCommand=",
    "core.gitProxy=",
    "core.alternateRefsCommand=",
    "core.attributesFile=/dev/null",
    "credential.helper=",
    "protocol.allow=never",
    "diff.external=",
    "commit.gpgSign=false",
    "tag.gpgSign=false",
    "log.showSignature=false",
    "gc.auto=0",
    "maintenance.auto=false",
    "submodule.recurse=false",
    "fetch.recurseSubmodules=false",
)

_KEEP_GIT_ENV = {"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
                 "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE"}
_DROP_ENV = {"PAGER", "EDITOR", "VISUAL", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE"}
# Host config copied as data only; none of these names a program.
_HOST_DATA_KEYS = ("user.name", "user.email", "author.name", "author.email",
                   "committer.name", "committer.email", "core.excludesfile")
_HOST_MULTI_KEYS = ("safe.directory",)


class GitUnsupported(RuntimeError):
    """The closed policy cannot perform this without a repository program."""


def _host_config_text() -> str:
    """Data-only copy of host system/global config. Reading runs no program."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG_PARAMETERS",
                                "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
    values: dict[str, list[str]] = {}
    for scope in ("--system", "--global"):
        try:
            res = subprocess.run(["git", "config", scope, "--includes", "-z", "--list"],
                                 cwd="/", env=env, capture_output=True, timeout=TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            continue
        if res.returncode:
            continue
        for record in res.stdout.split(b"\0"):
            key, _, value = record.decode("utf-8", "surrogateescape").partition("\n")
            key = key.lower()
            if key in _HOST_DATA_KEYS:
                values[key] = [value]
            elif key in _HOST_MULTI_KEYS:
                values.setdefault(key, []).append(value)
    if "core.excludesfile" in values:
        values["core.excludesfile"] = [os.path.expanduser(values["core.excludesfile"][0])]
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
        default = os.path.join(xdg, "git", "ignore")
        if os.path.isfile(default):
            values["core.excludesfile"] = [default]
    lines = []
    for key in sorted(values):
        section, name = key.split(".", 1)
        lines.append(f"[{section}]")
        for value in values[key]:
            if "\n" in value or "\0" in value:
                continue
            quoted = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'\t{name} = "{quoted}"')
    return "\n".join(lines) + "\n"


def closed_env(global_config: str, index_file: str | None = None) -> dict:
    env = {k: v for k, v in os.environ.items()
           if (not k.startswith("GIT_") or k in _KEEP_GIT_ENV) and k not in _DROP_ENV}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": global_config,
                "GIT_ATTR_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
                "GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0",
                "GIT_PROTOCOL_FROM_USER": "0", "GIT_PAGER": "cat", "LC_ALL": "C",
                # Overrides every protocol.*.allow: no transport, even a lazy fetch.
                "GIT_ALLOW_PROTOCOL": "skillflow-closed-none"})
    if index_file is not None:
        env["GIT_INDEX_FILE"] = index_file
    return env


class _Profile:
    """One generated global-config file per use; removed on exit."""
    def __init__(self, identity: dict | None = None):
        self.identity = {k: v for k, v in (identity or {}).items() if k in _KEEP_GIT_ENV}

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="skillflow-closed-git-")
        self.config = os.path.join(self.dir, "config")
        with open(self.config, "w", encoding="utf-8") as stream:
            stream.write(_host_config_text())
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.dir, ignore_errors=True)


def run(cwd, *args: str, input: bytes | str | None = None, check: bool = True,
        index_file: str | None = None, profile: _Profile | None = None,
        text: bool = True, worktree: tuple[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run one Git command under the closed profile.

    This closes configuration, not commands: callers in the engine use only
    the metadata/object/ref commands listed in the module docstring.
    """
    if profile is None:
        with _Profile() as owned:
            return run(cwd, *args, input=input, check=check, index_file=index_file,
                       profile=owned, text=text, worktree=worktree)
    argv = ["git", "--no-pager"]
    if args[0] not in ("check-ignore", "check-attr"):  # these take paths, not pathspecs
        argv.append("--literal-pathspecs")
    for item in CLOSED_CONFIG:
        argv += ["-c", item]
    if text and isinstance(input, bytes):
        input = input.decode("utf-8", "surrogateescape")
    env = {**closed_env(profile.config, index_file), **profile.identity}
    if worktree is not None:
        # (git dir, empty directory): writing an index re-checks racily clean
        # entries against their working files, through the clean filter. With
        # an empty work tree Git can only find them missing, never read one.
        env["GIT_DIR"], env["GIT_WORK_TREE"] = worktree
        cwd = worktree[1]
    result = subprocess.run(argv + list(args), cwd=str(cwd), input=input,
                            capture_output=True, text=text, timeout=TIMEOUT, env=env)
    if check and result.returncode:
        err = result.stderr or result.stdout
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        raise RuntimeError(f"git {args[0]} failed: {err.strip()}")
    return result


def _split_z(data: bytes) -> list[str]:
    return [os.fsdecode(part) for part in data.split(b"\0") if part]


class ClosedRepo:
    """A worktree's metadata through the closed profile, files read raw."""

    def __init__(self, cwd: Path, profile: _Profile):
        self.p = profile
        top = self.git(cwd, "rev-parse", "--show-toplevel").strip()
        self.top = Path(top).resolve()
        self.format = self.git(self.top, "rev-parse", "--show-object-format").strip() or "sha1"
        self.zero = "0" * (64 if self.format == "sha256" else 40)
        self.filemode = self.config("core.filemode", "--type=bool") != "false"
        self.symlinks = self.config("core.symlinks", "--type=bool") != "false"
        self.autocrlf = (self.config("core.autocrlf") or "false").lower()

    def git(self, cwd, *args, **kw) -> str:
        return run(cwd, *args, profile=self.p, **kw).stdout

    def gitb(self, *args, **kw) -> bytes:
        return run(self.top, *args, profile=self.p, text=False, **kw).stdout

    def config(self, key: str, *flags: str) -> str | None:
        res = run(self.top, "config", *flags, "--get", key, profile=self.p, check=False)
        return res.stdout.strip() if res.returncode == 0 else None

    # ── metadata ─────────────────────────────────────────────────────────
    def head(self) -> str | None:
        res = run(self.top, "rev-parse", "--verify", "-q", "HEAD^{commit}",
                  profile=self.p, check=False)
        return res.stdout.strip() if res.returncode == 0 else None

    def tree_entries(self, rev: str | None) -> dict[str, tuple[str, str]]:
        if rev is None:
            return {}
        out = {}
        for rec in _split_z(self.gitb("ls-tree", "-r", "-z", "--full-tree", rev)):
            meta, _, path = rec.partition("\t")
            mode, _kind, oid = meta.split()
            out[path] = (mode, oid)
        return out

    def index_entries(self):
        """(stage-0 entries, paths Git treats as unchanged, unmerged paths)."""
        entries, frozen, unmerged = {}, set(), set()
        for rec in _split_z(self.gitb("ls-files", "-z", "--stage", "-v")):
            meta, _, path = rec.partition("\t")
            tag, mode, oid, stage = meta.split()
            if stage != "0":
                unmerged.add(path)
                continue
            entries[path] = (mode, oid)
            # Lowercase: assume-unchanged; S: skip-worktree. Status ignores both.
            if tag.islower() or tag.upper() == "S":
                frozen.add(path)
        return entries, frozen, unmerged

    def untracked(self) -> list[str]:
        return _split_z(self.gitb("ls-files", "-z", "--others", "--exclude-standard"))

    def ignored(self, paths) -> set[str]:
        """Paths excluded by ignore rules; tracked paths are never reported."""
        if not paths:
            return set()
        res = run(self.top, "check-ignore", "-z", "--stdin", input="\0".join(paths) + "\0",
                  profile=self.p, check=False)
        if res.returncode not in (0, 1):
            raise RuntimeError(f"git check-ignore failed: {res.stderr.strip()}")
        return set(filter(None, res.stdout.split("\0")))

    def attributes(self, paths) -> dict[str, dict[str, str]]:
        names = ("filter", "ident", "working-tree-encoding", "text", "eol", "crlf")
        out: dict[str, dict[str, str]] = {p: {} for p in paths}
        if not paths:
            return out
        raw = self.gitb("check-attr", "-z", "--stdin", *names,
                        input=b"".join(os.fsencode(p) + b"\0" for p in paths))
        fields = _split_z(raw)
        for i in range(0, len(fields) - 2, 3):
            out.setdefault(fields[i], {})[fields[i + 1]] = fields[i + 2]
        return out

    # ── raw working files ────────────────────────────────────────────────
    def read(self, rel: str):
        """(mode, bytes) of a working path without following any symlink.

        A path below a symlink or a non-directory, a directory or a special file
        is absent as a Git file, exactly as Git's own index comparison sees it.
        """
        parts = rel.split("/")
        fd = os.open(self.top, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parts[:-1]:
                try:
                    nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except (FileNotFoundError, NotADirectoryError):
                    return None
                except OSError as exc:
                    if exc.errno == 40:  # ELOOP: a symlinked directory
                        return None
                    raise
                os.close(fd)
                fd = nfd
            try:
                st = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
            except (FileNotFoundError, NotADirectoryError):
                return None
            if stat.S_ISLNK(st.st_mode):
                return "120000", os.fsencode(os.readlink(parts[-1], dir_fd=fd))
            if not stat.S_ISREG(st.st_mode):
                return None
            ffd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                st = os.fstat(ffd)
                if not stat.S_ISREG(st.st_mode):
                    return None
                chunks = []
                while chunk := os.read(ffd, 1 << 20):
                    chunks.append(chunk)
            finally:
                os.close(ffd)
            return ("100755" if st.st_mode & stat.S_IXUSR else "100644"), b"".join(chunks)
        finally:
            os.close(fd)

    def blob_id(self, data: bytes) -> str:
        h = hashlib.new(self.format)
        h.update(b"blob %d\0" % len(data))
        h.update(data)
        return h.hexdigest()

    def _mode(self, raw_mode: str, recorded: str | None) -> str:
        """The index mode Git would record, as in its ce_mode_from_stat."""
        if raw_mode == "120000":
            return raw_mode
        if not self.symlinks and recorded == "120000":
            return recorded
        if not self.filemode:
            return recorded if recorded in ("100644", "100755") else "100644"
        return raw_mode

    def _converted(self, attrs: dict[str, str], data: bytes) -> str | None:
        """Why Git would transform these bytes on their way into the index."""
        def given(name):
            return attrs.get(name, "unspecified") not in ("unspecified", "unset")
        if given("filter"):
            return f"filter={attrs['filter']}"
        if given("working-tree-encoding"):
            return f"working-tree-encoding={attrs['working-tree-encoding']}"
        if attrs.get("ident") == "set" and b"$Id" in data:
            return "ident"
        if b"\r" in data and attrs.get("text") != "unset" and attrs.get("crlf") != "unset":
            explicit = attrs.get("text") == "set" or given("eol") or attrs.get("crlf") == "set"
            automatic = (attrs.get("text") == "auto" or attrs.get("crlf") in ("input", "auto")
                         or self.autocrlf in ("true", "input"))
            if explicit or (automatic and b"\0" not in data):
                return "end-of-line conversion"
        return None

    def _refuse_conversions(self, contents: dict[str, bytes], why: str) -> None:
        attrs = self.attributes(sorted(contents))
        for path, data in sorted(contents.items()):
            reason = self._converted(attrs.get(path, {}), data)
            if reason:
                raise GitUnsupported(
                    f"{why} {path!r}: Git would apply {reason} to it, and the closed Git "
                    "policy does not run repository conversions or fall back to native Git")

    def worktree_changes(self, paths, entries, frozen):
        """{path: (mode, oid, bytes) | None} for selected paths differing from the index.

        None means deleted. Gitlinks and paths Git itself treats as unchanged
        are skipped, like `git status`.
        """
        changes, differing = {}, {}
        for path in sorted(paths):
            recorded = entries.get(path)
            if path in frozen or (recorded and recorded[0] == "160000"):
                continue
            current = self.read(path)
            if current is None:
                if recorded:
                    changes[path] = None
                continue
            mode = self._mode(current[0], recorded and recorded[0])
            oid = self.blob_id(current[1])
            if recorded != (mode, oid):
                changes[path] = (mode, oid, current[1])
                if recorded and recorded[1] != oid:
                    differing[path] = current[1]
        self._refuse_conversions(differing, "Cannot compare tracked path")
        return changes

    def dirty(self, head: str | None = None):
        """Every path `git status --untracked-files=all` would list, computed
        without letting Git read a working file."""
        head_entries = self.tree_entries(head if head is not None else self.head())
        entries, frozen, unmerged = self.index_entries()
        dirty = set(unmerged)
        for path in set(head_entries) | set(entries):
            if head_entries.get(path) != entries.get(path):
                dirty.add(path)
        dirty.update(self.worktree_changes(entries, entries, frozen))
        dirty.update(self.untracked())
        return dirty

    # ── publication ──────────────────────────────────────────────────────
    def index_path(self) -> Path:
        path = Path(self.git(self.top, "rev-parse", "--git-path", "index").strip())
        return path if path.is_absolute() else (self.top / path)

    def publish(self, select, message: str, *, before=None, on_commit=None) -> dict:
        """Commit selected working paths (raw bytes) like `git add` + `git commit`.

        ``select(path) -> bool`` picks candidate paths among index, HEAD and
        untracked paths; other index entries are committed as staged, exactly
        as `git commit` would. ``before(dirty, head)`` runs under the index
        lock and may refuse. ``on_commit(oid)`` runs after the commit object
        exists and before HEAD moves.
        """
        index = self.index_path()
        lock = Path(str(index) + ".lock")
        try:
            lock_fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError:
            raise RuntimeError(f"Git index is locked ({lock}); another Git process may be "
                               "running. The lock was not removed.") from None
        work = tempfile.mkdtemp(prefix="skillflow-index-")
        locked = fd_open = True
        try:
            snapshot = index.read_bytes() if index.exists() else None
            head = self.head()
            if before is not None:
                before(self.dirty(head), head)
            entries, frozen, unmerged = self.index_entries()
            head_entries = self.tree_entries(head)
            untracked = [p for p in self.untracked() if select(p)]
            candidates = {p for p in set(entries) | set(head_entries) if select(p)}
            candidates.update(untracked)
            blocked = sorted(candidates & unmerged)
            if blocked:
                raise GitUnsupported("Unmerged index entries cannot be published: " + ", ".join(blocked))
            gitlinks = sorted(p for p in candidates if entries.get(p, ("",))[0] == "160000")
            if gitlinks and before is not None:
                raise GitUnsupported("Submodule (gitlink) paths are not published by the closed "
                                     "Git policy: " + ", ".join(gitlinks))
            changes = self.worktree_changes(candidates, entries, frozen)
            self._refuse_conversions({p: c[2] for p, c in changes.items() if c is not None},
                                     "Cannot publish")
            private = os.path.join(work, "index")
            blind = os.path.join(work, "no-worktree")
            os.mkdir(blind)
            hidden = (self.git(self.top, "rev-parse", "--absolute-git-dir").strip(), blind)
            if snapshot is not None:
                with open(private, "wb") as stream:
                    stream.write(snapshot)
            if changes:
                lines = []
                for path, change in sorted(changes.items()):
                    if change is None:
                        lines.append(f"0 {self.zero}\t{path}")
                        continue
                    mode, oid, data = change
                    written = run(self.top, "hash-object", "-w", "--no-filters", "--stdin",
                                  input=data, profile=self.p, text=False).stdout.decode().strip()
                    if written != oid:
                        raise RuntimeError(f"Object id mismatch while storing {path!r}")
                    lines.append(f"{mode} {oid}\t{path}")
                run(self.top, "update-index", "-z", "--index-info", index_file=private,
                    input=b"".join(os.fsencode(line) + b"\0" for line in lines),
                    profile=self.p, text=False, worktree=hidden)
            tree = run(self.top, "write-tree", index_file=private, profile=self.p,
                       worktree=hidden).stdout.strip()
            head_tree = (self.git(self.top, "rev-parse", f"{head}^{{tree}}").strip()
                         if head else None)
            commit = None
            if tree != head_tree:
                args = ["commit-tree", "--no-gpg-sign", tree, "-m", message]
                if head:
                    args[2:2] = ["-p", head]
                commit = self.git(self.top, *args).strip()
                if on_commit is not None:
                    on_commit(commit)
                self.git(self.top, "update-ref", "-m", "commit: " + (message.splitlines() or [""])[0],
                         "HEAD", commit, head or self.zero)
            settled = Path(private).read_bytes() if os.path.exists(private) else snapshot
            if settled is not None and settled != snapshot:
                os.write(lock_fd, settled)
                os.fsync(lock_fd)
                os.close(lock_fd)
                fd_open = False
                os.replace(lock, index)
                locked = False
            return {"commit": commit or head, "committed": commit is not None,
                    "changed": sorted(changes)}
        finally:
            if fd_open:
                os.close(lock_fd)
            if locked:
                os.unlink(lock)
            shutil.rmtree(work, ignore_errors=True)


def open_repo(cwd: Path, identity: dict | None = None):
    """Context manager yielding a ClosedRepo for ``cwd``'s worktree.

    ``identity`` may supply GIT_AUTHOR_*/GIT_COMMITTER_* values for commits.
    """
    class _Ctx:
        def __enter__(self):
            self.profile = _Profile(identity).__enter__()
            try:
                return ClosedRepo(Path(cwd), self.profile)
            except BaseException:
                self.profile.__exit__(None, None, None)
                raise

        def __exit__(self, *exc):
            self.profile.__exit__(*exc)
    return _Ctx()
