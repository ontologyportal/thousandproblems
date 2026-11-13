"""Tests for the parser. Run with: python -m unittest discover -s tests (from the project root).

The backend under test is whatever ``thousandproblems.BACKEND`` reports; set
``THOUSANDPROBLEMS_PURE_PYTHON=1`` to run the whole suite against the pure-Python parser."""

import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

import thousandproblems as tptp
from thousandproblems import (Binary, DistinctObject, FileSource, Function, Include, Inference, Introduced,
                  LongConnective, ModalAtom, NAry, Name, Number, ParseError, Quantified,
                  SourceList, Tuple, TypedVariable, Typing, Unary, Variable)

# Valid inputs, by language. Each is also checked to survive a print/parse round trip.
VALID = {
    "fof": [
        "p",
        "$true",
        "$$sys(a)",
        "p(X,f(Y),'a b',1,\"obj\",$sum(1,2))",
        "X = Y",
        "1 != 2.5",
        "\"a\" = \"b\"",
        "~ p",
        "~ ~ p",
        "~ a = b",
        "~ a != b",
        "! [X,Y] : p(X,Y)",
        "? [X] : ! [Y] : X = Y",
        "! [X] : a = X",
        "~ ! [X] : p(X)",
        "(! [X] : p(X)) & q",
        "! [X] : p(X) & q",
        "a | b | c",
        "a & b & c",
        "a | (b & c)",
        "(a | b) | c",
        "a <=> b",
        "a => b",
        "a <= b",
        "a <~> b",
        "a ~| b",
        "a ~& b",
        "(a => b) => c",
        "((a))",
        "`Abc",
        "[a,b] --> [c]",
        "[] --> []",
        "([a] --> [b])",
    ],
    "cnf": [
        "p",
        "~ p",
        "~ (p)",
        "p | ~ q | r(X)",
        "(p | q)",
        "((p))",
        "X = Y | a != b",
        "~ a = b",
        "$false",
    ],
    "tcf": [
        "p(X) | ~ q",
        "! [X: $int] : p(X) | q",
        "! [X: $int, Y] : ! [Z: t] : p(X,Y,Z)",
        "f: $i > $o",
    ],
    "tpi": ["p => q", "[a] --> [b]"],
    "tff": [
        "f: $i",
        "(f: $i)",
        "((f: t))",
        "\"obj\": $i",
        "t: $tType",
        "p: (a * b * c) > $o",
        "p: ((a * b) * c) > $o",
        "p: a > $o",
        "p: (a > $o)",
        "p: [a,b] > $o",
        "l: !> [A: $tType] : (list(A) > $i)",
        "c: !> [A: $tType, B: $tType] : !> [C: $tType] : pair(A,B)",
        "c: !> [A: $tType] : A",
        "a << b",
        "$int << b",
        "! [X: $int, Y] : (p(X) => q(Y))",
        "# [X: w] : p(X)",
        "p(1) & q(1/2) & r(1.5e-3)",
        "$less(X,3)",
        "~ (a = b)",
        "~ a = b",
        "a = b | c != d",
        "X",
        "X => Y",
        "f(a & b, c)",
        "f([a,b],1,\"s\")",
        "[a,b] = X",
        "[] = X",
        "[a] --> [b]",
        "[] --> []",
        "(p) = q",
        "(a & b) = (c | d)",
        "$ite(p,a,b) = c",
        "$let(f: $i, f := a, p(f))",
        "$let([f: $i, g: $i], [f := a, g := b], p(f,g))",
        "$let([f: $i, g: $i], [f,g] := [a,b], p(f,g))",
        "$let(f: $i > $i, f(X) := X, p(f(a)))",
        "$let(f: $o, f := (a & b), f)",
        "[.] p",
        "<.> p",
        "{.} p",
        "(.) p",
        "[.] [.] p",
        "{$box} @ (p)",
        "{$box(#a)} @ (p)",
        "{$box(#a,#(f(b)))} @ (p,q)",
        "{$$mine($k == v)} @ (p)",
        "$modal == [$constants == $constant,$modalities == $modal_system_K]",
        "p(a) == b & c",
    ],
    "thf": [
        "f: $i",
        "(f: $i)",
        "f: $i > $i > $o",
        "f: ($i > $i) > $o",
        "f: $i * $i",
        "f: $i + $i",
        "f: list @ $i",
        "f: !> [A: $tType] : (A > A)",
        "a << b",
        "$i > $o",
        "a * b * c",
        "a + b + c",
        "f @ a @ b",
        "(f @ a) = b",
        "f @ a = b",
        "(^ [X: $i] : X) @ a",
        "^ [X: $i, Y: $i > $o] : (Y @ X)",
        "! [X: $i] : (p @ X)",
        "? [X: $i] : p @ X",
        "@+ [X: $i] : p @ X",
        "@- [X: $i] : p @ X",
        "!> [T: $tType] : (T > $o)",
        "?* [T: $tType] : T",
        "# [X: $i] : p @ X",
        "a | b | c",
        "a & b",
        "a => b",
        "~ a",
        "~ a = b",
        "(~) @ a",
        "(&) @ a @ b",
        "(=) @ a @ b",
        "(!=) @ a",
        "(<=>) @ a",
        "(~|) @ a",
        "(|) @ a",
        "([.]) @ a",
        "!! @ p",
        "?? @ p",
        "@@+ @ p",
        "@@- @ p",
        "@= @ a",
        "[a,b,c]",
        "[]",
        "[a & b,c] --> [d]",
        "$distinct(a,b)",
        "f(a,b)",
        "$$sys(a & b)",
        "f == ^ [X: $i] : X",
        "$let(f: $i, f := a, p @ f)",
        "$let([f: $i], [f := a], p @ f)",
        "$let([f: $i, g: $i], [f,g] := [a,b], f = g)",
        "[.] p",
        "{$box} @ p",
        "{$box(#a)} @ p",
        "{$box($k == v)} @ p",
        "1 = X",
    ],
}

# Inputs the grammar forbids. Accepting these would make the parser useless for validation.
INVALID = {
    "fof": [
        "",
        "X",
        "1",
        "\"obj\"",
        "a | b & c",
        "a & b | c",
        "a => b => c",
        "a <=> b <=> c",
        "a | b => c",
        "p(a & b)",
        "p((a))",
        "p()",
        "(a) = b",
        "a = b = c",
        "! X : p(X)",
        "! [] : p",
        "! [x] : p",
        "! [X: $i] : p(X)",
        "^ [X] : p",
        "f: $i",
        "[a,b]",
        "a @ b",
        "[.] p",
        "007 = a",
        "1/0 = a",
        ".5 = a",
        "p(",
        "p)",
        "'' = a",
        "'unterminated",
    ],
    "cnf": [
        "! [X] : p(X)",
        "a & b",
        "a => b",
        "~ ~ a",
        "(a) | b",
        "a | (b | c)",
        "~ a != b",
        "~ (a != b)",
        "X",
    ],
    "tcf": ["? [X] : p(X)", "a & b", "! [X] : (! [Y] : p)"],
    "tff": [
        "1",
        "\"obj\"",
        "[a,b]",
        "1 & p",
        "p & 1",
        "~ 1",
        "! [X] : 1",
        "(1)",
        "a | b & c",
        "a => b => c",
        "! [X: a > b] : p",
        "$tType > $o > $i ==",
        "p: a * b > $o",
        "p: (a * b)",
        "p: a > b > c",
        "p: a > (b > c)",
        "p: (a > b) > c",
        "l: !> [A: $tType] : A > A",
        "f: 1",
        "f: $$sys",
        "a @ b",
        "a > b",
        "^ [X: $i] : X",
        "(f: $i) & p",
        "{$box} p",
        "{$box(a)} @ (p)",
        "{foo} @ (p)",
        "1 == 2",
        "a --> [b]",
        "[a] --> b",
        "$let(f: $i, a, p)",
    ],
    "thf": [
        "! [X] : p",
        "^ [X] : X",
        "a | b & c",
        "a @ b & c",
        "a > b | c",
        "a * b + c",
        "~ a > b",
        "a --> [b]",
        "(*) @ a",
        "(>) @ a",
    ],
}


def round_trip(text, language):
    first = tptp.parse_formula(text, language)
    second = tptp.parse_formula(str(first), language)
    return first, second


class ValidInputs(unittest.TestCase):
    def test_accepted_and_round_trips(self):
        for language, examples in VALID.items():
            for text in examples:
                with self.subTest(language=language, text=text):
                    first, second = round_trip(text, language)
                    self.assertEqual(first, second)


class InvalidInputs(unittest.TestCase):
    def test_rejected(self):
        for language, examples in INVALID.items():
            for text in examples:
                with self.subTest(language=language, text=text):
                    with self.assertRaises(ParseError):
                        tptp.parse_formula(text, language)

    def test_top_level_rejections(self):
        for text in [
            "fof(a,axiom,p)",                     # missing '.'
            "fof(a,axiom).",
            "fof(A,axiom,p).",                    # names can't be upper-case
            "fof(a,Axiom,p).",
            "fof($a,axiom,p).",
            "nope(a,axiom,p).",
            "p.",
            "fof(a,axiom,p). junk",
            "include(file).x",
            "include(X).",
            "include('f',[]).",
            "include('f',[a],).",
            "fof(a,axiom,p,creator(me)).",        # not a <source> in this grammar version
            "fof(a,axiom,p,file('f'),notalist).",
            "fof(a,axiom,p,inference(r,[])).",
            "fof(a,axiom,p,inference(r,[],x)).",
            "fof(a,axiom,p,file('f'),[$foo]).",
        ]:
            with self.subTest(text=text):
                with self.assertRaises(ParseError):
                    tptp.parse(text)


class Structure(unittest.TestCase):
    def test_annotated_formula_fields(self):
        f = tptp.parse("fof(ax1, axiom, p => q).\n").formulae[0]
        self.assertEqual((f.language, f.name, f.role), ("fof", "ax1", "axiom"))
        self.assertEqual(f.formula, Binary("=>", Function("p"), Function("q")))
        self.assertIsNone(f.annotations)
        self.assertEqual(f.line, 1)

    def test_integer_and_quoted_names(self):
        names = [f.name for f in tptp.parse("cnf(12,axiom,p). cnf('a b',axiom,p). cnf('ok',axiom,p).").formulae]
        self.assertEqual(names, ["12", "'a b'", "ok"])

    def test_role_with_subrole(self):
        for text, sub in [
            ("tff(a,type-datatype,t: $tType).", Function("datatype")),
            ("fof(a,axiom-file('x',y),p).", Function("file", (Function("x"), Function("y")))),
            ("fof(a,axiom-1,p).", Number("1")),
        ]:
            with self.subTest(text=text):
                file = tptp.parse(text)
                self.assertEqual(file.formulae[0].subrole, sub)
                self.assertEqual(tptp.parse(str(file)), file)

    def test_quote_normalisation(self):
        self.assertEqual(tptp.parse_formula("'cat'"), Function("cat"))
        self.assertEqual(tptp.parse_formula("cat"), tptp.parse_formula("'cat'"))
        for different in ["'Cat'", "'1a'", "'$true'", "'a b'"]:
            self.assertEqual(tptp.parse_formula(different), Function(different))
        self.assertNotEqual(tptp.parse_formula("'$true'"), tptp.parse_formula("$true"))

    def test_unquote(self):
        self.assertEqual(tptp.unquote(r"'it\'s \\ ok'"), "it's \\ ok")
        self.assertEqual(tptp.unquote("plain"), "plain")

    def test_distinct_object_escapes(self):
        node = tptp.parse_formula(r'"a\"b\\c" = X')
        self.assertEqual(node.left, DistinctObject(r'"a\"b\\c"'))

    def test_numbers(self):
        for text, kind, value in [
            ("0", "integer", 0),
            ("-12", "integer", -12),
            ("+3", "integer", 3),
            ("1/2", "rational", Fraction(1, 2)),
            ("-4/6", "rational", Fraction(-2, 3)),
            ("1.5", "real", 1.5),
            ("-0.25", "real", -0.25),
            ("1E3", "real", 1000.0),
            ("1.5e-1", "real", 0.15),
            ("007e1", "real", 70.0),
        ]:
            with self.subTest(text=text):
                number = tptp.parse_formula(f"{text} = X").left
                self.assertEqual(number, Number(text))
                self.assertEqual((number.kind, number.value), (kind, value))

    def test_chains_are_flat_but_parens_nest(self):
        self.assertEqual(tptp.parse_formula("a | b | c"),
                         NAry("|", (Function("a"), Function("b"), Function("c"))))
        self.assertEqual(tptp.parse_formula("a | (b | c)"),
                         NAry("|", (Function("a"), NAry("|", (Function("b"), Function("c"))))))

    def test_quantifier_binds_tighter_than_connectives(self):
        node = tptp.parse_formula("! [X] : p(X) & q")
        self.assertEqual(node.op, "&")
        self.assertIsInstance(node.args[0], Quantified)

    def test_negation_binds_tighter_than_connectives_but_not_equality(self):
        self.assertEqual(tptp.parse_formula("~ a & b").args[0], Unary("~", Function("a")))
        self.assertEqual(tptp.parse_formula("~ a = b"),
                         Unary("~", Binary("=", Function("a"), Function("b"))))

    def test_typed_variables(self):
        node = tptp.parse_formula("! [X: $int, Y] : p(X,Y)", "tff")
        self.assertEqual(node.variables, (TypedVariable("X", Function("$int")), Variable("Y")))

    def test_thf_mapping_is_right_associative(self):
        typing = tptp.parse_formula("f: a > b > c", "thf")
        self.assertEqual(typing, Typing("f", Binary(">", Function("a"), Binary(">", Function("b"), Function("c")))))
        self.assertEqual(str(typing), "f: a > b > c")
        left_nested = tptp.parse_formula("f: (a > b) > c", "thf")
        self.assertNotEqual(typing, left_nested)
        self.assertEqual(str(left_nested), "f: (a > b) > c")

    def test_tff_product_type(self):
        typing = tptp.parse_formula("p: (a * b) > $o", "tff")
        self.assertEqual(typing.type.left, NAry("*", (Function("a"), Function("b"))))

    def test_modal_atom(self):
        node = tptp.parse_formula("{$box(#a)} @ (p,q)", "tff")
        self.assertIsInstance(node, ModalAtom)
        self.assertEqual(node.connective.name, "$box")
        self.assertEqual(len(node.args), 2)
        self.assertIsInstance(tptp.parse_formula("{$box} @ p", "thf").args[0], LongConnective)

    def test_cnf_print_keeps_literals_unparenthesised(self):
        # Parenthesised literals are not valid CNF, so the printer must not add any.
        for text in ["a = b | c != d | ~ e", "~ a = b"]:
            printed = str(tptp.parse_formula(text, "cnf"))
            self.assertEqual(tptp.parse_formula(printed, "cnf"), tptp.parse_formula(text, "cnf"))

    def test_walk(self):
        node = tptp.parse_formula("! [X] : (p(X) => q(f(X)))")
        names = [n.name for n in tptp.walk(node) if isinstance(n, Function)]
        self.assertEqual(names, ["p", "q", "f"])
        self.assertEqual(sum(isinstance(n, Variable) for n in tptp.walk(node)), 3)


class Annotations(unittest.TestCase):
    def source_of(self, text):
        return tptp.parse(f"fof(a,plain,p,{text}).").formulae[0].annotations.source

    def test_sources(self):
        self.assertEqual(self.source_of("other"), Name("other"))
        self.assertEqual(self.source_of("12"), Name("12"))
        self.assertEqual(self.source_of("file('f.p')"), FileSource("'f.p'"))
        self.assertEqual(self.source_of("file('f.p',ax)"), FileSource("'f.p'", "ax"))
        self.assertEqual(self.source_of("[x,file('f')]"), SourceList((Name("x"), FileSource("f"))))
        self.assertEqual(self.source_of("introduced(definition,[],[])"), Introduced("definition", Tuple(), ()))

    def test_inference_record(self):
        source = self.source_of("inference(rule,[status(thm),a:b:c],[p1,p2:[bind(X,$fot(a))],"
                                "inference(sub,[],[p3])])")
        self.assertIsInstance(source, Inference)
        self.assertEqual(source.rule, "rule")
        self.assertEqual(source.info.items[0], Function("status", (Function("thm"),)))
        self.assertEqual(source.info.items[1],
                         Binary(":", Function("a"), Binary(":", Function("b"), Function("c"))))
        self.assertEqual(source.parents[0].source, Name("p1"))
        self.assertIsNotNone(source.parents[1].details)
        self.assertIsInstance(source.parents[2].source, Inference)

    def test_useful_info(self):
        f = tptp.parse("fof(a,plain,p,x,[description('hi there'),iquote('q'),1,X,\"s\",[nested,[]]]).").formulae[0]
        self.assertEqual(len(f.annotations.info.items), 6)

    def test_formula_data_for_every_language(self):
        text = ("fof(a,plain,p,x,[$thf(f @ a),$tff(f: $i),$fof(! [X] : p(X)),$cnf(a | b),$fot(f(X))]).")
        file = tptp.parse(text)
        self.assertEqual([i.language for i in file.formulae[0].annotations.info.items],
                         ["thf", "tff", "fof", "cnf", "fot"])
        self.assertEqual(tptp.parse(str(file)), file)

    def test_round_trip(self):
        text = ("cnf(c1,negated_conjecture,~ p | a != b,"
                "inference(res,[status(thm)],[c0:[bind(X,$fot(f(a)))],c2]),[proved,x:y]).\n"
                "fof(c2,axiom,p,file('f.p',ax)).\n"
                "fof(c3,axiom,p,[n1,n2]).\n"
                "fof(c4,axiom,p,introduced(tautology,[],[]),[]).")
        file = tptp.parse(text)
        self.assertEqual(tptp.parse(str(file)), file)


class Files(unittest.TestCase):
    def test_empty_and_comment_only(self):
        self.assertEqual(tptp.parse("").inputs, ())
        file = tptp.parse("% one\n/* two\nlines */\n%$ pragma\n")
        self.assertEqual(file.inputs, ())
        self.assertEqual([c.text for c in file.comments], ["% one", "/* two\nlines */", "%$ pragma"])
        self.assertEqual([c.line for c in file.comments], [1, 2, 4])

    def test_comments_between_tokens(self):
        file = tptp.parse("fof(/* c */a,%x\naxiom,p /* y */ & q).")
        self.assertEqual(file.formulae[0].formula, NAry("&", (Function("p"), Function("q"))))

    def test_comment_marker_inside_quotes_is_not_a_comment(self):
        self.assertEqual(tptp.parse("fof(a,axiom,'50% of /* x */').").comments, ())

    def test_whitespace_around_all_tokens(self):
        file = tptp.parse("fof ( a , axiom , ! [ X ] : p ( X ) ) .")
        self.assertEqual(len(file.formulae), 1)

    def test_keywords_are_ordinary_words(self):
        f = tptp.parse("fof(fof,axiom,cnf(include,file)).").formulae[0]
        self.assertEqual(f.name, "fof")
        self.assertEqual(f.formula.name, "cnf")

    def test_error_location(self):
        with self.assertRaises(ParseError) as caught:
            tptp.parse("fof(a,axiom,p).\nfof(b,axiom,\n  p | q & r).", "x.p")
        error = caught.exception
        self.assertEqual((error.line, error.column, error.filename), (3, 9, "x.p"))
        self.assertIn("x.p:3:9", str(error))
        self.assertIn("p | q & r", str(error))

    def test_include_forms(self):
        file = tptp.parse("include('a.ax'). include('b.ax',[x,y]). include('c.ax',*). include('d.ax',[x],sp).")
        self.assertEqual(file.includes, (
            Include("'a.ax'"),
            Include("'b.ax'", ("x", "y")),
            Include("'c.ax'", "*"),
            Include("'d.ax'", ("x",), "sp"),
        ))
        self.assertEqual(file.includes[0].path, "a.ax")
        self.assertEqual(tptp.parse(str(file)), file)

    def test_deep_nesting(self):
        depth = 20_000
        term = "f(" * depth + "a" + ")" * depth
        file = tptp.parse(f"fof(a,axiom,p({term})).")
        self.assertEqual(tptp.parse(str(file)).formulae[0].name, "a")
        self.assertEqual(str(file.formulae[0].formula).count("f("), depth)
        parens = "(" * depth + "p" + ")" * depth
        self.assertEqual(tptp.parse_formula(parens), Function("p"))

    def test_long_clause(self):
        clause = " | ".join(f"p{i}" for i in range(50_000))
        self.assertEqual(len(tptp.parse_formula(clause, "cnf").args), 50_000)

    def test_recursion_limit_is_restored(self):
        import sys
        before = sys.getrecursionlimit()
        tptp.parse("fof(a,axiom,p).")
        with self.assertRaises(ParseError):
            tptp.parse("nonsense")
        self.assertEqual(sys.getrecursionlimit(), before)


class ParseFile(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, name, text):
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def names(self, file):
        return [f.name for f in file.formulae]

    def test_includes_kept_by_default(self):
        main = self.write("main.p", "include('other.ax').")
        file = tptp.parse_file(main)
        self.assertEqual(len(file.includes), 1)
        self.assertEqual(file.filename, str(main))

    def test_expansion_in_order_and_nested(self):
        self.write("a.ax", "fof(a1,axiom,p). include('b.ax'). fof(a2,axiom,p).")
        self.write("b.ax", "fof(b1,axiom,p).")
        main = self.write("main.p", "fof(m1,axiom,p). include('a.ax'). fof(m2,axiom,p).")
        file = tptp.parse_file(main, expand_includes=True)
        self.assertEqual(self.names(file), ["m1", "a1", "b1", "a2", "m2"])
        self.assertEqual(file.includes, ())

    def test_selection(self):
        self.write("a.ax", "fof(x,axiom,p). fof(y,axiom,p). fof(z,axiom,p).")
        main = self.write("main.p", "include('a.ax',[x,z]).")
        self.assertEqual(self.names(tptp.parse_file(main, expand_includes=True)), ["x", "z"])
        star = self.write("star.p", "include('a.ax',*).")
        self.assertEqual(self.names(tptp.parse_file(star, expand_includes=True)), ["x", "y", "z"])

    def test_missing_selection_name_is_an_error(self):
        self.write("a.ax", "fof(x,axiom,p).")
        main = self.write("main.p", "include('a.ax',[nope]).")
        with self.assertRaisesRegex(ParseError, "nope"):
            tptp.parse_file(main, expand_includes=True)

    def test_resolves_against_tptp_root(self):
        self.write("root/Axioms/SET.ax", "fof(s,axiom,p).")
        main = self.write("problems/main.p", "include('Axioms/SET.ax').")
        file = tptp.parse_file(main, expand_includes=True, tptp_root=self.dir / "root")
        self.assertEqual(self.names(file), ["s"])

    def test_local_file_wins_over_root(self):
        self.write("root/x.ax", "fof(from_root,axiom,p).")
        self.write("x.ax", "fof(local,axiom,p).")
        main = self.write("main.p", "include('x.ax').")
        file = tptp.parse_file(main, expand_includes=True, tptp_root=self.dir / "root")
        self.assertEqual(self.names(file), ["local"])

    def test_missing_include(self):
        main = self.write("main.p", "\ninclude('gone.ax').")
        with self.assertRaises(ParseError) as caught:
            tptp.parse_file(main, expand_includes=True)
        self.assertEqual(caught.exception.line, 2)
        self.assertIn("gone.ax", str(caught.exception))

    def test_cycle(self):
        self.write("a.ax", "include('b.ax').")
        self.write("b.ax", "include('a.ax').")
        with self.assertRaisesRegex(ParseError, "circular"):
            tptp.parse_file(self.dir / "a.ax", expand_includes=True)

    def test_diamond_is_not_a_cycle(self):
        self.write("c.ax", "fof(c,axiom,p).")
        self.write("a.ax", "include('c.ax').")
        self.write("b.ax", "include('c.ax').")
        main = self.write("main.p", "include('a.ax'). include('b.ax').")
        self.assertEqual(self.names(tptp.parse_file(main, expand_includes=True)), ["c", "c"])


class Dialect(unittest.TestCase):
    def test_single_language(self):
        self.assertEqual(tptp.parse("fof(a,axiom,p). fof(b,axiom,q).").dialect, ["fof"])

    def test_mixed_languages_in_first_seen_order(self):
        text = "tff(a,axiom,p). cnf(b,axiom,q). tff(c,axiom,r). fof(d,axiom,s)."
        self.assertEqual(tptp.parse(text).dialect, ["tff", "cnf", "fof"])

    def test_no_formulae(self):
        self.assertEqual(tptp.parse("").dialect, [])

    def test_unexpanded_includes_do_not_count(self):
        self.assertEqual(tptp.parse("include('a.ax'). cnf(b,axiom,q).").dialect, ["cnf"])

    def test_expanded_includes_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a.ax").write_text("fof(x,axiom,p).")
            main = Path(tmp, "main.p")
            main.write_text("include('a.ax'). cnf(b,axiom,q).")
            self.assertEqual(tptp.parse_file(main, expand_includes=True).dialect, ["fof", "cnf"])


HEADER = """\
%--------------------------------------------------------------------------
% File     : SET001-1 : TPTP v9.0.0. Released v1.0.0.
% Domain   : Set Theory
% Problem  : Reflexivity of subset
% Refs     : [LS74]  Lawrence & Starkey (1974), Experimental Tests
%          : [Wos65] Wos (1965), Something: else
% Status   : Unsatisfiable
% Rating   : 0.10 v8.1.0, 0.00 v7.5.0
% SPC      : CNF_UNS_RFO_SEQ_NHN
%--------------------------------------------------------------------------
cnf(a,axiom,p).
"""


class HeaderTests(unittest.TestCase):
    def test_fields(self):
        header = tptp.parse(HEADER).header
        self.assertEqual(header.problem, "SET001-1")
        self.assertEqual(header.status, "Unsatisfiable")
        self.assertEqual(header.spc, "CNF_UNS_RFO_SEQ_NHN")
        self.assertEqual(header.get("domain"), "Set Theory")
        self.assertEqual([k for k, _ in header.entries].count("Refs"), 1)
        self.assertIn("Wos (1965), Something: else", header.get("Refs"))

    def test_rating(self):
        header = tptp.parse(HEADER).header
        self.assertEqual(header.rating, 0.10)
        self.assertEqual(header.ratings, ((0.10, "8.1.0"), (0.0, "7.5.0")))

    def test_unknown_and_missing(self):
        header = tptp.parse("% Status   : Theorem\n% Rating   : ?\n").header
        self.assertEqual((header.status, header.rating, header.ratings), ("Theorem", None, ()))
        self.assertIsNone(tptp.parse("fof(a,axiom,p).").header)
        self.assertIsNone(tptp.parse("% just a note\nfof(a,axiom,p).").header)

    def test_header_stops_at_gap(self):
        file = tptp.parse("% Status   : Open\n\n% Rating   : 0.5\nfof(a,axiom,p).")
        self.assertEqual(file.header.status, "Open")
        self.assertIsNone(file.header.rating)

    def test_real_headers_have_blank_lines_between_sections(self):
        # The layout of an actual problem file: sections separated by blank lines.
        text = (
            "%" + "-" * 40 + "\n% File     : SET001-1 : TPTP v10.0.0. Released v1.0.0.\n% Domain   : Set Theory\n"
            "% English  : A member of a set is also a member of that set's supersets.\n\n"
            "% Refs     : [LS74]  Lawrence & Starkey (1974), Experimental Tests of Resol\n"
            "%          : [WM76]  Wilson & Minker (1976), Resolution, Refinements, and S\n\n"
            "% Status   : Unsatisfiable\n% Rating   : 0.11 v10.0.0, 0.00 v2.0.0\n"
            "% Syntax   : Number of clauses     :    9 (   3 unt;   1 nHn;   8 RR)\n"
            "%            Number of literals    :   17 (   0 equ;   8 neg)\n"
            "% SPC      : CNF_UNS_RFO_NEQ_NHN\n\n% Comments :\n" + "%" + "-" * 40 + "\n"
            "%----Include the axioms\n% Status   : Open\ncnf(a,axiom,p).\n")
        header = tptp.parse(text).header
        self.assertEqual((header.problem, header.status, header.spc), ("SET001-1", "Unsatisfiable", "CNF_UNS_RFO_NEQ_NHN"))
        self.assertEqual(header.rating, 0.11)
        self.assertEqual(header.get("domain"), "Set Theory")
        self.assertIn("Number of literals", header.get("Syntax"))
        self.assertEqual([k for k, _ in header.entries].count("Status"), 1)   # comments after the closing rule are not header

    def test_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "x.p")
            path.write_text(HEADER)
            self.assertEqual(tptp.parse_file(path).header.status, "Unsatisfiable")


class Operators(unittest.TestCase):
    def test_parser_returns_specific_classes(self):
        cases = [
            ("a | b", tptp.Or), ("a & b", tptp.And), ("a => b", tptp.Implies),
            ("a <=> b", tptp.Iff), ("a = b", tptp.Equals), ("a != b", tptp.NotEquals),
            ("~ a", tptp.Not), ("! [X] : p", tptp.ForAll), ("? [X] : p", tptp.Exists),
        ]
        for text, cls in cases:
            with self.subTest(text=text):
                self.assertIs(type(tptp.parse_formula(text)), cls)
        self.assertIs(type(tptp.parse_formula("^ [X: $i] : X", "thf")), tptp.Lambda)
        self.assertIs(type(tptp.parse_formula("f @ a", "thf")), tptp.Apply)
        self.assertIs(type(tptp.parse_formula("f: a > b", "thf").type), tptp.Arrow)
        self.assertIs(type(tptp.parse_formula("[.] p", "tff")), tptp.Box)

    def test_generic_and_specific_constructors_are_equal(self):
        a, b = Function("a"), Function("b")
        self.assertEqual(NAry("|", (a, b)), tptp.Or.of(a, b))
        self.assertIs(type(NAry("|", (a, b))), tptp.Or)
        self.assertEqual(Binary("=>", a, b), tptp.Implies.of(a, b))
        self.assertEqual(Unary("~", a), tptp.Not.of(a))
        x = Variable("X")
        self.assertEqual(Quantified("!", (x,), a), tptp.ForAll.of((x,), a))
        self.assertEqual(hash(tptp.Or.of(a, b)), hash(NAry("|", (a, b))))

    def test_hierarchy_and_unknown_operators(self):
        self.assertTrue(issubclass(tptp.And, NAry) and issubclass(tptp.Implies, Binary))
        self.assertTrue(issubclass(tptp.Not, Unary) and issubclass(tptp.ForAll, Quantified))
        self.assertIs(type(tptp.parse_formula("{.} p", "tff")), Unary)
        self.assertIsInstance(tptp.parse_formula("a & b"), NAry)

    def test_copy_pickle_and_printing(self):
        import copy, pickle
        node = tptp.parse_formula("! [X] : (p(X) => (q | r))")
        self.assertEqual(pickle.loads(pickle.dumps(node)), node)
        self.assertEqual(copy.deepcopy(node), node)
        self.assertEqual(str(node), "! [X] : (p(X) => (q | r))")
        self.assertIn("Or(", repr(node.body.right))


def variables_of(node):
    """``(name, index)`` of every variable in the tree, in order, binders included."""
    return [(v.name, v.index) for v in tptp.walk(node) if isinstance(v, (Variable, TypedVariable))]


class VariableIndexes(unittest.TestCase):
    def test_the_documented_example(self):
        clause = tptp.parse("cnf(c,axiom,f(X) | f(Y) | g(X, f(Y))).").formulae[0].formula
        self.assertEqual(variables_of(clause), [("X", 1), ("Y", 2), ("X", 1), ("Y", 2)])

    def test_numbered_by_first_appearance_from_one(self):
        clause = tptp.parse_formula("p(Z, X, Y, X, Z, W)", "cnf")
        self.assertEqual(variables_of(clause), [("Z", 1), ("X", 2), ("Y", 3), ("X", 2), ("Z", 1), ("W", 4)])

    def test_numbering_restarts_for_each_formula(self):
        file = tptp.parse("cnf(a,axiom,p(X,Y)). cnf(b,axiom,q(Y,X)). fof(c,axiom,r(Z)).")
        self.assertEqual([variables_of(f.formula) for f in file.formulae],
                         [[("X", 1), ("Y", 2)], [("Y", 1), ("X", 2)], [("Z", 1)]])

    def test_equal_variables_share_an_index_and_equality_ignores_it(self):
        a, b = tptp.parse_formula("p(X, Y, X)", "cnf").args[::2]
        self.assertEqual((a.index, b.index), (1, 1))
        self.assertEqual(tptp.parse_formula("p(X, Y)"), Function("p", (Variable("X"), Variable("Y"))))
        self.assertEqual(Variable("X"), Variable("X", 7))
        self.assertEqual(hash(Variable("X")), hash(Variable("X", 7)))

    def test_not_in_a_formula_means_zero(self):
        self.assertEqual(Variable("X").index, 0)
        self.assertEqual(TypedVariable("X", Function("$i")).index, 0)

    def test_quantifier_binders_come_first_and_in_binder_order(self):
        self.assertEqual(variables_of(tptp.parse_formula("! [Y,X] : p(X,Y)")),
                         [("Y", 1), ("X", 2), ("X", 2), ("Y", 1)])

    def test_each_binding_is_its_own_variable(self):
        self.assertEqual(variables_of(tptp.parse_formula("(! [X] : p(X)) & (? [X] : q(X))")),
                         [("X", 1), ("X", 1), ("X", 2), ("X", 2)])

    def test_shadowing_and_the_outer_variable_coming_back(self):
        self.assertEqual(variables_of(tptp.parse_formula("! [X] : ((! [X] : q(X)) & p(X))")),
                         [("X", 1), ("X", 2), ("X", 2), ("X", 1)])

    def test_free_variable_and_a_binder_with_the_same_name(self):
        self.assertEqual(variables_of(tptp.parse_formula("p(Y) & ! [Y] : q(Y) & r(Y)")),
                         [("Y", 1), ("Y", 2), ("Y", 2), ("Y", 1)])

    def test_free_variable_met_inside_a_scope_keeps_its_index_outside(self):
        self.assertEqual(variables_of(tptp.parse_formula("(! [X] : p(X,Y)) & q(Y,X)")),
                         [("X", 1), ("X", 1), ("Y", 2), ("Y", 2), ("X", 3)])

    def test_typed_binders(self):
        node = tptp.parse_formula("! [X: $int, Y] : p(X,Y)", "tff")
        self.assertEqual(variables_of(node), [("X", 1), ("Y", 2), ("X", 1), ("Y", 2)])
        self.assertIsInstance(node.variables[0], TypedVariable)
        self.assertEqual(node.variables[0].index, 1)

    def test_thf_binders_and_lambdas(self):
        node = tptp.parse_formula("^ [X: $i, Y: $i > $o] : (Y @ X)", "thf")
        self.assertEqual(variables_of(node), [("X", 1), ("Y", 2), ("Y", 2), ("X", 1)])

    def test_type_variables(self):
        file = tptp.parse("tff(l,type,l: !> [A: $tType, B: $tType] : (A > B)).")
        self.assertEqual(variables_of(file.formulae[0].formula), [("A", 1), ("B", 2), ("A", 1), ("B", 2)])

    def test_tcf_quantifier(self):
        self.assertEqual(variables_of(tptp.parse_formula("! [X: $int, Y] : p(Y,X) | q(Z)", "tcf")),
                         [("X", 1), ("Y", 2), ("Y", 2), ("X", 1), ("Z", 3)])

    def test_let_and_sequents(self):
        self.assertEqual(variables_of(tptp.parse_formula("$let(f: $i > $i, f(X) := X, p(f(Y)))", "tff")),
                         [("X", 1), ("X", 1), ("Y", 2)])
        self.assertEqual(variables_of(tptp.parse_formula("[p(X)] --> [q(Y), r(X)]")),
                         [("X", 1), ("Y", 2), ("X", 1)])

    def test_variables_in_annotations_are_not_numbered(self):
        f = tptp.parse("fof(a,plain,p(X,Y),inference(r,[bind(Y,$fot(g(Z)))],[c1:[bind(X,W)]]),[u(V)]).").formulae[0]
        self.assertEqual(variables_of(f.formula), [("X", 1), ("Y", 2)])
        self.assertEqual({i for _, i in variables_of(f.annotations)}, {0})
        self.assertTrue(variables_of(f.annotations))

    def test_numbering_survives_printing_and_reparsing(self):
        for text in ["p(X) | q(Y,X) | r(Z)", "! [Y,X] : (p(X) & ! [X] : q(X,Y))"]:
            first = tptp.parse_formula(text)
            self.assertEqual(variables_of(tptp.parse_formula(str(first))), variables_of(first))

    def test_copy_and_pickle_keep_the_index(self):
        import copy, pickle
        node = tptp.parse_formula("! [Y,X] : p(X,Y)")
        for clone in (copy.deepcopy(node), pickle.loads(pickle.dumps(node))):
            self.assertEqual(variables_of(clone), variables_of(node))

    def test_many_variables_in_one_clause(self):
        clause = tptp.parse_formula(" | ".join(f"p(V{i}, V{i % 7})" for i in range(2000)), "cnf")
        indexes = {v.name: v.index for v in tptp.walk(clause) if isinstance(v, Variable)}
        self.assertEqual(indexes["V0"], 1)
        self.assertEqual(sorted(indexes.values()), list(range(1, 2001)))


def table(text):
    return [str(s) for s in tptp.parse(text).symbols]


class SymbolTable(unittest.TestCase):
    def test_functions_constants_and_predicates_in_order_of_first_appearance(self):
        # f is met before g, a and b, although it is only finished after them
        self.assertEqual(table("cnf(c,axiom,p(f(g(a),b)) | ~ q)."), ["p/1", "f/2", "g/1", "a/0", "b/0", "q/0"])

    def test_a_symbol_is_a_name_and_an_arity(self):
        symbols = tptp.parse("cnf(c,axiom,f(a) | f(a,b) | f | g(f(a))).").symbols
        self.assertEqual([str(s) for s in symbols], ["f/1", "a/0", "f/2", "b/0", "f/0", "g/1"])
        self.assertEqual(len(set(symbols)), len(symbols))
        self.assertEqual(symbols[0], tptp.Symbol("f", 1))
        self.assertEqual((symbols[2].name, symbols[2].arity), ("f", 2))

    def test_each_symbol_once_however_often_it_is_used(self):
        self.assertEqual(table("cnf(a,axiom,p(a,X)). cnf(b,axiom,p(b,Y) | p(a,a)). fof(c,axiom,q(a))."),
                         ["p/2", "a/0", "b/0", "q/1"])

    def test_symbols_of_each_formula_and_of_the_file(self):
        file = tptp.parse("cnf(a,axiom,p(a)). cnf(b,axiom,q(b) | p(a)). cnf(c,axiom,r).")
        self.assertEqual([[str(s) for s in f.symbols] for f in file.formulae],
                         [["p/1", "a/0"], ["q/1", "b/0", "p/1", "a/0"], ["r/0"]])
        self.assertEqual([str(s) for s in file.symbols], ["p/1", "a/0", "q/1", "b/0", "r/0"])

    def test_the_same_symbol_is_the_same_object(self):
        file = tptp.parse("cnf(a,axiom,p(a)). cnf(b,axiom,p(a)).")
        self.assertIs(file.formulae[0].symbols[0], file.formulae[1].symbols[0])
        self.assertIs(file.symbols[0], file.formulae[0].symbols[0])

    def test_defined_and_system_symbols_are_left_out(self):
        self.assertEqual(table("tff(a,axiom,p($sum(1,2),$$sys(x)) & $less(a,3) & $true)."), ["p/2", "x/0", "a/0"])

    def test_quotes_are_normalised(self):
        self.assertEqual(table("fof(a,axiom,p('cat') & p(cat) & 'Cat' & '$x')."), ["p/1", "cat/0", "'Cat'/0", "'$x'/0"])

    def test_equality_numbers_and_distinct_objects_are_not_symbols(self):
        self.assertEqual(table('fof(a,axiom,f(X) = 1 & "obj" != g(2.5, a)).'), ["f/1", "g/2", "a/0"])

    def test_variables_are_not_symbols(self):
        self.assertEqual(table("cnf(a,axiom,p(X,f(Y)))."), ["p/2", "f/1"])

    def test_quantifiers_and_connectives(self):
        self.assertEqual(table("fof(a,axiom,! [X] : (p(X) => ? [Y] : (q(X,Y) <=> ~ r)))."), ["p/1", "q/2", "r/0"])

    def test_type_declarations_are_not_symbols_but_uses_are(self):
        file = tptp.parse("tff(t1,type,s: $tType). tff(t2,type,f: s > s). tff(t3,type,c: s). "
                          "tff(a,axiom,! [X: s] : f(c) = X).")
        self.assertEqual(table_of(file), ["f/1", "c/0"])
        self.assertEqual(file.formulae[0].symbols, ())

    def test_typed_binders_and_type_applications_are_not_symbols(self):
        self.assertEqual(table("tff(a,axiom,! [X: list(elem), Y: $int] : p(X,Y))."), ["p/2"])
        self.assertEqual(table("tff(a,type,l: !> [A: $tType] : (list(A) > A))."), [])

    def test_thf_types_are_not_symbols(self):
        self.assertEqual(table("thf(t,type,f: ind > ind). thf(a,axiom,! [X: ind > bool, Y: ind] : (X @ (f @ Y)))."),
                         ["f/0"])
        self.assertEqual(table("thf(a,axiom,^ [X: set(ind)] : p)."), ["p/0"])

    def test_let_definitions_are_uses(self):
        self.assertEqual(table("tff(a,axiom,$let(f: $i > $i, f(X) := g(X), p(f(a))))."), ["f/1", "g/1", "p/1", "a/0"])

    def test_annotations_are_not_symbols(self):
        file = tptp.parse("fof(a,plain,p(c),inference(res,[status(thm),k(Z)],[b1,file('f.p',x)]),[source(s),$fot(h(d))]).")
        self.assertEqual(table_of(file), ["p/1", "c/0"])

    def test_includes_you_did_not_expand_contribute_nothing(self):
        self.assertEqual(table("include('Axioms/X.ax'). cnf(a,axiom,p)."), ["p/0"])
        self.assertEqual(tptp.parse("").symbols, ())

    def test_the_symbol_table_follows_include_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "ax.ax").write_text("cnf(x1,axiom,ax1(a)). cnf(x2,axiom,ax2(b)). cnf(x3,axiom,ax3).")
            Path(tmp, "all.p").write_text("cnf(m1,axiom,m(a)). include('ax.ax'). cnf(m2,axiom,n).")
            Path(tmp, "some.p").write_text("include('ax.ax',[x2]). cnf(m2,axiom,n(c)).")
            self.assertEqual(table_of(tptp.parse_file(Path(tmp, "all.p"))), ["m/1", "a/0", "n/0"])
            self.assertEqual(table_of(tptp.parse_file(Path(tmp, "all.p"), expand_includes=True)),
                             ["m/1", "a/0", "ax1/1", "ax2/1", "b/0", "ax3/0", "n/0"])
            # selecting only x2 must leave the other formulae's symbols out
            self.assertEqual(table_of(tptp.parse_file(Path(tmp, "some.p"), expand_includes=True)),
                             ["ax2/1", "b/0", "n/1", "c/0"])

    def test_str_and_repr(self):
        symbol = tptp.Symbol("f", 2)
        self.assertEqual(str(symbol), "f/2")
        self.assertEqual(repr(symbol), "Symbol(name='f', arity=2)")

    def test_a_long_clause(self):
        clause = " | ".join(f"p{i}(a)" for i in range(30_000))
        symbols = tptp.parse(f"cnf(c,axiom,{clause}).").symbols
        self.assertEqual(len(symbols), 30_001)
        self.assertEqual((str(symbols[0]), str(symbols[1])), ("p0/1", "a/0"))


def table_of(file):
    return [str(s) for s in file.symbols]


class Api(unittest.TestCase):
    def test_parse_formula_rejects_trailing_input(self):
        with self.assertRaises(ParseError):
            tptp.parse_formula("p q")

    def test_parse_formula_unknown_language(self):
        with self.assertRaises(ValueError):
            tptp.parse_formula("p", "nope")

    def test_nodes_are_hashable(self):
        self.assertEqual(len({tptp.parse_formula("p(X)"), tptp.parse_formula("p( X )")}), 1)


if __name__ == "__main__":
    unittest.main()
