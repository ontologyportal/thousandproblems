"""Random TPTP generator, shared by the backend-equivalence tests and the benchmarks.

Everything is generated as an AST and printed with ``str()``, so the output is valid by
construction. :func:`decorate` then re-spaces it and sprinkles comments in, so the
lexer sees more than the printer's canonical layout, and :func:`mutate` breaks text
to exercise the error paths.
"""

import random

import thousandproblems as t

LETTERS = "abcdefghijklmnopqrstuvwxyz"


class Gen:
    def __init__(self, seed: int):
        self.r = random.Random(seed)

    # --- terms -------------------------------------------------------------------

    def word(self):
        r = self.r
        n = r.choice(["p", "q", "f", "g", "h", "c", "d", "sk", "mult", "inv"])
        return n + (str(r.randrange(40)) if r.random() < 0.5 else "")

    def quoted(self):
        return self.r.choice(["'a b'", "'Cat'", "'it\\'s'", "'$weird'", "'x\\\\y'", "'1a'", "'cat'"])

    def var(self):
        return t.Variable(self.r.choice("XYZUVW") + str(self.r.randrange(6)))

    def number(self):
        return t.Number(self.r.choice(["0", "1", "-12", "+3", "42", "1/2", "-4/6", "1.5", "-0.25", "1E3",
                                       "1.5e-1", "007e1", "123456789"]))

    def term(self, depth, *, numbers=True):
        r = self.r
        x = r.random()
        if depth <= 0 or x < 0.25:
            c = r.random()
            if c < 0.4:
                return self.var()
            if c < 0.45 and numbers:
                return self.number()
            if c < 0.48:
                return t.DistinctObject(r.choice(['"obj"', '"a\\"b"', '"x y"']))
            return t.Function(self.quoted() if c < 0.55 else self.word())
        n = r.choice([1, 1, 2, 2, 3])
        return t.Function(self.word(), tuple(self.term(depth - 1, numbers=numbers) for _ in range(n)))

    def atom(self, depth):
        r = self.r
        x = r.random()
        if x < 0.2:
            return t.Equals.of(self.term(depth), self.term(depth))
        if x < 0.28:
            return t.NotEquals.of(self.term(depth), self.term(depth))
        if x < 0.35:
            return t.Function(r.choice(["p", "q", "$true", "$false", "r1"]))
        n = r.choice([1, 2, 2, 3])
        return t.Function(self.word(), tuple(self.term(depth) for _ in range(n)))

    # --- formulae ----------------------------------------------------------------

    def cnf(self, depth=3):
        lits = []
        for _ in range(self.r.choice([1, 2, 2, 3, 4, 6])):
            a = self.atom(depth)
            if isinstance(a, t.NotEquals):
                lits.append(a)
            elif self.r.random() < 0.5:
                lits.append(t.Not.of(a))
            else:
                lits.append(a)
        return lits[0] if len(lits) == 1 else t.Or.of(*lits)

    def fof(self, depth=3):
        r = self.r
        if depth <= 0 or r.random() < 0.2:
            return self.atom(2)
        x = r.random()
        if x < 0.12:
            return t.Not.of(self.fof(depth - 1))
        if x < 0.35:
            vs = tuple(self.var() for _ in range(r.choice([1, 2, 3])))
            return r.choice([t.ForAll, t.Exists]).of(vs, self.fof(depth - 1))
        if x < 0.6:
            return r.choice([t.And, t.Or]).of(*[self.fof(depth - 1) for _ in range(r.choice([2, 2, 3, 4]))])
        op = r.choice([t.Implies, t.ImpliedBy, t.Iff, t.Xor, t.Nor, t.Nand])
        return op.of(self.fof(depth - 1), self.fof(depth - 1))

    def tff_type(self, depth=2):
        r = self.r
        base = r.choice(["$i", "$o", "$int", "$rat", "$real", "$tType", "t1", "t2", "list(t1)"])
        node = t.Function(base) if "(" not in base else t.Function("list", (t.Function("t1"),))
        if depth > 0 and r.random() < 0.4:
            return t.Arrow.of(node, self.tff_type(0))
        return node

    def tff(self, depth=3):
        r = self.r
        if depth <= 0 or r.random() < 0.25:
            if r.random() < 0.15:
                return t.Function("$less", (self.term(1), t.Number("3")))
            return self.atom(2)
        x = r.random()
        if x < 0.1:
            return t.Not.of(self.tff(depth - 1))
        if x < 0.35:
            vs = tuple(
                t.TypedVariable(v.name, self.tff_type(0)) if r.random() < 0.6 else v
                for v in (self.var() for _ in range(r.choice([1, 2, 3]))))
            return r.choice([t.ForAll, t.Exists]).of(vs, self.tff(depth - 1))
        if x < 0.6:
            return r.choice([t.And, t.Or]).of(*[self.tff(depth - 1) for _ in range(r.choice([2, 3]))])
        op = r.choice([t.Implies, t.Iff, t.Xor])
        return op.of(self.tff(depth - 1), self.tff(depth - 1))

    def thf_term(self, depth=2):
        r = self.r
        if depth <= 0 or r.random() < 0.35:
            return self.var() if r.random() < 0.4 else t.Function(self.word())
        x = r.random()
        if x < 0.5:
            return t.Apply.of(*[self.thf_term(depth - 1) for _ in range(r.choice([2, 3]))])
        vs = tuple(t.TypedVariable(self.var().name, self.tff_type(0)) for _ in range(r.choice([1, 2])))
        return t.Lambda.of(vs, self.thf_term(depth - 1))

    def thf(self, depth=3):
        r = self.r
        if depth <= 0 or r.random() < 0.25:
            if r.random() < 0.4:
                return t.Equals.of(self.thf_term(1), self.thf_term(1))
            return self.thf_term(2)
        x = r.random()
        if x < 0.1:
            return t.Not.of(self.thf(depth - 1))
        if x < 0.4:
            vs = tuple(t.TypedVariable(self.var().name, self.tff_type(0)) for _ in range(r.choice([1, 2])))
            return r.choice([t.ForAll, t.Exists]).of(vs, self.thf(depth - 1))
        if x < 0.65:
            return r.choice([t.And, t.Or]).of(*[self.thf(depth - 1) for _ in range(2)])
        return t.Implies.of(self.thf(depth - 1), self.thf(depth - 1))

    # --- annotations and whole files ---------------------------------------------

    def source(self, depth=2):
        r = self.r
        x = r.random()
        if depth <= 0 or x < 0.3:
            return t.Name(r.choice(["a1", "12", "ax", "c_3"]))
        if x < 0.5:
            return t.FileSource(r.choice(["'f.p'", "'Axioms/SET001-0.ax'", "g"]), r.choice([None, "ax1", "7"]))
        if x < 0.65:
            return t.SourceList((self.source(depth - 1), self.source(depth - 1)))
        parents = tuple(t.Parent(self.source(depth - 1), self.general(1) if r.random() < 0.3 else None)
                        for _ in range(r.choice([0, 1, 2, 3])))
        cls, word = r.choice([(t.Inference, "resolution"), (t.Inference, "'sup rule'"), (t.Introduced, "definition")])
        return cls(word, self.general_list(1), parents)

    def general(self, depth):
        r = self.r
        x = r.random()
        if depth <= 0 or x < 0.3:
            return r.choice([t.Function("thm"), t.Number("5"), self.var(), t.DistinctObject('"s"'), t.Function("'q q'")])
        if x < 0.55:
            return t.Function(self.word(), (self.general(depth - 1), self.general(depth - 1)))
        if x < 0.7:
            left = self.general(depth - 1)
            while isinstance(left, (t.Tuple, t.Binary)):    # general_data cannot be a list or a pair
                left = self.general(0)
            return t.Binary(":", left, self.general(depth - 1))
        if x < 0.8:
            lang = r.choice(["fof", "cnf", "fot"])
            body = {"fof": self.fof, "cnf": self.cnf}.get(lang, lambda d: self.term(2, numbers=False))(2)
            return t.FormulaData(lang, body)
        return self.general_list(depth - 1)

    def general_list(self, depth):
        return t.Tuple(tuple(self.general(depth) for _ in range(self.r.choice([0, 1, 2, 3]))))

    def annotated(self, language, index):
        r = self.r
        formula = {"cnf": self.cnf, "fof": self.fof, "tff": self.tff, "thf": self.thf}[language]()
        role = r.choice(["axiom", "hypothesis", "conjecture", "negated_conjecture", "plain", "lemma"])
        ann = None
        if r.random() < 0.4:
            ann = t.Annotations(self.source(), self.general_list(2) if r.random() < 0.5 else None)
        sub = t.Number("1") if r.random() < 0.03 else None
        name = r.choice([f"c{index}", str(index), f"'n {index}'", f"ax_{index}"])
        return t.AnnotatedFormula(language, name, role, formula, ann, sub)

    def typing(self, language, index):
        ty = self.tff_type()
        if language == "thf":
            ty = t.Arrow.of(ty, self.tff_type(0))
        return t.AnnotatedFormula(language, f"ty{index}", "type", t.Typing(f"s{index}", ty))

    def file(self, count, languages=("cnf", "fof", "tff", "thf")):
        r = self.r
        parts = ["%" + "-" * 70, "% File     : GEN001-1 : TPTP v9.0.0. Released v1.0.0.",
                 "% Status   : Theorem", "% Rating   : 0.10 v8.1.0, 0.00 v7.5.0", "%" + "-" * 70]
        for i in range(count):
            if r.random() < 0.02:
                parts.append(f"include('Axioms/G{i}.ax').")
            elif r.random() < 0.03:
                parts.append(f"/* block comment {i}\n   over lines */")
            lang = r.choice(languages)
            node = self.typing(lang, i) if lang in ("tff", "thf") and r.random() < 0.1 else self.annotated(lang, i)
            parts.append(str(node))
        return "\n".join(parts) + "\n"


def decorate(text: str, seed: int) -> str:
    """Re-space ``text`` token by token and add comments, keeping the tokens the same."""
    from thousandproblems import _pyparser
    r = random.Random(seed)
    tokens, _ = _pyparser._tokenize(text, None)      # the original comments are dropped
    pieces = []
    for tok in tokens[:-1]:
        pieces.append(tok.text)
        x = r.random()
        pieces.append("" if x < 0.45 else " " if x < 0.8 else "\n" if x < 0.88 else "\t " if x < 0.92
                      else "  \r\n" if x < 0.95 else " %c\u00e9\n" if x < 0.97 else " /* \u00fc\n */ ")
    joined = "".join(pieces)
    # Writing tokens with no separator can fuse them (``a`` ``b``); keep only faithful results.
    again = _pyparser._tokenize(joined, None)
    return joined if [x.text for x in again[0]] == [x.text for x in tokens] else text


def mutate(text: str, seed: int) -> str:
    """A damaged copy of ``text``: a few random character-level or token-level edits."""
    r = random.Random(seed)
    chars = list(text)
    for _ in range(r.choice([1, 1, 2, 3])):
        if not chars:
            break
        i = r.randrange(len(chars))
        x = r.random()
        if x < 0.3:
            del chars[i]
        elif x < 0.5:
            chars.insert(i, r.choice("()[],.:|&~!?=<>@#^+-*/'\"%$`_ \n\\ aXZ019e"))
        elif x < 0.65:
            chars[i] = r.choice("()[],.:|&~!?=<>@#^+-*/'\"%$`_ \n\\ aXZ019e\u00e9")
        elif x < 0.8:
            j = min(len(chars), i + r.randrange(1, 30))
            del chars[i:j]
        elif x < 0.9:
            chars[i:] = []
        else:
            j = r.randrange(len(chars))
            chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)
