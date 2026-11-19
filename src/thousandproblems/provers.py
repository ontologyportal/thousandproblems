"""Theorem provers to run problems against.

Three kinds of prover share the :class:`Prover` interface, whose ``run(problem, timeout)``
returns a :class:`RunResult` (status, runtime ...) and never raises for a prover's own failures:

* :class:`SubprocessProver` runs a binary, given by name (looked up on ``$PATH``) or by path,
  with command-line arguments from a dict. The problem reaches it through a temporary file or
  its standard input, and its TSTP output is read for the SZS status. Subclass it to describe
  one prover (:class:`EProver` and :class:`Vampire` do) or to say how to install it: a given
  version for a given platform, downloaded or built from source (see :mod:`thousandproblems.install`).
* :class:`BuiltInProver` runs a prover written in Python, or one reached through a Python
  binding, inside this process. It is handed the parsed problem.
* :class:`ProverCollection` is a named group of provers.

    >>> from thousandproblems.provers import SubprocessProver, ProverCollection
    >>> e = SubprocessProver("eprover", {"--auto": None, "--cpu-limit=": 10})    # doctest: +SKIP
    >>> result = e.run(problem, timeout=10)                                       # doctest: +SKIP
    >>> result.status, result.runtime                                             # doctest: +SKIP
    (<Status.PROVED: 'proved'>, 0.04)

See :mod:`thousandproblems.runner` to run many problems against many provers.
"""

from __future__ import annotations

import abc
import math
import os
import posixpath
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Sequence, Union

from .install import (InstallError, Platform, download, extract, github_latest_version, missing_tools,
                      run_build, version_key)
from .library import LibraryError, default_cache_dir
from .nodes import TPTPFile
from .problem import Problem
from .szs import Status, extract_output_block, parse_szs_status, status_from_szs

__all__ = [
    "BuiltInProver", "CallableProver", "EProver", "Platform", "Prover", "ProverCollection", "ProverError", "RunResult",
    "SolveContext", "Solution", "Status", "SubprocessProver", "Vampire", "PRESETS", "prover_from_spec",
]


class ProverError(Exception):
    """A prover could not be set up (a bad specification, or an installation that failed)."""


# ---------------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------------


@dataclass
class RunResult:
    """What happened when one prover ran on one problem.

    ``szs`` is the status name the prover printed (``'Theorem'``, ``'GaveUp'`` ...), if any, and
    ``status`` the :class:`Status` it boils down to. ``runtime`` is wall-clock seconds, from
    starting the prover to its end (for a subprocess that includes its start-up and parsing; for a
    built-in prover, not the time spent parsing the problem for it). ``expected`` is the status in
    the problem's header. ``solution`` is the proof or model between the ``SZS output`` lines.
    """

    problem: str
    prover: str
    status: Status
    szs: Optional[str] = None
    runtime: float = 0.0
    expected: Optional[str] = None
    returncode: Optional[int] = None
    command: Optional[list[str]] = None
    error: Optional[str] = None
    solution: Optional[str] = None
    stdout: str = ""
    stderr: str = ""

    @property
    def solved(self) -> bool:
        return self.status.solved

    @property
    def correct(self) -> Optional[bool]:
        """Whether a verdict agrees with the header's status: ``None`` if there was no verdict or
        the header gives no verdict to compare with."""
        expected = status_from_szs(self.expected)
        if not self.solved or not expected.solved:
            return None
        return self.status == expected

    def as_dict(self, *, output: bool = False) -> dict:
        data = {"problem": self.problem, "prover": self.prover, "status": str(self.status), "szs": self.szs,
                "runtime": round(self.runtime, 4), "expected": self.expected, "correct": self.correct,
                "returncode": self.returncode, "error": self.error, "command": self.command}
        if output:
            data.update(solution=self.solution, stdout=self.stdout, stderr=self.stderr)
        return data


# ---------------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------------


class Prover(abc.ABC):
    """Something that can be run on a problem. ``name`` identifies it in results."""

    name: str

    @abc.abstractmethod
    def run(self, problem: Problem, timeout: Optional[float] = None) -> RunResult:
        """Run on ``problem`` for at most ``timeout`` seconds (``None``: no limit)."""

    def available(self) -> bool:
        """Whether it can be run now (a subprocess prover's binary exists)."""
        return True

    def install(self) -> Any:
        """Make the prover available locally. Provers that know how say so by overriding this."""
        raise NotImplementedError(f"{self.name} has no installation procedure")

    def _result(self, problem: Problem, status: Status, **fields) -> RunResult:
        return RunResult(problem.name, self.name, status, expected=problem.expected_status, **fields)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name}>"


# ---------------------------------------------------------------------------------
# Subprocess provers
# ---------------------------------------------------------------------------------


def format_args(args: Mapping[str, Any]) -> list[str]:
    """Command-line words from a dict of options.

    * ``{"--auto": None}`` (or ``True``) gives the bare flag ``--auto``; ``False`` leaves it out;
    * ``{"-t": 10}`` gives ``-t 10`` (two words);
    * a key ending in ``=`` joins: ``{"--cpu-limit=": 10}`` gives ``--cpu-limit=10``;
    * a list or tuple repeats the flag: ``{"-I": ["a", "b"]}`` gives ``-I a -I b``.
    """
    words: list[str] = []
    for flag, value in args.items():
        if value is False:
            continue
        for item in value if isinstance(value, (list, tuple)) else [value]:
            if item is None or item is True:
                words.append(flag)
            elif flag.endswith("="):
                words.append(f"{flag}{item}")
            else:
                words += [flag, os.fspath(item) if isinstance(item, os.PathLike) else str(item)]
    return words


def _kill(process: subprocess.Popen) -> None:
    """Stop a prover and anything it started."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True)
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        process.kill()
    except OSError:
        pass


def _text(data: Optional[bytes]) -> str:
    return (data or b"").decode("utf-8", errors="replace")


class SubprocessProver(Prover):
    """A prover that is a program, run once per problem.

    :param binary: the program: a name looked up on ``$PATH``, or a path (absolute, or relative
        to the current folder). Subclasses can set the ``binary`` class attribute instead.
    :param args: command-line options as a dict, merged over the class's ``default_args``; see
        :func:`format_args` for how they are written.
    :param name: how results refer to this prover (default: the binary's name, with ``@version``
        if a version was asked for).
    :param input: ``"file"`` (the problem is written to a temporary file whose path ends the
        command line) or ``"stdin"`` (it is piped to the prover). Default: the class's ``input_mode``.
    :param extra_args: more command-line words, put after ``args`` and before the input.
    :param env: environment variables to add. When the problem has includes, ``TPTP`` points at a
        temporary folder that holds them.
    :param cwd: folder to run in (default: the current one).
    :param time_limit_arg: the option that sets the prover's own time limit, from the
        timeout given to :meth:`run`, e.g. ``"-t"`` or ``"--cpu-limit="`` (written as in :func:`format_args`).
        The prover is killed at the timeout whether it has such an option or not.
    :param version: the version to use: ``"5.1.0"``, or ``"latest"`` for the newest installed one.
        A version is only ever taken from a copy installed by :meth:`install`; without one, the
        binary on ``$PATH`` is used, and failing that the newest installed copy.
    :param platform: the platform to install for and look for, such as ``"linux-arm64"`` (see
        :class:`~thousandproblems.install.Platform`); default: this machine's.
    :param install_dir: where installed copies live (default: ``provers/NAME`` in the cache directory).

    To describe a prover, subclass it and set ``binary``, ``default_args``, ``input_mode``,
    ``time_limit_arg`` as needed; override :meth:`parse_output` if its output isn't plain TSTP, and
    say how to get it: see below.

    **Installing.** :meth:`install` fetches one *version* for one *platform* into
    ``install_dir/VERSION-PLATFORM/``, so versions and platforms live side by side. What it fetches comes
    from these class attributes:

    * ``download_url``: a URL, or a dict of URLs keyed by platform (``"linux-x86_64"``) or system
      (``"linux"``). A URL may contain ``{version}``, ``{tag}`` (``release_tag`` with the version in
      it), ``{repo}`` (``github_repo``), ``{os}``, ``{arch}`` and ``{platform}``. ``os_names`` and
      ``arch_names`` map our names (``macos``, ``arm64``) to the vendor's (``macOS``, ``ARM64``); when
      they are given, a platform missing from them is unsupported.
    * ``default_version``: what :meth:`install` fetches when no version was asked for.
      ``version="latest"`` asks upstream instead (``github_repo`` is looked up; override
      :meth:`latest_version` for anything else).
    * ``checksums``: ``{"VERSION/PLATFORM": sha256}`` or ``{"VERSION": sha256}``; a download is checked
      against its entry if there is one.
    * for a prover distributed as source: ``build_commands`` (run in the unpacked source), ``build_tools``
      (what must be on ``$PATH``; a tuple means any of) and ``binary_path`` (the built program, relative to
      the source). Only the binary is kept, unless ``keep_build_tree`` is set.

    A prover with a more involved setup overrides :meth:`install` itself.
    """

    binary: Optional[str] = None
    default_args: Mapping[str, Any] = {}
    input_mode: str = "file"
    #: with ``input="stdin"``, words that tell the prover to read it, e.g. ``("-",)``
    stdin_args: Sequence[str] = ()
    #: with ``input="file"``, an option put before the file's path (default: the path alone)
    input_flag: Optional[str] = None
    time_limit_arg: Optional[str] = None

    default_version: Optional[str] = None
    release_tag: str = "{version}"
    github_repo: Optional[str] = None
    download_url: Union[None, str, Mapping[str, str]] = None
    os_names: Mapping[str, str] = {}
    arch_names: Mapping[str, str] = {}
    checksums: Mapping[str, str] = {}
    build_commands: Sequence[Sequence[str]] = ()
    build_tools: Sequence[Union[str, Sequence[str]]] = ()
    binary_path: Optional[str] = None
    keep_build_tree: bool = False

    def __init__(self, binary: Optional[str] = None, args: Optional[Mapping[str, Any]] = None, *,
                 name: Optional[str] = None, input: Optional[str] = None, extra_args: Sequence[str] = (),
                 env: Optional[Mapping[str, str]] = None, cwd: Union[str, os.PathLike, None] = None,
                 time_limit_arg: Optional[str] = None, version: Optional[str] = None,
                 platform: Union[str, Platform, None] = None, install_dir: Union[str, os.PathLike, None] = None):
        if binary is not None:
            self.binary = os.fspath(binary)
        if self.binary is None:
            raise ProverError(f"{type(self).__name__} needs a binary: a name on $PATH or a path")
        self.args = {**self.default_args, **(args or {})}
        self.extra_args = list(extra_args)
        self.input_mode = input or self.input_mode
        if self.input_mode not in ("file", "stdin"):
            raise ProverError(f"input must be 'file' or 'stdin', not {self.input_mode!r}")
        self.env = dict(env or {})
        self.cwd = os.fspath(cwd) if cwd is not None else None
        if time_limit_arg is not None:
            self.time_limit_arg = time_limit_arg
        self._install_dir = Path(install_dir) if install_dir is not None else None
        self.version = self.normalize_version(version) if version else None
        try:
            self.platform = Platform.parse(platform) if platform else Platform.detect()
        except ValueError as e:
            raise ProverError(str(e)) from None
        stem = re.sub(r"\.exe$", "", Path(self.binary).name, flags=re.IGNORECASE)
        self.name = name or (f"{stem}@{self.version}" if self.version else stem)

    # -- versions -----------------------------------------------------------------------

    def normalize_version(self, version: str) -> str:
        """``"v5.1.0"`` or ``"E-3.5.10"`` (a tag) to ``"5.1.0"`` or ``"3.5.10"``; ``"latest"`` stays."""
        text = version.strip()
        if text.lower() == "latest":
            return "latest"
        before, _, after = self.release_tag.partition("{version}")
        if before and text.startswith(before):
            text = text[len(before):]
        if after and text.endswith(after):
            text = text[:-len(after)]
        return text[1:] if re.match(r"v\d", text) else text

    def latest_version(self) -> str:
        """The newest version upstream has (asks the network). Looks at ``github_repo``'s releases,
        then its tags; override for a prover published elsewhere."""
        if not self.github_repo:
            raise NotImplementedError(f"{self.name} has no github_repo: {type(self).__name__} "
                                      "must override latest_version() to support version='latest'")
        try:
            return github_latest_version(self.github_repo, self.release_tag)
        except InstallError as e:
            raise ProverError(str(e)) from None

    def resolve_version(self, version: Optional[str] = None) -> str:
        """A concrete version to install: the one given, else the one asked for at construction, else
        ``default_version``; ``"latest"`` is looked up. Prefers ``"current"`` if there is nothing to go on."""
        wanted = self.normalize_version(version) if version else self.version
        if wanted == "latest":
            return self.latest_version()
        return wanted or self.default_version or "current"

    # -- platforms and URLs -------------------------------------------------------------

    def artifact_url(self, version: str, platform: Optional[Union[str, Platform]] = None) -> str:
        """The URL that :meth:`install` downloads for ``version`` on ``platform`` (default: this
        prover's). Raises ``NotImplementedError`` if the class has no ``download_url`` and
        :class:`ProverError` if there is no build for the platform."""
        target = Platform.parse(platform) if platform else self.platform
        template = self.download_url
        if template is None:
            raise NotImplementedError(
                f"{self.name} cannot be installed automatically: {type(self).__name__} has no download_url "
                f"and does not override install(). Install it yourself and give its path.")
        if isinstance(template, Mapping):
            template = template.get(str(target)) or template.get(target.os)
            if template is None:
                raise ProverError(f"{type(self).__name__} has no download for {target}")
        for names, value, kind in ((self.os_names, target.os, "system"), (self.arch_names, target.arch, "CPU")):
            if names and value not in names:
                raise ProverError(f"{type(self).__name__} has no build for {target}: "
                                  f"unsupported {kind} {value!r} (it supports {', '.join(sorted(names))})")
        return template.format(
            version=version, tag=self.release_tag.format(version=version), repo=self.github_repo or "",
            os=self.os_names.get(target.os, target.os), arch=self.arch_names.get(target.arch, target.arch),
            platform=str(target))

    # -- finding the binary -----------------------------------------------------------------

    @property
    def install_dir(self) -> Path:
        """The folder that holds installed copies, one folder per version and platform."""
        if self._install_dir is not None:
            return self._install_dir
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(self.binary).stem)
        return default_cache_dir() / "provers" / slug

    def install_root(self, version: str) -> Path:
        """Where ``version`` is (or would be) installed for this prover's platform."""
        return self.install_dir / f"{version}-{self.platform}"

    def installed_versions(self) -> list[str]:
        """The versions installed for this platform, newest first."""
        found = []
        suffix = f"-{self.platform}"
        if self.install_dir.is_dir():
            for folder in self.install_dir.iterdir():
                if folder.is_dir() and folder.name.endswith(suffix) and self._binary_in(folder):
                    found.append(folder.name[:-len(suffix)])
        return sorted(found, key=version_key, reverse=True)

    def _binary_in(self, root: Path, *, executable: bool = True) -> Optional[str]:
        wanted = {Path(self.binary).name, f"{Path(self.binary).name}.exe"}
        for candidate in [root / "bin" / name for name in sorted(wanted)] + (
                sorted(root.rglob("*")) if root.is_dir() else []):
            if candidate.name in wanted and candidate.is_file() and (not executable or os.access(candidate, os.X_OK)):
                return str(candidate)
        return None

    def find_binary(self) -> Optional[str]:
        """The program's path, or ``None``.

        A path is used as given. A name is looked for on ``$PATH``, then among installed copies, newest
        first. If a version was asked for, only that installed version counts (``"latest"``: the newest
        installed one), so a pinned prover never quietly runs some other version.
        """
        spec = self.binary
        if os.sep in spec or (os.altsep and os.altsep in spec):
            path = Path(spec).expanduser()
            return str(path.resolve()) if path.is_file() and os.access(path, os.X_OK) else None
        if self.version is None:
            found = shutil.which(spec)
            if found:
                return found
        versions = self.installed_versions()
        if self.version not in (None, "latest"):
            versions = [v for v in versions if v == self.version]
        for version in versions:
            found = self._binary_in(self.install_root(version))
            if found:
                return found
        return None

    def available(self) -> bool:
        return self.find_binary() is not None

    # -- installing -----------------------------------------------------------------------

    def install(self, *, force: bool = False, sha256: Optional[str] = None, jobs: Optional[int] = None,
                log: Optional[Callable[[str], None]] = None) -> str:
        """Fetch (and, for a prover distributed as source, build) the prover; returns the binary's path.

        The version is the one asked for, else ``default_version`` (``"latest"`` is looked up); the
        platform is this prover's. It goes to ``install_dir/VERSION-PLATFORM/`` and is where
        :meth:`find_binary` looks afterwards. An installed version is kept unless ``force``. ``sha256``
        overrides the expected checksum, ``jobs`` the number of parallel build jobs, and ``log(message)``
        hears what is going on.

        Raises ``NotImplementedError`` for a prover that has no ``download_url`` and does not override this.
        """
        version = self.resolve_version()
        root = self.install_root(version)
        if not force:
            existing = self._binary_in(root)
            if existing:
                return existing
        url = self.artifact_url(version)
        building = bool(self.build_commands)
        if building:
            if self.platform != Platform.detect():
                raise ProverError(f"{self.name} is built from source, so it can only be installed for this "
                                  f"machine ({Platform.detect()}), not {self.platform}")
            missing = missing_tools(self.build_tools)
            if missing:
                raise ProverError(f"building {self.name} needs {', '.join(missing)} on $PATH")
        digest = sha256 or self.checksums.get(f"{version}/{self.platform}") or self.checksums.get(version)
        if digest is None and log:
            log(f"no checksum is known for {Path(self.binary).name} {version}; the download will not be verified")
        self.install_dir.mkdir(parents=True, exist_ok=True)
        work = root.with_name(root.name + ".partial")
        logfile = self.install_dir / f"{root.name}.build.log"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir()
        try:
            archive = work / ".download"
            download(url, archive, sha256=digest, log=log)
            if building:
                self._build(archive, work, jobs, log, logfile)
            elif not extract(archive, work):
                (work / "bin").mkdir()
                shutil.move(archive, work / "bin" / Path(self.binary).name)
            archive.unlink(missing_ok=True)
            built = self._binary_in(work, executable=False)       # a zip does not keep the executable bit
            if built is None:
                raise ProverError(f"fetched {url}, but found no {Path(self.binary).name!r} in it")
            Path(built).chmod(Path(built).stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            shutil.rmtree(root, ignore_errors=True)
            os.replace(work, root)
        except InstallError as e:
            raise ProverError(str(e)) from None
        finally:
            shutil.rmtree(work, ignore_errors=True)
        logfile.unlink(missing_ok=True)
        if log:
            log(f"installed {Path(self.binary).name} {version} for {self.platform} in {root}")
        return self._binary_in(root)

    def _build(self, archive: Path, work: Path, jobs, log, logfile: Path) -> None:
        source = work / "src"
        source.mkdir()
        if not extract(archive, source):
            raise InstallError(f"{self.name} is built from source, but its download is not a zip or tar archive")
        children = list(source.iterdir())
        top = children[0] if len(children) == 1 and children[0].is_dir() else source
        run_build(self.build_commands, top, jobs=jobs, log=log, logfile=logfile)
        program = top / self.binary_path if self.binary_path else next(
            (p for p in sorted(top.rglob(Path(self.binary).name)) if p.is_file()), None)
        if program is None or not program.is_file():
            raise InstallError(f"the build finished, but made no {self.binary_path or self.binary!r}")
        (work / "bin").mkdir()
        shutil.copy2(program, work / "bin" / Path(self.binary).name)
        if not self.keep_build_tree:
            shutil.rmtree(source)

    # -- running ----------------------------------------------------------------------

    def command(self, binary: str, input_path: Optional[str], timeout: Optional[float] = None) -> list[str]:
        """The command line: ``binary``, the options, ``extra_args``, then the problem file's
        path (``input_path``; ``None`` when it goes through stdin)."""
        args = dict(self.args)
        if timeout is not None and self.time_limit_arg:
            key = self.time_limit_arg.rstrip("=")
            if not any(k.rstrip("=") == key for k in args):
                args[self.time_limit_arg] = max(1, math.ceil(timeout))
        words = [binary, *format_args(args), *self.extra_args]
        if input_path is None:
            words += list(self.stdin_args)
        elif self.input_flag:
            words += format_args({self.input_flag: input_path})
        else:
            words.append(input_path)
        return words

    def parse_output(self, stdout: str, stderr: str, returncode: Optional[int]) -> Optional[str]:
        """The SZS status name the prover reported, or ``None``. The default reads the ``SZS status``
        line of its output; override it for a prover that says it some other way."""
        return parse_szs_status(stdout) or parse_szs_status(stderr)

    def run(self, problem: Problem, timeout: Optional[float] = None) -> RunResult:
        binary = self.find_binary()
        if binary is None:
            return self._result(problem, Status.ERROR, error=f"prover binary {self.binary!r} not found")
        use_stdin = self.input_mode == "stdin"
        with tempfile.TemporaryDirectory(prefix="thousandproblems-") as folder:
            main = problem.write_to(folder)
            command = self.command(binary, None if use_stdin else str(main), timeout)
            env = {**os.environ, **({"TPTP": folder} if len(problem.files) > 1 else {}), **self.env}
            extra = {"start_new_session": True} if sys.platform != "win32" else \
                {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            start = time.perf_counter()
            try:
                process = subprocess.Popen(
                    command, stdin=subprocess.PIPE if use_stdin else subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.cwd, env=env, **extra)
            except OSError as e:
                return self._result(problem, Status.ERROR, command=command,
                                    error=f"cannot start {binary}: {e.strerror or e}")
            timed_out = False
            try:
                out, err = process.communicate(problem.text.encode("utf-8") if use_stdin else None, timeout)
                runtime = time.perf_counter() - start
            except subprocess.TimeoutExpired:
                timed_out = True
                runtime = time.perf_counter() - start
                _kill(process)
                try:
                    out, err = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    out = err = None
            finally:
                if process.poll() is None:
                    _kill(process)
                    process.wait()
        stdout, stderr = _text(out), _text(err)
        szs = None if timed_out else self.parse_output(stdout, stderr, process.returncode)
        error = None
        if timed_out:
            status = Status.TIMEOUT
        elif szs:
            status = status_from_szs(szs)
            if status is Status.ERROR:
                error = _last_line(stderr) or f"the prover reported SZS status {szs}"
        elif process.returncode == 0:
            status = Status.UNKNOWN
        else:
            status = Status.ERROR
            how = (f"killed by signal {-process.returncode}" if process.returncode < 0
                   else f"exit status {process.returncode}")
            error = f"{how}, and no SZS status in the output" + (f": {_last_line(stderr) or _last_line(stdout)}"
                                                                 if (stderr or stdout).strip() else "")
        return self._result(problem, status, szs=szs, runtime=runtime, returncode=process.returncode,
                            command=command, error=error, solution=extract_output_block(stdout),
                            stdout=stdout, stderr=stderr)


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:300] if lines else ""


# ---------------------------------------------------------------------------------
# Provers shipped as presets
# ---------------------------------------------------------------------------------


class EProver(SubprocessProver):
    """E (``eprover``) in automatic mode, with its CPU limit set from the timeout.

    E publishes no binaries, so :meth:`install` builds a tagged version from the source on GitHub
    (``./configure && make``), which needs ``make`` and a C compiler.
    """

    binary = "eprover"
    default_args = {"--auto": None}
    time_limit_arg = "--cpu-limit="

    github_repo = "eprover/eprover"
    release_tag = "E-{version}"
    default_version = "3.5.10"
    download_url = "https://github.com/{repo}/archive/refs/tags/{tag}.tar.gz"
    build_tools = ("make", ("cc", "gcc", "clang"))
    build_commands = (("./configure",), ("make", "-j{jobs}"))
    binary_path = "PROVER/eprover"

    def parse_output(self, stdout, stderr, returncode):
        szs = super().parse_output(stdout, stderr, returncode)
        if szs == "ResourceOut" and "Resource limit exceeded (time)" in stdout:
            return "Timeout"
        return szs


class Vampire(SubprocessProver):
    """Vampire (``vampire``) in its default mode, with its time limit set from the timeout.

    :meth:`install` downloads the release build for this platform from GitHub.
    """

    binary = "vampire"
    time_limit_arg = "-t"

    github_repo = "vprover/vampire"
    release_tag = "v{version}"
    default_version = "5.1.0"
    download_url = "https://github.com/{repo}/releases/download/{tag}/vampire-{os}-{arch}.zip"
    os_names = {"linux": "Linux", "macos": "macOS", "windows": "Windows"}
    arch_names = {"x86_64": "X64", "arm64": "ARM64"}
    checksums = {
        "5.1.0/linux-arm64": "7abc39224fdf41bdb2c241f8b180a80b7c1a4bce1e46dc79df02cfea2798ad5c",
        "5.1.0/linux-x86_64": "495828dc76cb17a27080d62dedce755e46de96f47f06f5f0ad4be9cdaf6f968f",
        "5.1.0/macos-arm64": "02d88d0669813909e7838e0d05c278fade6e1125f48c68b75e107ce0456cd21b",
        "5.1.0/macos-x86_64": "2a23e57f4f65019f32f4cc13f38588e9e9668090b057d030cc94f505a6d0b6a3",
        "5.1.0/windows-arm64": "3d4a65aafd1caab203571b06f03ad2bab8b77c5616e17230781f03735c2e9de6",
        "5.1.0/windows-x86_64": "b98d0c6b4a42e11c25243ca31b3cbabd1673b437c994263166804b92b715de33",
    }

    def parse_output(self, stdout, stderr, returncode):
        szs = super().parse_output(stdout, stderr, returncode)
        if szs is None:        # outside CASC mode, Vampire gives up without an SZS status
            reason = re.search(r"^% Termination reason: (.*)$", stdout, re.MULTILINE)
            if reason:
                return {"Time limit": "Timeout", "Memory limit": "MemoryOut"}.get(
                    reason[1].strip(), "GaveUp" if "not found" in reason[1] else None)
        return szs


#: Provers that ``--prover NAME`` and :func:`prover_from_spec` know by the name of their binary.
PRESETS: dict[str, type[SubprocessProver]] = {"eprover": EProver, "vampire": Vampire}


_CHUNK = re.compile(r"""\"([^\"]*)\"|'([^']*)'|([^\s"']+)""")


def _split_windows(spec: str) -> list[str]:
    """Words of a command line the way a Windows shell reads it: backslashes are kept, and ``'..'``
    and ``".."`` group text, also inside a word (``--opt="a b"``), and are removed."""
    words: list[str] = []
    current: Optional[str] = None
    pos = 0
    while pos < len(spec):
        if spec[pos].isspace():
            if current is not None:
                words.append(current)
                current = None
            pos += 1
            continue
        chunk = _CHUNK.match(spec, pos)
        if chunk is None:
            raise ValueError("No closing quotation")
        current = (current or "") + next(g for g in chunk.groups() if g is not None)
        pos = chunk.end()
    if current is not None:
        words.append(current)
    return words


def prover_from_spec(spec: str, *, input: Optional[str] = None,
                     platform: Union[str, Platform, None] = None) -> SubprocessProver:
    """A subprocess prover from a command line: ``"eprover --auto --cpu-limit=10"``.

    The first word is the binary; the rest are its options, passed through as they are. A
    binary named like a preset (``eprover``, ``vampire``; see :data:`PRESETS`) gets that preset's
    defaults. ``"label=COMMAND"`` names the prover, which is how to run one binary with different
    options: ``"vampire-casc=vampire --mode casc"``. ``binary@VERSION`` asks for an installed version
    (see :meth:`SubprocessProver.install`): ``"eprover@3.5.10 --auto-schedule"``.
    """
    label = None
    if (m := re.match(r"([A-Za-z_][\w.+-]*)=(?=\S)", spec)):
        label, spec = m[1], spec[m.end():]
    try:
        words = _split_windows(spec) if sys.platform == "win32" else shlex.split(spec)
    except ValueError as e:
        raise ProverError(f"cannot read prover {spec!r}: {e}") from None
    if not words:
        raise ProverError("an empty prover specification")
    binary, rest = words[0], words[1:]
    stem = re.sub(r"\.exe$", "", posixpath.basename(binary.replace("\\", "/")), flags=re.IGNORECASE)
    head, at, version = binary.rpartition("@")
    if at and head and "/" not in version and "\\" not in version:
        binary, version = head, version
        stem = stem.rpartition("@")[0]
    else:
        version = None
    cls = PRESETS.get(stem, SubprocessProver)
    return cls(binary, name=label, extra_args=rest, input=input, version=version, platform=platform)


# ---------------------------------------------------------------------------------
# Provers in this process
# ---------------------------------------------------------------------------------


@dataclass
class Solution:
    """What :meth:`BuiltInProver.solve` returns. ``status`` is a :class:`Status`, or an SZS status
    name such as ``"Theorem"`` or ``"GaveUp"``; ``solution`` is a proof or model, if you have one."""

    status: Union[Status, str]
    solution: Optional[str] = None
    output: str = ""


class Cancelled(Exception):
    """Raised by :meth:`SolveContext.check` once the time is up."""


class SolveContext:
    """What a built-in prover is told about its run: ``problem`` (the :class:`Problem`), ``config``
    (the prover's settings), ``timeout`` and a ``cancelled`` event, set when time runs out. A
    prover cannot be stopped from outside, so a long search should call :meth:`check` regularly."""

    def __init__(self, problem: Problem, config: Mapping[str, Any], timeout: Optional[float]):
        self.problem = problem
        self.config = config
        self.timeout = timeout
        self.cancelled = threading.Event()
        self.started = time.perf_counter()

    def expired(self) -> bool:
        """Whether to stop: the time is up."""
        return self.cancelled.is_set() or (
            self.timeout is not None and time.perf_counter() - self.started >= self.timeout)

    def check(self) -> None:
        """Raise :class:`Cancelled` if the time is up."""
        if self.expired():
            raise Cancelled()


class BuiltInProver(Prover):
    """A prover that runs inside this Python process: one written in Python, or a Python binding
    to a compiled one (an FFI).

    Subclass it and implement :meth:`solve`, which receives the parsed problem and a
    :class:`SolveContext`::

        class Trivial(BuiltInProver):
            default_config = {"max_steps": 1000}

            def solve(self, problem, context):
                for formula in problem.formulae:
                    context.check()
                    ...
                return "Theorem"                 # or Status.PROVED, or Solution("Theorem", proof)

    :param name: how results refer to the prover (default: the class name).
    :param config: settings, merged over ``default_config``, and also given as keyword arguments;
        available as ``self.config`` and ``context.config``.

    Set ``expand_includes = False`` to be given the problem without its ``include`` lines replaced.
    The timeout is enforced by running :meth:`solve` in a worker thread and reporting a timeout when
    it has not returned in time; a thread cannot be killed, so code that keeps going afterwards
    should watch ``context.expired()``. For a hard limit use a :class:`SubprocessProver`.
    """

    default_config: Mapping[str, Any] = {}
    expand_includes: bool = True

    def __init__(self, name: Optional[str] = None, config: Optional[Mapping[str, Any]] = None, **options):
        self.name = name or type(self).__name__
        self.config = {**self.default_config, **(config or {}), **options}

    @abc.abstractmethod
    def solve(self, problem: TPTPFile, context: SolveContext) -> Union[Solution, Status, str, None]:
        """Decide ``problem``. Return a :class:`Solution`, a :class:`Status`, or an SZS status name."""

    def run(self, problem: Problem, timeout: Optional[float] = None) -> RunResult:
        try:
            parsed = problem.parsed(expand_includes=self.expand_includes)
        except (ValueError, LibraryError) as e:
            return self._result(problem, Status.ERROR, error=f"cannot read the problem: {e}")
        context = SolveContext(problem, self.config, timeout)
        box: dict[str, Any] = {}

        def work() -> None:
            try:
                box["value"] = self.solve(parsed, context)
            except Cancelled:
                box["cancelled"] = True
            except Exception as e:
                box["error"] = f"{type(e).__name__}: {e}"
                box["trace"] = traceback.format_exc()

        start = time.perf_counter()
        context.started = start
        if timeout is None:
            work()
        else:
            worker = threading.Thread(target=work, name=f"prover-{self.name}", daemon=True)
            worker.start()
            worker.join(timeout)
            if worker.is_alive():
                context.cancelled.set()
                return self._result(problem, Status.TIMEOUT, runtime=time.perf_counter() - start)
        runtime = time.perf_counter() - start
        if box.get("cancelled") or (timeout is not None and runtime >= timeout):    # too late to count
            return self._result(problem, Status.TIMEOUT, runtime=runtime)
        if "error" in box:
            return self._result(problem, Status.ERROR, runtime=runtime, error=box["error"], stderr=box["trace"])
        value = box.get("value")
        if isinstance(value, Solution):
            kind, solution, output = value.status, value.solution, value.output
        else:
            kind, solution, output = value, None, ""
        if isinstance(kind, Status):
            status, szs = kind, None
        elif isinstance(kind, str):
            try:
                status, szs = Status(kind.lower()), None
            except ValueError:
                status, szs = status_from_szs(kind), kind
        elif kind is None:
            status, szs = Status.UNKNOWN, None
        else:
            return self._result(problem, Status.ERROR, runtime=runtime,
                                error=f"solve() returned {type(kind).__name__}, not a Status or SZS status name")
        return self._result(problem, status, szs=szs, runtime=runtime, solution=solution, stdout=output)


class CallableProver(BuiltInProver):
    """A built-in prover from a function ``function(problem, context)``, which returns what
    :meth:`BuiltInProver.solve` does::

        CallableProver(lambda problem, context: "GaveUp", name="giveup")
    """

    def __init__(self, function: Callable[[TPTPFile, SolveContext], Any], name: Optional[str] = None,
                 config: Optional[Mapping[str, Any]] = None, **options):
        super().__init__(name or getattr(function, "__name__", "function"), config, **options)
        self.function = function

    def solve(self, problem, context):
        return self.function(problem, context)


# ---------------------------------------------------------------------------------
# A collection
# ---------------------------------------------------------------------------------


class ProverCollection:
    """An ordered group of provers with distinct names.

        provers = ProverCollection([EProver(), Vampire(), prover_from_spec("z=./myprover -x")])
        report = provers.run(search_result, timeout=10)

    Items can be looked up by name or position, and ``in`` works with names and provers.
    """

    def __init__(self, provers: Iterable[Prover] = ()):
        self._provers: dict[str, Prover] = {}
        for prover in provers:
            self.add(prover)

    @classmethod
    def from_specs(cls, specs: Iterable[Union[str, Prover]], *, input: Optional[str] = None,
                   platform: Union[str, Platform, None] = None) -> "ProverCollection":
        """Provers from command lines (see :func:`prover_from_spec`) and ready-made provers."""
        return cls(prover_from_spec(s, input=input, platform=platform) if isinstance(s, str) else s
                   for s in specs)

    def add(self, prover: Prover) -> "ProverCollection":
        if not isinstance(prover, Prover):
            raise TypeError(f"{prover!r} is not a Prover")
        if prover.name in self._provers:
            raise ValueError(f"there is already a prover called {prover.name!r}; give one a different name")
        self._provers[prover.name] = prover
        return self

    @property
    def names(self) -> list[str]:
        return list(self._provers)

    def available(self) -> "ProverCollection":
        """The provers that can be run now."""
        return ProverCollection(p for p in self if p.available())

    def missing(self) -> list[Prover]:
        """The provers that cannot be run now (a missing binary)."""
        return [p for p in self if not p.available()]

    def run(self, problems, **options):
        """Run ``problems`` against every prover; see :func:`thousandproblems.runner.run`."""
        from .runner import run

        return run(problems, self, **options)

    def __iter__(self) -> Iterator[Prover]:
        return iter(list(self._provers.values()))

    def __len__(self) -> int:
        return len(self._provers)

    def __getitem__(self, key: Union[str, int]) -> Prover:
        if isinstance(key, int):
            return list(self._provers.values())[key]
        return self._provers[key]

    def __contains__(self, item) -> bool:
        return item in self._provers if isinstance(item, str) else item in self._provers.values()

    def __repr__(self) -> str:
        return f"<ProverCollection {', '.join(self._provers) or 'empty'}>"
