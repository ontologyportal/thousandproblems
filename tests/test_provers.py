"""Tests for running provers: Problem, SearchResult, the prover classes, the runner and `run`.

The subprocess provers here are a small Python script that behaves like a prover, so no real
prover needs to be installed.
"""

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import thousandproblems as tptp
from thousandproblems import cli, provers, runner, search, szs
from thousandproblems.library import DirectoryLibrary, LibraryError, ProblemNotFound
from thousandproblems.problem import Problem
from thousandproblems.provers import (BuiltInProver, CallableProver, ProverCollection, ProverError, RunResult,
                                      Solution, Status, SubprocessProver, format_args, prover_from_spec)

from test_cli import LIBRARY, TempCase, run, write_tree

FAKE = r'''
import os, re, sys, time
args = sys.argv[1:]
text = open(args[-1]).read() if args and os.path.isfile(args[-1]) else sys.stdin.read()
if "SLEEP" in text:
    time.sleep(60)
if "CRASH" in text:
    sys.stderr.write("boom\n")
    sys.exit(3)
if "SILENT" in text:
    sys.exit(0)
for include in re.findall(r"include\('([^']+)'\)", text):
    if not os.path.isfile(os.path.join(os.environ.get("TPTP", ""), include)):
        print("% SZS status InputError")
        sys.exit(1)
status = os.environ.get("FAKE_STATUS") or ("Theorem" if "onjecture" in text else "Satisfiable")
print("%% SZS status %s for x" % status)
print("% SZS output start Proof for x")
print("a proof")
print("% SZS output end Proof for x")
'''


class WithFake(TempCase):
    def setUp(self):
        super().setUp()
        self.script = self.tmp / "fake_prover.py"
        self.script.write_text(FAKE)

    def fake(self, **options):
        options.setdefault("name", "fake")
        extra = list(options.pop("extra_args", ()))
        return SubprocessProver(sys.executable, extra_args=[str(self.script), *extra], **options)


# ---------------------------------------------------------------------------------
# SZS
# ---------------------------------------------------------------------------------


class SZS(unittest.TestCase):
    def test_status_lines(self):
        self.assertEqual(szs.parse_szs_status("% SZS status Theorem for SET001-1\n"), "Theorem")
        self.assertEqual(szs.parse_szs_status("# Proof found!\n# SZS status Unsatisfiable\n"), "Unsatisfiable")
        self.assertEqual(szs.parse_szs_status("% SZS status Timeout\nSZS status GaveUp"), "GaveUp")
        self.assertIsNone(szs.parse_szs_status("% Status : Theorem\nnothing"))

    def test_classes(self):
        for name, status in [("Theorem", Status.PROVED), ("Unsatisfiable", Status.PROVED),
                             ("ContradictoryAxioms", Status.PROVED), ("CounterSatisfiable", Status.SATISFIABLE),
                             ("Satisfiable", Status.SATISFIABLE), ("Timeout", Status.TIMEOUT),
                             ("ResourceOut", Status.RESOURCE_OUT), ("MemoryOut", Status.RESOURCE_OUT),
                             ("GaveUp", Status.UNKNOWN), ("Unknown", Status.UNKNOWN), (None, Status.UNKNOWN),
                             ("SyntaxError", Status.ERROR), ("theorem", Status.PROVED)]:
            self.assertEqual(szs.status_from_szs(name), status, name)
        self.assertTrue(Status.PROVED.solved and Status.SATISFIABLE.solved)
        self.assertFalse(Status.TIMEOUT.solved)

    def test_output_block(self):
        text = "% x\n% SZS output start Proof for a\nline 1\nline 2\n% SZS output end Proof for a\n% y\n"
        self.assertEqual(szs.extract_output_block(text), "line 1\nline 2")
        self.assertIsNone(szs.extract_output_block("nothing"))


# ---------------------------------------------------------------------------------
# Problem
# ---------------------------------------------------------------------------------


class ProblemTests(TempCase):
    def test_from_library(self):
        problem = Problem.from_library("SET001-1", self.root)
        self.assertEqual(problem.name, "SET001-1")
        self.assertEqual(list(problem.files), ["Problems/SET/SET001-1.p", "Axioms/SET001-0.ax"])
        self.assertEqual(problem.expected_status, "Unsatisfiable")
        self.assertEqual(len(problem.parsed().formulae), 3)
        self.assertEqual(len(problem.parsed(expand_includes=True).formulae), 9)
        self.assertIs(problem.parsed(), problem.parsed())
        with self.assertRaises(ProblemNotFound):
            Problem.from_library("SET999-1", self.root)

    def test_write_to(self):
        problem = Problem.from_library("SET001-1", self.root)
        main = problem.write_to(self.tmp / "out")
        self.assertEqual(main, self.tmp / "out/Problems/SET/SET001-1.p")
        self.assertEqual(main.read_text(), problem.text)
        self.assertTrue((self.tmp / "out/Axioms/SET001-0.ax").is_file())

    def test_from_file_finds_includes(self):
        file = self.root / "Problems/SET/SET001-1.p"
        with self.assertRaisesRegex(ProblemNotFound, "tptp_root"):
            Problem.from_file(file)
        problem = Problem.from_file(file, tptp_root=self.root)
        self.assertEqual(sorted(problem.files), ["Axioms/SET001-0.ax", "SET001-1.p"])
        self.assertEqual(len(problem.parsed(expand_includes=True).formulae), 9)
        with mock.patch.dict(os.environ, {"TPTP": str(self.root)}):
            self.assertEqual(len(Problem.from_file(file).files), 2)

    def test_relative_include(self):
        (self.tmp / "sub").mkdir()
        (self.tmp / "sub" / "ax.ax").write_text("cnf(a,axiom,p).\n")
        (self.tmp / "p.p").write_text("include('sub/ax.ax').\ncnf(c,negated_conjecture,~p).\n")
        problem = Problem.from_file(self.tmp / "p.p")
        self.assertEqual(sorted(problem.files), ["p.p", "sub/ax.ax"])

    def test_from_text(self):
        problem = Problem.from_text("% Status : Theorem\nfof(c,conjecture,$true).\n", "triv")
        self.assertEqual((problem.name, problem.path), ("triv", "triv.p"))
        self.assertEqual(problem.expected_status, "Theorem")
        self.assertEqual(problem.parsed().formulae[0].name, "c")


# ---------------------------------------------------------------------------------
# SearchResult
# ---------------------------------------------------------------------------------


class Counting(DirectoryLibrary):
    fetched: list

    def problem_files(self, name):
        self.fetched.append(name)
        return super().problem_files(name)


class SearchResults(TempCase):
    def test_fields(self):
        result = search.search(self.root, form="not:CNF", status="THM")
        self.assertIsInstance(result, search.SearchResult)
        self.assertEqual(result.names, ["DAT001_1", "PUZ001+1", "SYO001^1"])
        self.assertEqual(result.params, {"form": ["not:CNF"], "status": ["THM"]})
        self.assertEqual(result.source, str(self.root))
        self.assertEqual((len(result), result.searched), (3, 4))
        self.assertEqual(result.records[1].name, "PUZ001+1")
        self.assertEqual(result.as_dict()["problems"], result.names)
        self.assertIn("3 of 4", repr(result))

    def test_query_params_are_kept(self):
        result = search.search(self.root, query=search.Query.build(domain="SET"), rating="..0.5")
        self.assertEqual(result.params, {"domain": ["SET"], "rating": ["..0.5"]})

    def test_iteration_fetches_lazily(self):
        library = Counting(self.root)
        library.fetched = []
        result = search.search(library)
        self.assertEqual(len(result.names), 4)
        self.assertEqual(library.fetched, [])
        iterator = iter(result)
        first = next(iterator)
        self.assertEqual(library.fetched, ["DAT001_1"])
        self.assertIsInstance(first, Problem)
        self.assertEqual(first.record.name, "DAT001_1")
        next(iterator)
        self.assertEqual(library.fetched, ["DAT001_1", "PUZ001+1"])
        self.assertEqual([p.name for p in result], result.names)

    def test_indexing(self):
        with search.search(self.root, domain="SET,PUZ") as result:
            self.assertEqual(result[0].name, "PUZ001+1")
            sliced = result[1:]
            self.assertIsInstance(sliced, search.SearchResult)
            self.assertEqual((sliced.names, sliced.params, sliced.source), (["SET001-1"], result.params, result.source))
            self.assertEqual(sliced[0].name, "SET001-1")
            self.assertEqual(result.problem("SET001-1").record.status, "Unsatisfiable")
            with self.assertRaises(KeyError):
                result.problem("DAT001_1")

    def test_tarball_source(self):
        from test_cli import make_tarball
        tarball = make_tarball(self.tmp / "t.tgz")
        with search.search(tarball, form="CNF") as result:
            self.assertEqual(result.source, str(tarball))
            self.assertEqual([p.name for p in result], ["SET001-1"])

    def test_records_without_source(self):
        records = search.build_index(self.root)
        result = search.search(records, domain="SET")
        self.assertEqual((result.names, result.source), (["SET001-1"], None))
        with self.assertRaisesRegex(LibraryError, "no library"):
            list(result)
        with search.search(records, domain="SET", source=self.root) as result:
            self.assertEqual([p.name for p in result], ["SET001-1"])
            self.assertEqual(result.source, str(self.root))


# ---------------------------------------------------------------------------------
# Subprocess provers
# ---------------------------------------------------------------------------------


class FormatArgs(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(format_args({"--auto": None, "--x": True, "--no": False}), ["--auto", "--x"])
        self.assertEqual(format_args({"-t": 10, "--mode": "casc"}), ["-t", "10", "--mode", "casc"])
        self.assertEqual(format_args({"--cpu-limit=": 5}), ["--cpu-limit=5"])
        self.assertEqual(format_args({"-I": ["a", "b"]}), ["-I", "a", "-I", "b"])


class Commands(unittest.TestCase):
    def test_command_line(self):
        p = SubprocessProver("tool", {"--fast": None, "-m": 3}, extra_args=["--x"])
        self.assertEqual(p.command("/bin/tool", "/tmp/a.p"), ["/bin/tool", "--fast", "-m", "3", "--x", "/tmp/a.p"])
        self.assertEqual(p.command("/bin/tool", None), ["/bin/tool", "--fast", "-m", "3", "--x"])
        self.assertEqual(p.name, "tool")

    def test_time_limit(self):
        p = SubprocessProver("tool", time_limit_arg="--cpu-limit=")
        self.assertEqual(p.command("t", "f", 2.2), ["t", "--cpu-limit=3", "f"])
        self.assertEqual(p.command("t", "f", None), ["t", "f"])
        own = SubprocessProver("tool", {"--cpu-limit=": 99}, time_limit_arg="--cpu-limit=")
        self.assertEqual(own.command("t", "f", 5), ["t", "--cpu-limit=99", "f"])

    def test_input_options(self):
        class Flagged(SubprocessProver):
            input_flag = "--file="
            stdin_args = ("-",)

        self.assertEqual(Flagged("t").command("t", "f"), ["t", "--file=f"])
        self.assertEqual(Flagged("t").command("t", None), ["t", "-"])

    def test_bad_arguments(self):
        with self.assertRaises(ProverError):
            SubprocessProver("tool", input="pigeon")
        with self.assertRaises(ProverError):
            SubprocessProver()

    def test_subclass_defaults(self):
        class Mine(SubprocessProver):
            binary = "mine"
            default_args = {"--a": 1}

        self.assertEqual(Mine().name, "mine")
        self.assertEqual(Mine(args={"--b": 2}).args, {"--a": 1, "--b": 2})
        self.assertEqual(Mine("other").binary, "other")


class SubprocessRuns(WithFake):
    def setUp(self):
        super().setUp()
        self.problem = Problem.from_library("PUZ001+1", self.root)

    def test_file_and_stdin(self):
        for mode in ("file", "stdin"):
            result = self.fake(input=mode).run(self.problem, timeout=20)
            self.assertEqual((result.status, result.szs), (Status.PROVED, "Theorem"), mode)
            self.assertEqual((result.problem, result.prover, result.returncode), ("PUZ001+1", "fake", 0))
            self.assertEqual(result.solution, "a proof")
            self.assertEqual(result.expected, "Theorem")
            self.assertIs(result.correct, True)
            self.assertGreater(result.runtime, 0)
            self.assertIn("SZS status", result.stdout)
        self.assertEqual(result.command[-1], str(self.script))          # stdin: no path on the command line

    def test_file_mode_passes_a_path(self):
        result = self.fake().run(self.problem)
        self.assertTrue(result.command[-1].endswith("PUZ001+1.p"))
        self.assertFalse(Path(result.command[-1]).exists())          # the temporary file is gone

    def test_satisfiable_and_mismatch(self):
        satisfiable = self.fake(env={"FAKE_STATUS": "CounterSatisfiable"}).run(self.problem)
        self.assertEqual(satisfiable.status, Status.SATISFIABLE)
        self.assertIs(satisfiable.correct, False)

    def test_includes_are_found_through_TPTP(self):
        problem = Problem.from_library("SET001-1", self.root)
        for mode in ("file", "stdin"):
            self.assertEqual(self.fake(input=mode).run(problem).szs, "Theorem", mode)

    def test_timeout_kills_the_prover(self):
        problem = Problem.from_text("% SLEEP\ncnf(a,axiom,p).\n")
        start = time.perf_counter()
        result = self.fake().run(problem, timeout=0.5)
        self.assertLess(time.perf_counter() - start, 10)
        self.assertEqual(result.status, Status.TIMEOUT)
        self.assertTrue(0.4 < result.runtime < 5)

    def test_failures(self):
        result = self.fake().run(Problem.from_text("% CRASH\ncnf(a,axiom,p).\n"))
        self.assertEqual((result.status, result.returncode), (Status.ERROR, 3))
        self.assertIn("boom", result.error)
        result = self.fake().run(Problem.from_text("% SILENT\ncnf(a,axiom,p).\n"))
        self.assertEqual((result.status, result.szs), (Status.UNKNOWN, None))
        result = SubprocessProver("no-such-prover-anywhere").run(self.problem)
        self.assertEqual(result.status, Status.ERROR)
        self.assertIn("not found", result.error)
        self.assertFalse(SubprocessProver("no-such-prover-anywhere").available())
        self.assertTrue(self.fake().available())

    def test_binary_by_path(self):
        self.assertTrue(SubprocessProver(sys.executable).available())
        self.assertFalse(SubprocessProver(str(self.tmp / "nothing")).available())
        if os.name == "posix":
            tool = self.tmp / "tool"
            tool.write_text("#!/bin/sh\necho '% SZS status Theorem'\n")
            tool.chmod(0o755)
            self.assertEqual(SubprocessProver(str(tool)).run(self.problem).status, Status.PROVED)
            with mock.patch.dict(os.environ, {"PATH": str(self.tmp)}):
                self.assertEqual(SubprocessProver("tool").run(self.problem).status, Status.PROVED)
            cwd = os.getcwd()
            try:
                os.chdir(self.tmp)
                self.assertEqual(SubprocessProver("./tool").run(self.problem).status, Status.PROVED)
            finally:
                os.chdir(cwd)

    def test_parse_output_override(self):
        class Quirky(SubprocessProver):
            def parse_output(self, stdout, stderr, returncode):
                return "Theorem" if "QED" in stdout else None

        tool = self.tmp / "q.py"
        tool.write_text("print('QED')\n")
        self.assertEqual(Quirky(sys.executable, extra_args=[str(tool)]).run(self.problem).status, Status.PROVED)

    def test_presets(self):
        e = provers.EProver()
        self.assertEqual(e.command("eprover", "f", 3), ["eprover", "--auto", "--cpu-limit=3", "f"])
        self.assertEqual(provers.Vampire().command("vampire", "f", 3), ["vampire", "-t", "3", "f"])
        self.assertEqual(e.parse_output("# Failure: Resource limit exceeded (time)\n# SZS status ResourceOut\n", "", 1),
                         "Timeout")
        self.assertEqual(e.parse_output("# SZS status ResourceOut\n", "", 1), "ResourceOut")
        v = provers.Vampire()
        self.assertEqual(v.parse_output("% Termination reason: Time limit\n", "", 0), "Timeout")
        self.assertEqual(v.parse_output("% Termination reason: Refutation not found, incomplete strategy\n", "", 0),
                         "GaveUp")
        self.assertEqual(v.parse_output("% SZS status Theorem for x\n% Termination reason: Refutation\n", "", 0),
                         "Theorem")


class Specs(unittest.TestCase):
    def test_windows_quoting(self):
        # On Windows shlex runs with posix=False, which keeps quotes; they must not reach the prover.
        with mock.patch.object(sys, "platform", "win32"):
            p = prover_from_spec(r"""C:\tools\prover.exe -a 'b c' "d e" --opt="f g" --plain""")
        self.assertEqual(p.binary, r"C:\tools\prover.exe")
        self.assertEqual(p.extra_args, ["-a", "b c", "d e", "--opt=f g", "--plain"])
        with mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(prover_from_spec('"C:\\Program Files\\p\\prover.exe" -x').binary,
                             r"C:\Program Files\p\prover.exe")

    def test_specs(self):
        p = prover_from_spec("eprover --auto-schedule --cpu-limit=10")
        self.assertIsInstance(p, provers.EProver)
        self.assertEqual((p.name, p.extra_args), ("eprover", ["--auto-schedule", "--cpu-limit=10"]))
        p = prover_from_spec("casc=vampire --mode casc", input="stdin")
        self.assertIsInstance(p, provers.Vampire)
        self.assertEqual((p.name, p.extra_args, p.input_mode), ("casc", ["--mode", "casc"], "stdin"))
        p = prover_from_spec("/opt/my provers/x -a 'b c'".replace("my provers", "mine"))
        self.assertEqual((type(p), p.binary, p.name, p.extra_args), (SubprocessProver, "/opt/mine/x", "x", ["-a", "b c"]))
        self.assertEqual(prover_from_spec("tool --a=b").extra_args, ["--a=b"])
        for bad in ("", "  ", "x 'unterminated"):
            with self.assertRaises(ProverError):
                prover_from_spec(bad)


# ---------------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------
# Built-in provers
# ---------------------------------------------------------------------------------


class Builtin(TempCase):
    def setUp(self):
        super().setUp()
        self.problem = Problem.from_library("SET001-1", self.root)

    def test_receives_the_parsed_problem_and_config(self):
        seen = {}

        class Counter(BuiltInProver):
            default_config = {"mode": "default", "depth": 1}

            def solve(self, problem, context):
                seen.update(formulae=len(problem.formulae), config=dict(self.config),
                            context_config=dict(context.config), name=context.problem.name)
                return "Theorem"

        result = Counter(config={"depth": 5}, extra=True).run(self.problem)
        self.assertEqual(seen, {"formulae": 9, "name": "SET001-1",
                                "config": {"mode": "default", "depth": 5, "extra": True},
                                "context_config": {"mode": "default", "depth": 5, "extra": True}})
        self.assertEqual((result.status, result.szs, result.prover), (Status.PROVED, "Theorem", "Counter"))
        self.assertIs(result.correct, True)

    def test_includes_not_expanded(self):
        class Shallow(BuiltInProver):
            expand_includes = False

            def solve(self, problem, context):
                return Status.UNKNOWN if len(problem.formulae) == 3 else Status.ERROR

        self.assertEqual(Shallow().run(self.problem).status, Status.UNKNOWN)

    def test_return_values(self):
        for value, status, szs in [(Status.SATISFIABLE, Status.SATISFIABLE, None), ("CounterSatisfiable",
                                   Status.SATISFIABLE, "CounterSatisfiable"), ("proved", Status.PROVED, None),
                                   ("GaveUp", Status.UNKNOWN, "GaveUp"), (None, Status.UNKNOWN, None)]:
            result = CallableProver(lambda p, c, v=value: v).run(self.problem)
            self.assertEqual((result.status, result.szs), (status, szs), value)
        result = CallableProver(lambda p, c: Solution("Theorem", "the proof", "log")).run(self.problem)
        self.assertEqual((result.solution, result.stdout), ("the proof", "log"))
        self.assertEqual(CallableProver(lambda p, c: 42).run(self.problem).status, Status.ERROR)

    def test_exceptions_become_errors(self):
        def broken(problem, context):
            raise RuntimeError("bug")

        result = CallableProver(broken, name="b").run(self.problem, timeout=5)
        self.assertEqual(result.status, Status.ERROR)
        self.assertEqual(result.error, "RuntimeError: bug")
        self.assertIn("Traceback", result.stderr)

    def test_timeout_cancels(self):
        stopped = []

        def slow(problem, context):
            while not context.expired():
                time.sleep(0.01)
            stopped.append(True)
            return "Theorem"

        start = time.perf_counter()
        result = CallableProver(slow).run(self.problem, timeout=0.3)
        self.assertEqual(result.status, Status.TIMEOUT)
        self.assertLess(time.perf_counter() - start, 5)
        time.sleep(0.2)
        self.assertEqual(stopped, [True])                      # the worker saw the cancellation

        def checking(problem, context):
            while True:
                context.check()
                time.sleep(0.01)

        self.assertEqual(CallableProver(checking).run(self.problem, timeout=0.2).status, Status.TIMEOUT)
        self.assertEqual(CallableProver(checking).run(Problem.from_text("cnf(a,axiom,p)."), timeout=0.2).status,
                         Status.TIMEOUT)

    def test_no_timeout_runs_inline(self):
        import threading
        where = []
        CallableProver(lambda p, c: where.append(threading.current_thread()) or "Theorem").run(self.problem)
        self.assertIs(where[0], threading.current_thread())

    def test_unreadable_problem(self):
        problem = Problem.from_text("cnf(a,axiom,p).")
        problem.text = "cnf(a,axiom,p | ).\n"
        result = CallableProver(lambda p, c: "Theorem").run(problem)
        self.assertEqual(result.status, Status.ERROR)
        self.assertIn("cannot read the problem", result.error)

    def test_abstract(self):
        with self.assertRaises(TypeError):
            BuiltInProver()


# ---------------------------------------------------------------------------------
# Collections and the runner
# ---------------------------------------------------------------------------------


class Collections(WithFake):
    def test_collection(self):
        a, b = self.fake(name="a"), CallableProver(lambda p, c: "Theorem", name="b")
        provers_ = ProverCollection([a, b])
        self.assertEqual((provers_.names, len(provers_), provers_[0], provers_["b"]), (["a", "b"], 2, a, b))
        self.assertTrue("a" in provers_ and b in provers_ and "c" not in provers_)
        self.assertEqual(list(provers_), [a, b])
        with self.assertRaisesRegex(ValueError, "already"):
            provers_.add(self.fake(name="a"))
        with self.assertRaises(TypeError):
            provers_.add("eprover")
        missing = SubprocessProver("no-such-prover-anywhere")
        provers_.add(missing)
        self.assertEqual(provers_.missing(), [missing])
        self.assertEqual(provers_.available().names, ["a", "b"])

    def test_from_specs(self):
        collection = ProverCollection.from_specs(["x=" + sys.executable, "y=" + sys.executable, self.fake()])
        self.assertEqual(collection.names, ["x", "y", "fake"])


class Runner(WithFake):
    def provers(self):
        return ProverCollection([
            self.fake(name="fake"),
            CallableProver(lambda p, c: "GaveUp", name="giveup"),
            self.fake(name="liar", env={"FAKE_STATUS": "CounterSatisfiable"}),
        ])

    def test_search_result_against_provers(self):
        seen = []
        result = search.search(self.root, form="not:THF")
        report = runner.run(result, self.provers(), timeout=20,
                            on_result=lambda r, done, total: seen.append((done, total)))
        self.assertEqual(report.problems, ["DAT001_1", "PUZ001+1", "SET001-1"])
        self.assertEqual(report.provers, ["fake", "giveup", "liar"])
        self.assertEqual([(r.problem, r.prover) for r in report.results],
                         [(p, n) for p in report.problems for n in report.provers])
        self.assertEqual(sorted(seen), [(i, 9) for i in range(1, 10)])
        self.assertEqual(report.search, {"source": str(self.root), "params": {"form": ["not:THF"]}})
        summary = report.summary()
        self.assertEqual((summary["fake"]["proved"], summary["fake"]["solved"]), (3, 3))
        self.assertEqual((summary["giveup"]["unknown"], summary["giveup"]["solved"]), (3, 0))
        self.assertEqual((summary["liar"]["satisfiable"], summary["liar"]["mismatches"]), (3, 3))
        self.assertEqual(len(report.mismatches()), 3)

    def test_formats(self):
        report = runner.run(search.search(self.root, domain="SET"), self.provers(), timeout=20)
        table = report.table()
        self.assertIn("SET001-1", table)
        self.assertIn("proved", table)
        self.assertIn("! = the verdict contradicts", table)
        data = json.loads(report.to_json())
        self.assertEqual(data["problems"], ["SET001-1"])
        self.assertEqual(data["results"][0]["status"], "proved")
        self.assertNotIn("stdout", data["results"][0])
        self.assertIn("stdout", json.loads(report.to_json(output=True))["results"][0])
        lines = report.to_csv().splitlines()
        self.assertEqual(lines[0].split(",")[:4], ["problem", "prover", "status", "szs"])
        self.assertEqual(len(lines), 4)

    def test_other_inputs(self):
        file = self.root / "Problems" / "PUZ" / "PUZ001+1.p"
        report = runner.run(["SET001-1", file, Problem.from_text("cnf(a,axiom,p).", "mine")],
                            self.fake(), timeout=20, library=self.root)
        self.assertEqual(report.problems, ["SET001-1", "PUZ001+1", "mine"])
        self.assertEqual([r.status for r in report.results], [Status.PROVED, Status.PROVED, Status.SATISFIABLE])
        single = runner.run("SET001-1", [self.fake()], library=self.root)
        self.assertEqual(len(single.results), 1)
        by_spec = runner.run("SET001-1", f"x={sys.executable} {self.script}", library=self.root)
        self.assertEqual(by_spec.results[0].status, Status.PROVED)

    def test_unloadable_problems_are_errors(self):
        report = runner.run(["SET001-1", "SET999-1"], self.provers(), timeout=20, library=self.root)
        missing = [r for r in report.results if r.problem == "SET999-1"]
        self.assertEqual(len(missing), 3)
        self.assertTrue(all(r.status == Status.ERROR and "cannot load" in r.error for r in missing))
        self.assertEqual(len(report.results), 6)

    def test_failing_prover_does_not_stop_the_run(self):
        class Explodes(provers.Prover):
            name = "explodes"

            def run(self, problem, timeout=None):
                raise OSError("disk on fire")

        report = runner.run(["SET001-1"], [Explodes(), self.fake()], library=self.root, timeout=20)
        self.assertEqual([r.status for r in report.results], [Status.ERROR, Status.PROVED])
        self.assertIn("disk on fire", report.results[0].error)

    def test_output_is_dropped_unless_kept(self):
        kept = runner.run(["SET001-1"], self.fake(), library=self.root, timeout=20).results[0]
        dropped = runner.run(["SET001-1"], self.fake(), library=self.root, timeout=20, keep_output=False).results[0]
        self.assertIn("SZS", kept.stdout)
        self.assertEqual((dropped.stdout, dropped.solution), ("", None))

    def test_parallel_gives_the_same_results(self):
        result = search.search(self.root)
        serial = runner.run(result, self.provers(), timeout=20)
        parallel = runner.run(result, self.provers(), timeout=20, jobs=4)
        key = lambda report: [(r.problem, r.prover, r.status) for r in report.results]  # noqa: E731
        self.assertEqual(key(serial), key(parallel))
        self.assertEqual(len(parallel.results), 12)
        streamed = list(runner.iter_results(result, self.provers(), timeout=20, jobs=3))
        self.assertEqual(len(streamed), 12)

    def test_timeouts_in_parallel(self):
        problems = [Problem.from_text("% SLEEP\ncnf(a,axiom,p).\n", f"s{i}") for i in range(4)]
        start = time.perf_counter()
        report = runner.run(problems, self.fake(), timeout=0.5, jobs=4)
        self.assertLess(time.perf_counter() - start, 5)
        self.assertEqual({r.status for r in report.results}, {Status.TIMEOUT})

    def test_collection_run_method(self):
        report = ProverCollection([self.fake()]).run(search.search(self.root, domain="SET"), timeout=20)
        self.assertEqual(report.results[0].status, Status.PROVED)

    def test_lazy_fetching_during_a_run(self):
        library = Counting(self.root)
        library.fetched = []
        result = search.search(library)
        seen = []
        runner.run(result, CallableProver(lambda p, c: seen.append(list(library.fetched)) or "Theorem"), timeout=None)
        self.assertEqual([len(s) for s in seen], [1, 2, 3, 4])


# ---------------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------------


class RunCommand(WithFake):
    def prover_arg(self, label="fake", *words):
        return f'{label}={sys.executable} {self.script}' + "".join(f" {w}" for w in words)

    def test_names(self):
        status, out, err = run("run", "-L", self.root, "SET001-1", "PUZ001+1", "-p", self.prover_arg(), "-t", "20")
        self.assertEqual(status, 0, err)
        self.assertIn("proved", out)
        self.assertIn("SET001-1", out)
        self.assertIn("[2/2]", err)

    def test_search_criteria_and_json(self):
        status, out, err = run("run", "-L", self.root, "--domain", "SET,PUZ", "-p", self.prover_arg(),
                               "-p", "giveup=" + sys.executable + " -c pass", "--format", "json", "-q", "-t", "20")
        self.assertEqual(status, 0, err)
        data = json.loads(out)
        self.assertEqual(data["problems"], ["PUZ001+1", "SET001-1"])
        self.assertEqual(data["search"]["params"], {"domain": ["SET,PUZ"]})
        self.assertEqual(data["summary"]["fake"]["proved"], 2)
        self.assertEqual(data["summary"]["giveup"]["unknown"], 2)
        self.assertEqual(err, "")

    def test_csv_stdin_input_limit_and_output_dir(self):
        status, out, err = run("run", "-L", self.root, "--form", "CNF,FOF", "--limit", "1", "-p", self.prover_arg(),
                               "--input", "stdin", "--format", "csv", "--output-dir", self.tmp / "saved", "-t", "20")
        self.assertEqual(status, 0, err)
        self.assertEqual(len(out.splitlines()), 2)
        self.assertEqual(out.splitlines()[1].split(",")[:3], ["PUZ001+1", "fake", "proved"])
        self.assertIn("SZS status", (self.tmp / "saved" / "fake" / "PUZ001+1.out").read_text())

    def test_names_from_stdin_and_files(self):
        file = self.tmp / "mine.p"
        file.write_text("fof(c,conjecture,$true).\n")
        status, out, err = run("run", "-L", self.root, "-", file, "-p", self.prover_arg(), "--format", "csv",
                               "-t", "20", "-q", stdin="SET001-1\n")
        self.assertEqual(status, 0, err)
        self.assertEqual([line.split(",")[0] for line in out.splitlines()[1:]], ["SET001-1", "mine"])

    def test_mismatch_is_flagged(self):
        status, _, err = run("run", "-L", self.root, "SET001-1", "-p", self.prover_arg(), "-t", "20")
        self.assertNotIn("contradicts", err)
        with mock.patch.dict(os.environ, {"FAKE_STATUS": "CounterSatisfiable"}):
            status, out, err = run("run", "-L", self.root, "SET001-1", "-p", self.prover_arg(), "-t", "20")
        self.assertIn("contradicts the header", err)
        self.assertIn("!", out)

    def test_errors(self):
        status, _, err = run("run", "-L", self.root, "SET001-1")
        self.assertEqual(status, 1)
        self.assertIn("-p/--prover", err)
        status, _, err = run("run", "-L", self.root, "-p", "x")
        self.assertIn("no problems", err)
        status, _, err = run("run", "-L", self.root, "SET001-1", "--domain", "SET", "-p", "x")
        self.assertIn("not both", err)
        status, _, err = run("run", "-L", self.root, "SET001-1", "-p", "no-such-prover-anywhere")
        self.assertEqual(status, 1)
        self.assertIn("prover not found", err)
        status, _, err = run("run", "-L", self.root, "--form", "DH1", "-p", self.prover_arg())
        self.assertIn("no problems match", err)
        status, _, err = run("run", "-L", self.root, "--form", "nope", "-p", self.prover_arg())
        self.assertIn("unknown logical form", err)
        status, _, err = run("run", "--form", "FOF", "-p", self.prover_arg())
        self.assertIn("-L/--library", err)
        status, _, err = run("run", "-L", self.root, "SET001-1", "-p", "x 'oops")
        self.assertEqual(status, 1)

    def test_help_lists_criteria(self):
        status, out, _ = run("run", "--help")
        self.assertEqual(status, 0)
        for text in ("--prover", "--timeout", "--jobs", "--rating", "--output-dir"):
            self.assertIn(text, out)


if __name__ == "__main__":
    unittest.main()
