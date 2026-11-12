"""The C and pure-Python parsers must be indistinguishable. Skipped without the extension."""

import dataclasses
import os
import threading
import unittest

import thousandproblems as tptp
from thousandproblems import _pyparser

try:
    from thousandproblems import _cparser
except ImportError:
    _cparser = None

import corpus
from test_tptp import INVALID, VALID

FUZZ_ROUNDS = int(os.environ.get("THOUSANDPROBLEMS_FUZZ_ROUNDS", "400"))


def deep(x):
    """Every field of every node, including those ``==`` ignores, and the exact types."""
    if isinstance(x, tuple):
        return tuple(deep(i) for i in x)
    if dataclasses.is_dataclass(x):
        return (type(x), tuple((f.name, deep(getattr(x, f.name))) for f in dataclasses.fields(x)))
    return (type(x), x)


def outcome(fn, *args):
    """Either ('ok', tree) or ('error', everything the user can see about the error)."""
    try:
        result = fn(*args)
    except tptp.ParseError as e:
        return ("error", str(e), e.message, e.line, e.column, e.filename, e.source_line)
    if result is NotImplemented:
        return ("fallback",)
    return ("ok", deep(result))


@unittest.skipIf(_cparser is None, "C extension not built")
class Equivalence(unittest.TestCase):
    def same_file(self, text, filename=None):
        c = outcome(_cparser.parse, text, filename)
        if c == ("fallback",):
            return
        self.assertEqual(c, outcome(_pyparser.parse, text, filename), repr(text[:200]))

    def same_formula(self, text, language):
        c = outcome(_cparser.parse_formula, text, language)
        if c == ("fallback",):
            return
        self.assertEqual(c, outcome(_pyparser.parse_formula, text, language), (language, text))

    def test_every_case_of_the_main_suite(self):
        for table in (VALID, INVALID):
            for language, examples in table.items():
                for text in examples:
                    with self.subTest(language=language, text=text):
                        self.same_formula(text, language)

    def test_generated_files(self):
        for seed in range(FUZZ_ROUNDS // 4):
            text = corpus.Gen(seed).file(25)
            with self.subTest(seed=seed):
                self.same_file(text, "gen.p")
                self.same_file(corpus.decorate(text, seed))

    def test_decoration_is_exercised(self):
        decorated = sum(corpus.decorate(corpus.Gen(s).file(10), s) != corpus.Gen(s).file(10)
                        for s in range(30))
        self.assertGreater(decorated, 20)

    def test_damaged_files_give_identical_errors(self):
        errors = 0
        for seed in range(FUZZ_ROUNDS * 5):
            base = corpus.Gen(seed % 50).file(6)
            text = corpus.mutate(corpus.decorate(base, seed) if seed % 3 else base, seed)
            with self.subTest(seed=seed):
                self.same_file(text)
            errors += outcome(_pyparser.parse, text, None)[0] == "error"
        self.assertGreater(errors, FUZZ_ROUNDS)      # the mutations really do hit error paths

    def test_damaged_formulas_give_identical_errors(self):
        for seed in range(FUZZ_ROUNDS * 5):
            language = ("fof", "cnf", "tff", "thf", "tcf")[seed % 5]
            samples = VALID[language]
            text = corpus.mutate(samples[seed % len(samples)], seed)
            with self.subTest(seed=seed, language=language):
                self.same_formula(text, language)

    def test_non_ascii(self):
        for text in ["% caf\u00e9\nfof(a,axiom,p).", "fof(a,axiom,p). % \u4e2d\u6587",
                     "fof(a,axiom,p\u00e9).", "/* \u00fc */ fof(a,axiom,\u00e9).",
                     "fof(a,axiom,p).\n\u00e9\u00e9\u00e9 x", "fof(a,axiom,'\u00e9')."]:
            with self.subTest(text=text):
                self.same_file(text, "u.p")

    def test_lexical_edge_cases(self):
        for text in ["", " ", "%", "/", "/*", "/**/", "/*/", "'", "''", "\"\"", "`", "`a", "$", "$$",
                     "+", "-", "1.", "1.e5", "0e", "1e+", "007", "-0", "1/0", "1/", "a-1", "fof(a,axiom-1,p).",
                     "fof(a,axiom - 1,p).", "fof(a,axiom-1.5,p).", "fof(a,axiom-1/2,p).", "\x00", "a\x00",
                     "fof(a,axiom,p).\r\n\r\nfof(b,axiom,", "[.]", "[.", "<=>", "<=", "<~", "@@+", "@@", "(.", "\f"]:
            with self.subTest(text=text):
                self.same_file(text)
                for language in ("fof", "tff", "thf", "cnf"):
                    self.same_formula(text, language)

    def test_lone_surrogates_fall_back(self):
        text = "fof(a,axiom,p). % \ud800"
        self.assertIs(_cparser.parse(text, None), NotImplemented)
        self.assertEqual(len(tptp.parse(text).formulae), 1)

    def test_bad_arguments(self):
        for bad in (None, 1, b"fof(a,axiom,p)."):
            with self.assertRaises(TypeError):
                _cparser.parse(bad, None)
        with self.assertRaises(ValueError):
            _cparser.parse_formula("p", "nope")


@unittest.skipIf(_cparser is None, "C extension not built")
class DepthLimits(unittest.TestCase):
    def test_deep_nesting_falls_back_and_succeeds(self):
        term = "f(" * 5000 + "a" + ")" * 5000
        self.assertIs(_cparser.parse(f"fof(a,axiom,p({term})).", None), NotImplemented)
        self.assertEqual(tptp.parse(f"fof(a,axiom,p({term})).").formulae[0].name, "a")

    def test_moderate_nesting_stays_in_c(self):
        term = "f(" * 200 + "a" + ")" * 200
        self.assertIsNot(_cparser.parse(f"fof(a,axiom,p({term})).", None), NotImplemented)

    def test_every_nesting_shape_is_guarded(self):
        n = 50_000
        shapes = {
            "fof": ["(" * n + "p" + ")" * n, "~ " * n + "p", "! [X] : " * n + "p", "p & (" * n + "q" + ")" * n,
                    "f(" * n + "a" + ")" * n + " = b", "[" * n + "p" + "] --> [q]" * n],
            "cnf": ["(" * n + "p" + ")" * n],
            "tcf": ["! [X] : " * n + "p"],
            "tff": ["(" * n + "p" + ")" * n, "! [X: $i] : " * n + "p", "[" * n + "]" * n,
                    "$let(f: $i, f := " + "g(" * n + "a" + ")" * n + ", p)", "(f: " + "(" * n + "t" + ")" * n,
                    "p: " + "(" * n + "t" + ")" * n, "p: " + "!> [A: $tType] : " * n + "t",
                    "[.] " * n + "p"],
            "thf": ["^ [X: " * n + "t" + "] : p" * n, "(" * n + "p" + ")" * n, "p: " + "(" * n + "t" + ")" * n,
                    "f @ (" * n + "a" + ")" * n],
        }
        for language, texts in shapes.items():
            for text in texts:
                with self.subTest(language=language, text=text[:30]):
                    # Handled in C, handed back, or a ParseError; never a crash.
                    try:
                        _cparser.parse_formula(text, language)
                    except tptp.ParseError:
                        pass
        for text in ["fof(a,axiom,p,inference(r,[],[" + "[" * n + "]" * n + "]))).",
                     "fof(a,axiom,p," + "inference(r,[],[" * n + "x" + "])" * n + ").",
                     "fof(a,axiom,p,x,[" + "f(" * n + "a" + ")" * n + "])."]:
            try:
                _cparser.parse(text, None)
            except tptp.ParseError:
                pass

    def test_small_thread_stack(self):
        # macOS gives threads 512 KB by default; the C parser must stay well inside that.
        old = threading.stack_size()
        failures = []

        def work():
            try:
                for n in (1000, 5000, 50_000):
                    for text in ["f(" * n + "a" + ")" * n, "(" * n + "p" + ")" * n,
                                 "~ " * n + "p", "! [X] : " * n + "p"]:
                        try:
                            _cparser.parse_formula(text, "fof")
                        except tptp.ParseError:
                            pass
                    _cparser.parse_formula("(" * n + "p" + ")" * n, "tff")
                    _cparser.parse_formula("p: " + "(" * n + "t" + ")" * n, "tff")
                    try:
                        _cparser.parse_formula("^ [X: " * n + "t" + "] : p" * n, "thf")
                    except tptp.ParseError:
                        pass
            except BaseException as e:      # a real overflow would kill the process instead
                failures.append(e)

        threading.stack_size(256 * 1024)
        try:
            thread = threading.Thread(target=work)
            thread.start()
            thread.join()
        finally:
            threading.stack_size(old)
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
