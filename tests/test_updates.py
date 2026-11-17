"""Tests for release checks: the 30-day reminder, `refresh`, and the online cache's version."""

import io
import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from thousandproblems import download, updates
from thousandproblems.library import LibraryError

from test_cli import LIBRARY, SET001, Server, TempCase, run, write_tree

DAY = 24 * 3600
LISTING = b'<a href="TPTP-v9.3.0.tgz">x</a> <a href="TPTP-v10.0.0.tgz">TPTP-v10.0.0.tgz</a>'


class TTY(io.StringIO):
    def isatty(self):
        return True


class Reminder(TempCase):
    def setUp(self):
        super().setUp()
        os.environ.pop("THOUSANDPROBLEMS_NO_UPDATE_CHECK", None)
        self.cache = self.tmp / "cache"

    def test_clock_starts_on_first_use(self):
        now = 1_000_000_000
        self.assertIsNone(updates.check_due(now))
        self.assertEqual(updates.load_state()["last_check"], now)
        self.assertIsNone(updates.check_due(now + 29 * DAY))
        self.assertEqual(updates.check_due(now + 31 * DAY), 31)

    def test_snooze_and_postpone(self):
        now = 1_000_000_000
        updates.check_due(now)
        later = now + 40 * DAY
        updates.snooze(later)
        self.assertIsNone(updates.check_due(later + 29 * DAY))
        self.assertEqual(updates.check_due(later + 31 * DAY), 71)
        updates.postpone(later + 31 * DAY)
        self.assertIsNone(updates.check_due(later + 31 * DAY + DAY / 2))
        self.assertIsNotNone(updates.check_due(later + 33 * DAY))

    def test_can_be_turned_off(self):
        updates.check_due(0)
        with mock.patch.dict(os.environ, {"THOUSANDPROBLEMS_NO_UPDATE_CHECK": "1"}):
            self.assertIsNone(updates.check_due(100 * DAY))

    def test_unreadable_state_starts_again(self):
        self.cache.mkdir(parents=True, exist_ok=True)
        (self.cache / "state.json").write_text("not json")
        self.assertIsNone(updates.check_due(5))
        self.assertEqual(updates.load_state()["last_check"], 5)


class Versions(TempCase):
    def test_from_folder_name(self):
        self.assertEqual(updates.detect_version(self.root), "10.0.0")
        self.assertEqual(updates.detect_version(self.tmp / "TPTP-v9.0.0.tgz"), "9.0.0")

    def test_from_a_problem_header(self):
        root = write_tree(self.tmp / "mytptp")
        self.assertEqual(updates.detect_version(root), "10.0.0")
        self.assertIsNone(updates.detect_version(write_tree(self.tmp / "bare", {"Problems/SET/SET001-1.p": "cnf(a,axiom,p).\n"})))

    def test_online_cache_version(self):
        cache = self.tmp / "cache"
        self.assertIsNone(updates.online_cache_version(cache))
        host = cache / "seetptp.tptp.org"
        write_tree(host, {"Problems/SET/SET001-1.p": SET001.replace("v10.0.0", "v9.3.0")})
        self.assertEqual(updates.online_cache_version(cache), "9.3.0")     # from the header
        (host / "VERSION").write_text("10.0.0\n")
        self.assertEqual(updates.online_cache_version(cache), "10.0.0")    # recorded


class CheckForUpdates(TempCase):
    def setUp(self):
        super().setUp()
        self.server = Server({"/dist/": LISTING})
        self.addCleanup(self.server.close)
        patch = mock.patch.object(download, "DISTRIBUTION_URL", f"{self.server.url}/dist/")
        patch.start()
        self.addCleanup(patch.stop)
        self.cache = self.tmp / "cache"
        self.host = self.cache / "seetptp.tptp.org"

    def fill_cache(self, version):
        write_tree(self.host, {"Problems/SET/SET001-1.p": SET001.replace("v10.0.0", f"v{version}"),
                               "Axioms/SET001-0.ax": LIBRARY["Axioms/SET001-0.ax"]})

    def test_old_cache_is_cleared(self):
        self.fill_cache("9.3.0")
        report = updates.check_for_updates()
        self.assertEqual((report.latest, report.cache_version, report.cache_cleared), ("10.0.0", "9.3.0", 2))
        self.assertFalse((self.host / "Problems").exists())
        self.assertEqual((self.host / "VERSION").read_text().strip(), "10.0.0")
        self.assertIn("cleared 2 files", report.summary())

    def test_current_cache_is_kept(self):
        self.fill_cache("10.0.0")
        report = updates.check_for_updates()
        self.assertEqual(report.cache_cleared, 0)
        self.assertTrue((self.host / "Problems/SET/SET001-1.p").exists())
        self.assertIn("up to date", report.summary())

    def test_other_servers_caches_are_untouched(self):
        write_tree(self.cache / "mirror.example", {"Problems/SET/SET001-1.p": SET001.replace("v10.0.0", "v8.0.0")})
        updates.check_for_updates()
        self.assertTrue((self.cache / "mirror.example/Problems/SET/SET001-1.p").exists())

    def test_local_library(self):
        old = write_tree(self.tmp / "TPTP-v9.3.0")
        report = updates.check_for_updates(old)
        self.assertTrue(report.local_outdated)
        self.assertIn("get v10.0.0 with 'thousandproblems download'", report.summary())
        self.assertFalse(updates.check_for_updates(self.root).local_outdated)

    def test_check_resets_the_reminder(self):
        updates.check_due(0)
        updates.snooze(0)
        updates.check_for_updates(now=50 * DAY)
        state = updates.load_state()
        self.assertEqual(state["last_check"], 50 * DAY)
        self.assertNotIn("quiet_until", state)
        self.assertEqual(state["latest_version"], "10.0.0")

    def test_unreachable(self):
        with mock.patch.object(download, "DISTRIBUTION_URL", f"{self.server.url}/missing/"):
            with self.assertRaises(LibraryError):
                updates.check_for_updates()

    # --- the command line ---

    def test_refresh_command(self):
        self.fill_cache("9.3.0")
        old = write_tree(self.tmp / "TPTP-v9.3.0")
        status, out, err = run("refresh", "-L", old)
        self.assertEqual(status, 0, err)
        self.assertIn("latest TPTP release : v10.0.0", out)
        self.assertIn("cleared 2 files", out)
        self.assertIn("is v9.3.0; get v10.0.0", out)
        status, out, _ = run("refresh", "--json", "--index", "-L", self.root)
        data = json.loads(out)
        self.assertEqual((data["latest"], data["local_version"], data["local_outdated"]), ("10.0.0", "10.0.0", False))

    def overdue(self):
        os.environ.pop("THOUSANDPROBLEMS_NO_UPDATE_CHECK", None)
        updates.save_state({"last_check": time.time() - 45 * DAY})

    def prompt(self, answer):
        """Run a command at a terminal, answering the reminder with ``answer``."""
        stderr = TTY()
        with mock.patch("sys.stdin", TTY(answer)), mock.patch("sys.stderr", stderr), \
                mock.patch("sys.stdout", io.StringIO()) as stdout:
            from thousandproblems import cli
            status = cli.main(["find", "-r", "-L", str(self.root), "SET001-1"])
        return status, stdout.getvalue(), stderr.getvalue()

    def test_reminder_without_a_terminal_warns_once_a_day(self):
        self.overdue()
        _, out, err = run("find", "-r", "-L", self.root, "SET001-1")
        self.assertEqual(out.strip(), "Problems/SET/SET001-1.p")
        self.assertIn("not checked for a new TPTP library version in 45 days", err)
        self.assertIn("thousandproblems refresh", err)
        _, _, err = run("find", "-r", "-L", self.root, "SET001-1")
        self.assertNotIn("not checked", err)

    def test_reminder_yes(self):
        self.overdue()
        self.fill_cache("9.3.0")
        status, out, err = self.prompt("y\n")
        self.assertEqual(status, 0)
        self.assertIn("in 45 days, check now? [y]es / [n]o / [s]nooze for thirty days", err)
        self.assertIn("latest TPTP release : v10.0.0", err)
        self.assertEqual(out.strip(), "Problems/SET/SET001-1.p")      # the command still runs
        self.assertIsNone(updates.check_due())

    def test_reminder_no_asks_again_tomorrow(self):
        self.overdue()
        self.prompt("n\n")
        self.assertIsNone(updates.check_due())
        self.assertIsNotNone(updates.check_due(time.time() + 2 * DAY))

    def test_reminder_snooze(self):
        self.overdue()
        self.prompt("maybe\ns\n")                    # asks again after a bad answer
        self.assertIsNone(updates.check_due(time.time() + 29 * DAY))
        self.assertIsNotNone(updates.check_due(time.time() + 31 * DAY))

    def test_reminder_check_fails(self):
        self.overdue()
        with mock.patch.object(download, "DISTRIBUTION_URL", f"{self.server.url}/missing/"):
            status, _, err = self.prompt("y\n")
        self.assertEqual(status, 0)
        self.assertIn("could not check", err)

    def test_no_reminder_for_refresh_or_download(self):
        self.overdue()
        _, _, err = run("refresh")
        self.assertNotIn("not checked", err)


if __name__ == "__main__":
    unittest.main()
