"""Search a local TPTP library by problem properties, like the TPTP2T form on tptp.org.

    >>> from thousandproblems.search import search                         # doctest: +SKIP
    >>> hits = search("/data/TPTP-v10.0.0", form="FOF", status="THM",
    ...               equality="none", rating="0.5..1", formulae="..50")   # doctest: +SKIP
    >>> [r.name for r in hits][:3]                                         # doctest: +SKIP
    ['AGT001+1', 'AGT001+2', 'AGT002+1']

Everything TPTP2T matches on comes from the problem headers: the ``% SPC`` line (logical
form, status, order, equality, arithmetic ...), the ``% Rating`` line, the ``% Version`` line
and the ``% Syntax`` statistics. Reading the headers of a whole library takes a little while,
so they are kept in an index under :func:`thousandproblems.library.default_cache_dir`, which
is rebuilt when the library changes.

Criteria are written as strings, the same as on the command line:

* lists are comma-separated and match any item: ``status="THM,UNS"``;
* numbers take ``N``, ``MIN..MAX``, ``MIN..``, ``..MAX``, ``>N``, ``>=N``, ``<N`` or ``<=N``:
  ``formulae="10..100"``, ``predicates=">1"``;
* a ``not:`` prefix negates a criterion, like TPTP2T's ``~`` box: ``form="not:CNF"``.

TPTP2T's solution properties (system results, solution sizes ...) come from the TSTP
solution library, not the problem library, so they are not available here.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import os
import re
import tarfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, Union

from .library import (DirectoryLibrary, Library, LibraryError, TarballLibrary, _NAME_RE,
                      default_cache_dir, open_library)
from .nodes import Comment, Header, _parse_header
from .stats import SPC, STATUS_CODES, syntax_stats

__all__ = ["ProblemRecord", "Query", "build_index", "read_header", "search", "CRITERIA"]

_INDEX_FORMAT = 1

# ---------------------------------------------------------------------------------
# Headers and records
# ---------------------------------------------------------------------------------


def read_header(lines: Iterable[str], max_lines: int = 2000) -> Optional[Header]:
    """Parse the standard header from the first lines of a problem, without reading the
    formulae. ``lines`` can be an open text file."""
    comments: list[Comment] = []
    rules = 0
    for number, line in enumerate(lines, 1):
        text = line.rstrip("\r\n")
        if text.startswith("%"):
            comments.append(Comment(text, number))
            if re.fullmatch(r"%-{5,}\s*", text):
                rules += 1
                if rules == 2:
                    break
        elif text.strip() and comments:
            break
        if number >= max_lines:
            break
    return _parse_header(tuple(comments))


_VERSION_WORDS = (("Unbiased", "UNB"), ("Biased", "BIA"), ("Incomplete", "INC"),
                  ("Augmented", "AUG"), ("Especial", "ESP"))


@dataclass
class ProblemRecord:
    """What the index knows about one problem.

    ``syntax`` holds the ``% Syntax`` statistics as ``{key: [value, {detail: number}]}``,
    with the keys of :func:`thousandproblems.stats.syntax_stats`; a range detail such as
    ``aty`` is a two-item list.
    """

    name: str
    path: str
    spc: Optional[str] = None
    status: Optional[str] = None        # SZS name from the % Status line
    rating: Optional[float] = None
    version: str = "STD"                # STD, INC, AUG, ESP, BIA or UNB
    syntax: dict = field(default_factory=dict)

    @property
    def domain(self) -> str:
        return self.name[:3]

    @property
    def form(self) -> Optional[str]:
        return SPC(self.spc).form if self.spc else None

    @property
    def flags(self) -> frozenset[str]:
        return SPC(self.spc).flags if self.spc else frozenset()

    @property
    def status_code(self) -> Optional[str]:
        if self.spc and SPC(self.spc).status:
            return SPC(self.spc).status
        return STATUS_CODES.get(self.status or "")

    def stat(self, *keys: str, detail: Optional[str] = None):
        """The first of the ``% Syntax`` statistics ``keys`` present (or its ``detail``)."""
        for key in keys:
            if key in self.syntax:
                value, details = self.syntax[key]
                if detail is None:
                    return value
                found = details.get(detail)
                return tuple(found) if isinstance(found, list) else found
        return None

    @classmethod
    def from_header(cls, name: str, path: str, header: Optional[Header]) -> "ProblemRecord":
        if header is None:
            return cls(name, path)
        version_line = header.get("Version") or ""
        version = next((code for word, code in _VERSION_WORDS if word in version_line), "STD")
        syntax = {k: [s.value, {d: list(v) if isinstance(v, tuple) else v for d, v in s.details.items()}]
                  for k, s in syntax_stats(header).items()}
        return cls(name, path, header.spc, header.status, header.rating, version, syntax)

    def as_dict(self) -> dict:
        return {**asdict(self), "domain": self.domain, "form": self.form}


# ---------------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------------


def _problem_parts(rel: str) -> Optional[str]:
    """``'Problems/SET/SET001-1.p'`` -> ``'SET001-1'``, or None if it is not a problem file."""
    parts = rel.split("/")
    if len(parts) == 3 and parts[0] == "Problems" and parts[2].endswith(".p"):
        stem = parts[2][:-2]
        if _NAME_RE.fullmatch(stem) and stem[:3] == parts[1]:
            return stem
    return None


def _directory_signature(root: Path) -> list:
    problems = root / "Problems"
    signature = []
    for domain in sorted(problems.iterdir()):
        if domain.is_dir():
            signature.append([domain.name, domain.stat().st_mtime_ns, sum(1 for _ in domain.iterdir())])
    return signature


def _scan_directory(root: Path, progress) -> Iterator[ProblemRecord]:
    files = sorted((root / "Problems").glob("*/*.p"))
    for count, file in enumerate(files, 1):
        rel = file.relative_to(root).as_posix()
        name = _problem_parts(rel)
        if name is None:
            continue
        try:
            with open(file, encoding="utf-8", errors="replace") as handle:
                header = read_header(handle)
        except OSError:
            header = None
        if progress:
            progress(count, len(files))
        yield ProblemRecord.from_header(name, rel, header)


def _scan_tarball(path: Path, progress) -> Iterator[ProblemRecord]:
    try:
        with tarfile.open(path, "r|*") as tar:      # one sequential pass
            count = 0
            for member in tar:
                name_in_tar = member.name[2:] if member.name.startswith("./") else member.name
                parts = name_in_tar.split("/")
                if not member.isfile() or "Problems" not in parts[:-1]:
                    continue
                rel = "/".join(parts[parts.index("Problems"):])
                name = _problem_parts(rel)
                if name is None:
                    continue
                handle = tar.extractfile(member)
                # a streamed member is not seekable, so TextIOWrapper can't be used
                header = read_header(line.decode("utf-8", errors="replace")
                                     for line in iter(handle.readline, b"")) if handle else None
                count += 1
                if progress:
                    progress(count, None)
                yield ProblemRecord.from_header(name, rel, header)
    except (tarfile.TarError, OSError, EOFError) as e:
        raise LibraryError(f"cannot read {path}: {e}") from e


def build_index(library: Union[str, os.PathLike, Library, None] = None, *, cache: Union[bool, str, os.PathLike] = True,
                refresh: bool = False, progress: Optional[Callable[[int, Optional[int]], None]] = None
                ) -> list[ProblemRecord]:
    """Read the header of every problem in a local library (folder or tarball).

    The result is cached (``cache=True``: under :func:`default_cache_dir`; or a folder;
    ``False`` for no cache) and reused until the library changes. ``refresh`` rebuilds it.
    ``progress(done, total)`` is called as files are read; ``total`` is None for a tarball.
    """
    lib = open_library(library)
    try:
        if isinstance(lib, DirectoryLibrary):
            source = lib.root.resolve()
            signature = _directory_signature(source)
            scan = lambda: _scan_directory(source, progress)      # noqa: E731
        elif isinstance(lib, TarballLibrary):
            source = lib.path.resolve()
            st = source.stat()
            signature = [st.st_size, st.st_mtime_ns]
            scan = lambda: _scan_tarball(source, progress)        # noqa: E731
        else:
            raise LibraryError("searching needs a local copy of the TPTP library (a folder or tarball); "
                               "the website cannot be searched. Get one with 'thousandproblems download'.")
    finally:
        if lib is not library:
            lib.close()

    cache_file = None
    if cache:
        folder = default_cache_dir() if cache is True else Path(cache)
        digest = hashlib.sha1(str(source).encode()).hexdigest()[:16]
        cache_file = folder / "indexes" / f"{digest}.json"
        if not refresh:
            try:
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                if data.get("format") == _INDEX_FORMAT and data.get("signature") == signature:
                    return [ProblemRecord(**r) for r in data["problems"]]
            except (OSError, ValueError, TypeError, KeyError):
                pass

    records = sorted(scan(), key=lambda r: r.name)
    if cache_file is not None:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_file.with_name(f".{cache_file.name}.{os.getpid()}.tmp")
            temporary.write_text(json.dumps({
                "format": _INDEX_FORMAT, "source": str(source), "signature": signature,
                "problems": [asdict(r) for r in records]}), encoding="utf-8")
            os.replace(temporary, cache_file)
        except OSError:
            pass
    return records


# ---------------------------------------------------------------------------------
# Criteria
# ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Range:
    low: float = -math.inf
    high: float = math.inf
    low_open: bool = False
    high_open: bool = False

    def __contains__(self, x) -> bool:
        if x is None:
            return False
        return ((x > self.low) if self.low_open else (x >= self.low)) and \
               ((x < self.high) if self.high_open else (x <= self.high))


def parse_range(text: str) -> _Range:
    """``'5'``, ``'1..10'``, ``'1-10'``, ``'3..'``, ``'..3'``, ``'>1'``, ``'>=1'``, ``'<1'``, ``'<=1'``."""
    t = text.strip().replace(" ", "")
    number = r"\d+(?:\.\d*)?|\.\d+"
    try:
        if m := re.fullmatch(rf"(>=|<=|>|<)({number})", t):
            op, value = m[1], float(m[2])
            return {">": _Range(low=value, low_open=True), ">=": _Range(low=value),
                    "<": _Range(high=value, high_open=True), "<=": _Range(high=value)}[op]
        if m := re.fullmatch(rf"({number})?(?:\.\.|-)({number})?", t):
            if m[1] is None and m[2] is None:
                raise ValueError
            return _Range(float(m[1]) if m[1] else -math.inf, float(m[2]) if m[2] else math.inf)
        value = float(t)
        return _Range(value, value)
    except ValueError:
        raise ValueError(f"{text!r} is not a number or range (try 5, 1..10, 3.., ..3, >1)") from None


def _items(text: str) -> list[str]:
    return [item.strip() for item in text.split(",") if item.strip()]


_FORM_GROUPS = {"TFF": {"TF0", "TF1"}, "TXF": {"TX0", "TX1"}, "THF": {"TH0", "TH1"},
                "NXF": {"NX0", "NX1"}, "NHF": {"NH0", "NH1"}, "DHF": {"DH0", "DH1"}}
_FORMS = {"FOF", "CNF", "TF0", "TF1", "TX0", "TX1", "TH0", "TH1", "NX0", "NX1", "NH0", "NH1", "DH0", "DH1"}
_ORDER_WORDS = {"PROPOSITIONAL": "PRP", "EFFECTIVELYPROPOSITIONAL": "EPR", "FINITEHU": "FHU",
                "FINITE": "FHU", "REAL": "RFO", "REALFIRSTORDER": "RFO"}
_EQUALITY = {"NONE": "NEQ", "NO": "NEQ", "SOME": "SEQ", "PURE": "PEQ", "ANY": "EQU",
             "NEQ": "NEQ", "SEQ": "SEQ", "PEQ": "PEQ", "EQU": "EQU"}
_QUANTIFIERS = {"^": "^", "!": "!", "?": "?", "!>": "!>", "LAMBDA": "^", "FORALL": "!",
                "EXISTS": "?", "TYPEFORALL": "!>", "VAL": "^", "VAU": "!", "VAE": "?", "VAP": "!>"}
_YES = {"YES", "Y", "TRUE", "SOME", "1"}
_NO = {"NO", "N", "FALSE", "NONE", "0"}


def _choice(value: str, table: dict[str, str], what: str) -> str:
    key = re.sub(r"[\s_-]", "", value).upper()
    if key in table:
        return table[key]
    raise ValueError(f"unknown {what} {value!r}; expected one of {', '.join(sorted(set(table.values())))}")


def _yes_no(value: str, what: str) -> bool:
    key = value.strip().upper()
    if key in _YES:
        return True
    if key in _NO:
        return False
    raise ValueError(f"{what} must be yes or no, not {value!r}")


def _numeric(*keys: str, detail: Optional[str] = None, default=None, arity: bool = False):
    def build(value: str):
        wanted = parse_range(value)

        def test(r: ProblemRecord) -> bool:
            found = r.stat(*keys, detail=detail)
            if arity and isinstance(found, tuple):
                found = found[1]          # the maximal arity
            return (default if found is None else found) in wanted
        return test
    return build


def _domain(value: str):
    patterns = [p.upper() for p in _items(value)]
    return lambda r: any(fnmatch.fnmatchcase(r.domain, p) for p in patterns)


def _name(value: str):
    patterns = _items(value)
    return lambda r: any(fnmatch.fnmatchcase(r.name, p) for p in patterns)


def _form(value: str):
    wanted: set[str] = set()
    for item in _items(value.upper()):
        if item in _FORM_GROUPS:
            wanted |= _FORM_GROUPS[item]
        elif item in _FORMS:
            wanted.add(item)
        else:
            raise ValueError(f"unknown logical form {item!r}; expected one of "
                             f"{', '.join(sorted(_FORMS | set(_FORM_GROUPS)))}")
    return lambda r: r.form in wanted


def _order(value: str):
    table = {**_ORDER_WORDS, **{c: c for c in ("PRP", "EPR", "FHU", "RFO")}}
    wanted = {_choice(v, table, "order") for v in _items(value)}
    return lambda r: bool(wanted & r.flags)


def _version(value: str):
    table = {c: c for c in ("STD", "INC", "AUG", "ESP", "BIA", "UNB")}
    table.update({"STANDARD": "STD", "INCOMPLETE": "INC", "AUGMENTED": "AUG", "ESPECIAL": "ESP",
                  "BIASED": "BIA", "UNBIASED": "UNB"})
    wanted = {_choice(v, table, "version") for v in _items(value)}
    return lambda r: r.version in wanted


def _status(value: str):
    table = {c: c for c in STATUS_CODES.values()}
    table.update({k.upper(): v for k, v in STATUS_CODES.items()})
    wanted = {_choice(v, table, "status") for v in _items(value)}
    return lambda r: r.status_code in wanted


def _rating(value: str):
    word = value.strip().lower()
    if word in ("easy", "esy"):
        return lambda r: r.rating == 0.0
    if word in ("difficult", "dif", "hard"):
        return lambda r: r.rating is not None and 0.0 < r.rating < 1.0
    if word in ("unsolved", "uso"):
        return lambda r: r.rating == 1.0
    wanted = parse_range(value)
    return lambda r: r.rating in wanted


def _has_equality(r: ProblemRecord) -> bool:
    if r.flags & {"SEQ", "PEQ", "EQU", "UEQ"}:
        return True
    if "NEQ" in r.flags:
        return False
    return bool(r.stat("atoms", "literals", detail="equ"))


def _equality(value: str):
    code = _choice(value, _EQUALITY, "equality")
    if code == "NEQ":
        return lambda r: not _has_equality(r)
    if code == "EQU":
        return _has_equality
    return lambda r: code in r.flags


def _unit_equality(value: str):
    yes = _yes_no(value, "unit equality")
    return lambda r: ("UEQ" in r.flags) == yes


def _arithmetic(value: str):
    yes = _yes_no(value, "arithmetic")

    def test(r: ProblemRecord) -> bool:
        has = "ARI" in r.flags or bool(r.stat("arithmetic"))
        return has == yes
    return test


def _variables(value: str):
    word = value.strip().upper()
    if word in _YES:
        return lambda r: bool(r.stat("variables"))
    if word in _NO:
        return lambda r: r.stat("variables") == 0
    return _numeric("variables")(value)


def _quantifiers(value: str):
    wanted = [_choice(v, _QUANTIFIERS, "quantifier") for v in _items(value)]

    def test(r: ProblemRecord) -> bool:
        cnf = r.form == "CNF"
        for q in wanted:
            count = r.stat("variables") if cnf and q == "!" else r.stat("variables", detail=q)
            if not count:
                return False
        return True
    return test


def _spc(value: str):
    pattern = re.compile(value, re.IGNORECASE)
    return lambda r: bool(r.spc and pattern.search(r.spc))


#: Every criterion: name -> (builder, help text). Names match the keyword arguments of
#: :func:`search` and, with ``_`` as ``-``, the command-line options.
CRITERIA: dict[str, tuple[Callable[[str], Callable[[ProblemRecord], bool]], str]] = {
    "domain": (_domain, "domain codes, e.g. SET,SEU (wildcards allowed: SE?)"),
    "name": (_name, "problem name patterns, e.g. 'SET0*-1'"),
    "form": (_form, "logical form: FOF CNF TFF TXF THF TF0 TF1 TX0 TX1 TH0 TH1 NXF NHF DHF NX0 NX1 NH0 NH1 DH0 DH1"),
    "order": (_order, "first-order subform: PRP (propositional), EPR (effectively propositional), "
                      "FHU (finite Herbrand universe), RFO (real first-order)"),
    "version": (_version, "problem version: STD INC AUG ESP BIA UNB (standard, incomplete, augmented, "
                          "especial, biased, unbiased)"),
    "status": (_status, "THM CAX CSA UNK UNS SAT OPN, or SZS names such as Theorem"),
    "rating": (_rating, "easy (0.00), difficult (between), unsolved (1.00), or a range such as 0.5..1"),
    "formulae": (_numeric("formulae", "clauses"), "number of formulae (clauses in CNF)"),
    "unit_formulae": (_numeric("formulae", "clauses", detail="unt"), "number of unit formulae"),
    "atoms": (_numeric("atoms", "literals"), "number of atoms (literals in CNF)"),
    "equality": (_equality, "none, some (some but not pure), pure, or any"),
    "unit_equality": (_unit_equality, "yes or no: a unit equality (UEQ) problem"),
    "types": (_numeric("types", default=0), "number of types"),
    "datatypes": (_numeric("types", detail="dat", default=0), "number of datatypes"),
    "codatatypes": (_numeric("types", detail="cdt", default=0), "number of codatatypes"),
    "symbols": (None, "number of symbols (predicates + functors; THF symbols)"),
    "predicates": (_numeric("predicates"), "number of predicates"),
    "predicate_arity": (_numeric("predicates", detail="aty", arity=True), "maximal predicate arity"),
    "functions": (_numeric("functors"), "number of functors (including constants)"),
    "function_arity": (_numeric("functors", detail="aty", arity=True), "maximal functor arity"),
    "variables": (_variables, "none, some, or a number/range"),
    "quantifiers": (_quantifiers, "binders that must be used: ^ ! ? !> (comma-separated)"),
    "arithmetic": (_arithmetic, "yes or no"),
    "spc": (_spc, "regular expression matched against the SPC, e.g. '_NHN'"),
}


def _symbols(value: str):
    wanted = parse_range(value)

    def test(r: ProblemRecord) -> bool:
        count = r.stat("symbols")
        if count is None:
            p, f = r.stat("predicates"), r.stat("functors")
            count = None if p is None and f is None else (p or 0) + (f or 0)
        return count in wanted
    return test


CRITERIA["symbols"] = (_symbols, CRITERIA["symbols"][1])


@dataclass
class Query:
    """A conjunction of criteria; see :data:`CRITERIA` for the names."""

    tests: list[tuple[str, str, Callable[[ProblemRecord], bool], bool]] = field(default_factory=list)

    @classmethod
    def build(cls, **criteria: Optional[str]) -> "Query":
        query = cls()
        for name, value in criteria.items():
            if value is None:
                continue
            values = value if isinstance(value, (list, tuple)) else [value]
            for item in values:
                query.add(name, str(item))
        return query

    def add(self, name: str, value: str) -> "Query":
        key = name.replace("-", "_")
        if key not in CRITERIA:
            raise ValueError(f"unknown search criterion {name!r}; known: {', '.join(CRITERIA)}")
        negate = False
        text = value.strip()
        if text.lower().startswith("not:"):
            negate, text = True, text[4:]
        self.tests.append((key, value, CRITERIA[key][0](text), negate))
        return self

    def matches(self, record: ProblemRecord) -> bool:
        return all(test(record) != negate for _, _, test, negate in self.tests)

    def __bool__(self) -> bool:
        return bool(self.tests)


def search(library: Union[str, os.PathLike, Library, None, list[ProblemRecord]] = None, *,
           query: Optional[Query] = None, refresh: bool = False, **criteria: Optional[str]) -> list[ProblemRecord]:
    """The problems of a local library that meet every criterion, sorted by name.

    ``library`` is as for :func:`thousandproblems.open_library`, or a list of records from
    :func:`build_index`. Criteria are keyword arguments (see :data:`CRITERIA`), or a prebuilt
    :class:`Query`.
    """
    records = library if isinstance(library, list) else build_index(library, refresh=refresh)
    q = query or Query()
    if criteria:
        q = Query(list(q.tests) + Query.build(**criteria).tests)
    return [r for r in records if q.matches(r)]
