"""AST node classes, :class:`ParseError` and the helpers shared by every parser backend."""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from fractions import Fraction
from typing import Optional, Union

__all__ = [
    "And", "Annotations", "AnnotatedFormula", "Apply", "Arrow", "Assignment", "Binary",
    "Box", "Choice", "Colon", "Definition", "Description", "Diamond", "Equals", "Exists",
    "ForAll", "Iff", "Implies", "ImpliedBy", "Lambda", "Nand", "Nor", "Not", "NotEquals",
    "Or", "Product", "Sequent", "Subtype", "Sum", "TypeExists", "TypeForAll", "Xor",
    "Comment", "ConnectiveTerm", "DistinctObject", "FileSource", "FormulaData", "Function", "Header", "Include", "Index",
    "Inference", "Introduced", "Let", "LongConnective", "ModalAtom", "NAry", "Name",
    "Node", "Number", "ParseError", "Parent", "Quantified", "SourceList", "TPTPFile",
    "Symbol", "Tuple", "TypedVariable", "Typing", "Unary", "Variable",
    "unquote", "walk",
]

_NONASSOC = frozenset({"<=>", "=>", "<=", "<~>", "~|", "~&"})
_TH1_TERMS = frozenset({"!!", "??", "@@+", "@@-", "@="})

#: Python frames allowed while parsing or printing. Deeply nested terms do occur in
#: real problems, and the default limit of 1000 is only a few hundred levels of them.
_RECURSION_LIMIT = 100_000


@contextmanager
def _deep_stack() -> Iterator[None]:
    # The limit is interpreter-wide, so this isn't safe alongside other threads that
    # depend on hitting it, but it is restored as soon as the call finishes.
    previous = sys.getrecursionlimit()
    sys.setrecursionlimit(max(previous, _RECURSION_LIMIT))
    try:
        yield
    finally:
        sys.setrecursionlimit(previous)


class ParseError(ValueError):
    """Invalid TPTP input. ``line`` and ``column`` are 1-based."""

    def __init__(self, message: str, *, line: int = 0, column: int = 0,
                 filename: Optional[str] = None, source_line: Optional[str] = None):
        self.message = message
        self.line = line
        self.column = column
        self.filename = filename
        self.source_line = source_line
        where = f"{filename or '<string>'}:{line}:{column}: " if line else ""
        text = where + message
        if source_line is not None:
            text += f"\n    {source_line}\n    {' ' * (column - 1)}^"
        super().__init__(text)


# ---------------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------------


class Node:
    """Base class of all AST nodes. ``str(node)`` gives valid TPTP."""

    __slots__ = ()

    def __str__(self) -> str:
        with _deep_stack():
            return self._tptp()

    def _tptp(self) -> str:
        raise NotImplementedError


def _specialise(cls: type, base: type, table: dict, key: str, args: tuple, kwargs: dict) -> Node:
    """Allocate the operator subclass for ``base(op, ...)``, so that ``NAry('|', xs)``
    and ``Or.of(*xs)`` are the same value. Subclasses and unknown operators stay as is."""
    if cls is base:
        cls = table.get(args[0] if args else kwargs.get(key), base)
    return object.__new__(cls)


@dataclass(frozen=True, slots=True)
class Variable(Node):
    """A variable.

    ``index`` numbers the distinct variables of one formula 1, 2, 3 ... in order of first
    appearance, so in ``f(X) | f(Y) | g(X, f(Y))`` every ``X`` has index 1 and every ``Y``
    index 2. A quantifier counts as the first appearance of the variables it binds, and each
    binding is its own variable: in ``(! [X] : p(X)) & (! [X] : q(X))`` the two ``X`` have
    indexes 1 and 2. Numbering starts again for each formula. A variable that does not belong
    to a formula (one in an annotation, or built by hand) has index 0.

    ``index`` is not compared: ``Variable('X') == Variable('X', 1)``.
    """

    name: str
    index: int = field(default=0, compare=False)

    def _tptp(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True)
class Function(Node):
    """A function or predicate application; a constant if ``args`` is empty."""

    name: str
    args: tuple[Node, ...] = ()

    def _tptp(self) -> str:
        if not self.args:
            return self.name
        return f"{self.name}({_commas(self.args)})"


@dataclass(frozen=True, slots=True)
class Number(Node):
    """A numeric literal, compared by its source text (``1.0`` differs from ``1.00``)."""

    text: str

    @property
    def kind(self) -> str:
        """``'integer'``, ``'rational'`` or ``'real'``."""
        if "/" in self.text:
            return "rational"
        return "integer" if self.text.lstrip("+-").isdigit() else "real"

    @property
    def value(self) -> Union[int, Fraction, float]:
        return {"integer": int, "rational": Fraction, "real": float}[self.kind](self.text)

    def _tptp(self) -> str:
        return self.text


@dataclass(frozen=True, slots=True)
class DistinctObject(Node):
    """A double-quoted distinct object; ``text`` includes the quotes."""

    text: str

    def _tptp(self) -> str:
        return self.text


@dataclass(frozen=True, slots=True)
class Tuple(Node):
    """``[a, b, c]``: a THF/TXF tuple, tuple type, or general list."""

    items: tuple[Node, ...] = ()

    def _tptp(self) -> str:
        return f"[{_commas(self.items)}]"


@dataclass(frozen=True, slots=True)
class Unary(Node):
    """``~ a``, or a short non-classical connective: ``[.]``, ``<.>``, ``{.}``, ``(.)``."""

    op: str
    arg: Node

    def __new__(cls, *args, **kwargs):
        return _specialise(cls, Unary, _UNARY_OPS, "op", args, kwargs)

    @classmethod
    def of(cls, arg: Node):
        """Build the node for this operator class, e.g. ``Not.of(a)``."""
        return cls(cls.OP, arg)

    def _tptp(self) -> str:
        arg = self.arg
        text = arg._tptp()
        return f"{self.op} {text if isinstance(arg, (*_ATOMIC, Unary)) else f'({text})'}"


@dataclass(frozen=True, slots=True)
class Binary(Node):
    """A non-chaining infix operator."""

    op: str
    left: Node
    right: Node

    def __new__(cls, *args, **kwargs):
        return _specialise(cls, Binary, _BINARY_OPS, "op", args, kwargs)

    @classmethod
    def of(cls, left: Node, right: Node):
        """Build the node for this operator class, e.g. ``Implies.of(a, b)``."""
        return cls(cls.OP, left, right)

    def _tptp(self) -> str:
        op = self.op
        if op in _NONASSOC:
            return f"{_operand(self.left)} {op} {_operand(self.right)}"
        if op in ("=", "!="):
            return f"{_tight(self.left)} {op} {_tight(self.right)}"
        if op == ">":
            right = self.right
            rhs = right._tptp() if isinstance(right, Binary) and right.op == ">" else _tight(right)
            return f"{_tight(self.left)} > {rhs}"
        if op == "==":
            return f"{_tight(self.left)} == {self.right._tptp()}"
        if op == ":":  # general_data:general_term
            return f"{self.left._tptp()}:{self.right._tptp()}"
        return f"{self.left._tptp()} {op} {self.right._tptp()}"  # := --> <<


@dataclass(frozen=True, slots=True)
class NAry(Node):
    """A flat chain: ``|``, ``&``, or THF ``@``, ``*``, ``+``."""

    op: str
    args: tuple[Node, ...]

    def __new__(cls, *args, **kwargs):
        return _specialise(cls, NAry, _NARY_OPS, "op", args, kwargs)

    @classmethod
    def of(cls, *operands: Node):
        """Build the node for this operator class, e.g. ``Or.of(a, b)``."""
        return cls(cls.OP, operands)

    def _tptp(self) -> str:
        render = _operand if self.op in ("|", "&") else _tight
        return f" {self.op} ".join([render(arg) for arg in self.args])


@dataclass(frozen=True, slots=True)
class TypedVariable(Node):
    """A bound variable with a type, ``X: $int``. ``index`` is as for :class:`Variable`."""

    name: str
    type: Node
    index: int = field(default=0, compare=False)

    def _tptp(self) -> str:
        return f"{self.name}: {self.type._tptp()}"


@dataclass(frozen=True, slots=True)
class Quantified(Node):
    """``quantifier [variables] : body``. ``variables`` holds Variable or TypedVariable."""

    quantifier: str
    variables: tuple[Node, ...]
    body: Node

    def __new__(cls, *args, **kwargs):
        return _specialise(cls, Quantified, _QUANTIFIERS_BY_SYMBOL, "quantifier", args, kwargs)

    @classmethod
    def of(cls, variables: tuple[Node, ...], body: Node):
        """Build the node for this quantifier class, e.g. ``ForAll.of((X,), body)``."""
        return cls(cls.OP, tuple(variables), body)

    def _tptp(self) -> str:
        body = self.body
        if isinstance(body, Quantified):
            text = body._tptp()
        else:
            text = _operand(body)
        return f"{self.quantifier} [{_commas(self.variables)}] : {text}"


# --- operator subclasses --------------------------------------------------------------
# Each is a Binary/NAry/Unary/Quantified whose operator symbol is fixed. They add no
# fields, so they print and compare like their base, and the parser returns them.
# ``OP`` is the TPTP symbol. Operators without a class here (``{.}``, ``(.)``, ``#``)
# stay plain Unary/Quantified nodes.


class Not(Unary): __slots__ = (); OP = "~"
class Box(Unary): __slots__ = (); OP = "[.]"
class Diamond(Unary): __slots__ = (); OP = "<.>"


class Or(NAry): __slots__ = (); OP = "|"
class And(NAry): __slots__ = (); OP = "&"
class Apply(NAry): __slots__ = (); OP = "@"      # THF application
class Product(NAry): __slots__ = (); OP = "*"    # THF product type
class Sum(NAry): __slots__ = (); OP = "+"        # THF union type


class Implies(Binary): __slots__ = (); OP = "=>"
class ImpliedBy(Binary): __slots__ = (); OP = "<="
class Iff(Binary): __slots__ = (); OP = "<=>"
class Xor(Binary): __slots__ = (); OP = "<~>"
class Nor(Binary): __slots__ = (); OP = "~|"
class Nand(Binary): __slots__ = (); OP = "~&"
class Equals(Binary): __slots__ = (); OP = "="
class NotEquals(Binary): __slots__ = (); OP = "!="
class Arrow(Binary): __slots__ = (); OP = ">"        # mapping type, right-associative
class Definition(Binary): __slots__ = (); OP = "=="
class Assignment(Binary): __slots__ = (); OP = ":="
class Sequent(Binary): __slots__ = (); OP = "-->"
class Subtype(Binary): __slots__ = (); OP = "<<"
class Colon(Binary): __slots__ = (); OP = ":"        # key:value in general data


class ForAll(Quantified): __slots__ = (); OP = "!"
class Exists(Quantified): __slots__ = (); OP = "?"
class Lambda(Quantified): __slots__ = (); OP = "^"
class Choice(Quantified): __slots__ = (); OP = "@+"
class Description(Quantified): __slots__ = (); OP = "@-"
class TypeForAll(Quantified): __slots__ = (); OP = "!>"
class TypeExists(Quantified): __slots__ = (); OP = "?*"

_UNARY_OPS = {c.OP: c for c in (Not, Box, Diamond)}
_NARY_OPS = {c.OP: c for c in (Or, And, Apply, Product, Sum)}
_BINARY_OPS = {c.OP: c for c in (
    Implies, ImpliedBy, Iff, Xor, Nor, Nand, Equals, NotEquals, Arrow, Definition,
    Assignment, Sequent, Subtype, Colon)}
_QUANTIFIERS_BY_SYMBOL = {c.OP: c for c in (
    ForAll, Exists, Lambda, Choice, Description, TypeForAll, TypeExists)}


@dataclass(frozen=True, slots=True)
class Typing(Node):
    """A type declaration, ``atom : type``."""

    atom: str
    type: Node

    def _tptp(self) -> str:
        return f"{self.atom}: {self.type._tptp()}"


@dataclass(frozen=True, slots=True)
class ConnectiveTerm(Node):
    """THF connective used as a term, as in ``(&)``, or a TH1 constant such as ``!!``."""

    op: str

    def _tptp(self) -> str:
        return self.op if self.op in _TH1_TERMS else f"({self.op})"


@dataclass(frozen=True, slots=True)
class Index(Node):
    """``#term``, a parameter of a long connective."""

    term: Node

    def _tptp(self) -> str:
        return f"#{_tight(self.term)}"


@dataclass(frozen=True, slots=True)
class LongConnective(Node):
    """``{$box}`` or ``{$box(#a, $key == value)}``."""

    name: str
    params: tuple[Node, ...] = ()

    def _tptp(self) -> str:
        return f"{{{self.name}({_commas(self.params)})}}" if self.params else f"{{{self.name}}}"


@dataclass(frozen=True, slots=True)
class ModalAtom(Node):
    """TXF connective application, ``{$box} @ (a, b)``."""

    connective: LongConnective
    args: tuple[Node, ...]

    def _tptp(self) -> str:
        return f"{self.connective._tptp()} @ ({_commas(self.args)})"


@dataclass(frozen=True, slots=True)
class Let(Node):
    """``$let(types, definitions, body)``. Definitions are ``:=`` Binary nodes."""

    types: tuple[Typing, ...]
    definitions: tuple[Binary, ...]
    body: Node

    def _tptp(self) -> str:
        return f"$let([{_commas(self.types)}],[{_commas(self.definitions)}],{self.body._tptp()})"


@dataclass(frozen=True, slots=True)
class FormulaData(Node):
    """Formula quoted in non-logical data: ``$fof(...)``, ``$fot(...)``, etc."""

    language: str  # thf, tff, fof, cnf or fot
    formula: Node

    def _tptp(self) -> str:
        return f"${self.language}({self.formula._tptp()})"


@dataclass(frozen=True, slots=True)
class Name(Node):
    """A source that refers to another annotated formula by name."""

    name: str

    def _tptp(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True)
class Parent(Node):
    """An entry in the parents of an inference: ``source`` or ``source:details``."""

    source: Node
    details: Optional[Node] = None

    def _tptp(self) -> str:
        if self.details is None:
            return self.source._tptp()
        return f"{self.source._tptp()}:{self.details._tptp()}"


@dataclass(frozen=True, slots=True)
class Inference(Node):
    """``inference(rule, info, parents)``. ``info`` is a general list (Tuple)."""

    rule: str
    info: Tuple
    parents: tuple[Parent, ...]

    def _tptp(self) -> str:
        return f"inference({self.rule},{self.info._tptp()},[{_commas(self.parents)}])"


@dataclass(frozen=True, slots=True)
class Introduced(Node):
    """``introduced(kind, info, parents)``."""

    kind: str
    info: Tuple
    parents: tuple[Parent, ...]

    def _tptp(self) -> str:
        return f"introduced({self.kind},{self.info._tptp()},[{_commas(self.parents)}])"


@dataclass(frozen=True, slots=True)
class FileSource(Node):
    """``file(file_name)`` or ``file(file_name, name)``."""

    file: str
    name: Optional[str] = None

    def _tptp(self) -> str:
        return f"file({self.file},{self.name})" if self.name else f"file({self.file})"


@dataclass(frozen=True, slots=True)
class SourceList(Node):
    """Alternative sources: ``[source, source]``."""

    sources: tuple[Node, ...]

    def _tptp(self) -> str:
        return f"[{_commas(self.sources)}]"


@dataclass(frozen=True, slots=True)
class Annotations(Node):
    source: Node
    info: Optional[Tuple] = None

    def _tptp(self) -> str:
        if self.info is None:
            return self.source._tptp()
        return f"{self.source._tptp()},{self.info._tptp()}"


@dataclass(frozen=True, slots=True)
class Symbol:
    """A function, constant or predicate symbol: its name and its arity.

    ``f/1`` and ``f/2`` are different symbols, and a constant is a symbol of arity 0.
    ``str(symbol)`` is ``'f/2'``.
    """

    name: str
    arity: int

    def __str__(self) -> str:
        return f"{self.name}/{self.arity}"


@dataclass(frozen=True, slots=True)
class AnnotatedFormula(Node):
    language: str
    name: str
    role: str
    formula: Node
    annotations: Optional[Annotations] = None
    subrole: Optional[Node] = None  # the term in role-term
    line: int = field(default=0, compare=False, repr=False)
    #: The symbols this formula uses, in order of first appearance.
    symbols: tuple[Symbol, ...] = field(default=(), compare=False, repr=False)

    def _tptp(self) -> str:
        role = self.role if self.subrole is None else f"{self.role}-{self.subrole._tptp()}"
        tail = "" if self.annotations is None else f",{self.annotations._tptp()}"
        return f"{self.language}({self.name},{role},{self.formula._tptp()}{tail})."


@dataclass(frozen=True, slots=True)
class Include(Node):
    """An include directive. ``selection`` is ``None``, ``'*'``, or a tuple of names."""

    file: str
    selection: Union[None, str, tuple[str, ...]] = None
    space: Optional[str] = None
    line: int = field(default=0, compare=False, repr=False)

    @property
    def path(self) -> str:
        """The file name without quotes."""
        return unquote(self.file)

    def _tptp(self) -> str:
        parts = [self.file]
        if self.selection == "*":
            parts.append("*")
        elif self.selection is not None:
            parts.append(f"[{','.join(self.selection)}]")
        if self.space is not None:
            parts.append(self.space)
        return f"include({','.join(parts)})."


@dataclass(frozen=True, slots=True)
class Comment:
    """A comment, with its delimiters. ``%$`` and ``/*$`` pragmas are comments too."""

    text: str
    line: int


_HEADER_FIELD_RE = re.compile(r"%\s(\S[^:\n]*?)\s*:\s?(.*)")
_HEADER_CONTINUATION_RE = re.compile(r"%\s{2,}(\S.*)")
_HEADER_RULE_RE = re.compile(r"%-{5,}")
_RATING_RE = re.compile(r"(\d+(?:\.\d+)?|\?)(?:\s+v(\d[\w.]*))?")


@dataclass(frozen=True, slots=True)
class Header:
    """The standard comment header of a TPTP problem (``% Key : value`` lines).

    ``entries`` holds every field in file order as ``(key, value)``; a value that
    continues over several lines has them joined by single spaces. A key that occurs
    twice (``Refs`` for example) is listed twice. Use :meth:`get` for lookup.
    """

    entries: tuple[tuple[str, str], ...] = ()

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """The first value for ``key`` (case-insensitive), or ``default``."""
        key = key.lower()
        return next((v for k, v in self.entries if k.lower() == key), default)

    @property
    def problem(self) -> Optional[str]:
        """The problem name from the ``File`` field, e.g. ``'SET001-1'``."""
        file = self.get("File")
        return file.split(":")[0].strip() or None if file else None

    @property
    def status(self) -> Optional[str]:
        """The expected SZS status: ``'Theorem'``, ``'Unsatisfiable'``,
        ``'Satisfiable'``, ``'CounterSatisfiable'``, ``'Open'``, ... or ``None``."""
        return self.get("Status") or None

    @property
    def spc(self) -> Optional[str]:
        """The problem class, e.g. ``'CNF_UNS_RFO_SEQ_NHN'``."""
        return self.get("SPC") or None

    @property
    def ratings(self) -> tuple[tuple[float, Optional[str]], ...]:
        """Difficulty history as ``(rating, TPTP version)``, newest first.

        0.0 means every ATP system solves it and 1.0 that none does. Entries whose
        rating is unknown (``?``) are left out.
        """
        text = self.get("Rating") or ""
        return tuple((float(m[1]), m[2] or None) for m in _RATING_RE.finditer(text)
                     if m[1] != "?")

    @property
    def rating(self) -> Optional[float]:
        """The most recent difficulty rating in [0, 1], or ``None`` if unrated."""
        ratings = self.ratings
        return ratings[0][0] if ratings else None


def _parse_header(comments: tuple[Comment, ...]) -> Optional[Header]:
    # A problem's header sits between two ``%-----`` rules and has blank lines between its
    # sections. Without the rules, it is the first run of consecutive ``%`` lines.
    run: list[Comment] = []
    ruled = False
    for comment in comments:
        if not comment.text.startswith("%"):
            continue
        is_rule = _HEADER_RULE_RE.fullmatch(comment.text.rstrip()) is not None
        if not run:
            ruled = is_rule
        elif ruled:
            if is_rule:
                break
        elif comment.line != run[-1].line + 1:
            break
        run.append(comment)
    entries: list[list[str]] = []
    for comment in run:
        text = comment.text.rstrip()
        if (field_match := _HEADER_FIELD_RE.fullmatch(text)):
            entries.append([field_match[1], field_match[2].strip()])
        elif entries and (more := _HEADER_CONTINUATION_RE.fullmatch(text)):
            entries[-1][1] = f"{entries[-1][1]} {more[1].strip()}".strip()
    return Header(tuple((k, v) for k, v in entries)) if entries else None


@dataclass(frozen=True, slots=True)
class TPTPFile(Node):
    inputs: tuple[Union[AnnotatedFormula, Include], ...] = ()
    comments: tuple[Comment, ...] = ()
    filename: Optional[str] = field(default=None, compare=False)
    #: The symbol table: every symbol the formulae use, in order of first appearance.
    #: See :class:`Symbol` for what counts.
    symbols: tuple[Symbol, ...] = field(default=(), compare=False, repr=False)

    @property
    def formulae(self) -> tuple[AnnotatedFormula, ...]:
        return tuple(i for i in self.inputs if isinstance(i, AnnotatedFormula))

    @property
    def includes(self) -> tuple[Include, ...]:
        return tuple(i for i in self.inputs if isinstance(i, Include))

    @property
    def header(self) -> Optional[Header]:
        """The standard problem header parsed from the leading ``%`` comments, if any."""
        return _parse_header(self.comments)

    @property
    def dialect(self) -> list[str]:
        """The languages used by the formulae, once each, in order of first appearance.

        Unexpanded includes aren't read, so they don't contribute.
        """
        return list(dict.fromkeys(f.language for f in self.formulae))

    def _tptp(self) -> str:
        return "\n".join([i._tptp() for i in self.inputs])


def _symbol_table(formulae) -> tuple[Symbol, ...]:
    """The symbol table of some formulae: their symbols, each once, in order of first appearance."""
    seen: dict[Symbol, None] = {}
    for formula in formulae:
        seen.update(dict.fromkeys(formula.symbols))
    return tuple(seen)


_ATOMIC = (Variable, Function, Number, DistinctObject, Tuple, ConnectiveTerm,
           LongConnective, ModalAtom, Let, FormulaData)


def _commas(nodes: tuple[Node, ...]) -> str:
    return ",".join([n._tptp() for n in nodes])


def _tight(node: Node) -> str:
    """Render an operand of an operator that takes only atomic operands."""
    text = node._tptp()
    return text if isinstance(node, _ATOMIC) else f"({text})"


def _operand(node: Node) -> str:
    """Render an operand of a logical connective, or a quantifier body."""
    text = node._tptp()
    # Equations stay bare because CNF forbids parentheses around literals.
    bare = isinstance(node, (*_ATOMIC, Unary)) or (isinstance(node, Binary) and node.op in ("=", "!="))
    return text if bare else f"({text})"


def unquote(word: str) -> str:
    """Return the text of a single-quoted word; other words are returned unchanged."""
    if len(word) >= 2 and word[0] == "'" and word[-1] == "'":
        return re.sub(r"\\(.)", r"\1", word[1:-1])
    return word


def walk(node: Node) -> Iterator[Node]:
    """Yield ``node`` and all nodes beneath it, in depth-first pre-order."""
    stack: list[Node] = [node]
    while stack:
        current = stack.pop()
        yield current
        children: list[Node] = []
        for f in fields(current):  # type: ignore[arg-type]
            value = getattr(current, f.name)
            if isinstance(value, Node):
                children.append(value)
            elif isinstance(value, tuple):
                children.extend(v for v in value if isinstance(v, Node))
        stack.extend(reversed(children))



def _error_at(message: str, text: str, line: int, column: int, filename: Optional[str]) -> ParseError:
    lines = text.split("\n")
    source_line = lines[line - 1].rstrip("\r") if 0 < line <= len(lines) else None
    return ParseError(message, line=line, column=column, filename=filename, source_line=source_line)
