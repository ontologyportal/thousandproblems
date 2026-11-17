"""Find TPTP libraries on this machine, and make one the default by setting ``$TPTP``.

    >>> from thousandproblems.discover import find_libraries
    >>> for found in find_libraries(["~"]):                      # doctest: +SKIP
    ...     print(found.path, found.version, found.valid)
    /Users/me/tptp/TPTP-v10.0.0 10.0.0 True

A library is a folder holding ``Problems/`` and ``Axioms/``; each one found is checked with
:func:`thousandproblems.validate.validate_library`. Tarballs named like ``TPTP-v10.0.0.tgz``
are reported too, but ``$TPTP`` must name an unpacked folder.

:func:`export_command` gives the line that sets ``$TPTP`` in a given shell, and
:func:`install` writes it into the shell's startup file, between marker comments, so that
running it again replaces the earlier line instead of adding another.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Union

__all__ = ["Found", "find_libraries", "default_roots", "detect_shell", "export_command",
           "startup_file", "install", "MARK_BEGIN", "MARK_END"]

MARK_BEGIN = "# >>> thousandproblems: TPTP library >>>"
MARK_END = "# <<< thousandproblems: TPTP library <<<"

#: Folders never searched inside: big, and never where a TPTP library lives.
_SKIP = {"node_modules", "Library", "Applications", "__pycache__", "site-packages", "venv",
         "proc", "sys", "dev", "System", "Windows", "Program Files", "Program Files (x86)", "AppData"}
_TARBALL_RE = re.compile(r"TPTP-v(\d+\.\d+\.\d+)\.(?:tgz|tar\.gz|tar\.bz2|tar\.xz|tar)")


@dataclass
class Found:
    """One library found: a folder, or a tarball (``packed``)."""

    path: Path
    version: Optional[str] = None
    valid: bool = False
    packed: bool = False
    #: Has every folder of a distribution, not only Problems/ and Axioms/ (as ``fetch -o`` makes).
    complete: bool = False
    problems: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def sort_key(self):
        version = tuple(int(x) for x in self.version.split(".")) if self.version else ()
        usable = 2 if self.valid and not self.packed else 1 if self.packed else 0    # a tarball can be unpacked
        return (usable, self.complete, version, self.problems, str(self.path))


def default_roots() -> list[Path]:
    """Where to look when no folder is given: here, the home folder, the usual install places."""
    roots = [Path.cwd(), Path.home()]
    if sys.platform == "win32":
        roots += [Path(os.environ.get("SystemDrive", "C:") + "\\")]
    else:
        roots += [Path(p) for p in ("/opt", "/usr/local/share", "/usr/share", "/srv", "/data")]
    unique: dict[str, Path] = {}
    for root in roots:
        if root.is_dir():
            unique.setdefault(str(root.resolve()), root)
    return list(unique.values())


def _is_library(path: Path) -> bool:
    return (path / "Problems").is_dir() and (path / "Axioms").is_dir()


def _walk(root: Path, depth: int, budget: list[int]) -> Iterable[tuple[Path, bool]]:
    """Yield (path, is_tarball) for each library-looking thing under ``root``."""
    if _is_library(root):
        yield root, False
        return
    stack = [(root, 0)]
    while stack:
        folder, level = stack.pop()
        if budget[0] <= 0:
            return
        budget[0] -= 1
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError:
            continue
        below = []
        for entry in entries:
            name = entry.name
            try:
                if entry.is_file() and _TARBALL_RE.fullmatch(name):
                    yield Path(entry.path), True
                    continue
                if not entry.is_dir() or name.startswith((".", "$")) or name in _SKIP:
                    continue
                symlink = entry.is_symlink()
            except OSError:
                continue
            path = Path(entry.path)
            if _is_library(path):
                yield path, False
            elif level + 1 < depth and not symlink:      # a symlink may be a library, but isn't crawled
                below.append((path, level + 1))
        stack.extend(reversed(below))


def find_libraries(roots: Optional[Iterable[Union[str, os.PathLike]]] = None, *, depth: int = 4,
                   max_folders: int = 200_000, check: bool = True) -> list[Found]:
    """TPTP libraries under ``roots`` (default: :func:`default_roots`), best first: valid
    folders, then tarballs, then invalid folders; complete distributions before partial copies,
    then newer versions and more problems first.

    ``depth`` is how many folder levels below each root are searched, and ``max_folders``
    caps the folders looked at in all. With ``check``, each folder is validated.
    """
    from .updates import detect_version
    from .validate import validate_library

    paths = [Path(os.path.expanduser(os.fspath(r))) for r in roots] if roots else default_roots()
    budget = [max_folders]
    seen: dict[str, Found] = {}
    for root in paths:
        if not root.exists():
            continue
        for path, packed in _walk(root, depth, budget):
            key = os.path.normcase(str(path.resolve()))
            if key in seen:
                continue
            # keep a symlink as it is, so $TPTP follows it when it is pointed at a new release
            found = Found(path.absolute(), detect_version(path.resolve()), packed=packed)
            if packed:
                found.errors.append("a tarball: unpack it to use it as $TPTP")
            elif check:
                report = validate_library(path)
                found.valid, found.problems = report.ok, report.problems
                found.complete = report.ok and not any(w.startswith("no ") and w.endswith("folder (not needed for reading problems)")
                                                       for w in report.warnings)
                found.version = found.version or (report.version or "").lstrip("v") or None
                found.errors = list(report.errors)
            else:
                found.valid = found.complete = True
            seen[key] = found
    return sorted(seen.values(), key=lambda f: f.sort_key, reverse=True)


# ---------------------------------------------------------------------------------
# Shells
# ---------------------------------------------------------------------------------

SHELLS = ("bash", "zsh", "fish", "sh", "powershell", "cmd")


def detect_shell() -> str:
    """The user's shell: ``bash``, ``zsh``, ``fish``, ``sh``, ``powershell`` or ``cmd``."""
    if sys.platform == "win32":
        return "powershell" if os.environ.get("PSModulePath") else "cmd"
    name = Path(os.environ.get("SHELL", "")).name
    for shell in ("zsh", "bash", "fish"):
        if name.startswith(shell):
            return shell
    return "sh"


def _quote_posix(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def export_command(path: Union[str, os.PathLike], shell: str) -> str:
    """The command that sets ``$TPTP`` to ``path`` in ``shell``."""
    text = str(path)
    if shell == "fish":
        return f"set -gx TPTP {_quote_posix(text)}"
    if shell == "powershell":
        return f"$env:TPTP = '{text.replace(chr(39), chr(39) * 2)}'"
    if shell == "cmd":
        return f'set "TPTP={text}"'
    return f"export TPTP={_quote_posix(text)}"


def startup_file(shell: str, home: Optional[Path] = None) -> Optional[Path]:
    """The file ``shell`` runs at startup, where ``install`` puts the setting.

    ``None`` for Windows shells, where the setting is made with ``setx`` instead.
    """
    home = home or Path.home()
    if shell == "zsh":
        return Path(os.environ.get("ZDOTDIR") or home) / ".zshrc"
    if shell == "bash":
        # macOS terminals start login shells, which read .bash_profile, not .bashrc
        return home / (".bash_profile" if sys.platform == "darwin" else ".bashrc")
    if shell == "fish":
        config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        return config / "fish" / "conf.d" / "thousandproblems.fish"
    if shell == "sh":
        return home / ".profile"
    return None


def _block(path: Union[str, os.PathLike], shell: str) -> str:
    return f"{MARK_BEGIN}\n{export_command(path, shell)}\n{MARK_END}\n"


def planned_text(current: str, path: Union[str, os.PathLike], shell: str) -> str:
    """``current`` (a startup file's text) with the TPTP block added or replaced."""
    block = _block(path, shell)
    pattern = re.compile(re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END) + r"\n?", re.DOTALL)
    if pattern.search(current):
        return pattern.sub(lambda _: block, current, count=1)
    separator = "" if not current or current.endswith("\n\n") else ("\n" if current.endswith("\n") else "\n\n")
    return current + separator + block


def other_settings(text: str) -> list[str]:
    """Lines outside our block that also set TPTP, which the user should know about."""
    outside = re.sub(re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END), "", text, flags=re.DOTALL)
    return [line.strip() for line in outside.splitlines()
            if re.match(r"\s*(export\s+TPTP=|TPTP=|set\s+-\w*x\w*\s+TPTP\b|setenv\s+TPTP\b)", line)]


def install(path: Union[str, os.PathLike], shell: str, file: Optional[Path] = None) -> Path:
    """Write the TPTP setting into ``shell``'s startup file (or ``file``); returns the file."""
    target = file or startup_file(shell)
    if target is None:
        raise ValueError(f"{shell} has no startup file; use setx")
    current = target.read_text(encoding="utf-8") if target.exists() else ""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(planned_text(current, path, shell), encoding="utf-8")
    if target.exists():
        try:
            os.chmod(temporary, target.stat().st_mode)
        except OSError:
            pass
    os.replace(temporary, target)
    return target
