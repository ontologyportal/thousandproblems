"""Reading what a prover says: the SZS status line and output block of its TSTP output.

A prover that follows the TPTP conventions ends with a line such as::

    % SZS status Theorem for SET001-1

(E writes ``# SZS status ...``), optionally followed by the proof or model between
``% SZS output start ...`` and ``% SZS output end`` lines. :func:`parse_szs_status` finds the
status, and :func:`status_from_szs` boils the many SZS names down to a :class:`Status`.

    >>> parse_szs_status("# Proof found\\n# SZS status Theorem\\n")
    'Theorem'
    >>> status_from_szs("CounterSatisfiable")
    <Status.SATISFIABLE: 'satisfiable'>
"""

from __future__ import annotations

import enum
import re
from typing import Optional

__all__ = ["Status", "extract_output_block", "parse_szs_status", "status_from_szs"]


class Status(enum.StrEnum):
    """The outcome of running a prover on a problem."""

    PROVED = "proved"              # Theorem, Unsatisfiable, ContradictoryAxioms ...
    SATISFIABLE = "satisfiable"    # Satisfiable, CounterSatisfiable ...
    UNKNOWN = "unknown"            # the prover gave up, or said nothing we can read
    TIMEOUT = "timeout"
    RESOURCE_OUT = "resource_out"  # out of memory or another resource
    ERROR = "error"                # a crash, a syntax error, a missing prover ...

    @property
    def solved(self) -> bool:
        """The prover reached a verdict: :attr:`PROVED` or :attr:`SATISFIABLE`."""
        return self in (Status.PROVED, Status.SATISFIABLE)


_PROVED = {"Theorem", "Unsatisfiable", "ContradictoryAxioms", "Tautology", "Equivalent", "EquivalentTheorem",
           "TautologousConclusion", "WeakerConclusion", "WeakerTheorem", "WeakerTautologousConclusion"}
_SATISFIABLE = {"Satisfiable", "FinitelySatisfiable", "CounterSatisfiable", "CounterEquivalent",
                "CounterTheorem", "UnsatisfiableConclusion"}
_RESOURCE_OUT = {"ResourceOut", "MemoryOut"}
_ERROR = {"Error", "InputError", "OSError", "SyntaxError", "SemanticError", "TypeError", "Inappropriate"}

_BY_NAME = {name.lower(): status for names, status in (
    (_PROVED, Status.PROVED), (_SATISFIABLE, Status.SATISFIABLE), (_RESOURCE_OUT, Status.RESOURCE_OUT),
    (_ERROR, Status.ERROR), ({"Timeout"}, Status.TIMEOUT)) for name in names}

_STATUS_LINE = re.compile(r"^[ \t]*[%#]?[ \t]*SZS status[ \t]+([A-Za-z]+)", re.MULTILINE)
_OUTPUT_START = re.compile(r"^[ \t]*[%#]?[ \t]*SZS output start\b.*$", re.MULTILINE)
_OUTPUT_END = re.compile(r"^[ \t]*[%#]?[ \t]*SZS output end\b.*$", re.MULTILINE)


def status_from_szs(name: Optional[str]) -> Status:
    """The :class:`Status` for an SZS status name; names that are not a verdict, a timeout or
    an error (``GaveUp``, ``Unknown``, ``Incomplete`` ...) are :attr:`Status.UNKNOWN`."""
    return _BY_NAME.get((name or "").lower(), Status.UNKNOWN)


def parse_szs_status(text: str) -> Optional[str]:
    """The SZS status name in a prover's output (the last one, if there are several), or ``None``."""
    found = _STATUS_LINE.findall(text)
    return found[-1] if found else None


def extract_output_block(text: str) -> Optional[str]:
    """The lines between ``SZS output start`` and ``SZS output end`` (the proof or model), or ``None``."""
    start = _OUTPUT_START.search(text)
    if start is None:
        return None
    end = _OUTPUT_END.search(text, start.end())
    body = text[start.end():end.start() if end else len(text)]
    return body.strip("\r\n") or None
