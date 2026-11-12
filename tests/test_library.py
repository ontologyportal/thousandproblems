"""Tests for fetching problems from a TPTP library: folder, tarball and (mock) website."""

import http.server
import io
import os
import tarfile
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import thousandproblems as tptp
from thousandproblems import library as lib

FILES = {
    "Problems/SET/SET001-1.p": "% File     : SET001-1 : TPTP v9.0.0.\n% Status   : Unsatisfiable\n"
                               "include('Axioms/SET001-0.ax').\ncnf(goal,negated_conjecture,~ member(a,b)).\n",
    "Problems/SET/SET002-1.p": "include('Axioms/SET001-0.ax',[ax_two]).\ncnf(own,hypothesis,q).\n",
    "Problems/SET/SET003-1.p": "include('Axioms/SET001-0.ax',[nope]).\n",
    "Problems/SYN/SYN000+1.p": "fof(f1,conjecture,? [X] : p(X)).\n",
    "Problems/SYN/SYN000^1.p": "thf(t,axiom,$true).\n",
    "Problems/SET/SET010-1.p": "include('Axioms/NESTED1.ax').\n",
    "Problems/SET/SET011-1.p": "include('Axioms/LOOP1.ax').\n",
    "Problems/SET/SET012-1.p": "include('Axioms/MISSING.ax').\n",
    "Problems/SET/SET013-1.p": "include('local.ax').\ncnf(z,axiom,z).\n",
    "Problems/SET/local.ax": "cnf(from_local,axiom,l).\n",
    "Problems/SET/SET014-1.p": "cnf(broken,axiom,p | ).\n",
    "Axioms/SET001-0.ax": "cnf(ax_one,axiom,member(X,Y) | ~ subset(X,Y)).\ncnf(ax_two,axiom,subset(a,a)).\n",
    "Axioms/NESTED1.ax": "include('Axioms/NESTED2.ax').\ncnf(n1,axiom,n1).\n",
    "Axioms/NESTED2.ax": "cnf(n2,axiom,n2).\n",
    "Axioms/LOOP1.ax": "include('Axioms/LOOP2.ax').\n",
    "Axioms/LOOP2.ax": "include('Axioms/LOOP1.ax').\n",
}


def write_tree(root: Path, files=FILES):
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def make_tarball(path: Path, files=FILES, top="TPTP-v9.0.0", mode="w:gz", dot_slash=False):
    with tarfile.open(path, mode) as tar:
        for rel, text in files.items():
            data = text.encode()
            name = "/".join(filter(None, [top, rel]))
            info = tarfile.TarInfo(("./" if dot_slash else "") + name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def names(parsed):
    return [f.name for f in parsed.formulae]


class TempCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)


class Names(unittest.TestCase):
    def test_problem_path(self):
        self.assertEqual(lib.problem_path("SET001-1"), "Problems/SET/SET001-1.p")
        self.assertEqual(lib.problem_path("SET001-1.p"), "Problems/SET/SET001-1.p")
        self.assertEqual(lib.problem_path("SEU140+3"), "Problems/SEU/SEU140+3.p")
        self.assertEqual(lib.problem_path("SYO000^1"), "Problems/SYO/SYO000^1.p")
        self.assertEqual(lib.problem_path("SYN000-1.002"), "Problems/SYN/SYN000-1.002.p")

    def test_rejects_things_that_are_not_problem_names(self):
        for bad in ["", "set001-1", "SET", "../SET001-1", "SET001-1/../../x", "SET/SET001-1", "/etc/passwd",
                    "SET001-1\\..\\x", "SET001..1", "SET001 1", "SET001-1\x00", "..", "Problems/SET/SET001-1.p"]:
            with self.subTest(name=bad):
                with self.assertRaises(ValueError):
                    lib.problem_path(bad)

    def test_clean_paths_stay_inside_the_library(self):
        self.assertEqual(lib._clean("Axioms/./X.ax"), "Axioms/X.ax")
        self.assertEqual(lib._clean("Problems/SET/../../Axioms/X.ax"), "Axioms/X.ax")
        for bad in ["../x", "Axioms/../../x", "/abs", "C:/x", "", ".", "a\x00b"]:
            with self.subTest(path=bad):
                self.assertIsNone(lib._clean(bad))


class Behaviour:
    """Checks every library type must pass; subclasses provide ``self.library``."""

    def test_problem_text(self):
        self.assertEqual(self.library.problem_text("SET001-1"), FILES["Problems/SET/SET001-1.p"])
        self.assertEqual(self.library.problem_text("SYN000+1.p"), FILES["Problems/SYN/SYN000+1.p"])
        self.assertEqual(self.library.problem_text("SYN000^1"), FILES["Problems/SYN/SYN000^1.p"])

    def test_parse_keeps_includes_by_default(self):
        problem = self.library.problem("SET001-1")
        self.assertEqual(names(problem), ["goal"])
        self.assertEqual([i.path for i in problem.includes], ["Axioms/SET001-0.ax"])
        self.assertEqual(problem.header.status, "Unsatisfiable")

    def test_expand_includes(self):
        self.assertEqual(names(self.library.problem("SET001-1", expand_includes=True)),
                         ["ax_one", "ax_two", "goal"])

    def test_selection(self):
        self.assertEqual(names(self.library.problem("SET002-1", expand_includes=True)), ["ax_two", "own"])
        with self.assertRaisesRegex(tptp.ParseError, "nope"):
            self.library.problem("SET003-1", expand_includes=True)

    def test_nested_includes(self):
        self.assertEqual(names(self.library.problem("SET010-1", expand_includes=True)), ["n2", "n1"])

    def test_circular_include(self):
        with self.assertRaisesRegex(tptp.ParseError, "circular"):
            self.library.problem("SET011-1", expand_includes=True)

    def test_missing_include_is_a_parse_error_with_a_line(self):
        with self.assertRaises(tptp.ParseError) as caught:
            self.library.problem("SET012-1", expand_includes=True)
        self.assertIn("MISSING.ax", str(caught.exception))
        self.assertEqual(caught.exception.line, 1)

    def test_other_languages(self):
        self.assertEqual(self.library.problem("SYN000+1").formulae[0].language, "fof")
        self.assertEqual(self.library.problem("SYN000^1").formulae[0].language, "thf")

    def test_syntax_errors_name_the_file(self):
        with self.assertRaises(tptp.ParseError) as caught:
            self.library.problem("SET014-1")
        self.assertIn("SET014-1.p", str(caught.exception))

    def test_unknown_problem(self):
        with self.assertRaises(lib.ProblemNotFound) as caught:
            self.library.problem("SET999-1")
        self.assertIn("SET999-1", str(caught.exception))
        self.assertIsInstance(caught.exception, FileNotFoundError)
        with self.assertRaises(lib.ProblemNotFound):
            self.library.problem_text("ZZZ001-1")

    def test_bad_names_never_reach_the_library(self):
        with self.assertRaises(ValueError):
            self.library.problem("../../etc/passwd")

    def test_read_cannot_escape_the_library(self):
        for path in ["../x", "Problems/../../x", "/etc/passwd"]:
            with self.subTest(path=path), self.assertRaises(lib.ProblemNotFound):
                self.library.read(path)

    def test_read_axiom_file(self):
        self.assertEqual(self.library.read("Axioms/SET001-0.ax"), FILES["Axioms/SET001-0.ax"])


class DirectoryTests(Behaviour, TempCase):
    def setUp(self):
        super().setUp()
        write_tree(self.tmp / "TPTP-v9.0.0")
        self.library = lib.DirectoryLibrary(self.tmp / "TPTP-v9.0.0")

    def test_includes_resolve_next_to_the_problem_too(self):
        self.assertEqual(names(self.library.problem("SET013-1", expand_includes=True)), ["from_local", "z"])

    def test_problem_names(self):
        self.assertEqual(self.library.problem_names("SYN"), ["SYN000+1", "SYN000^1"])
        self.assertIn("SET001-1", self.library.problem_names())
        self.assertEqual(self.library.problem_names("ZZZ"), [])

    def test_not_a_library(self):
        with self.assertRaisesRegex(lib.LibraryError, "Problems"):
            lib.DirectoryLibrary(self.tmp)


class TarballTests(Behaviour, TempCase):
    def setUp(self):
        super().setUp()
        make_tarball(self.tmp / "t.tgz")
        self.library = lib.TarballLibrary(self.tmp / "t.tgz")
        self.addCleanup(self.library.close)

    def test_problem_names(self):
        self.assertEqual(self.library.problem_names("SYN"), ["SYN000+1", "SYN000^1"])
        self.assertIn("SET001-1", self.library.problem_names())

    def test_includes_resolve_next_to_the_problem_too(self):
        self.assertEqual(names(self.library.problem("SET013-1", expand_includes=True)), ["from_local", "z"])


class TarballLayouts(TempCase):
    def test_gzip_bzip2_xz_and_plain(self):
        for mode in ["w", "w:gz", "w:bz2", "w:xz"]:
            with self.subTest(mode=mode):
                path = self.tmp / f"lib-{mode.replace(':', '')}.tar"
                make_tarball(path, mode=mode)
                with lib.TarballLibrary(path) as library:
                    self.assertEqual(names(library.problem("SYN000+1")), ["f1"])

    def test_no_top_level_folder(self):
        path = self.tmp / "flat.tar"
        make_tarball(path, top="")
        with lib.TarballLibrary(path) as library:
            self.assertEqual(names(library.problem("SET001-1", expand_includes=True)), ["ax_one", "ax_two", "goal"])

    def test_dot_slash_prefix(self):
        path = self.tmp / "dot.tar"
        make_tarball(path, dot_slash=True)
        with lib.TarballLibrary(path) as library:
            self.assertEqual(names(library.problem("SYN000+1")), ["f1"])

    def test_nested_top_level_folders(self):
        path = self.tmp / "deep.tar"
        make_tarball(path, top="dist/TPTP-v9.0.0")
        with lib.TarballLibrary(path) as library:
            self.assertEqual(names(library.problem("SYN000+1")), ["f1"])

    def test_not_a_tptp_tarball(self):
        path = self.tmp / "other.tar"
        make_tarball(path, files={"README": "hi\n"})
        with lib.TarballLibrary(path) as library, self.assertRaisesRegex(lib.LibraryError, "Problems"):
            library.problem("SET001-1")

    def test_not_a_tarball_at_all(self):
        path = self.tmp / "junk.tgz"
        path.write_bytes(b"this is not an archive")
        with self.assertRaisesRegex(lib.LibraryError, "cannot read"):
            lib.TarballLibrary(path).problem("SET001-1")

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            lib.TarballLibrary(self.tmp / "nope.tgz")

    def test_members_outside_the_library_are_ignored(self):
        path = self.tmp / "evil.tar"
        make_tarball(path, mode="w")
        with tarfile.open(path, "a") as tar:
            info = tarfile.TarInfo("TPTP-v9.0.0/../../escape.txt")
            info.size = 2
            tar.addfile(info, io.BytesIO(b"x\n"))
        with lib.TarballLibrary(path) as library:
            self.assertEqual(names(library.problem("SYN000+1")), ["f1"])
            with self.assertRaises(lib.ProblemNotFound):
                library.read("../escape.txt")


class MockServer:
    """Stands in for the TPTP website: ``/download/TPTPWorld/TPTP/<path>`` returns a .tgz of
    the file (stored under its bare name) plus everything it includes, laid out from the
    root; unknown files get an HTML page with status 200, as the real server does."""

    def __init__(self, files=FILES):
        self.files = files
        self.requests = []          # (raw path, headers)
        self.mode = "ok"            # or "429", "500", "garbage"
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.requests.append((self.path, dict(self.headers)))
                if outer.mode == "429":
                    self.send_response(429)
                    self.send_header("Retry-After", "7")
                    self.end_headers()
                    self.wfile.write(b"<h1>429 Too Many Requests</h1>")
                    return
                if outer.mode == "500":
                    self.send_response(500)
                    self.end_headers()
                    return
                if outer.mode == "garbage":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"\x1f\x8bnot really gzip")
                    return
                prefix = "/download/TPTPWorld/TPTP/"
                path = urllib.parse.unquote(self.path[len(prefix):]) if self.path.startswith(prefix) else None
                body = outer.bundle(path) if path else None
                if body is None:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<html>no such file</html>")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()

    def bundle(self, path):
        import posixpath
        if path not in self.files:
            return None
        members = {posixpath.basename(path): self.files[path]}
        todo = [path]
        while todo:
            try:
                includes = tptp.parse(self.files[todo.pop()]).includes
            except tptp.ParseError:        # a broken problem is still served
                includes = ()
            for item in includes:
                if item.path in self.files and item.path not in members:
                    members[item.path] = self.files[item.path]
                    todo.append(item.path)
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for name, text in members.items():
                info = tarfile.TarInfo(name)
                data = text.encode()
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return buffer.getvalue()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class OnlineTests(Behaviour, TempCase):
    def setUp(self):
        super().setUp()
        self.server = MockServer()
        self.addCleanup(self.server.close)
        self.library = lib.OnlineLibrary(self.server.url, cache=self.tmp / "cache")

    def test_problem_and_includes_arrive_in_one_request(self):
        self.library.problem("SET001-1", expand_includes=True)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.server.requests[0][0], "/download/TPTPWorld/TPTP/Problems/SET/SET001-1.p")

    def test_names_are_url_encoded(self):
        self.library.problem("SYN000^1")
        self.assertEqual(self.server.requests[0][0], "/download/TPTPWorld/TPTP/Problems/SYN/SYN000%5E1.p")
        self.library.problem("SYN000+1")
        self.assertEqual(self.server.requests[1][0], "/download/TPTPWorld/TPTP/Problems/SYN/SYN000+1.p")

    def test_identifies_itself(self):
        self.library.problem("SYN000+1")
        agent = self.server.requests[0][1]["User-Agent"]
        self.assertTrue(agent.startswith("thousandproblems/"), agent)

    def test_memory_serves_repeat_reads(self):
        self.library.problem("SET001-1")
        self.library.problem("SET001-1", expand_includes=True)
        self.library.read("Axioms/SET001-0.ax")
        self.assertEqual(len(self.server.requests), 1)

    def test_disk_cache_survives_a_new_instance(self):
        self.library.problem("SET001-1", expand_includes=True)
        fresh = lib.OnlineLibrary(self.server.url, cache=self.tmp / "cache")
        self.server.requests.clear()
        self.assertEqual(names(fresh.problem("SET001-1", expand_includes=True)), ["ax_one", "ax_two", "goal"])
        self.assertEqual(self.server.requests, [])
        cached = list((self.tmp / "cache").rglob("SET001-1.p"))
        self.assertEqual(len(cached), 1)
        self.assertEqual(cached[0].read_text(), FILES["Problems/SET/SET001-1.p"])
        self.assertEqual(list((self.tmp / "cache").rglob("*.tmp")), [])

    def test_shared_axioms_are_downloaded_once(self):
        self.library.problem("SET001-1", expand_includes=True)
        self.library.problem("SET002-1", expand_includes=True)        # axiom file already cached
        self.assertEqual([r[0].rsplit("/", 1)[1] for r in self.server.requests], ["SET001-1.p", "SET002-1.p"])

    def test_no_cache(self):
        library = lib.OnlineLibrary(self.server.url, cache=False)
        library.problem("SYN000+1")
        self.assertIsNone(library.cache_dir)
        self.assertFalse((self.tmp / "cache").exists())

    def test_missing_problem_is_not_found_even_though_status_is_200(self):
        with self.assertRaises(lib.ProblemNotFound):
            self.library.problem("SET999-1")
        self.assertEqual(list((self.tmp / "cache").rglob("*")), [])      # nothing cached for a miss

    def test_rate_limit(self):
        self.server.mode = "429"
        with self.assertRaises(lib.RateLimited) as caught:
            self.library.problem("SYN000+1")
        self.assertEqual(caught.exception.retry_after, 7)
        self.assertIn("7", str(caught.exception))
        self.assertEqual(len(self.server.requests), 1)         # not retried behind your back

    def test_server_error(self):
        self.server.mode = "500"
        with self.assertRaisesRegex(lib.LibraryError, "500"):
            self.library.problem("SYN000+1")

    def test_corrupt_download(self):
        self.server.mode = "garbage"
        with self.assertRaisesRegex(lib.LibraryError, "unreadable"):
            self.library.problem("SYN000+1")

    def test_unreachable_server(self):
        port = self.server.server.server_address[1]
        self.server.close()
        with self.assertRaisesRegex(lib.LibraryError, "cannot reach"):
            lib.OnlineLibrary(f"http://127.0.0.1:{port}", cache=False, timeout=5).problem("SYN000+1")

    def test_no_listing(self):
        with self.assertRaises(NotImplementedError):
            self.library.problem_names()

    def test_cache_that_cannot_be_written_is_fine(self):
        blocker = self.tmp / "blocker"
        blocker.write_text("a file where a directory should go")
        library = lib.OnlineLibrary(self.server.url, cache=blocker)
        self.assertEqual(names(library.problem("SYN000+1")), ["f1"])


class OpenLibrary(TempCase):
    def setUp(self):
        super().setUp()
        write_tree(self.tmp / "dir")
        make_tarball(self.tmp / "t.tgz")
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("TPTP", None)

    def test_folder_tarball_and_url(self):
        self.assertIsInstance(lib.open_library(self.tmp / "dir"), lib.DirectoryLibrary)
        self.assertIsInstance(lib.open_library(str(self.tmp / "dir")), lib.DirectoryLibrary)
        self.assertIsInstance(lib.open_library(self.tmp / "t.tgz"), lib.TarballLibrary)
        online = lib.open_library("https://example.org/tptp/")
        self.assertIsInstance(online, lib.OnlineLibrary)
        self.assertEqual(online.base_url, "https://example.org/tptp")

    def test_default_is_the_tptp_environment_variable_else_the_website(self):
        self.assertIsInstance(lib.open_library(), lib.OnlineLibrary)
        self.assertEqual(lib.open_library().base_url, lib.DEFAULT_URL)
        os.environ["TPTP"] = str(self.tmp / "dir")
        self.assertIsInstance(lib.open_library(), lib.DirectoryLibrary)
        os.environ["TPTP"] = str(self.tmp / "does-not-exist")
        self.assertIsInstance(lib.open_library(), lib.OnlineLibrary)

    def test_library_is_passed_through(self):
        library = lib.DirectoryLibrary(self.tmp / "dir")
        self.assertIs(lib.open_library(library), library)

    def test_nothing_there(self):
        with self.assertRaises(FileNotFoundError):
            lib.open_library(self.tmp / "nope")

    def test_fetch_problem_from_each_kind_of_source(self):
        server = MockServer()
        self.addCleanup(server.close)
        results = [
            tptp.fetch_problem("SET001-1", self.tmp / "dir", expand_includes=True),
            tptp.fetch_problem("SET001-1", self.tmp / "t.tgz", expand_includes=True),
            tptp.fetch_problem("SET001-1", lib.OnlineLibrary(server.url, cache=False), expand_includes=True),
        ]
        for result in results:
            self.assertEqual(names(result), ["ax_one", "ax_two", "goal"])
        self.assertEqual({r.formulae for r in results[1:]}, {results[1].formulae})   # tarball == website
        self.assertEqual([f.formula for f in results[0].formulae], [f.formula for f in results[1].formulae])

    def test_fetch_problem_closes_what_it_opened_but_not_what_it_was_given(self):
        library = lib.TarballLibrary(self.tmp / "t.tgz")
        tptp.fetch_problem("SYN000+1", library)
        self.assertIsNotNone(library._tar)
        library.close()
        with mock.patch.object(lib.TarballLibrary, "close", autospec=True) as close:
            tptp.fetch_problem("SYN000+1", self.tmp / "t.tgz")
            self.assertEqual(close.call_count, 1)

    def test_environment_variable_is_used_by_fetch_problem(self):
        os.environ["TPTP"] = str(self.tmp / "dir")
        self.assertEqual(names(tptp.fetch_problem("SYN000+1")), ["f1"])


class PublicNames(unittest.TestCase):
    def test_exported_from_the_package(self):
        for name in ["fetch_problem", "open_library", "Library", "DirectoryLibrary", "TarballLibrary",
                     "OnlineLibrary", "LibraryError", "ProblemNotFound", "RateLimited"]:
            with self.subTest(name=name):
                self.assertIs(getattr(tptp, name), getattr(lib, name))
                self.assertIn(name, tptp.__all__)

    def test_star_import(self):
        namespace = {}
        exec("from thousandproblems import *", namespace)
        self.assertIn("fetch_problem", namespace)
        self.assertIn("parse", namespace)


@unittest.skipUnless(os.environ.get("THOUSANDPROBLEMS_LIVE"), "set THOUSANDPROBLEMS_LIVE=1 to talk to tptp.org")
class Live(unittest.TestCase):
    def test_real_server(self):
        with tempfile.TemporaryDirectory() as cache:
            library = lib.OnlineLibrary(cache=cache)
            problem = library.problem("SET001-1", expand_includes=True)
            self.assertGreater(len(problem.formulae), 5)
            self.assertEqual(problem.header.status, "Unsatisfiable")
            with self.assertRaises(lib.ProblemNotFound):
                library.problem("SET999-9")


if __name__ == "__main__":
    unittest.main()
