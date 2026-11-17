"""Tests for the command line and the modules behind it: stats, search, validate, download."""

import contextlib
import http.server
import io
import json
import os
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import thousandproblems as tptp
from thousandproblems import cli, download, search, stats, validate

# Headers copied from the TPTP library (v10.0.0), formulae shortened where noted.
SET001 = """\
%--------------------------------------------------------------------------
% File     : SET001-1 : TPTP v10.0.0. Released v1.0.0.
% Domain   : Set Theory
% Problem  : Set members are superset members
% Version  : [LS74] axioms.
% English  : A member of a set is also a member of that set's supersets.

% Status   : Unsatisfiable
% Rating   : 0.11 v10.0.0, 0.00 v2.0.0
% Syntax   : Number of clauses     :    9 (   3 unt;   1 nHn;   8 RR)
%            Number of literals    :   17 (   0 equ;   8 neg)
%            Maximal clause size   :    3 (   1 avg)
%            Maximal term depth    :    2 (   1 avg)
%            Number of predicates  :    3 (   3 usr;   0 prp; 2-2 aty)
%            Number of functors    :    4 (   4 usr;   3 con; 0-2 aty)
%            Number of variables   :   13 (   0 sgn)
% SPC      : CNF_UNS_RFO_NEQ_NHN

% Comments :
%--------------------------------------------------------------------------
include('Axioms/SET001-0.ax').
%--------------------------------------------------------------------------
cnf(b_equals_bb,hypothesis, equal_sets(b,bb) ).
cnf(element_of_b,hypothesis, member(element_of_b,b) ).
cnf(prove_element_of_bb,negated_conjecture, ~ member(element_of_b,bb) ).
"""

SET001_AX = """\
cnf(membership_in_subsets,axiom,~ member(E,S) | ~ subset(S,T) | member(E,T)).
cnf(subsets_axiom1,axiom,subset(S,T) | member(member_of_1_not_of_2(S,T),S)).
cnf(subsets_axiom2,axiom,~ member(member_of_1_not_of_2(S,T),T) | subset(S,T)).
cnf(set_equal_sets_are_subsets1,axiom,~ equal_sets(S,T) | subset(S,T)).
cnf(set_equal_sets_are_subsets2,axiom,~ equal_sets(T,S) | subset(S,T)).
cnf(subsets_are_set_equal_sets,axiom,~ subset(A,B) | ~ subset(B,A) | equal_sets(B,A)).
"""

PUZ001 = """\
%------------------------------------------------------------------------------
% File     : PUZ001+1 : TPTP v10.0.0. Released v2.0.0.
% Domain   : Puzzles
% Problem  : Dreadbury Mansion
% Version  : Especial.

% Status   : Theorem
% Rating   : 0.11 v10.0.0, 0.09 v9.1.0
% Syntax   : Number of formulae    :   14 (   6 unt;   0 def)
%            Number of atoms       :   24 (   5 equ)
%            Maximal formula atoms :    4 (   1 avg)
%            Number of connectives :   16 (   6   ~;   2   |;   1   &)
%                                         (   0 <=>;   7  =>;   0  <=;   0 <~>)
%            Maximal formula depth :    5 (   3 avg)
%            Maximal term depth    :    1 (   1 avg)
%            Number of predicates  :    5 (   4 usr;   0 prp; 1-2 aty)
%            Number of functors    :    3 (   3 usr;   3 con; 0-0 aty)
%            Number of variables   :   12 (  10   !;   2   ?)
% SPC      : FOF_THM_RFO_SEQ

% Comments : Modified by Geoff Sutcliffe.
%------------------------------------------------------------------------------
fof(pel55_1,axiom, ? [X] : ( lives(X) & killed(X,agatha) ) ).
fof(pel55_4,axiom, ! [X] : ( lives(X) => ( X = agatha | X = butler | X = charles ) ) ).
fof(pel55,conjecture, killed(agatha,agatha) ).
"""

DAT001 = """\
%------------------------------------------------------------------------------
% File     : DAT001_1 : TPTP v10.0.0. Released v5.0.0.
% Domain   : Data Structures
% Problem  : Sorted lists
% Version  : [Wal10] axioms.

% Status   : Theorem
% Rating   : 0.00 v6.2.0, 0.20 v6.1.0
% Syntax   : Number of formulae    :    8 (   3 unt;   4 typ;   0 def)
%            Number of atoms       :    6 (   0 equ)
%            Number arithmetic     :    9 (   1 atm;   0 fun;   5 num;   3 var)
%            Number of types       :    3 (   1 usr;   1 ari;   0 dat;   0 cdt)
%            Number of predicates  :    2 (   1 usr;   0 prp; 1-2 aty)
%            Number of functors    :    7 (   2 usr;   6 con; 0-2 aty)
%            Number of variables   :    4 (   4   !;   0   ?;   4   :)
% SPC      : TF0_THM_NEQ_ARI_NDT

% Comments :
%------------------------------------------------------------------------------
tff(list_type,type, list: $tType ).
tff(nil_type,type, nil: list ).
tff(mycons_type,type, mycons: ( $int * list ) > list ).
tff(sorted_type,type, sorted: list > $o ).
tff(empty_is_sorted,axiom, sorted(nil) ).
tff(single_is_sorted,axiom, ! [X: $int] : sorted(mycons(X,nil)) ).
tff(conj,conjecture, sorted(mycons(1,mycons(2,nil))) ).
"""

SYO001 = """\
%------------------------------------------------------------------------------
% File     : SYO001^1 : TPTP v10.0.0. Released v3.7.0.
% Domain   : Syntactic
% Problem  : Leibniz equality is transitive
% Version  : Especial.

% Status   : Theorem
% Rating   : 1.00 v8.2.0
% Syntax   : Number of formulae    :    3 (   1 unt;   1 typ;   1 def)
%            Number of atoms       :    5 (   1 equ;   0 cnn)
%            Number of types       :    2 (   0 usr)
%            Number of symbols     :    2 (   1 usr;   0 con; 2-2 aty)
%            Number of variables   :    6 (   2   ^;   4   !;   0   ?;   6   :)
% SPC      : TH0_THM_EQU_NAR_NDT

% Comments :
%------------------------------------------------------------------------------
thf(leibeq_decl,type, leibeq: $i > $i > $o ).
thf(leibeq,definition, leibeq = ( ^ [X: $i,Y: $i] : ! [P: $i > $o] : ( ( P @ X ) => ( P @ Y ) ) ) ).
thf(trans,conjecture, ! [X: $i,Y: $i,Z: $i] : ( ( ( leibeq @ X @ Y ) & ( leibeq @ Y @ Z ) ) => ( leibeq @ X @ Z ) ) ).
"""

LIBRARY = {
    "Problems/SET/SET001-1.p": SET001,
    "Problems/PUZ/PUZ001+1.p": PUZ001,
    "Problems/DAT/DAT001_1.p": DAT001,
    "Problems/SYO/SYO001^1.p": SYO001,
    "Axioms/SET001-0.ax": SET001_AX,
    "Documents/README": "docs\n",
    "Generators/README": "gen\n",
    "Scripts/README": "scripts\n",
    "TPTP2X/README": "tptp2x\n",
}


def write_tree(root: Path, files=LIBRARY) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
    return root


def make_tarball(path: Path, files=LIBRARY, top="TPTP-v10.0.0") -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for rel, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{top}/{rel}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def run(*argv, stdin: str = ""):
    """Run the CLI; returns (exit status, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch("sys.stdin", io.StringIO(stdin)):
        try:
            status = cli.main([str(a) for a in argv])
        except SystemExit as e:
            status = e.code
    return status, out.getvalue(), err.getvalue()


class TempCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = write_tree(self.tmp / "TPTP-v10.0.0")
        env = mock.patch.dict(os.environ, {"THOUSANDPROBLEMS_CACHE": str(self.tmp / "cache")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("TPTP", None)


# ---------------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------------


class SyntaxStats(unittest.TestCase):
    def test_cnf(self):
        s = stats.syntax_stats(tptp.parse(SET001).header)
        self.assertEqual(s["clauses"].value, 9)
        self.assertEqual(s["clauses"].details, {"unt": 3, "nHn": 1, "RR": 8})
        self.assertEqual(s["literals"].get("equ"), 0)
        self.assertEqual(s["predicates"].get("aty"), (2, 2))
        self.assertEqual(s["functors"].get("aty"), (0, 2))
        self.assertEqual(s["max_clause_size"].value, 3)
        self.assertEqual(s["variables"].details, {"sgn": 0})

    def test_connectives_over_two_lines(self):
        s = stats.syntax_stats(tptp.parse(PUZ001).header)
        self.assertEqual(s["connectives"].value, 16)
        self.assertEqual(s["connectives"].details,
                         {"~": 6, "|": 2, "&": 1, "<=>": 0, "=>": 7, "<=": 0, "<~>": 0})
        self.assertEqual(s["max_formula_depth"].value, 5)
        self.assertEqual(s["variables"].details, {"!": 10, "?": 2})

    def test_typed_and_html_escaped(self):
        s = stats.syntax_stats(tptp.parse(DAT001).header)
        self.assertEqual(s["arithmetic"].details, {"atm": 1, "fun": 0, "num": 5, "var": 3})
        self.assertEqual(s["types"].details, {"usr": 1, "ari": 1, "dat": 0, "cdt": 0})
        escaped = tptp.parse("% Syntax   : Number of connectives :    2 (   1   &amp;;   1  =&gt;)\n").header
        self.assertEqual(stats.syntax_stats(escaped)["connectives"].details, {"&": 1, "=>": 1})

    def test_no_header(self):
        self.assertEqual(stats.syntax_stats(None), {})
        self.assertEqual(stats.syntax_stats(tptp.parse("cnf(a,axiom,p).").header), {})


class SPCTests(unittest.TestCase):
    def test_parts(self):
        spc = stats.SPC("CNF_UNS_RFO_NEQ_NHN")
        self.assertEqual((spc.form, spc.status), ("CNF", "UNS"))
        self.assertEqual(spc.flags, {"RFO", "NEQ", "NHN"})
        self.assertEqual(spc.describe(),
                         "clause normal form, Unsatisfiable, real first-order, no equality, non-Horn")


class ComputeStats(unittest.TestCase):
    def test_cnf_with_axioms_matches_the_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_tree(Path(tmp))
            parsed = tptp.fetch_problem("SET001-1", root, expand_includes=True)
        c = stats.compute_stats(parsed)
        self.assertEqual(c.dialects, ("cnf",))
        self.assertEqual((c.formulae, c.unit_formulae, c.atoms, c.equality_atoms), (9, 3, 17, 0))
        self.assertEqual(c.predicates, ("member/2", "subset/2", "equal_sets/2"))
        self.assertEqual(c.functions, ("member_of_1_not_of_2/2",))
        self.assertEqual(c.constants, ("b/0", "bb/0", "element_of_b/0"))
        self.assertEqual((c.variables, c.singleton_variables), (13, 0))
        self.assertEqual(c.max_term_depth, 2)
        self.assertEqual(c.roles, {"axiom": 6, "hypothesis": 2, "negated_conjecture": 1})

    def test_unexpanded_includes_are_listed(self):
        c = stats.compute_stats(tptp.parse(SET001))
        self.assertEqual(c.includes, ("Axioms/SET001-0.ax",))
        self.assertEqual(c.formulae, 3)

    def test_fof(self):
        c = stats.compute_stats(tptp.parse(PUZ001))
        self.assertEqual((c.atoms, c.equality_atoms), (7, 3))
        self.assertEqual(c.bound_variables, {"?": 1, "!": 1})
        self.assertEqual(c.predicates, ("lives/1", "killed/2"))
        self.assertEqual(c.constants, ("agatha/0", "butler/0", "charles/0"))
        self.assertEqual(c.unit_formulae, 1)

    def test_tff_types_and_interpreted_symbols(self):
        c = stats.compute_stats(tptp.parse(DAT001 + "tff(x,axiom,$less(1,2)).\n"))
        self.assertEqual(c.type_declarations, 4)
        self.assertEqual(c.user_types, ("list",))
        self.assertEqual(c.predicates, ("sorted/1",))
        self.assertIn("$less/2", c.defined_symbols)
        self.assertFalse(c.polymorphic)

    def test_thf(self):
        c = stats.compute_stats(tptp.parse(SYO001))
        self.assertEqual(c.dialects, ("thf",))
        self.assertIsNone(c.atoms)
        self.assertIsNone(c.unit_formulae)
        self.assertEqual(c.bound_variables, {"^": 2, "!": 4})      # as the header says
        self.assertEqual(c.equality_atoms, 1)
        self.assertEqual(c.predicates, ())

    def test_polymorphism(self):
        c = stats.compute_stats(tptp.parse("tff(t,type,list: $tType > $tType)."
                                           "tff(a,axiom,! [A: $tType] : p(A))."))
        self.assertTrue(c.polymorphic)


# ---------------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------------


class Ranges(unittest.TestCase):
    def test_forms(self):
        r = search.parse_range
        self.assertIn(5, r("5"))
        self.assertNotIn(6, r("5"))
        self.assertIn(10, r("1..10"))
        self.assertIn(10, r("1-10"))
        self.assertNotIn(11, r("1..10"))
        self.assertIn(1000, r("3.."))
        self.assertIn(0, r("..3"))
        self.assertNotIn(1, r(">1"))
        self.assertIn(2, r(">1"))
        self.assertIn(1, r(">=1"))
        self.assertNotIn(1, r("<1"))
        self.assertIn(0.75, r("0.5..1"))
        self.assertNotIn(None, r(".."[:0] + "0.."))

    def test_bad(self):
        for bad in ["", "..", "x", "1..x", ">"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                search.parse_range(bad)


class ReadHeader(unittest.TestCase):
    def test_stops_after_the_header(self):
        lines = iter(SET001.splitlines(True))
        header = search.read_header(lines)
        self.assertEqual(header.spc, "CNF_UNS_RFO_NEQ_NHN")
        self.assertTrue(next(lines).startswith("include("))      # nothing more was read

    def test_same_as_full_parse(self):
        for text in (SET001, PUZ001, DAT001, SYO001):
            self.assertEqual(search.read_header(text.splitlines(True)), tptp.parse(text).header)


class SearchTests(TempCase):
    def names(self, **criteria):
        return [r.name for r in search.search(self.root, **criteria)]

    def test_everything(self):
        self.assertEqual(self.names(), ["DAT001_1", "PUZ001+1", "SET001-1", "SYO001^1"])

    def test_criteria(self):
        cases = {
            "form=FOF": (dict(form="FOF"), ["PUZ001+1"]),
            "form=TFF": (dict(form="TFF"), ["DAT001_1"]),
            "form list": (dict(form="THF,CNF"), ["SET001-1", "SYO001^1"]),
            "form negated": (dict(form="not:CNF"), ["DAT001_1", "PUZ001+1", "SYO001^1"]),
            "domain": (dict(domain="S??"), ["SET001-1", "SYO001^1"]),
            "name": (dict(name="*_1,PUZ*"), ["DAT001_1", "PUZ001+1"]),
            "status code": (dict(status="UNS"), ["SET001-1"]),
            "status name": (dict(status="Theorem"), ["DAT001_1", "PUZ001+1", "SYO001^1"]),
            "easy": (dict(rating="easy"), ["DAT001_1"]),
            "difficult": (dict(rating="difficult"), ["PUZ001+1", "SET001-1"]),
            "unsolved": (dict(rating="unsolved"), ["SYO001^1"]),
            "rating range": (dict(rating="0.1..0.5"), ["PUZ001+1", "SET001-1"]),
            "order": (dict(order="RFO"), ["PUZ001+1", "SET001-1"]),
            "version": (dict(version="ESP"), ["PUZ001+1", "SYO001^1"]),
            "standard": (dict(version="standard"), ["DAT001_1", "SET001-1"]),
            "formulae": (dict(formulae="9..14"), ["PUZ001+1", "SET001-1"]),
            "unit formulae": (dict(unit_formulae=">=3"), ["DAT001_1", "PUZ001+1", "SET001-1"]),
            "atoms": (dict(atoms="<10"), ["DAT001_1", "SYO001^1"]),
            "no equality": (dict(equality="none"), ["DAT001_1", "SET001-1"]),
            "any equality": (dict(equality="any"), ["PUZ001+1", "SYO001^1"]),
            "some equality": (dict(equality="some"), ["PUZ001+1"]),
            "pure equality": (dict(equality="pure"), []),
            "unit equality": (dict(unit_equality="no"), ["DAT001_1", "PUZ001+1", "SET001-1", "SYO001^1"]),
            "types": (dict(types=">1"), ["DAT001_1", "SYO001^1"]),
            "no types": (dict(types="0"), ["PUZ001+1", "SET001-1"]),
            "datatypes": (dict(datatypes="0"), ["DAT001_1", "PUZ001+1", "SET001-1", "SYO001^1"]),
            "symbols": (dict(symbols="7..8"), ["PUZ001+1", "SET001-1"]),
            "thf symbols": (dict(symbols="2"), ["SYO001^1"]),
            "predicates": (dict(predicates=">3"), ["PUZ001+1"]),
            "predicate arity": (dict(predicate_arity="2"), ["DAT001_1", "PUZ001+1", "SET001-1"]),
            "function arity": (dict(function_arity="0"), ["PUZ001+1"]),
            "functions": (dict(functions="4"), ["SET001-1"]),
            "variables none": (dict(variables="none"), []),
            "variables range": (dict(variables=">10"), ["PUZ001+1", "SET001-1"]),
            "lambda": (dict(quantifiers="^"), ["SYO001^1"]),
            "exists": (dict(quantifiers="?"), ["PUZ001+1"]),
            "cnf is universal": (dict(quantifiers="!"), ["DAT001_1", "PUZ001+1", "SET001-1", "SYO001^1"]),
            "arithmetic": (dict(arithmetic="yes"), ["DAT001_1"]),
            "no arithmetic": (dict(arithmetic="no"), ["PUZ001+1", "SET001-1", "SYO001^1"]),
            "spc": (dict(spc="_NHN$"), ["SET001-1"]),
            "combined": (dict(form="not:THF", status="THM", equality="none"), ["DAT001_1"]),
            "repeated": (dict(form=["not:CNF", "not:FOF"]), ["DAT001_1", "SYO001^1"]),
        }
        records = search.build_index(self.root)
        for label, (criteria, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual([r.name for r in search.search(records, **criteria)], expected)

    def test_bad_values(self):
        for criteria in (dict(form="XYZ"), dict(status="nope"), dict(equality="lots"),
                         dict(formulae="many"), dict(arithmetic="maybe"), dict(colour="red")):
            with self.subTest(criteria), self.assertRaises(ValueError):
                search.Query.build(**criteria)

    def test_index_is_cached_and_refreshed(self):
        first = search.build_index(self.root)
        self.assertEqual(len(first), 4)
        with mock.patch.object(search, "_scan_directory", side_effect=AssertionError("rescanned")):
            self.assertEqual(len(search.build_index(self.root)), 4)
        write_tree(self.root, {"Problems/SET/SET002-1.p": SET001.replace("SET001-1", "SET002-1")})
        self.assertEqual(len(search.build_index(self.root)), 5)

    def test_tarball(self):
        tarball = make_tarball(self.tmp / "TPTP-v10.0.0.tgz")
        records = search.build_index(tarball)
        self.assertEqual([r.name for r in records], ["DAT001_1", "PUZ001+1", "SET001-1", "SYO001^1"])
        self.assertEqual(records[2].spc, "CNF_UNS_RFO_NEQ_NHN")

    def test_online_library_cannot_be_searched(self):
        with self.assertRaisesRegex(tptp.LibraryError, "local copy"):
            search.build_index("https://example.invalid")


# ---------------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------------


class ValidateTests(TempCase):
    def test_valid(self):
        report = validate.validate_library(self.root, check_includes=True, parse=True)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.warnings, [])
        self.assertEqual((report.version, report.domains, report.problems, report.axioms),
                         ("v10.0.0", 4, 4, 1))

    def test_valid_tarball(self):
        report = validate.validate_library(make_tarball(self.tmp / "t.tgz"), check_includes=True)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.problems, 4)

    def test_missing_folders(self):
        report = validate.validate_library(self.tmp)
        self.assertFalse(report.ok)
        self.assertIn("missing the Problems/ folder (did you mean", report.errors[0])
        self.assertFalse(validate.validate_library(self.tmp / "nowhere").ok)

    def test_trimmed_copy_only_warns(self):
        root = write_tree(self.tmp / "trim", {k: v for k, v in LIBRARY.items()
                                               if k.startswith(("Problems", "Axioms"))})
        report = validate.validate_library(root)
        self.assertTrue(report.ok)
        self.assertEqual(len(report.warnings), 4)

    def test_bad_layout(self):
        write_tree(self.root, {
            "Problems/SET/PUZ002-1.p": "cnf(a,axiom,p).\n",          # wrong domain
            "Problems/SET/notaname.p": "cnf(a,axiom,p).\n",
            "Problems/SETS/SET003-1.p": "cnf(a,axiom,p).\n",         # bad domain folder
            "Problems/SET/SET004-1.p": "include('Axioms/NOPE.ax').\ncnf(a,axiom,p | ).\n",
        })
        report = validate.validate_library(self.root, check_includes=True, parse=True)
        self.assertFalse(report.ok)
        text = "\n".join(report.errors)
        self.assertIn("PUZ002-1 is in the wrong domain folder", text)
        self.assertIn("'notaname' is not a TPTP problem name", text)
        self.assertIn("domain folder 'SETS'", text)
        self.assertIn("includes 'Axioms/NOPE.ax', which is missing", text)
        self.assertIn("SET004-1.p:2:", text)

    def test_error_lists_are_capped(self):
        write_tree(self.root, {f"Problems/SET/bad{i}.p": "" for i in range(30)})
        report = validate.validate_library(self.root)
        self.assertEqual(len(report.errors), report.limit + 1)


# ---------------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------------


class Server:
    def __init__(self, routes: dict[str, bytes]):
        outer = self
        self.routes = routes

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = outer.routes.get(self.path)
                self.send_response(200 if body is not None else 404)
                self.send_header("Content-Length", str(len(body or b"")))
                self.end_headers()
                self.wfile.write(body or b"")

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class DownloadTests(TempCase):
    def setUp(self):
        super().setUp()
        tarball = make_tarball(self.tmp / "TPTP-v10.0.0.tgz").read_bytes()
        listing = b'<a href="TPTP-v9.3.0.tgz">x</a> <a href="TPTP-v10.0.0.tgz">TPTP-v10.0.0.tgz</a>'
        self.server = Server({"/dist/": listing, "/dist/TPTP-v10.0.0.tgz": tarball,
                              "/arch/": b'<a href="TPTP-v8.0.0.tgz">'})
        self.addCleanup(self.server.close)
        self.dist, self.arch = f"{self.server.url}/dist/", f"{self.server.url}/arch/"

    def test_versions(self):
        self.assertEqual(download.latest_version(self.dist), "10.0.0")
        self.assertEqual(download.release_url(distribution_url=self.dist), f"{self.dist}TPTP-v10.0.0.tgz")
        self.assertEqual(download.release_url("v9.3.0", distribution_url=self.dist, archive_url=self.arch),
                         f"{self.dist}TPTP-v9.3.0.tgz")
        self.assertEqual(download.release_url("8.0.0", distribution_url=self.dist, archive_url=self.arch),
                         f"{self.arch}TPTP-v8.0.0.tgz")
        with self.assertRaises(ValueError):
            download.release_url("latest-ish")

    def test_download_and_extract(self):
        seen = []
        root = download.download_library(self.tmp / "dl", url=f"{self.dist}TPTP-v10.0.0.tgz",
                                         progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(root, self.tmp / "dl" / "TPTP-v10.0.0")
        self.assertTrue((root / "Problems" / "SET" / "SET001-1.p").is_file())
        self.assertFalse((self.tmp / "dl" / "TPTP-v10.0.0.tgz").exists())
        self.assertEqual(seen[-1][0], seen[-1][1])
        self.assertTrue(validate.validate_library(root).ok)

    def test_download_without_extracting(self):
        path = download.download_library(self.tmp / "dl", url=f"{self.dist}TPTP-v10.0.0.tgz", extract=False)
        self.assertEqual(path.name, "TPTP-v10.0.0.tgz")
        self.assertFalse(list((self.tmp / "dl").glob("*.part")))

    def test_cli(self):
        status, out, err = run("download", "-o", self.tmp / "dl", "--url", f"{self.dist}TPTP-v10.0.0.tgz")
        self.assertEqual(status, 0, err)
        self.assertEqual(out.strip(), str(self.tmp / "dl" / "TPTP-v10.0.0"))
        self.assertIn("VALID", err)
        self.assertIn("export TPTP=", err)

    def test_missing_release(self):
        with self.assertRaisesRegex(tptp.LibraryError, "404"):
            download.download_library(self.tmp / "dl", url=f"{self.dist}TPTP-v1.0.0.tgz")


# ---------------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------------


class CommandLine(TempCase):
    def test_help_and_version(self):
        status, out, _ = run("--version")
        self.assertEqual(status, 0)
        self.assertIn("thousandproblems", out)
        status, out, _ = run("search", "--help")
        self.assertEqual(status, 0)
        self.assertIn("--predicate-arity", out)

    def test_validate(self):
        status, out, _ = run("validate", self.root)
        self.assertEqual(status, 0)
        self.assertIn("VALID TPTP library", out)
        status, out, _ = run("validate", self.tmp)
        self.assertEqual(status, 1)
        self.assertIn("INVALID", out)
        status, out, _ = run("validate", "--json", "-L", self.root)
        self.assertEqual(json.loads(out)["problems"], 4)
        with mock.patch.dict(os.environ, {"TPTP": str(self.root)}):
            self.assertEqual(run("validate")[0], 0)
        self.assertEqual(run("validate")[0], 1)

    def test_validate_strict(self):
        root = write_tree(self.tmp / "trim", {k: v for k, v in LIBRARY.items()
                                               if k.startswith(("Problems", "Axioms"))})
        self.assertEqual(run("validate", root)[0], 0)
        self.assertEqual(run("validate", "--strict", root)[0], 1)

    def test_find(self):
        status, out, _ = run("find", "-L", self.root, "SET001-1", "SET001-0", "SET001-0.ax")
        self.assertEqual(status, 0)
        self.assertEqual(out.split(), [str(self.root / "Problems/SET/SET001-1.p"),
                                       str(self.root / "Axioms/SET001-0.ax"),
                                       str(self.root / "Axioms/SET001-0.ax")])
        status, out, _ = run("find", "-r", "-L", self.root, "S*")
        self.assertEqual(out.split(), ["Problems/SET/SET001-1.p", "Problems/SYO/SYO001^1.p"])
        status, out, err = run("find", "-L", self.root, "SET999-1", "nonsense")
        self.assertEqual(status, 1)
        self.assertIn("SET999-1 is not in", err)
        self.assertIn("not a TPTP problem name", err)

    def test_find_needs_a_local_library(self):
        status, _, err = run("find", "SET001-1")
        self.assertEqual(status, 1)
        self.assertIn("-L/--library", err)
        status, _, err = run("find", "-L", "https://example.invalid", "SET001-1")
        self.assertIn("local copy", err)

    def test_stats(self):
        status, out, err = run("stats", "-L", self.root, "SET001-1")
        self.assertEqual(status, 0, err)
        self.assertIn("SET001-1  (Problems/SET/SET001-1.p)", out)
        self.assertIn("CNF_UNS_RFO_NEQ_NHN  (clause normal form, Unsatisfiable", out)
        self.assertRegex(out, r"Formulae\s+: 9 ")
        self.assertRegex(out, r"Variables\s+: 13 ")
        self.assertIn("Declared in the header", out)

    def test_stats_json_and_files(self):
        status, out, _ = run("stats", "--json", "-L", self.root, "PUZ001+1")
        data = json.loads(out)
        self.assertEqual(data["form"], "FOF")
        self.assertEqual(data["declared"]["predicates"], {"value": 5, "usr": 4, "prp": 0, "aty": [1, 2]})
        self.assertEqual(data["computed"]["dialects"], ["fof"])
        file = self.root / "Problems/SET/SET001-1.p"
        status, out, err = run("stats", "--json", file)          # no library: includes can't be read
        self.assertEqual(status, 0)
        self.assertIn("warning", err)
        self.assertEqual(json.loads(out)["computed"]["formulae"], 3)
        status, out, _ = run("stats", "--json", "-L", self.root, file)
        self.assertEqual(json.loads(out)["computed"]["formulae"], 9)
        status, out, _ = run("stats", "--no-expand", "--symbols", "-L", self.root, "SET001-1")
        self.assertIn("not expanded", out)
        self.assertIn("equal_sets/2", out)

    def test_stats_errors(self):
        self.assertEqual(run("stats", "-L", self.root, "XYZ999-1")[0], 1)
        status, _, err = run("stats", "-L", self.root, "not a problem")
        self.assertEqual(status, 1)
        self.assertIn("neither a file nor", err)
        bad = self.tmp / "bad.p"
        bad.write_text("cnf(a,axiom,p | ).\n")
        status, _, err = run("stats", bad)
        self.assertEqual(status, 1)
        self.assertIn("bad.p:1:", err)

    def test_search(self):
        status, out, err = run("search", "-L", self.root, "--form", "not:CNF", "--equality", "any")
        self.assertEqual(status, 0)
        self.assertEqual(out.split(), ["PUZ001+1", "SYO001^1"])
        self.assertIn("2 of 4 problems match", err)
        status, out, _ = run("search", "-q", "-L", self.root, "--count", "--status", "THM")
        self.assertEqual(out.strip(), "3")
        status, out, _ = run("search", "-q", "-L", self.root, "--format", "table", "--domain", "SET")
        self.assertIn("SET001-1", out)
        self.assertIn("CNF_UNS_RFO_NEQ_NHN", out)
        status, out, _ = run("search", "-q", "-L", self.root, "--format", "json", "--name", "DAT*")
        self.assertEqual(json.loads(out)[0]["syntax"]["types"][1]["usr"], 1)
        status, out, _ = run("search", "-q", "-L", self.root, "--format", "paths", "--form", "TH0")
        self.assertEqual(out.strip(), str(self.root / "Problems/SYO/SYO001^1.p"))
        status, out, err = run("search", "-L", self.root, "--limit", "1")
        self.assertEqual(len(out.split()), 1)
        self.assertIn("showing 1", err)

    def test_search_failures(self):
        self.assertEqual(run("search", "-q", "-L", self.root, "--form", "DH1")[0], 1)
        status, _, err = run("search", "-L", self.root, "--form", "nope")
        self.assertEqual(status, 1)
        self.assertIn("unknown logical form", err)
        status, _, err = run("search", "--form", "FOF")
        self.assertIn("-L/--library", err)

    def test_fetch_prints(self):
        status, out, _ = run("fetch", "-L", self.root, "SET001-1")
        self.assertEqual(out, SET001)
        status, out, _ = run("fetch", "-L", self.root, "--expand", "SET001-1")
        self.assertEqual(len(out.splitlines()), 9)
        self.assertTrue(out.startswith("cnf(membership_in_subsets,axiom,"))

    def test_fetch_saves_with_axioms(self):
        status, out, err = run("fetch", "-L", self.root, "-o", self.tmp / "out", "SET*", "PUZ001+1.p")
        self.assertEqual(status, 0, err)
        saved = sorted(p.relative_to(self.tmp / "out").as_posix() for p in (self.tmp / "out").rglob("*") if p.is_file())
        self.assertEqual(saved, ["Axioms/SET001-0.ax", "Problems/PUZ/PUZ001+1.p", "Problems/SET/SET001-1.p"])
        self.assertTrue(validate.validate_library(self.tmp / "out", check_includes=True).ok)
        run("fetch", "-q", "-L", self.root, "-o", self.tmp / "only", "--no-axioms", "SET001-1")
        self.assertFalse((self.tmp / "only" / "Axioms").exists())

    def test_fetch_from_search(self):
        _, names, _ = run("search", "-q", "-L", self.root, "--arithmetic", "yes")
        status, out, _ = run("fetch", "-L", self.root, "-o", self.tmp / "out", "-", stdin=names)
        self.assertEqual(status, 0)
        self.assertEqual(out.split(), [str(self.tmp / "out/Problems/DAT/DAT001_1.p")])

    def test_fetch_errors(self):
        status, _, err = run("fetch", "-L", self.root, "SET999-1")
        self.assertEqual(status, 1)
        self.assertIn("not in", err)
        status, _, err = run("fetch", "-L", self.root, "../etc/passwd")
        self.assertEqual(status, 1)
        status, _, err = run("fetch", "-L", self.root, "QQQ*")
        self.assertIn("no problem matches", err)
        status, _, err = run("fetch", "-L", "https://example.invalid", "SET*")
        self.assertIn("need a local library", err)


class ProblemFiles(TempCase):
    def test_follows_nested_includes(self):
        write_tree(self.root, {"Problems/SET/SET010-1.p": "include('Axioms/A.ax').\n",
                               "Axioms/A.ax": "include('Axioms/B.ax').\ncnf(a,axiom,a).\n",
                               "Axioms/B.ax": "include('Axioms/A.ax').\ncnf(b,axiom,b).\n"})
        with tptp.open_library(self.root) as library:
            files = library.problem_files("SET010-1")
            self.assertEqual(list(files), ["Problems/SET/SET010-1.p", "Axioms/A.ax", "Axioms/B.ax"])
            write_tree(self.root, {"Problems/SET/SET011-1.p": "include('Axioms/GONE.ax').\n"})
            with self.assertRaisesRegex(tptp.ProblemNotFound, "GONE"):
                library.problem_files("SET011-1")


if __name__ == "__main__":
    unittest.main()
