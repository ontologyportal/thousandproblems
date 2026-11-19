"""Versions, platforms and downloads: what it takes to install a prover locally.

A prover is published for some *platforms* (operating system and CPU) in some *versions*. This
module names both and does the generic work of fetching and building:

    >>> from thousandproblems.install import Platform, version_key
    >>> str(Platform.parse("darwin/aarch64"))
    'macos-arm64'
    >>> Platform.parse("linux-x86_64") == Platform(os="linux", arch="x86_64")
    True
    >>> sorted(["3.5.9", "3.5.10", "3.4"], key=version_key)
    ['3.4', '3.5.9', '3.5.10']

:class:`thousandproblems.provers.SubprocessProver` uses these to turn "Vampire 5.1.0 on macOS/arm64"
into a URL, download and unpack it (or build it from source), and keep each version and platform
in a folder of its own.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Sequence, Union

__all__ = ["InstallError", "Platform", "download", "extract", "github_latest_version", "missing_tools",
           "run_build", "version_key"]

Log = Callable[[str], None]


class InstallError(Exception):
    """A prover could not be downloaded, unpacked or built."""


# ---------------------------------------------------------------------------------
# Platforms
# ---------------------------------------------------------------------------------

_OS = {"linux": "linux", "macos": "macos", "mac": "macos", "osx": "macos",
       "darwin": "macos", "windows": "windows", "win": "windows", "win32": "windows", "nt": "windows"}
_ARCH = {"x86_64": "x86_64", "x64": "x86_64", "amd64": "x86_64", "intel": "x86_64",
         "arm64": "arm64", "aarch64": "arm64", "arm": "arm64", "apple": "arm64"}


@dataclass(frozen=True)
class Platform:
    """An operating system (``linux``, ``macos``, ``windows``) and a CPU architecture
    (``x86_64``, ``arm64``; others keep the name the machine reports)."""

    os: str
    arch: str

    @classmethod
    def detect(cls) -> "Platform":
        """The machine this is running on."""
        system = {"darwin": "macos", "win32": "windows", "cygwin": "windows"}.get(sys.platform, sys.platform)
        if system.startswith("linux"):
            system = "linux"
        machine = _platform.machine().lower()
        return cls(system, _ARCH.get(machine, machine))

    @classmethod
    def parse(cls, text: Union[str, "Platform"]) -> "Platform":
        """``"macos-arm64"``, ``"linux/x86_64"``, ``"darwin aarch64"``, ``"windows-x64"`` ... A missing
        architecture is this machine's; a missing system is too."""
        if isinstance(text, Platform):
            return text
        here = cls.detect()
        system = arch = None
        for token in re.findall(r"x86[_-]64|[a-z][a-z0-9]*", text.lower()):
            token = token.replace("-", "_")
            if token in _OS and system is None:
                system = _OS[token]
            elif token in _ARCH and arch is None:
                arch = _ARCH[token]
            else:
                raise ValueError(
                    f"cannot read platform {text!r}: unknown {token!r}; "
                    f"use something like 'linux-x86_64', 'macos-arm64' or 'windows-x86_64'")
        if system is None and arch is None:
            raise ValueError(f"cannot read platform {text!r}")
        return cls(system or here.os, arch or here.arch)

    def __str__(self) -> str:
        return f"{self.os}-{self.arch}"


# ---------------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------------


def version_key(version: str) -> tuple:
    """A sort key for version strings: ``3.5.10`` is after ``3.5.9``."""
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"[.\-_]", version.strip()))


def _github(url: str):
    headers = {"User-Agent": "thousandproblems", "Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        e.close()
        if e.code == 404:
            return None
        hint = " (set GITHUB_TOKEN to raise the limit)" if e.code in (403, 429) else ""
        raise InstallError(f"{url}: HTTP {e.code} {e.reason}{hint}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise InstallError(f"cannot reach {url}: {getattr(e, 'reason', e)}") from e


def github_latest_version(repo: str, tag_template: str = "{version}") -> str:
    """The newest version of a GitHub project, without the tag's decoration.

    This is the project's latest *release* if it has one, and otherwise the highest tag that
    looks like ``tag_template`` (with ``{version}`` for the number: ``"E-{version}"``).
    """
    before, _, after = tag_template.partition("{version}")
    pattern = re.compile(re.escape(before) + r"(\d+(?:\.\d+)*)" + re.escape(after) + r"$")
    release = _github(f"https://api.github.com/repos/{repo}/releases/latest")
    if release and (m := pattern.match(release.get("tag_name", ""))):
        return m[1]
    found = []
    for page in range(1, 4):
        tags = _github(f"https://api.github.com/repos/{repo}/tags?per_page=100&page={page}")
        if not tags:
            break
        found += [m[1] for t in tags if (m := pattern.match(t["name"]))]
    if not found:
        raise InstallError(f"found no release or tag of {repo} that looks like {tag_template!r}")
    return max(found, key=version_key)


# ---------------------------------------------------------------------------------
# Downloading, unpacking, building
# ---------------------------------------------------------------------------------


def download(url: str, target: Path, *, sha256: Optional[str] = None, log: Optional[Log] = None) -> None:
    """Fetch ``url`` into the file ``target``, checking its SHA-256 if given."""
    if log:
        log(f"downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "thousandproblems"})
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(request, timeout=60) as response, open(target, "wb") as out:
            while chunk := response.read(1 << 20):
                digest.update(chunk)
                out.write(chunk)
    except urllib.error.HTTPError as e:
        e.close()
        raise InstallError(f"cannot download {url}: HTTP {e.code} {e.reason}"
                           + (" (there may be no such version or platform)" if e.code == 404 else "")) from e
    except OSError as e:
        raise InstallError(f"cannot download {url}: {getattr(e, 'reason', e)}") from e
    if sha256 and digest.hexdigest() != sha256.lower():
        raise InstallError(f"{url} has SHA-256 {digest.hexdigest()}, expected {sha256}")


def _check_members(folder: Path, names: Iterable[str]) -> None:
    root = folder.resolve()
    for name in names:
        target = (folder / name).resolve()
        if root != target and root not in target.parents:
            raise InstallError(f"unsafe path in the downloaded archive: {name!r}")


def extract(archive: Path, folder: Path) -> bool:
    """Unpack a zip or tar archive into ``folder``; ``False`` (nothing done) if it is neither."""
    try:
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as z:
                _check_members(folder, z.namelist())
                z.extractall(folder)
        elif tarfile.is_tarfile(archive):
            with tarfile.open(archive) as t:
                _check_members(folder, t.getnames())
                t.extractall(folder, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
        else:
            return False
    except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError) as e:
        raise InstallError(f"cannot unpack {archive.name}: {e}") from e
    return True


def missing_tools(tools: Sequence[Union[str, Sequence[str]]]) -> list[str]:
    """The entries of ``tools`` that are not on ``$PATH``. An entry that is a list of names is
    satisfied by any one of them: ``("make", ("cc", "gcc", "clang"))``."""
    missing = []
    for tool in tools:
        names = [tool] if isinstance(tool, str) else list(tool)
        if not any(shutil.which(n) for n in names):
            missing.append(" or ".join(names))
    return missing


def run_build(commands: Sequence[Sequence[str]], folder: Path, *, jobs: Optional[int] = None,
              log: Optional[Log] = None, logfile: Optional[Path] = None) -> None:
    """Run build commands one after another in ``folder``. ``{jobs}`` in a word is the number of
    parallel jobs. The output goes to ``logfile``; a failure reports its end."""
    count = str(jobs or os.cpu_count() or 1)
    sink = open(logfile, "w", encoding="utf-8") if logfile else None
    try:
        for command in commands:
            words = [w.replace("{jobs}", count) for w in command]
            if log:
                log("running " + " ".join(words))
            try:
                done = subprocess.run(words, cwd=folder, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True, errors="replace")
            except OSError as e:
                raise InstallError(f"cannot run {words[0]!r}: {e.strerror or e}") from e
            if sink:
                sink.write(f"$ {' '.join(words)}\n{done.stdout}\n")
            if done.returncode != 0:
                tail = "\n".join(done.stdout.strip().splitlines()[-15:])
                where = f" (full output in {logfile})" if logfile else ""
                raise InstallError(f"{' '.join(words)} failed with exit status {done.returncode}{where}:\n{tail}")
    finally:
        if sink:
            sink.close()
