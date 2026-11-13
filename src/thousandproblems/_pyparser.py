"""Pure-Python lexer and recursive-descent parser. Always available; the C extension
``_cparser`` is a faster port of exactly this module."""

from __future__ import annotations

import re
from typing import NamedTuple, Optional, Union

from .nodes import *  # noqa: F403
from .nodes import _NONASSOC, _TH1_TERMS, _deep_stack, _error_at, _symbol_table

# ---------------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------------

_OPERATORS = [
    "<=>", "<~>", "-->", "@@+", "@@-", "<.>", "[.]", "{.}", "(.)",
    "=>", "<=", "<<", "~|", "~&", "!=", "!!", "!>", "??", "?*", "@+", "@-", "@=", ":=", "==",
    "(", ")", "[", "]", "{", "}", ",", ".", ":", "|", "&", "~", "!", "?", "=", "@", ">",
    "*", "+", "-", "#", "^",
]

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>[ \t\r\n]+)
  | (?P<line_comment>%[^\r\n]*)
  | (?P<block_comment>/\*.*?\*/)
  | (?P<sq>'(?:[ -&(-\[\]-~]|\\\\|\\')+')
  | (?P<bq>`[A-Z][A-Za-z0-9_]*)
  | (?P<distinct>"(?:[ !#-\[\]-~]|\\\\|\\")+")
  | (?P<real>[+-]?(?:(?:0|[1-9][0-9]*)\.[0-9]+(?:[Ee][+-]?[0-9]+)?|[0-9]+[Ee][+-]?[0-9]+))
  | (?P<rational>[+-]?(?:0|[1-9][0-9]*)/[1-9][0-9]*)
  | (?P<integer>[+-]?(?:0|[1-9][0-9]*))
  | (?P<system>\$\$[A-Za-z0-9_]*)
  | (?P<defined>\$[A-Za-z0-9_]*)
  | (?P<upper>[A-Z][A-Za-z0-9_]*)
  | (?P<lower>[a-z][A-Za-z0-9_]*)
  | (?P<op>"""
    + "|".join(re.escape(op) for op in _OPERATORS)
    + r"""
    )
    """,
    re.VERBOSE | re.DOTALL,
)

_LOWER_WORD_RE = re.compile(r"[a-z][A-Za-z0-9_]*")
_NUMBER_KINDS = frozenset({"integer", "rational", "real"})
_WORD_KINDS = frozenset({"lower", "sq", "bq"})


class _Token(NamedTuple):
    kind: str
    text: str
    line: int
    column: int


def _tokenize(text: str, filename: Optional[str]) -> tuple[list[_Token], list[Comment]]:
    tokens: list[_Token] = []
    comments: list[Comment] = []
    match = _TOKEN_RE.match
    pos, line, line_start = 0, 1, 0
    size = len(text)
    while pos < size:
        found = match(text, pos)
        if found is None:
            char = text[pos]
            if char == "'":
                problem = "unterminated or invalid single-quoted word"
            elif char == '"':
                problem = "unterminated or invalid distinct object"
            elif text.startswith("/*", pos):
                problem = "unterminated block comment"
            else:
                problem = f"unexpected character {char!r}"
            raise _error_at(problem, text, line, pos - line_start + 1, filename)
        kind = found.lastgroup
        value = found.group()
        if kind == "ws" or kind == "block_comment":
            if kind == "block_comment":
                comments.append(Comment(value, line))
            newlines = value.count("\n")
            if newlines:
                line += newlines
                line_start = pos + value.rindex("\n") + 1
        elif kind == "line_comment":
            comments.append(Comment(value, line))
        else:
            tokens.append(_Token(kind, value, line, pos - line_start + 1))  # type: ignore[arg-type]
        pos = found.end()
    tokens.append(_Token("eof", "", line, pos - line_start + 1))
    return tokens, comments



# ---------------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------------

_SHORT_CONNECTIVES = frozenset({"[.]", "<.>", "{.}", "(.)"})
_TYPE_QUANTIFIERS = frozenset({"!>", "?*"})
_QUANTIFIERS = {
    "fof": frozenset({"!", "?"}),
    "tff": frozenset({"!", "?", "#"}),
    "thf": frozenset({"!", "?", "#", "^", "@+", "@-"}) | _TYPE_QUANTIFIERS,
}
_UNARY = {
    "fof": frozenset({"~"}),
    "tff": frozenset({"~"}) | _SHORT_CONNECTIVES,
    "thf": frozenset({"~"}) | _SHORT_CONNECTIVES,
}
_CHAINS = {
    "fof": frozenset({"|", "&"}),
    "tff": frozenset({"|", "&"}),
    "thf": frozenset({"|", "&", "@"}),
}
_THF_CONNECTIVE_TERMS = (_NONASSOC | _SHORT_CONNECTIVES
                         | frozenset({"|", "&", "=", "!=", "~"}))
_THF_DEFINITION_LHS = (Function, Tuple, ConnectiveTerm, LongConnective, Let, Number, DistinctObject)


class _Parser:
    def __init__(self, text: str, filename: Optional[str] = None):
        self.text = text
        self.filename = filename
        self.tokens, self.comments = _tokenize(text, filename)
        self.i = 0
        # Variable numbering and the symbols used, for the formula being parsed.
        self.numbering = False       # only inside a formula, not in annotations
        self.count = 0               # variables numbered so far in this formula
        self.vars: dict[str, int] = {}      # name -> index of the binding in scope (0: none)
        self.undo: list[tuple[str, int]] = []      # bindings to restore when a quantifier ends
        self.events: list[Optional[tuple[str, int]]] = []   # (name, arity) of each function use
        self.type_depth = 0          # > 0 while reading a THF type, whose names are not symbols
        self.symtab: dict[tuple[str, int], Symbol] = {}     # every symbol of the file

    # --- token helpers ------------------------------------------------------------

    @property
    def tok(self) -> _Token:
        return self.tokens[self.i]

    def peek(self, offset: int = 1) -> _Token:
        return self.tokens[min(self.i + offset, len(self.tokens) - 1)]

    def advance(self) -> _Token:
        tok = self.tokens[self.i]
        if tok.kind != "eof":
            self.i += 1
        return tok

    def at(self, text: str) -> bool:
        return self.tokens[self.i].text == text

    def accept(self, text: str) -> bool:
        if self.tokens[self.i].text == text:
            self.i += 1
            return True
        return False

    def expect(self, text: str) -> _Token:
        if self.tokens[self.i].text != text:
            raise self.error(f"expected {text!r}, found {self.describe()}")
        return self.advance()

    def describe(self, tok: Optional[_Token] = None) -> str:
        tok = tok or self.tok
        return "end of input" if tok.kind == "eof" else repr(tok.text)

    def error(self, message: str, tok: Optional[_Token] = None) -> ParseError:
        tok = tok or self.tok
        return _error_at(message, self.text, tok.line, tok.column, self.filename)

    # --- variable numbering and symbols ---------------------------------------------

    def begin_formula(self) -> None:
        self.numbering = True
        self.count = 0
        self.vars.clear()
        self.undo.clear()
        self.events.clear()
        self.type_depth = 0

    def bind(self, name: str) -> int:
        """A quantifier binds a new variable ``name``."""
        if not self.numbering:
            return 0
        self.count += 1
        self.undo.append((name, self.vars.get(name, 0)))
        self.vars[name] = self.count
        return self.count

    def use(self, name: str) -> int:
        """The index of the variable ``name`` in scope; a variable not bound is numbered on first use."""
        if not self.numbering:
            return 0
        index = self.vars.get(name, 0)
        if not index:
            self.count += 1
            index = self.vars[name] = self.count
        return index

    def release(self, mark: int) -> None:
        """A quantifier's scope has ended: restore what its variables shadowed."""
        undo = self.undo
        while len(undo) > mark:
            name, previous = undo.pop()
            self.vars[name] = previous

    def end_formula(self) -> tuple[Symbol, ...]:
        """Finish the formula: the symbols it used, in order of first appearance."""
        self.numbering = False
        symbols: dict[tuple[str, int], Symbol] = {}
        for key in self.events:
            if key not in symbols:
                symbol = self.symtab.get(key)
                if symbol is None:
                    symbol = self.symtab[key] = Symbol(*key)
                symbols[key] = symbol
        self.events.clear()
        return tuple(symbols.values())

    # --- files --------------------------------------------------------------------

    def parse_file(self) -> TPTPFile:
        inputs: list[Union[AnnotatedFormula, Include]] = []
        while self.tok.kind != "eof":
            tok = self.tok
            if tok.text == "include" and self.peek().text == "(":
                inputs.append(self.include())
            elif tok.text in _FORMULA_RULES and self.peek().text == "(":
                inputs.append(self.annotated_formula())
            else:
                raise self.error(f"expected an annotated formula or include, found {self.describe()}")
        return TPTPFile(tuple(inputs), tuple(self.comments), self.filename, tuple(self.symtab.values()))

    def annotated_formula(self) -> AnnotatedFormula:
        start = self.advance()
        language = start.text
        self.expect("(")
        name = self.name()
        self.expect(",")
        role, subrole = self.role()
        self.expect(",")
        self.begin_formula()
        formula = _FORMULA_RULES[language](self)
        symbols = self.end_formula()
        annotations = None
        if self.accept(","):
            source = self.source()
            info = self.general_list() if self.accept(",") else None
            annotations = Annotations(source, info)
        self.expect(")")
        self.expect(".")
        return AnnotatedFormula(language, name, role, formula, annotations, subrole, start.line, symbols)

    def include(self) -> Include:
        start = self.advance()
        self.expect("(")
        file = self.atomic_word()
        selection: Union[None, str, tuple[str, ...]] = None
        space = None
        if self.accept(","):
            if self.accept("*"):
                selection = "*"
            else:
                self.expect("[")
                names = [self.name()]
                while self.accept(","):
                    names.append(self.name())
                self.expect("]")
                selection = tuple(names)
            if self.accept(","):
                space = self.name()
        self.expect(")")
        self.expect(".")
        return Include(file, selection, space, start.line)

    def role(self) -> tuple[str, Optional[Node]]:
        tok = self.tok
        if tok.kind != "lower":
            raise self.error(f"expected a formula role, found {self.describe()}")
        self.advance()
        next_tok = self.tok
        if self.accept("-"):
            return tok.text, self.general_term()
        if next_tok.kind in _NUMBER_KINDS and next_tok.text[0] == "-":
            # "axiom-1" lexes the subrole as a negative number; split the sign off.
            self.tokens[self.i] = next_tok._replace(text=next_tok.text[1:], column=next_tok.column + 1)
            return tok.text, self.general_term()
        return tok.text, None

    def atomic_word(self) -> str:
        tok = self.tok
        if tok.kind not in _WORD_KINDS:
            raise self.error(f"expected an atomic word, found {self.describe()}")
        self.advance()
        return self.word_text(tok)

    @staticmethod
    def word_text(tok: _Token) -> str:
        if tok.kind == "sq" and _LOWER_WORD_RE.fullmatch(tok.text, 1, len(tok.text) - 1):
            return tok.text[1:-1]
        return tok.text

    def name(self) -> str:
        if self.tok.kind == "integer":
            return self.advance().text
        if self.tok.kind not in _WORD_KINDS:
            raise self.error(f"expected a name, found {self.describe()}")
        return self.atomic_word()

    # --- CNF and TCF --------------------------------------------------------------

    def cnf_formula(self) -> Node:
        if self.accept("("):
            formula = self.cnf_formula()
            self.expect(")")
            return formula
        literals = [self.cnf_literal()]
        while self.accept("|"):
            literals.append(self.cnf_literal())
        return literals[0] if len(literals) == 1 else NAry("|", tuple(literals))

    def cnf_literal(self) -> Node:
        if not self.accept("~"):
            return self.fof_atomic(allow_inequality=True)
        if self.accept("("):
            atom = self.fof_atomic(allow_inequality=False)
            self.expect(")")
        else:
            atom = self.fof_atomic(allow_inequality=False)
        return Unary("~", atom)

    def tcf_formula(self) -> Node:
        if self.at_atom_typing():
            return self.atom_typing("tff")
        return self.tcf_logic()

    def tcf_logic(self) -> Node:
        if self.at("!") and self.peek().text == "[":
            self.advance()
            mark = len(self.undo)
            variables = self.variable_list("tff")
            body = self.tcf_logic()
            self.release(mark)
            return Quantified("!", variables, body)
        return self.cnf_formula()

    # --- FOF ----------------------------------------------------------------------

    def fof_formula(self) -> Node:
        offset = 0
        while self.peek(offset).text == "(":
            offset += 1
        return self.fof_sequent() if self.peek(offset).text == "[" else self.logic("fof")

    def fof_sequent(self) -> Node:
        if self.accept("("):
            sequent = self.fof_sequent()
            self.expect(")")
            return sequent
        left = self.fof_tuple()
        self.expect("-->")
        return Binary("-->", left, self.fof_tuple())

    def fof_tuple(self) -> Tuple:
        self.expect("[")
        items: list[Node] = []
        if not self.at("]"):
            items.append(self.logic("fof"))
            while self.accept(","):
                items.append(self.logic("fof"))
        self.expect("]")
        return Tuple(tuple(items))

    def fof_atomic(self, *, allow_inequality: bool) -> Node:
        start = self.tok
        left = self.primary("fof")
        op = self.tok.text
        if op == "=" or (op == "!=" and allow_inequality):
            self.advance()
            return Binary(op, left, self.primary("fof"))
        if not isinstance(left, Function):
            raise self.error(f"expected a formula, found the term {self.describe(start)}", start)
        return left

    # --- shared FOF/TFF/THF logic formulae ----------------------------------------

    def logic(self, lang: str, *, as_term: bool = False) -> Node:
        """A logic formula; with ``as_term``, also a TFF term such as a number or tuple."""
        start = self.tok
        left = self.unit(lang)
        op = self.tok.text
        if op in _NONASSOC:
            self.require_formula(lang, left, start)
            self.advance()
            right_start = self.tok
            right = self.unit(lang)
            self.require_formula(lang, right, right_start)
            return Binary(op, left, right)
        if op in _CHAINS[lang]:
            self.require_formula(lang, left, start)
            args = [left]
            while self.accept(op):
                right_start = self.tok
                right = self.unit(lang)
                self.require_formula(lang, right, right_start)
                args.append(right)
            return NAry(op, tuple(args))
        if lang != "fof":
            if op == "==":
                return self.definition(lang, left, start)
            if op == "-->":
                return self.sequent(lang, left, start)
        if lang == "thf" and op in (">", "*", "+"):
            if start.text != "(" and (isinstance(left, Unary) or (
                    isinstance(left, Binary) and left.op in ("=", "!="))):
                raise self.error(f"{op!r} needs a unitary left operand; add parentheses", start)
            return self.thf_type_tail(left)
        if not as_term:
            self.require_formula(lang, left, start)
        return left

    def require_formula(self, lang: str, node: Node, start: _Token) -> None:
        """Numbers, distinct objects and tuples can't stand alone as TFF formulae."""
        if lang == "tff" and isinstance(node, (Number, DistinctObject, Tuple)):
            raise self.error(f"expected a formula, found the term {self.describe(start)}", start)

    def definition(self, lang: str, left: Node, start: _Token) -> Node:
        allowed = _THF_DEFINITION_LHS if lang == "thf" else (Function, ModalAtom, Let)
        if not isinstance(left, allowed):
            raise self.error("the left side of '==' must be atomic", start)
        self.expect("==")
        return Binary("==", left, self.logic(lang, as_term=True))

    def sequent(self, lang: str, left: Node, start: _Token) -> Node:
        if not isinstance(left, Tuple):
            raise self.error("the left side of '-->' must be a tuple", start)
        self.expect("-->")
        if not self.at("["):
            raise self.error(f"expected a tuple after '-->', found {self.describe()}")
        return Binary("-->", left, self.primary(lang))

    def unit(self, lang: str) -> Node:
        tok = self.tokens[self.i]
        text = tok.text
        if text in _UNARY[lang]:
            self.advance()
            # The grammar requires TFF/THF to write ~ (a = b), but ~ a = b is unambiguous
            # and older TPTP versions allow it, so all languages accept it, as in FOF.
            arg_start = self.tok
            arg = self.unit(lang)
            self.require_formula(lang, arg, arg_start)
            return Unary(text, arg)
        if text in _QUANTIFIERS[lang] and self.peek().text == "[":
            return self.quantified(lang)
        if lang == "fof":
            if self.accept("("):
                formula = self.logic("fof")
                self.expect(")")
                return formula
            return self.fof_atomic(allow_inequality=True)
        left = self.primary(lang)
        op = self.tok.text
        if op == "=" or op == "!=":
            self.advance()
            return Binary(op, left, self.primary(lang))
        return left

    def quantified(self, lang: str) -> Quantified:
        quantifier = self.advance().text
        mark = len(self.undo)
        variables = self.variable_list(lang)
        body_start = self.tok
        body = self.unit(lang)
        self.require_formula(lang, body, body_start)
        self.release(mark)
        return Quantified(quantifier, variables, body)

    def variable_list(self, lang: str) -> tuple[Node, ...]:
        self.expect("[")
        variables = [self.variable(lang)]
        while self.accept(","):
            variables.append(self.variable(lang))
        self.expect("]")
        self.expect(":")
        return tuple(variables)

    def variable(self, lang: str) -> Node:
        tok = self.tok
        if tok.kind != "upper":
            raise self.error(f"expected a variable, found {self.describe()}")
        self.advance()
        index = self.bind(tok.text)
        if lang == "fof":
            return Variable(tok.text, index)
        if lang == "thf":
            self.expect(":")
            self.type_depth += 1
            var_type = self.thf_type()
            self.type_depth -= 1
            return TypedVariable(tok.text, var_type, index)
        if self.accept(":"):
            return TypedVariable(tok.text, self.tff_atomic_type(), index)
        return Variable(tok.text, index)

    def primary(self, lang: str) -> Node:
        """A term, or the atomic formula that has the same shape as one."""
        tok = self.tokens[self.i]
        kind = tok.kind
        if kind == "upper":
            self.i += 1
            return Variable(tok.text, self.use(tok.text))
        if kind in _NUMBER_KINDS:
            self.i += 1
            return Number(tok.text)
        if kind == "distinct":
            self.i += 1
            return DistinctObject(tok.text)
        if kind in _WORD_KINDS or kind == "defined" or kind == "system":
            if tok.text == "$let" and lang != "fof" and self.peek().text == "(":
                return self.let(lang)
            self.i += 1
            name = self.word_text(tok)
            # Reserve this use's place in the order of first appearance now, and fill in the
            # arity once the arguments are read. $-symbols are not recorded.
            slot = -1
            if self.numbering and not self.type_depth and kind in _WORD_KINDS:
                slot = len(self.events)
                self.events.append(None)
            if self.accept("("):
                args = [self.argument(lang)]
                while self.accept(","):
                    args.append(self.argument(lang))
                self.expect(")")
                if slot >= 0:
                    self.events[slot] = (name, len(args))
                return Function(name, tuple(args))
            if slot >= 0:
                self.events[slot] = (name, 0)
            return Function(name)
        if lang != "fof":
            text = tok.text
            if text == "(":
                if (lang == "thf" and self.peek().text in _THF_CONNECTIVE_TERMS
                        and self.peek(2).text == ")"):
                    op = self.peek().text
                    self.i += 3
                    return ConnectiveTerm(op)
                self.advance()
                formula = self.logic(lang)
                self.expect(")")
                return formula
            if text == "[":
                return self.tuple(lang)
            if text == "{":
                return self.long_connective(lang)
            if lang == "thf" and text in _TH1_TERMS:
                self.advance()
                return ConnectiveTerm(text)
        raise self.error(f"expected a term, found {self.describe()}")

    def argument(self, lang: str) -> Node:
        if lang == "fof":
            return self.primary("fof")
        return self.logic(lang, as_term=True)

    def tuple(self, lang: str) -> Tuple:
        self.expect("[")
        items: list[Node] = []
        if not self.at("]"):
            items.append(self.argument(lang))
            while self.accept(","):
                items.append(self.argument(lang))
        self.expect("]")
        return Tuple(tuple(items))

    def long_connective(self, lang: str) -> Node:
        self.expect("{")
        tok = self.tok
        if tok.kind not in ("defined", "system"):
            raise self.error(f"expected a connective name, found {self.describe()}")
        self.advance()
        params: list[Node] = []
        if self.accept("("):
            params.append(self.connective_parameter(lang))
            while self.accept(","):
                params.append(self.connective_parameter(lang))
            self.expect(")")
        self.expect("}")
        connective = LongConnective(tok.text, tuple(params))
        if lang != "tff":
            return connective
        self.expect("@")
        self.expect("(")
        args = [self.argument("tff")]
        while self.accept(","):
            args.append(self.argument("tff"))
        self.expect(")")
        return ModalAtom(connective, tuple(args))

    def connective_parameter(self, lang: str) -> Node:
        if self.accept("#"):
            return Index(self.primary("tff"))
        start = self.tok
        param = self.logic(lang, as_term=True)
        if not (isinstance(param, Binary) and param.op == "=="):
            raise self.error("expected '#index' or 'key == value'", start)
        return param

    def let(self, lang: str) -> Let:
        self.expect("$let")
        self.expect("(")
        if self.accept("["):
            types = [self.atom_typing(lang)]
            while self.accept(","):
                types.append(self.atom_typing(lang))
            self.expect("]")
        else:
            types = [self.atom_typing(lang)]
        self.expect(",")
        if self.at("[") and not self.bracket_is_let_lhs():
            self.advance()
            definitions = [self.let_definition(lang)]
            while self.accept(","):
                definitions.append(self.let_definition(lang))
            self.expect("]")
        else:
            definitions = [self.let_definition(lang)]
        self.expect(",")
        body = self.logic(lang, as_term=lang == "tff")
        self.expect(")")
        return Let(tuple(types), tuple(definitions), body)

    def bracket_is_let_lhs(self) -> bool:
        """Tell the tuple in ``[a,b] := t`` from the definition list ``[a := t, ...]``."""
        depth = 0
        for j in range(self.i, len(self.tokens)):
            text = self.tokens[j].text
            if text == "[":
                depth += 1
            elif text == "]":
                depth -= 1
                if depth == 0:
                    return self.tokens[j + 1].text == ":="
        return False

    def let_definition(self, lang: str) -> Binary:
        start = self.tok
        if lang == "thf":
            left = self.logic("thf")
        else:
            left = self.primary("tff")
            if not isinstance(left, (Function, Tuple)):
                raise self.error("the left side of ':=' must be an atom or tuple", start)
        self.expect(":=")
        return Binary(":=", left, self.logic(lang, as_term=lang == "tff"))

    # --- type declarations --------------------------------------------------------

    def at_atom_typing(self) -> bool:
        j = self.i
        while self.tokens[j].text == "(":
            j += 1
        tok = self.tokens[j]
        return (tok.kind in _WORD_KINDS or tok.kind == "distinct") and self.tokens[j + 1].text == ":"

    def atom_typing(self, lang: str) -> Typing:
        if self.accept("("):
            typing = self.atom_typing(lang)
            self.expect(")")
            return typing
        tok = self.tok
        if tok.kind not in _WORD_KINDS and tok.kind != "distinct":
            raise self.error(f"expected an atom to declare a type for, found {self.describe()}")
        self.advance()
        self.expect(":")
        if lang == "thf":
            self.type_depth += 1
            kind_type = self.thf_type()
            self.type_depth -= 1
        else:
            kind_type = self.tff_type()
        return Typing(self.word_text(tok), kind_type)

    def at_subtype(self) -> bool:
        tok = self.tok
        return ((tok.kind in _WORD_KINDS or tok.kind in ("defined", "system", "distinct"))
                and self.peek().text == "<<")

    def subtype(self) -> Binary:
        left = self.subtype_atom()
        self.expect("<<")
        return Binary("<<", left, self.subtype_atom())

    def subtype_atom(self) -> Node:
        tok = self.tok
        if tok.kind == "distinct":
            self.advance()
            return DistinctObject(tok.text)
        if tok.kind in _WORD_KINDS or tok.kind in ("defined", "system"):
            self.advance()
            return Function(self.word_text(tok))
        raise self.error(f"expected a type name, found {self.describe()}")

    def tff_formula(self) -> Node:
        if self.at_atom_typing():
            return self.atom_typing("tff")
        if self.at_subtype():
            return self.subtype()
        return self.logic("tff")

    def thf_formula(self) -> Node:
        if self.at_atom_typing():
            return self.atom_typing("thf")
        if self.at_subtype():
            return self.subtype()
        return self.logic("thf")

    def tff_type(self, *, in_parens: bool = False) -> Node:
        if self.tok.text in _TYPE_QUANTIFIERS:
            quantifier = self.advance().text
            mark = len(self.undo)
            variables = self.variable_list("tff")
            body_start = self.tok
            body = self.tff_type() if self.tok.text in _TYPE_QUANTIFIERS else self.tff_type_primary()
            if isinstance(body, NAry):
                raise self.error("a product type must be followed by '>'", body_start)
            self.release(mark)
            return Quantified(quantifier, variables, body)
        start = self.tok
        left = self.tff_type_primary()
        if isinstance(left, (Binary, Quantified)) and self.tok.text in (">", "*"):
            raise self.error(f"this type can't be an operand of {self.tok.text!r}", start)
        if self.accept(">"):
            return Binary(">", left, self.tff_atomic_type())
        if in_parens and self.at("*"):
            args = [left]
            while self.accept("*"):
                args.append(self.tff_atomic_type())
            return NAry("*", tuple(args))
        if isinstance(left, NAry):
            raise self.error("a product type must be followed by '>'", start)
        return left

    def tff_type_primary(self) -> Node:
        tok = self.tok
        if self.accept("("):
            inner = self.tff_type(in_parens=True)
            self.expect(")")
            return inner
        if tok.text == "[":
            self.advance()
            items = [self.tff_type()]
            while self.accept(","):
                items.append(self.tff_type())
            self.expect("]")
            return Tuple(tuple(items))
        if tok.kind == "upper":
            self.advance()
            return Variable(tok.text, self.use(tok.text))
        if tok.kind == "defined":
            self.advance()
            return Function(tok.text)
        if tok.kind in _WORD_KINDS:
            self.advance()
            name = self.word_text(tok)
            if self.accept("("):
                args = [self.tff_atomic_type()]
                while self.accept(","):
                    args.append(self.tff_atomic_type())
                self.expect(")")
                return Function(name, tuple(args))
            return Function(name)
        raise self.error(f"expected a type, found {self.describe()}")

    def tff_atomic_type(self) -> Node:
        start = self.tok
        node = self.tff_type_primary()
        if not isinstance(node, (Variable, Function, Tuple)):
            raise self.error("expected an atomic type", start)
        return node

    def thf_type(self) -> Node:
        left = self.thf_unitary()
        if self.tok.text in (">", "*", "+", "@"):
            return self.thf_type_tail(left)
        return left

    def thf_type_tail(self, left: Node) -> Node:
        op = self.tok.text
        if op == ">":
            operands = [left]
            while self.accept(">"):
                operands.append(self.thf_unitary())
            result = operands[-1]
            for operand in reversed(operands[:-1]):
                result = Binary(">", operand, result)
            return result
        args = [left]
        read = self.unit if op == "@" else None
        while self.accept(op):
            args.append(read("thf") if read else self.thf_unitary())
        return NAry(op, tuple(args))

    def thf_unitary(self) -> Node:
        if self.tok.text in _QUANTIFIERS["thf"] and self.peek().text == "[":
            return self.quantified("thf")
        return self.primary("thf")

    # --- annotations and non-logical data -----------------------------------------

    def source(self) -> Node:
        tok = self.tok
        if tok.text == "[":
            self.advance()
            sources = [self.source()]
            while self.accept(","):
                sources.append(self.source())
            self.expect("]")
            return SourceList(tuple(sources))
        if self.peek().text == "(":
            if tok.text == "inference":
                return self.derivation_source(Inference)
            if tok.text == "introduced":
                return self.derivation_source(Introduced)
            if tok.text == "file":
                self.i += 2
                file = self.atomic_word()
                name = self.name() if self.accept(",") else None
                self.expect(")")
                return FileSource(file, name)
        return Name(self.name())

    def derivation_source(self, node_type: type) -> Node:
        self.i += 2
        word = self.atomic_word()
        self.expect(",")
        info = self.general_list()
        self.expect(",")
        self.expect("[")
        parents: list[Parent] = []
        if not self.at("]"):
            parents.append(self.parent())
            while self.accept(","):
                parents.append(self.parent())
        self.expect("]")
        self.expect(")")
        return node_type(word, info, tuple(parents))

    def parent(self) -> Parent:
        source = self.source()
        return Parent(source, self.general_term() if self.accept(":") else None)

    def general_list(self) -> Tuple:
        self.expect("[")
        items: list[Node] = []
        if not self.at("]"):
            items.append(self.general_term())
            while self.accept(","):
                items.append(self.general_term())
        self.expect("]")
        return Tuple(tuple(items))

    def general_term(self) -> Node:
        if self.at("["):
            return self.general_list()
        data = self.general_data()
        if self.accept(":"):
            return Binary(":", data, self.general_term())
        return data

    def general_data(self) -> Node:
        tok = self.tok
        kind = tok.kind
        if kind == "upper":
            self.advance()
            return Variable(tok.text, self.use(tok.text))
        if kind in _NUMBER_KINDS:
            self.advance()
            return Number(tok.text)
        if kind == "distinct":
            self.advance()
            return DistinctObject(tok.text)
        if kind in _WORD_KINDS:
            self.advance()
            name = self.word_text(tok)
            if self.accept("("):
                args = [self.general_term()]
                while self.accept(","):
                    args.append(self.general_term())
                self.expect(")")
                return Function(name, tuple(args))
            return Function(name)
        if tok.text in _FORMULA_DATA_RULES and self.peek().text == "(":
            self.i += 2
            formula = _FORMULA_DATA_RULES[tok.text](self)
            self.expect(")")
            return FormulaData(tok.text[1:], formula)
        raise self.error(f"expected general data, found {self.describe()}")


_FORMULA_RULES = {
    "thf": _Parser.thf_formula,
    "tff": _Parser.tff_formula,
    "tcf": _Parser.tcf_formula,
    "fof": _Parser.fof_formula,
    "cnf": _Parser.cnf_formula,
    "tpi": _Parser.fof_formula,
}

_FORMULA_DATA_RULES = {
    "$thf": _Parser.thf_formula,
    "$tff": _Parser.tff_formula,
    "$fof": _Parser.fof_formula,
    "$cnf": _Parser.cnf_formula,
    "$fot": lambda parser: parser.primary("fof"),
}


# ---------------------------------------------------------------------------------
# Entry points used by the package's public functions
# ---------------------------------------------------------------------------------


def parse(text: str, filename: Optional[str] = None) -> TPTPFile:
    with _deep_stack():
        parser = _Parser(text, filename)
        try:
            return parser.parse_file()
        except RecursionError:
            raise parser.error("formula is too deeply nested") from None


def parse_formula(text: str, language: str) -> Node:
    with _deep_stack():
        parser = _Parser(text)
        parser.begin_formula()
        try:
            formula = _FORMULA_RULES[language](parser)
        except RecursionError:
            raise parser.error("formula is too deeply nested") from None
        if parser.tok.kind != "eof":
            raise parser.error(f"unexpected {parser.describe()} after the formula")
        return formula
