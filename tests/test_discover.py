"""Tests for `discover`: finding TPTP libraries and setting $TPTP."""

import io
import json
import os
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

from thousandproblems import cli, discover

from test_cli import LIBRARY, TempCase, make_tarball, run, write_tree

PARTIAL = {k: v for k, v in LIBRARY.items() if k.startswith(("Problems", "Axioms"))}


class TTY(io.StringIO):
    def isatty(self):
        return True


class Base(TempCase):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home),
                                           "SHELL": "/bin/zsh", "THOUSANDPROBLEMS_NO_UPDATE_CHECK": "1"})
        env.start()
        self.addCleanup(env.stop)
        for name in ("ZDOTDIR", "XDG_CONFIG_HOME", "TPTP"):
            os.environ.pop(name, None)
        self.zshrc = self.home / ".zshrc"
        self.search = self.tmp / "search"
        self.search.mkdir()


class Finding(Base):
    def test_nested_libraries_best_first(self):
        write_tree(self.search / "a" / "TPTP-v9.0.0")
        write_tree(self.search / "b" / "c" / "TPTP-v10.0.0")
        write_tree(self.search / "copy", PARTIAL)                          # like fetch -o
        found = discover.find_libraries([self.search])
        self.assertEqual([f.path.name for f in found], ["TPTP-v10.0.0", "TPTP-v9.0.0", "copy"])
        self.assertEqual([f.version for f in found], ["10.0.0", "9.0.0", "10.0.0"])
        self.assertEqual([f.complete for f in found], [True, True, False])
        self.assertTrue(all(f.valid for f in found))
        self.assertEqual(found[0].problems, 4)

    def test_depth_limit(self):
        write_tree(self.search / "1" / "2" / "3" / "4" / "TPTP-v10.0.0")
        self.assertEqual(discover.find_libraries([self.search], depth=4), [])
        self.assertEqual(len(discover.find_libraries([self.search], depth=5)), 1)

    def test_root_itself_and_skipped_folders(self):
        root = write_tree(self.search / "TPTP-v10.0.0")
        write_tree(self.tmp / "s2" / ".hidden" / "TPTP-v9.0.0")
        write_tree(self.tmp / "s2" / "node_modules" / "TPTP-v9.0.0")
        self.assertEqual([f.path for f in discover.find_libraries([root])], [root.absolute()])
        self.assertEqual(discover.find_libraries([self.tmp / "s2"]), [])

    def test_does_not_look_inside_a_library(self):
        root = write_tree(self.search / "TPTP-v10.0.0")
        write_tree(root / "Documents" / "old" / "TPTP-v1.0.0")
        self.assertEqual(len(discover.find_libraries([self.search])), 1)

    def test_invalid_and_packed(self):
        write_tree(self.search / "good" / "TPTP-v9.0.0")
        write_tree(self.search / "bad", {"Problems/SET/PUZ001-1.p": "cnf(a,axiom,p).\n", "Axioms/x.ax": ""})
        make_tarball(self.search / "TPTP-v10.0.0.tgz")
        found = discover.find_libraries([self.search])
        self.assertEqual([(f.path.name, f.valid, f.packed) for f in found],
                         [("TPTP-v9.0.0", True, False), ("TPTP-v10.0.0.tgz", False, True), ("bad", False, False)])
        self.assertIn("wrong domain folder", found[2].errors[0])

    @unittest.skipIf(sys.platform == "win32", "symlinks need extra rights on Windows")
    def test_symlink_is_kept(self):
        real = write_tree(self.tmp / "real" / "TPTP-v10.0.0")
        link = self.search / "tptp"
        link.symlink_to(real, target_is_directory=True)
        found = discover.find_libraries([self.search, self.tmp / "real"])
        self.assertEqual(len(found), 1)                        # the same library, once
        self.assertEqual(found[0].path, link.absolute())       # $TPTP follows the link
        self.assertEqual(found[0].version, "10.0.0")

    def test_folder_budget(self):
        write_tree(self.search / "z" / "TPTP-v10.0.0")
        for i in range(20):
            (self.search / f"a{i:02d}").mkdir()
        self.assertEqual(discover.find_libraries([self.search], max_folders=5), [])


class Shells(Base):
    def test_commands(self):
        path = "/data/it's here"
        self.assertEqual(discover.export_command(path, "zsh"), "export TPTP='/data/it'\\''s here'")
        self.assertEqual(discover.export_command(path, "fish"), "set -gx TPTP '/data/it'\\''s here'")
        self.assertEqual(discover.export_command(r"C:\it's", "powershell"), r"$env:TPTP = 'C:\it''s'")
        self.assertEqual(discover.export_command(r"C:\tptp", "cmd"), r'set "TPTP=C:\tptp"')

    def test_detect(self):
        if sys.platform == "win32":
            self.skipTest("POSIX shells")
        for shell, name in (("/bin/zsh", "zsh"), ("/usr/local/bin/bash", "bash"), ("/opt/homebrew/bin/fish", "fish"),
                            ("/bin/dash", "sh"), ("", "sh")):
            with self.subTest(shell), mock.patch.dict(os.environ, {"SHELL": shell}):
                self.assertEqual(discover.detect_shell(), name)

    def test_startup_files(self):
        self.assertEqual(discover.startup_file("zsh", self.home), self.home / ".zshrc")
        with mock.patch.dict(os.environ, {"ZDOTDIR": str(self.tmp / "z")}):
            self.assertEqual(discover.startup_file("zsh", self.home), self.tmp / "z" / ".zshrc")
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(discover.startup_file("bash", self.home), self.home / ".bash_profile")
        with mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(discover.startup_file("bash", self.home), self.home / ".bashrc")
        self.assertEqual(discover.startup_file("fish", self.home),
                         self.home / ".config" / "fish" / "conf.d" / "thousandproblems.fish")
        self.assertEqual(discover.startup_file("sh", self.home), self.home / ".profile")
        self.assertIsNone(discover.startup_file("powershell", self.home))

    def test_block_is_added_then_replaced(self):
        first = discover.planned_text("alias ll='ls -l'\n", "/a", "zsh")
        self.assertEqual(first, f"alias ll='ls -l'\n\n{discover.MARK_BEGIN}\nexport TPTP='/a'\n{discover.MARK_END}\n")
        second = discover.planned_text(first + "echo after\n", "/b", "zsh")
        self.assertEqual(second.count(discover.MARK_BEGIN), 1)
        self.assertIn("export TPTP='/b'", second)
        self.assertNotIn("'/a'", second)
        self.assertTrue(second.endswith("echo after\n"))
        self.assertEqual(discover.planned_text("", "/a", "zsh").count("\n"), 3)

    def test_other_settings(self):
        text = discover.planned_text("export TPTP=/old\nTPTP=/older\n# TPTP=commented\n", "/new", "zsh")
        self.assertEqual(discover.other_settings(text), ["export TPTP=/old", "TPTP=/older"])

    def test_install_keeps_content_and_mode(self):
        self.zshrc.write_text("setopt autocd\n")
        if sys.platform != "win32":
            os.chmod(self.zshrc, 0o600)
        discover.install("/data/TPTP", "zsh", self.zshrc)
        self.assertTrue(self.zshrc.read_text().startswith("setopt autocd\n"))
        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(self.zshrc.stat().st_mode), 0o600)
        fish = discover.install("/data/TPTP", "fish", self.home / ".config/fish/conf.d/thousandproblems.fish")
        self.assertIn("set -gx TPTP '/data/TPTP'", fish.read_text())


class Command(Base):
    def setUp(self):
        super().setUp()
        # detect_shell() ignores $SHELL on Windows (it picks powershell/cmd), so pin it
        # here; tests that want another shell pass --shell explicitly.
        shell = mock.patch.object(discover, "detect_shell", return_value="zsh")
        shell.start()
        self.addCleanup(shell.stop)
        self.lib = write_tree(self.search / "TPTP-v10.0.0")

    def interactive(self, *argv, answers=""):
        out, err = io.StringIO(), TTY()
        with mock.patch("sys.stdin", TTY(answers)), mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            status = cli.main(["discover", *map(str, argv)])
        return status, out.getvalue(), err.getvalue()

    def test_prints_the_command_and_changes_nothing(self):
        status, out, _ = run("discover", self.search)
        self.assertEqual(status, 0)
        self.assertIn(f"1. v10.0.0   {self.lib.absolute()}  (4 problems)", out)
        self.assertIn(f"export TPTP='{self.lib.absolute()}'", out)
        self.assertIn("discover --set", out)
        self.assertFalse(self.zshrc.exists())

    def test_set_needs_yes_without_a_terminal(self):
        status, _, err = run("discover", "--set", self.search)
        self.assertEqual(status, 1)
        self.assertIn("--yes", err)
        self.assertFalse(self.zshrc.exists())

    def test_set_yes(self):
        self.zshrc.write_text("export TPTP=/somewhere/else\n")
        status, out, _ = run("discover", "--set", "--yes", self.search)
        self.assertEqual(status, 0)
        self.assertIn(f"export TPTP='{self.lib.absolute()}'", self.zshrc.read_text())
        self.assertIn("warning:", out)                          # the older line is pointed out
        status, out, _ = run("discover", "--set", "--yes", self.search)
        self.assertIn("already sets it", out)
        self.assertEqual(self.zshrc.read_text().count(discover.MARK_BEGIN), 1)

    def test_set_another_library_replaces_the_block(self):
        run("discover", "--set", "--yes", self.search)
        other = write_tree(self.tmp / "other" / "TPTP-v11.0.0")
        run("discover", "--set", "--yes", self.tmp / "other")
        text = self.zshrc.read_text()
        self.assertEqual(text.count(discover.MARK_BEGIN), 1)
        self.assertIn(str(other.absolute()), text)
        self.assertNotIn(str(self.lib.absolute()), text)

    def test_already_current(self):
        with mock.patch.dict(os.environ, {"TPTP": str(self.lib)}):
            _, out, _ = run("discover", self.search)
        self.assertIn("<- current $TPTP", out)
        self.assertIn("$TPTP is already set to it", out)

    def test_interactive_choice_and_confirmation(self):
        old = write_tree(self.search / "TPTP-v9.0.0")
        status, out, err = self.interactive(self.search, answers="7\n2\ny\n")
        self.assertEqual(status, 0)
        self.assertIn("Use which one? [1]", err)
        self.assertIn("choose one of 1, 2", err)
        self.assertIn("so that new terminals use it? [y/N]", err)
        self.assertIn(f"export TPTP='{old.absolute()}'", self.zshrc.read_text())

    def test_interactive_default_is_no(self):
        status, _, _ = self.interactive(self.search, answers="\n")
        self.assertEqual(status, 0)
        self.assertFalse(self.zshrc.exists())

    def test_fish(self):
        run("discover", "--shell", "fish", "--set", "--yes", self.search)
        self.assertIn("set -gx TPTP", (self.home / ".config/fish/conf.d/thousandproblems.fish").read_text())

    def test_windows_uses_setx(self):
        with mock.patch("subprocess.run") as setx:
            setx.return_value.returncode = 0
            status, out, _ = run("discover", "--shell", "powershell", self.search)
            self.assertIn(f"$env:TPTP = '{self.lib.absolute()}'", out)
            self.assertIn("setx TPTP", out)
            setx.assert_not_called()
            status, out, _ = run("discover", "--shell", "powershell", "--set", "--yes", self.search)
            setx.assert_called_once_with(["setx", "TPTP", str(self.lib.absolute())], capture_output=True, text=True)
        self.assertEqual(status, 0)

    def test_nothing_usable(self):
        status, out, _ = run("discover", self.tmp / "nowhere")
        self.assertEqual(status, 1)
        self.assertIn("No TPTP library found", out)
        write_tree(self.tmp / "bad", {"Problems/SET/PUZ001-1.p": "", "Axioms/x.ax": ""})
        status, out, _ = run("discover", self.tmp / "bad")
        self.assertEqual(status, 1)
        self.assertIn("INVALID", out)

    def test_json(self):
        status, out, _ = run("discover", "--json", "--shell", "bash", self.search)
        data = json.loads(out)
        self.assertEqual(data["chosen"], str(self.lib.absolute()))
        self.assertEqual(data["command"], f"export TPTP='{self.lib.absolute()}'")
        self.assertEqual(data["found"][0]["problems"], 4)
        self.assertFalse(self.zshrc.exists())


if __name__ == "__main__":
    unittest.main()
