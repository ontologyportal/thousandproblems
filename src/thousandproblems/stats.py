"""Statistics about a TPTP problem: the ones its header declares, and ones computed from the AST.

Every problem in the TPTP library carries a ``% Syntax`` block in its header, written by the
TPTP tools, and an ``% SPC`` line giving its Specialist Problem Class::

    % Syntax   : Number of clauses     :    9 (   3 unt;   1 nHn;   8 RR)
    %            Number of literals    :   17 (   0 equ;   8 neg)
    %            Number of predicates  :    3 (   3 usr;   0 prp; 2-2 aty)
    %            Number of variables   :   13 (   0 sgn)
    % SPC      : CNF_UNS_RFO_NEQ_NHN

:func:`syntax_stats` reads the first into a dict of :class:`Stat`, and :class:`SPC` splits the
second. :func:`compute_stats` works the main figures out from a parsed file, which also works
for files that have no header.

    >>> import thousandproblems as tptp
    >>> from thousandproblems.stats import compute_stats
    >>> stats = compute_stats(tptp.parse("fof(a, axiom, ! [X] : (p(X) => q(f(X))))."))
    >>> stats.formulae, stats.atoms, stats.predicates, stats.functions
    (1, 2, ('p/1', 'q/1'), ('f/1',))
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Optional, Union

from .nodes import (Binary, Function, Header, Let, ModalAtom, NAry, Node, Quantified, TPTPFile,
                    Tuple, TypedVariable, Typing, Unary, Variable, walk)

__all__ = ["SPC", "Stat", "ComputedStats", "syntax_stats", "compute_stats", "DOMAINS",
           "STATUS_CODES", "FORMS"]

# ---------------------------------------------------------------------------------
# Header statistics
# ---------------------------------------------------------------------------------

Value = Union[int, float, tuple[int, int]]


@dataclass(frozen=True)
class Stat:
    """One line of a ``% Syntax`` block: ``Number of predicates : 3 (3 usr; 0 prp; 2-2 aty)``
    is ``Stat(3, {'usr': 3, 'prp': 0, 'aty': (2, 2)})``.

    A detail written as a range (``2-2 aty``) is a ``(min, max)`` tuple. When a key appears
    twice, as in the THF ``Number of connectives`` line, the first wins.
    """

    value: int
    details: dict[str, Value] = field(default_factory=dict)

    def get(self, key: str, default=None):
        return self.details.get(key, default)


_STAT_RE = re.compile(r"((?:Number|Maximal)[A-Za-z ]*?)\s*:\s*(\d+)((?:\s*\([^()]*\))*)")
_DETAIL_RE = re.compile(r"(\d+(?:\.\d+)?)(?:-(\d+))?\s*(\S+)")


def _key(label: str) -> str:
    """``'Number of type conns'`` -> ``'type_conns'``, ``'Maximal formula depth'`` ->
    ``'max_formula_depth'``, ``'Number arithmetic'`` -> ``'arithmetic'``."""
    label = label.strip().lower()
    if label.startswith("maximal "):
        return "max_" + label[8:].replace(" ", "_")
    return re.sub(r"^number (of )?", "", label).replace(" ", "_")


def syntax_stats(header: Optional[Header]) -> dict[str, Stat]:
    """The ``% Syntax`` block of a problem header, keyed by a short name.

    Keys are the labels with ``Number of`` dropped and ``Maximal`` shortened to ``max_``,
    e.g. ``formulae`` (``clauses`` in CNF), ``atoms`` (``literals`` in CNF), ``predicates``,
    ``functors``, ``symbols`` (THF), ``variables``, ``types``, ``max_formula_depth``.
    """
    text = header.get("Syntax") if header is not None else None
    if not text:
        return {}
    stats: dict[str, Stat] = {}
    for label, value, groups in _STAT_RE.findall(text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")):
        details: dict[str, Value] = {}
        for group in re.findall(r"\(([^()]*)\)", groups):
            for item in group.split(";"):
                m = _DETAIL_RE.fullmatch(item.strip())
                if not m:
                    continue
                low, high, key = m.groups()
                number: Value = (int(low), int(high)) if high is not None else (
                    float(low) if "." in low else int(low))
                details.setdefault(key, number)
        stats.setdefault(_key(label), Stat(int(value), details))
    return stats


#: SZS status names and the three-letter codes used in SPCs and by TPTP2T.
STATUS_CODES = {
    "Theorem": "THM", "ContradictoryAxioms": "CAX", "CounterSatisfiable": "CSA",
    "Unknown": "UNK", "Unsatisfiable": "UNS", "Satisfiable": "SAT", "Open": "OPN",
}

#: Logical forms: the first component of an SPC.
FORMS = {
    "CNF": "clause normal form", "FOF": "first-order form",
    "TF0": "typed first-order, monomorphic", "TF1": "typed first-order, polymorphic",
    "TX0": "typed extended first-order, monomorphic", "TX1": "typed extended first-order, polymorphic",
    "TH0": "typed higher-order, monomorphic", "TH1": "typed higher-order, polymorphic",
    "NX0": "non-classical typed extended first-order, monomorphic",
    "NX1": "non-classical typed extended first-order, polymorphic",
    "NH0": "non-classical typed higher-order, monomorphic",
    "NH1": "non-classical typed higher-order, polymorphic",
    "DH0": "dynamic higher-order, monomorphic", "DH1": "dynamic higher-order, polymorphic",
}

_FLAGS = {
    "PRP": "propositional", "EPR": "effectively propositional", "FHU": "finite Herbrand universe",
    "RFO": "real first-order", "NEQ": "no equality", "SEQ": "some equality", "PEQ": "pure equality",
    "EQU": "equality", "UEQ": "unit equality", "HRN": "Horn", "NHN": "non-Horn",
    "ARI": "arithmetic", "NAR": "no arithmetic", "NDT": "no datatypes", "DAT": "datatypes",
}

#: The TPTP domains, by their three-letter code.
DOMAINS = {
    "AGT": "Agents", "ALG": "General Algebra", "ANA": "Analysis", "ARI": "Arithmetic",
    "BIO": "Biology", "BOO": "Boolean Algebra", "CAT": "Category Theory",
    "COL": "Combinatory Logic", "COM": "Computing Theory", "CSR": "Commonsense Reasoning",
    "DAT": "Data Structures", "FLD": "Field Theory", "GEG": "Geography", "GEO": "Geometry",
    "GRA": "Graph Theory", "GRP": "Group Theory", "HAL": "Homological Algebra",
    "HEN": "Henkin Models", "HWC": "Hardware Creation", "HWV": "Hardware Verification",
    "ITP": "Interactive Theorem Proving", "KLE": "Kleene Algebra", "KRS": "Knowledge Representation",
    "LAT": "Lattice Theory", "LCL": "Logic Calculi", "LDA": "LD-Algebras", "LIN": "Linear Algebra",
    "MED": "Medicine", "MGT": "Management", "MSC": "Miscellaneous", "MVA": "Multi-valued Algebra",
    "NLP": "Natural Language Processing", "NUM": "Number Theory", "NUN": "Number Theory (no arithmetic)",
    "PHI": "Philosophy", "PLA": "Planning", "PRD": "Products", "PRO": "Processes",
    "PUZ": "Puzzles", "QUA": "Quantales", "RAL": "Relation Algebra", "REL": "Relation Algebra",
    "RNG": "Ring Theory", "ROB": "Robbins Algebra", "SCT": "Social Choice Theory",
    "SET": "Set Theory", "SEU": "Set Theory (Mizar)", "SEV": "Set Theory (Henkin)",
    "SWB": "Semantic Web", "SWC": "Software Creation", "SWV": "Software Verification",
    "SWW": "Software Verification (Why3)", "SYN": "Syntactic", "SYO": "Syntactic (Henkin)",
    "TOP": "Topology",
}


@dataclass(frozen=True)
class SPC:
    """A Specialist Problem Class such as ``FOF_THM_RFO_SEQ``: the logical form, the status,
    and a set of flags (``RFO``, ``SEQ``, ``NHN``, ``ARI`` ...)."""

    text: str

    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(self.text.split("_"))

    @property
    def form(self) -> str:
        return self.parts[0]

    @property
    def status(self) -> Optional[str]:
        return self.parts[1] if len(self.parts) > 1 else None

    @property
    def flags(self) -> frozenset[str]:
        return frozenset(self.parts[2:])

    def describe(self) -> str:
        """``'FOF_THM_RFO_SEQ'`` -> ``'first-order form, Theorem, real first-order, some equality'``."""
        status = {v: k for k, v in STATUS_CODES.items()}
        words = [FORMS.get(self.form, self.form)]
        if self.status:
            words.append(status.get(self.status, self.status))
        words += [_FLAGS.get(f, f) for f in self.parts[2:]]
        return ", ".join(words)

    def __str__(self) -> str:
        return self.text


# ---------------------------------------------------------------------------------
# Computed statistics
# ---------------------------------------------------------------------------------


@dataclass
class ComputedStats:
    """Figures worked out from the formulae themselves.

    Symbols are written ``name/arity``. For THF, where the parser cannot tell a predicate
    from a function, ``predicates`` and ``functions`` are empty and ``atoms`` and
    ``unit_formulae`` are ``None``;
    use ``symbols``. Interpreted symbols (``$less``, ``$true`` ...) are counted under
    ``defined_symbols`` rather than as predicates or functions.
    """

    dialects: tuple[str, ...] = ()
    formulae: int = 0
    roles: dict[str, int] = field(default_factory=dict)
    unit_formulae: Optional[int] = 0
    type_declarations: int = 0
    atoms: Optional[int] = 0
    equality_atoms: int = 0
    symbols: tuple[str, ...] = ()
    predicates: tuple[str, ...] = ()
    functions: tuple[str, ...] = ()
    constants: tuple[str, ...] = ()
    defined_symbols: tuple[str, ...] = ()
    user_types: tuple[str, ...] = ()
    variables: int = 0
    singleton_variables: int = 0
    bound_variables: dict[str, int] = field(default_factory=dict)
    max_formula_depth: int = 0
    max_term_depth: int = 0
    includes: tuple[str, ...] = ()
    polymorphic: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


_FORMULA_BINARY = {"=>", "<=", "<=>", "<~>", "~|", "~&", "-->"}
_EQUALITY = {"=", "!="}


class _Collector:
    def __init__(self) -> None:
        self.atoms = 0
        self.equality = 0
        self.predicates: dict[str, None] = {}
        self.functions: dict[str, None] = {}
        self.defined: dict[str, None] = {}
        self.bound: Counter = Counter()
        self.term_depth = 0
        self.polymorphic = False

    # ``formula`` is whether ``node`` stands where a formula is expected; returns its depth.
    def visit(self, node: Node, formula: bool) -> int:
        if isinstance(node, Function):
            if not formula:
                return self.term(node)
            if node.name == "$ite" and node.args:          # a conditional formula
                return 1 + max(self.visit(a, True) for a in node.args)
            self.atoms += 1
            self._symbol(node, self.predicates)
            for arg in node.args:
                self.term(arg)
            return 1
        if isinstance(node, Variable):
            if formula:
                self.atoms += 1
            return 1
        if isinstance(node, Binary):
            if node.op in _EQUALITY:
                self.atoms += 1
                self.equality += 1
                self.term(node.left)
                self.term(node.right)
                return 1
            if node.op in _FORMULA_BINARY:
                return 1 + max(self.visit(node.left, True), self.visit(node.right, True))
            return 1 + max(self.visit(node.left, formula), self.visit(node.right, formula))
        if isinstance(node, NAry):
            if node.op in ("|", "&"):
                return 1 + max(self.visit(a, True) for a in node.args)
            return 1 + max((self.visit(a, False) for a in node.args), default=0)
        if isinstance(node, Unary):
            return 1 + self.visit(node.arg, formula)
        if isinstance(node, Quantified):
            self.bound[node.quantifier] += len(node.variables)
            if node.quantifier in ("!>", "?*"):
                self.polymorphic = True
            return 1 + self.visit(node.body, formula)
        if isinstance(node, ModalAtom):
            return 1 + max((self.visit(a, True) for a in node.args), default=0)
        if isinstance(node, Let):
            for definition in node.definitions:
                self.visit(definition.right, formula)
            return 1 + self.visit(node.body, formula)
        if isinstance(node, Tuple):
            return 1 + max((self.visit(a, formula) for a in node.items), default=0)
        return 1        # FormulaData, ConnectiveTerm, numbers, distinct objects ...

    def term(self, node: Node) -> int:
        if isinstance(node, Function):
            self._symbol(node, self.functions)
            depth = 1 + max((self.term(a) for a in node.args), default=0)
        elif isinstance(node, Binary) and node.op in _EQUALITY:     # e.g. inside $ite
            self.equality += 1
            depth = 1 + max(self.term(node.left), self.term(node.right))
        elif isinstance(node, (Quantified, Unary, NAry, Binary, Let)):
            depth = self.visit(node, True)
        else:
            depth = 1
        self.term_depth = max(self.term_depth, depth)
        return depth

    def _symbol(self, node: Function, table: dict[str, None]) -> None:
        key = f"{node.name}/{len(node.args)}"
        if node.name.startswith("$"):
            self.defined[key] = None
        elif not node.name.startswith('"'):
            table[key] = None


def compute_stats(problem: TPTPFile) -> ComputedStats:
    """Work out statistics for ``problem`` from its formulae.

    Only the formulae actually in ``problem`` count, so parse with ``expand_includes=True``
    to include the axioms; ``includes`` lists the include directives still unexpanded.
    """
    formulae = problem.formulae
    result = ComputedStats(
        dialects=tuple(dict.fromkeys(f.language for f in formulae)),
        formulae=len(formulae),
        roles=dict(Counter(f.role for f in formulae)),
        includes=tuple(i.path for i in problem.includes),
        symbols=tuple(str(s) for s in problem.symbols),
    )
    collector = _Collector()
    higher_order = False
    singletons = distinct = 0
    user_types: dict[str, None] = {}
    for f in formulae:
        if f.role == "type" or isinstance(f.formula, Typing):
            result.type_declarations += 1
            if isinstance(f.formula, Typing):
                text = str(f.formula.type)
                if text.endswith("$tType"):
                    user_types[f.formula.atom] = None
                if "!>" in text or text.count("$tType") > 1:
                    collector.polymorphic = True
            continue
        occurrences: Counter = Counter()
        for node in walk(f.formula):
            if isinstance(node, Variable) and node.index:
                occurrences[node.index] += 1
            elif isinstance(node, TypedVariable) and node.index:
                occurrences[node.index] += 0
        distinct += len(occurrences)
        if f.language == "cnf":
            singletons += sum(1 for n in occurrences.values() if n == 1)
            if occurrences:
                collector.bound["!"] += len(occurrences)
        if f.language == "thf":
            higher_order = True
            before = collector.atoms
            depth = _thf_depth(f.formula, collector)
            collector.atoms = before
        else:
            atoms_before = collector.atoms
            depth = collector.visit(f.formula, True)
            if collector.atoms - atoms_before == 1:
                result.unit_formulae += 1
        result.max_formula_depth = max(result.max_formula_depth, depth)
    result.variables = distinct
    result.singleton_variables = singletons
    result.bound_variables = dict(collector.bound)
    result.max_term_depth = collector.term_depth
    result.user_types = tuple(user_types)
    result.polymorphic = collector.polymorphic
    result.defined_symbols = tuple(collector.defined)
    result.equality_atoms = collector.equality
    if higher_order:
        result.atoms = result.unit_formulae = None
    else:
        result.atoms = collector.atoms
        result.predicates = tuple(collector.predicates)
        result.functions = tuple(s for s in collector.functions if not s.endswith("/0"))
        result.constants = tuple(s for s in collector.functions if s.endswith("/0"))
    return result


def _thf_depth(node: Node, collector: _Collector) -> int:
    """Formula depth of a THF formula, also counting its binders."""
    if isinstance(node, Quantified):
        collector.bound[node.quantifier] += len(node.variables)
        if node.quantifier in ("!>", "?*"):
            collector.polymorphic = True
        return 1 + _thf_depth(node.body, collector)
    if isinstance(node, Binary):
        if node.op in _EQUALITY:
            collector.equality += 1
        return 1 + max(_thf_depth(node.left, collector), _thf_depth(node.right, collector))
    if isinstance(node, NAry):
        return 1 + max((_thf_depth(a, collector) for a in node.args), default=0)
    if isinstance(node, Unary):
        return 1 + _thf_depth(node.arg, collector)
    if isinstance(node, Function) and node.name.startswith("$"):
        collector.defined[f"{node.name}/{len(node.args)}"] = None
    return 1
