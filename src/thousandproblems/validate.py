"""Check that a folder or tarball is a usable TPTP library.

    >>> from thousandproblems.validate import validate_library
    >>> report = validate_library("/data/TPTP-v10.0.0")        # doctest: +SKIP
    >>> report.ok, report.problems, report.version             # doctest: +SKIP
    (True, 25257, 'v10.0.0')

The standard layout of a TPTP distribution is::

    TPTP-v10.0.0/
        Problems/<DOM>/<DOM><NNN><form><version>.p     e.g. Problems/SET/SET001-1.p
        Axioms/<DOM><NNN><form><version>.ax            e.g. Axioms/SET001-0.ax
        Documents/  Generators/  Scripts/  TPTP2X/

``Problems`` and ``Axioms`` are required, and every problem must have a TPTP name in the
folder of its domain. The other folders only produce warnings, as a trimmed copy can still
be used for reading problems. ``check_includes`` reads every problem and checks that its
includes are present, and ``parse`` also parses every problem and axiom file, which takes a
long time for the whole library.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Union

from .library import DirectoryLibrary, LibraryError, TarballLibrary, _NAME_RE, _clean
from .nodes import ParseError

__all__ = ["ValidationReport", "validate_library"]

REQUIRED = ("Problems", "Axioms")
OPTIONAL = ("Documents", "Generators", "Scripts", "TPTP2X")

_INCLUDE_RE = re.compile(r"^\s*include\(\s*'((?:[^'\\]|\\.)*)'", re.MULTILINE)
_VERSION_RE = re.compile(r"v(\d+\.\d+\.\d+)")


@dataclass
class ValidationReport:
    """The outcome of :func:`validate_library`. ``ok`` is whether there were no errors."""

    source: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    version: Optional[str] = None
    domains: int = 0
    problems: int = 0
    axioms: int = 0
    #: Errors after this many of one kind are counted, not listed.
    limit: int = 20
    _counts: dict = field(default_factory=dict, repr=False)

    @property
    def ok(self) -> bool:
        return not self.errors

    def _add(self, bucket: list[str], kind: str, message: str) -> None:
        count = self._counts.get(kind, 0) + 1
        self._counts[kind] = count
        if count <= self.limit:
            bucket.append(message)
        elif count == self.limit + 1:
            bucket.append(f"... more of these ({kind}); only the first {self.limit} are listed")

    def error(self, message: str, kind: str = "") -> None:
        self._add(self.errors, kind or message, message)

    def warn(self, message: str, kind: str = "") -> None:
        self._add(self.warnings, kind or message, message)

    def summary(self) -> str:
        lines = [f"{'VALID' if self.ok else 'INVALID'} TPTP library: {self.source}"]
        if self.version:
            lines.append(f"  version   : {self.version}")
        lines.append(f"  domains   : {self.domains}")
        lines.append(f"  problems  : {self.problems}")
        lines.append(f"  axioms    : {self.axioms}")
        for e in self.errors:
            lines.append(f"  error     : {e}")
        for w in self.warnings:
            lines.append(f"  warning   : {w}")
        return "\n".join(lines)


def _list_files(source: Path) -> tuple[list[str], Callable[[str], Optional[str]], str]:
    """Library-relative paths of every file, a reader, and a label for the version guess."""
    if source.is_dir():
        library = DirectoryLibrary(source)
        files = [p.relative_to(source).as_posix() for d in (*REQUIRED, *OPTIONAL)
                 if (source / d).is_dir() for p in (source / d).rglob("*") if p.is_file()]
        files += [p.name for p in source.iterdir() if p.is_file()]
        return files, library.read_optional, source.name
    library = TarballLibrary(source)
    try:
        index = library._members()
    except LibraryError as e:
        raise _Invalid(str(e)) from None
    return list(index), library.read_optional, source.name


class _Invalid(Exception):
    pass


def validate_library(source: Union[str, os.PathLike], *, check_includes: bool = False, parse: bool = False,
                     progress: Optional[Callable[[int, int], None]] = None) -> ValidationReport:
    """Check that ``source`` (a folder or tarball) is a TPTP library in the standard layout."""
    path = Path(source)
    report = ValidationReport(str(path))
    if not path.exists():
        report.error(f"{str(path)!r} does not exist")
        return report
    if path.is_dir():
        for name in REQUIRED:
            if not (path / name).is_dir():
                hint = ""
                inner = [c for c in path.iterdir() if c.is_dir() and (c / "Problems").is_dir()]
                if inner:
                    hint = f" (did you mean {str(inner[0])!r}?)"
                report.error(f"missing the {name}/ folder{hint}")
        if not report.ok:
            return report
    try:
        files, read, label = _list_files(path)
    except (_Invalid, OSError) as e:
        report.error(str(e))
        return report
    present = {f.split("/")[0] for f in files if "/" in f}
    for name in REQUIRED:
        if name not in present:
            report.error(f"missing the {name}/ folder")
    for name in OPTIONAL:
        if name not in present:
            report.warn(f"no {name}/ folder (not needed for reading problems)")
    if not report.ok:
        return report

    problems: list[str] = []
    axioms: set[str] = set()
    domains: set[str] = set()
    for rel in files:
        parts = rel.split("/")
        if parts[0] == "Problems":
            if len(parts) != 3:
                if len(parts) == 2:
                    report.warn(f"{rel}: a file directly in Problems/", "loose file")
                else:
                    report.warn(f"{rel}: nested deeper than Problems/<DOMAIN>/", "deep file")
                continue
            domain, filename = parts[1], parts[2]
            if not re.fullmatch(r"[A-Z]{3}", domain):
                report.error(f"{rel}: domain folder {domain!r} is not three capital letters", "bad domain")
                continue
            domains.add(domain)
            if not filename.endswith(".p"):
                report.warn(f"{rel}: not a .p problem file", "non-problem file")
                continue
            stem = filename[:-2]
            if not _NAME_RE.fullmatch(stem):
                report.error(f"{rel}: {stem!r} is not a TPTP problem name", "bad problem name")
            elif stem[:3] != domain:
                report.error(f"{rel}: problem {stem} is in the wrong domain folder ({domain})", "wrong domain")
            else:
                problems.append(rel)
        elif parts[0] == "Axioms":
            if rel.endswith(".ax"):
                axioms.add(rel)
    report.domains, report.problems, report.axioms = len(domains), len(problems), len(axioms)
    if not problems:
        report.error("Problems/ holds no problem files")
    if not axioms:
        report.warn("Axioms/ holds no .ax files")

    match = _VERSION_RE.search(label)
    if match:
        report.version = f"v{match[1]}"
    for rel in problems[:1]:
        text = read(rel) or ""
        found = re.search(r"^% File\s*:.*?TPTP (v\d+\.\d+\.\d+)", text, re.MULTILINE)
        if found:
            if report.version and report.version != found[1]:
                report.warn(f"the folder says {report.version} but {rel} says {found[1]}")
            report.version = report.version or found[1]

    if check_includes or parse:
        from . import parse as parse_text
        everything = sorted(problems) + (sorted(axioms) if parse else [])
        for count, rel in enumerate(everything, 1):
            if progress:
                progress(count, len(everything))
            text = read(rel)
            if text is None:
                report.error(f"{rel}: cannot be read", "unreadable")
                continue
            for target in _INCLUDE_RE.findall(text):
                clean = _clean(target)
                if clean is None or (clean not in axioms and read(clean) is None):
                    report.error(f"{rel}: includes {target!r}, which is missing", "missing include")
            if parse:
                try:
                    parse_text(text, rel)
                except ParseError as e:
                    report.error(f"{rel}:{e.line}:{e.column}: {e.message}", "syntax error")
    return report
