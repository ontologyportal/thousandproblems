"""Run problems against provers and collect the results.

    >>> from thousandproblems.search import search
    >>> from thousandproblems.provers import EProver, Vampire
    >>> from thousandproblems.runner import run
    >>> hits = search("/data/TPTP-v10.0.0", domain="SET", status="THM", rating="0")   # doctest: +SKIP
    >>> report = run(hits[:20], [EProver(), Vampire()], timeout=10, jobs=4)           # doctest: +SKIP
    >>> print(report.table())                                                         # doctest: +SKIP

:func:`run` takes a :class:`~thousandproblems.search.SearchResult`, a
:class:`~thousandproblems.problem.Problem`, a problem name, a path to a problem file, or a list
of these. Problems are fetched one at a time as the run reaches them. :func:`iter_results` is the
same as a generator, for handling each result as it arrives.
"""

from __future__ import annotations

import csv
import io
import json
import os
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional, Union

from .library import Library, LibraryError, RateLimited, open_library
from .problem import Problem
from .provers import Prover, ProverCollection, RunResult, Status
from .search import SearchResult

__all__ = ["DEFAULT_TIMEOUT", "RunReport", "iter_results", "run"]

#: Seconds each prover gets per problem unless told otherwise.
DEFAULT_TIMEOUT = 60.0

_Loader = Callable[[], Problem]
ProblemsLike = Union[SearchResult, Problem, str, "os.PathLike[str]", Iterable[Union[Problem, str, "os.PathLike[str]"]]]


# ---------------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------------


@dataclass
class RunReport:
    """The results of a run, in problem order and, for each problem, prover order.

    ``search`` holds the ``source`` and ``params`` of the :class:`SearchResult` that was run, if it
    was one. ``problems`` and ``provers`` are the names, in the order they were given.
    """

    results: list[RunResult]
    problems: list[str] = field(default_factory=list)
    provers: list[str] = field(default_factory=list)
    timeout: Optional[float] = None
    search: Optional[dict[str, Any]] = None

    def by_prover(self) -> dict[str, list[RunResult]]:
        grouped: dict[str, list[RunResult]] = {name: [] for name in self.provers}
        for r in self.results:
            grouped.setdefault(r.prover, []).append(r)
        return grouped

    def by_problem(self) -> dict[str, dict[str, RunResult]]:
        grouped: dict[str, dict[str, RunResult]] = {name: {} for name in self.problems}
        for r in self.results:
            grouped.setdefault(r.problem, {})[r.prover] = r
        return grouped

    def mismatches(self) -> list[RunResult]:
        """Verdicts that contradict the problem's header, which points to a bug in the prover
        (or in the header)."""
        return [r for r in self.results if r.correct is False]

    def summary(self) -> dict[str, dict[str, Any]]:
        """Per prover: how many problems ended in each status, how many were ``solved`` (got a
        verdict), and the ``time`` spent on the solved ones."""
        table = {}
        for name, results in self.by_prover().items():
            counts = {str(s): 0 for s in Status}
            for r in results:
                counts[str(r.status)] += 1
            solved = [r for r in results if r.solved]
            table[name] = {**counts, "total": len(results), "solved": len(solved),
                           "time": round(sum(r.runtime for r in solved), 4),
                           "mismatches": sum(1 for r in results if r.correct is False)}
        return table

    def table(self) -> str:
        """A text table with a row per problem and a column per prover, then totals per prover."""
        def cell(r: Optional[RunResult]) -> str:
            if r is None:
                return "-"
            mark = " !" if r.correct is False else ""
            return f"{r.status} {r.runtime:.2f}s{mark}" if r.solved else f"{r.status}"

        rows = [["Problem", *self.provers]]
        for problem, by in self.by_problem().items():
            rows.append([problem, *(cell(by.get(p)) for p in self.provers)])
        widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
        lines = ["  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in rows]
        lines.insert(1, "  ".join("-" * w for w in widths))
        summary = self.summary()
        width = max([8, *map(len, self.provers)])
        lines += ["", f"{'Prover':<{width}}  solved  proved  satisfiable  timeout  unknown  error  time(s)"]
        for name, s in summary.items():
            lines.append(f"{name:<{width}}  {s['solved']:>3}/{s['total']:<3} "
                         f"{s['proved']:>6}  {s['satisfiable']:>11}  {s['timeout']:>7}  "
                         f"{s['unknown'] + s['resource_out']:>7}  {s['error']:>5}  {s['time']:>7.2f}")
        if self.mismatches():
            lines.append("\n! = the verdict contradicts the status in the problem's header")
        return "\n".join(lines)

    def as_dict(self, *, output: bool = False) -> dict[str, Any]:
        return {"search": self.search, "timeout": self.timeout, "problems": self.problems,
                "provers": self.provers, "summary": self.summary(),
                "results": [r.as_dict(output=output) for r in self.results]}

    def to_json(self, *, output: bool = False, indent: Optional[int] = 2) -> str:
        return json.dumps(self.as_dict(output=output), indent=indent)

    def to_csv(self) -> str:
        out = io.StringIO()
        columns = ["problem", "prover", "status", "szs", "runtime", "expected", "correct", "returncode", "error"]
        writer = csv.DictWriter(out, columns, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for r in self.results:
            writer.writerow(r.as_dict())
        return out.getvalue()


# ---------------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------------


def _as_provers(provers: Union[Prover, Iterable[Union[Prover, str]], str]) -> ProverCollection:
    if isinstance(provers, ProverCollection):
        return provers
    if isinstance(provers, (Prover, str)):
        provers = [provers]
    return ProverCollection.from_specs(provers)


def _as_items(problems: ProblemsLike, library: Union[None, str, os.PathLike, Library],
              opened: list[Library]) -> list[tuple[str, _Loader]]:
    """``(name, loader)`` for each problem, without fetching any of them yet."""
    items: list[tuple[str, _Loader]] = []

    def add(thing) -> None:
        if isinstance(thing, SearchResult):
            items.extend((r.name, lambda r=r: thing.problem(r.name)) for r in thing.records)
        elif isinstance(thing, Problem):
            items.append((thing.name, lambda: thing))
        elif isinstance(thing, (str, os.PathLike)):
            text = os.fspath(thing)
            if Path(text).is_file():
                items.append((Path(text).stem, lambda: Problem.from_file(text)))
            else:
                if not opened:
                    opened.append(open_library(library))
                name = text[:-2] if text.endswith(".p") else text
                items.append((name, lambda: Problem.from_library(name, opened[0])))
        else:
            for item in thing:
                add(item)

    add(problems)
    return items


def _attempt(prover: Prover, problem: Problem, timeout: Optional[float]) -> RunResult:
    try:
        return prover.run(problem, timeout)
    except Exception as e:      # a prover that raises must not lose the results of the others
        return RunResult(problem.name, prover.name, Status.ERROR, error=f"{type(e).__name__}: {e}",
                         expected=problem.expected_status)


def iter_results(problems: ProblemsLike, provers, *, timeout: Optional[float] = DEFAULT_TIMEOUT, jobs: int = 1,
                 library: Union[None, str, os.PathLike, Library] = None,
                 keep_output: bool = True) -> Iterator[RunResult]:
    """Run every problem against every prover, yielding each :class:`RunResult` as it is done
    (in order with ``jobs=1``, as they finish otherwise). See :func:`run` for the arguments."""
    collection = _as_provers(provers)
    opened: list[Library] = []
    try:
        items = _as_items(problems, library, opened)
        yield from _execute(items, list(collection), timeout, max(1, jobs), keep_output)
    finally:
        for lib in opened:
            if lib is not library:
                lib.close()


def _execute(items, provers: list[Prover], timeout, jobs: int, keep_output: bool) -> Iterator[RunResult]:
    def finish(result: RunResult) -> RunResult:
        if not keep_output:
            result.stdout = result.stderr = ""
            result.solution = None
        return result

    def load(name: str, loader: _Loader):
        try:
            return loader(), []
        except RateLimited:
            raise
        except (LibraryError, ValueError, OSError, KeyError) as e:
            message = e.args[0] if isinstance(e, KeyError) and e.args else e
            return None, [RunResult(name, p.name, Status.ERROR, error=f"cannot load the problem: {message}")
                          for p in provers]

    if jobs == 1:
        for name, loader in items:
            problem, failed = load(name, loader)
            yield from failed
            if problem is not None:
                for prover in provers:
                    yield finish(_attempt(prover, problem, timeout))
        return

    pool = ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="run")
    pending: set = set()
    try:
        for name, loader in items:
            problem, failed = load(name, loader)
            yield from failed
            if problem is None:
                continue
            for prover in provers:
                pending.add(pool.submit(_attempt, prover, problem, timeout))
            while len(pending) >= jobs * 2:         # keep the number of fetched problems small
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    yield finish(future.result())
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                yield finish(future.result())
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def run(problems: ProblemsLike, provers, *, timeout: Optional[float] = DEFAULT_TIMEOUT, jobs: int = 1,
        library: Union[None, str, os.PathLike, Library] = None, keep_output: bool = True,
        on_result: Optional[Callable[[RunResult, int, int], None]] = None) -> RunReport:
    """Run ``problems`` against ``provers`` and return a :class:`RunReport`.

    :param problems: a :class:`SearchResult`, a :class:`Problem`, a problem name such as
        ``"SET001-1"`` (fetched from ``library``), the path of a problem file, or a list of these.
    :param provers: a :class:`ProverCollection`, a :class:`Prover`, a command line such as
        ``"eprover --auto"`` (see :func:`thousandproblems.provers.prover_from_spec`), or a list of these.
    :param timeout: seconds each prover may take on each problem (``None``: no limit).
    :param jobs: how many provers to run at once. Runtimes are wall-clock, so they grow when the
        machine is busy; use ``jobs=1`` for timing comparisons.
    :param library: where to fetch problems given by name (default: ``$TPTP``, else the TPTP website).
    :param keep_output: keep each run's stdout, stderr and proof (they can be large).
    :param on_result: called as ``on_result(result, done, total)`` after each run.

    A problem that cannot be fetched gives an ``error`` result for each prover; a prover that
    fails does the same. Neither stops the run.
    """
    collection = _as_provers(provers)
    opened: list[Library] = []
    try:
        items = _as_items(problems, library, opened)
        names = list(dict.fromkeys(name for name, _ in items))
        order = {name: i for i, name in enumerate(names)}
        prover_order = {name: i for i, name in enumerate(collection.names)}
        total = len(items) * len(collection)
        results: list[RunResult] = []
        for result in _execute(items, list(collection), timeout, max(1, jobs), keep_output):
            results.append(result)
            if on_result:
                on_result(result, len(results), total)
    finally:
        for lib in opened:
            if lib is not library:
                lib.close()
    results.sort(key=lambda r: (order.get(r.problem, 0), prover_order.get(r.prover, 0)))
    origin = None
    if isinstance(problems, SearchResult):
        origin = {"source": problems.source, "params": problems.params}
    return RunReport(results, names, collection.names, timeout, origin)
