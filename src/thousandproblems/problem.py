"""A problem ready to hand to a theorem prover.

A :class:`Problem` is one TPTP problem together with every file it includes, so it can be
given to a prover as text, written to disk in the standard TPTP layout (which is what a
prover that follows ``include`` directives needs), or parsed.

    >>> from thousandproblems.problem import Problem
    >>> problem = Problem.from_library("SET001-1", "/data/TPTP-v10.0.0")      # doctest: +SKIP
    >>> problem = Problem.from_file("my_problem.p")                           # doctest: +SKIP
    >>> problem = Problem.from_text("fof(c, conjecture, $true).", "trivial")
    >>> problem.parsed().formulae[0].name
    'c'

Iterating a :class:`thousandproblems.search.SearchResult` yields these.
"""

from __future__ import annotations

import os
import posixpath
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Sequence, Union

from .library import Library, LibraryError, ProblemNotFound, _clean, open_library, problem_path
from .nodes import TPTPFile

if TYPE_CHECKING:
    from .search import ProblemRecord

__all__ = ["Problem"]


class _MemoryLibrary(Library):
    """Files held in a dict, then looked up in some folders. Used to find a file's includes."""

    def __init__(self, files: dict[str, str], search_dirs: Sequence[Union[str, os.PathLike]] = (),
                 label: str = "the problem"):
        self.files = files
        self.search_dirs = [Path(d) for d in search_dirs]
        self.label = label

    def read_optional(self, path: str) -> Optional[str]:
        clean = _clean(path)
        if clean is None:
            return None
        if clean in self.files:
            return self.files[clean]
        for folder in self.search_dirs:
            file = folder / clean
            try:
                if file.is_file():
                    return file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        return None


@dataclass
class Problem:
    """One problem, fetched.

    ``files`` maps library paths to text, the problem first, followed by every file it
    includes (``{'Problems/SET/SET001-1.p': ..., 'Axioms/SET001-0.ax': ...}``). ``path`` is
    the key of the problem itself and ``text`` its source. ``source`` says where it came from
    and ``record`` holds what the search index knows about it, if it came from a search.
    """

    name: str
    text: str
    files: dict[str, str]
    path: str
    source: str = ""
    record: Optional["ProblemRecord"] = None
    _parsed: dict = field(default_factory=dict, repr=False, compare=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    # -- construction ---------------------------------------------------------------

    @classmethod
    def from_library(cls, name: str, library: Union[None, str, os.PathLike, Library] = None,
                     record: Optional["ProblemRecord"] = None) -> "Problem":
        """Fetch problem ``name`` and its includes from a library (see :func:`open_library`)."""
        lib = open_library(library)
        try:
            files = lib.problem_files(name)
            path = problem_path(name)
            return cls(name[:-2] if name.endswith(".p") else name, files[path], files, path, lib.label, record)
        finally:
            if lib is not library:
                lib.close()

    @classmethod
    def from_file(cls, path: Union[str, os.PathLike], *, name: Optional[str] = None,
                  tptp_root: Union[str, os.PathLike, None] = None) -> "Problem":
        """Read a problem file. Its includes are found next to it, then under ``tptp_root``
        (default: ``$TPTP``)."""
        file = Path(path)
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise ProblemNotFound(f"cannot read {str(file)!r}: {e.strerror or e}") from e
        return cls._build(name or file.stem, text, file.name, [file.parent], tptp_root, str(file))

    @classmethod
    def from_text(cls, text: str, name: str = "problem", *,
                  tptp_root: Union[str, os.PathLike, None] = None) -> "Problem":
        """A problem from its source text. Includes are looked up under ``tptp_root`` (default: ``$TPTP``)."""
        return cls._build(name, text, f"{name}.p", [], tptp_root, "<text>")

    @classmethod
    def _build(cls, name, text, key, folders, tptp_root, source) -> "Problem":
        root = tptp_root if tptp_root is not None else os.environ.get("TPTP")
        if root:
            folders = [*folders, root]
        library = _MemoryLibrary({key: text}, folders, source)
        try:
            files = library._with_includes(key, {key: text})
        except ProblemNotFound as e:
            raise ProblemNotFound(f"{e.args[0]}; give tptp_root (or set $TPTP) to find files under Axioms/") from None
        return cls(name, text, files, key, source)

    # -- what a prover needs --------------------------------------------------------

    def write_to(self, directory: Union[str, os.PathLike]) -> Path:
        """Write the problem and its includes under ``directory``, laid out as in the TPTP
        library, and return the path of the problem file. ``directory`` can then serve as ``$TPTP``."""
        base = Path(directory)
        for rel, text in self.files.items():
            target = base / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))       # bytes: no newline translation on Windows
        return base / self.path

    def parsed(self, *, expand_includes: bool = False) -> TPTPFile:
        """The parsed problem (kept, so several provers share one parse). With
        ``expand_includes`` each ``include`` is replaced by the formulae it selects."""
        from . import parse

        with self._lock:
            if expand_includes not in self._parsed:
                tree = parse(self.text, self.path)
                if expand_includes:
                    tree = _MemoryLibrary(self.files, label=self.source or self.name)._expand(
                        self.path, self.text, tree, ())
                self._parsed[expand_includes] = tree
            return self._parsed[expand_includes]

    @property
    def expected_status(self) -> Optional[str]:
        """The SZS status named in the problem's header (``'Theorem'``, ``'Unsatisfiable'`` ...), if any."""
        if self.record is not None:
            return self.record.status
        from .search import read_header

        header = read_header(self.text.splitlines(True))
        return header.status if header else None

    def __repr__(self) -> str:
        extra = f", {len(self.files) - 1} includes" if len(self.files) > 1 else ""
        return f"<Problem {self.name} from {self.source or '?'}{extra}>"
