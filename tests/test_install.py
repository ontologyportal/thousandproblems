"""Tests for installing provers: platforms, versions, download URLs, source builds and `install`.

Nothing here touches the network: downloads are `file://` URLs, and GitHub is replaced by a stub.
"""

import hashlib
import io
import os
import shutil
import sys
import tarfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from thousandproblems import install, provers
from thousandproblems.install import InstallError, Platform, version_key
from thousandproblems.problem import Problem
from thousandproblems.provers import EProver, ProverError, Status, SubprocessProver, Vampire, prover_from_spec

from test_cli import TempCase, run

LINUX = Platform("linux", "x86_64")
SCRIPT = b"#!/bin/sh\necho '% SZS status Theorem'\n"
needs_sh = unittest.skipIf(os.name == "nt", "the installed test programs are shell scripts")


class Platforms(unittest.TestCase):
    def test_detect(self):
        here = Platform.detect()
        self.assertIn(here.os, ("linux", "macos", "windows") if sys.platform in ("linux", "darwin", "win32")
                      else (here.os,))
        self.assertEqual(Platform.parse(str(here)), here)

    def test_parse(self):
        for text, expected in [("macos-arm64", Platform("macos", "arm64")), ("Darwin aarch64", Platform("macos", "arm64")),
                               ("linux/x86_64", LINUX), ("linux_x86_64", LINUX), ("LINUX-amd64", LINUX),
                               ("linux x86-64", LINUX), ("windows-x64", Platform("windows", "x86_64")),
                               ("osx-arm64", Platform("macos", "arm64"))]:
            self.assertEqual(Platform.parse(text), expected, text)
        here = Platform.detect()
        self.assertEqual(Platform.parse("linux"), Platform("linux", here.arch))      # missing parts: this machine's
        self.assertEqual(Platform.parse("arm64"), Platform(here.os, "arm64"))
        self.assertIs(Platform.parse(LINUX), LINUX)
        for bad in ("", "plan9-x86_64", "linux-riscv64", "linux-linux"):
            with self.assertRaises(ValueError, msg=bad):
                Platform.parse(bad)

    def test_str(self):
        self.assertEqual(str(Platform("macos", "arm64")), "macos-arm64")


class Versions(unittest.TestCase):
    def test_order(self):
        self.assertEqual(sorted(["3.5.10", "3.5.9", "3.4", "10.0", "3.5.10.1"], key=version_key),
                         ["3.4", "3.5.9", "3.5.10", "3.5.10.1", "10.0"])

    def test_normalize(self):
        v, e = Vampire(), EProver()
        self.assertEqual([v.normalize_version(x) for x in ("5.1.0", "v5.1.0", " v5.1.0 ", "latest", "LATEST")],
                         ["5.1.0", "5.1.0", "5.1.0", "latest", "latest"])
        self.assertEqual([e.normalize_version(x) for x in ("3.5.10", "E-3.5.10")], ["3.5.10", "3.5.10"])

    def test_resolve(self):
        self.assertEqual(Vampire().resolve_version(), "5.1.0")                 # the class's default
        self.assertEqual(Vampire(version="v5.0.1").resolve_version(), "5.0.1")
        self.assertEqual(Vampire().resolve_version("4.9"), "4.9")
        self.assertEqual(SubprocessProver("tool").resolve_version(), "current")

    def test_latest_from_a_release(self):
        with mock.patch.object(install, "_github", return_value={"tag_name": "v5.2.0"}) as github:
            self.assertEqual(Vampire(version="latest").resolve_version(), "5.2.0")
        self.assertIn("vprover/vampire/releases/latest", github.call_args[0][0])

    def test_latest_from_tags_when_there_are_no_releases(self):
        pages = {1: [{"name": n} for n in ("start", "E-3.5.9", "E-3.5.10", "E-3.6-pre1", "E-2.6", "SPEC-1-0")]}

        def fake(url):
            if url.endswith("/releases/latest"):
                return None
            return pages.get(int(url.rsplit("page=", 1)[1]), [])

        with mock.patch.object(install, "_github", side_effect=fake):
            self.assertEqual(EProver().latest_version(), "3.5.10")
        with mock.patch.object(install, "_github", side_effect=lambda url: None if "releases" in url else []):
            with self.assertRaisesRegex(ProverError, "no release or tag"):
                EProver().latest_version()

    def test_latest_needs_a_repo(self):
        with self.assertRaises(NotImplementedError):
            SubprocessProver("tool", version="latest").resolve_version()


class Urls(unittest.TestCase):
    def test_vampire_releases(self):
        base = "https://github.com/vprover/vampire/releases/download/v5.1.0/vampire-"
        v = Vampire()
        for platform, asset in [("linux-x86_64", "Linux-X64"), ("linux-arm64", "Linux-ARM64"),
                                ("macos-x86_64", "macOS-X64"), ("macos-arm64", "macOS-ARM64"),
                                ("windows-x86_64", "Windows-X64"), ("windows-arm64", "Windows-ARM64")]:
            self.assertEqual(v.artifact_url("5.1.0", platform), f"{base}{asset}.zip")
        self.assertEqual(v.artifact_url("5.0.1", LINUX),
                         "https://github.com/vprover/vampire/releases/download/v5.0.1/vampire-Linux-X64.zip")

    def test_platform_comes_from_the_prover(self):
        self.assertTrue(Vampire(platform="linux-arm64").artifact_url("5.1.0").endswith("vampire-Linux-ARM64.zip"))
        self.assertTrue(Vampire(platform="macos").platform.os == "macos")
        with self.assertRaises(ProverError):
            Vampire(platform="amiga-m68k")

    def test_unsupported_platforms_are_reported(self):
        with self.assertRaisesRegex(ProverError, "no build for linux-s390x"):
            Vampire().artifact_url("5.1.0", Platform("linux", "s390x"))
        with self.assertRaisesRegex(ProverError, "no build for freebsd-x86_64"):
            Vampire().artifact_url("5.1.0", Platform("freebsd", "x86_64"))

    def test_e_is_source(self):
        e = EProver()
        self.assertEqual(e.artifact_url("3.5.10"), "https://github.com/eprover/eprover/archive/refs/tags/E-3.5.10.tar.gz")
        self.assertEqual(e.artifact_url("3.5.10", "linux-arm64"), e.artifact_url("3.5.10"))     # any platform

    def test_dict_urls_and_placeholders(self):
        class Tool(SubprocessProver):
            binary = "tool"
            download_url = {"linux-x86_64": "https://x/{version}/{platform}/{os}/{arch}",
                            "macos": "https://x/mac/{tag}"}
            release_tag = "t{version}"

        self.assertEqual(Tool().artifact_url("2", LINUX), "https://x/2/linux-x86_64/linux/x86_64")
        self.assertEqual(Tool().artifact_url("2", "macos-arm64"), "https://x/mac/t2")
        with self.assertRaisesRegex(ProverError, "no download for windows"):
            Tool().artifact_url("2", "windows-x86_64")

    def test_no_url_at_all(self):
        with self.assertRaisesRegex(NotImplementedError, "no download_url"):
            SubprocessProver("tool").artifact_url("1")

    def test_checksums_cover_every_platform_vampire_ships(self):
        v = Vampire()
        for key in v.checksums:
            version, platform = key.split("/")
            v.artifact_url(version, platform)                   # a platform it can build a URL for
        self.assertEqual(len(v.checksums), 6)
        self.assertTrue(all(len(c) == 64 for c in v.checksums.values()))


class Specs(unittest.TestCase):
    def test_version_in_a_spec(self):
        p = prover_from_spec("eprover@3.5.10 --auto-schedule")
        self.assertIsInstance(p, EProver)
        self.assertEqual((p.version, p.name, p.extra_args), ("3.5.10", "eprover@3.5.10", ["--auto-schedule"]))
        p = prover_from_spec("fast=vampire@v5.1.0", platform="linux-arm64")
        self.assertIsInstance(p, Vampire)
        self.assertEqual((p.version, p.name, str(p.platform)), ("5.1.0", "fast", "linux-arm64"))
        p = prover_from_spec("/opt/tools@home/prover")           # an @ in a directory is not a version
        self.assertEqual((p.binary, p.version), ("/opt/tools@home/prover", None))


class Layout(TempCase):
    """Where installed copies live and which one is found."""

    def make(self, version, platform=None, name="tool"):
        platform = platform or Platform.detect()
        path = self.tmp / "installed" / f"{version}-{platform}" / "bin" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(SCRIPT)
        path.chmod(0o755)
        return str(path)

    def tool(self, **kw):
        return SubprocessProver("tool", install_dir=self.tmp / "installed", **kw)

    @needs_sh
    def test_finding(self):
        old, new = self.make("1.9"), self.make("1.10")
        self.make("2.0", LINUX if Platform.detect() != LINUX else Platform("linux", "arm64"))   # another platform
        self.assertEqual(self.tool().installed_versions(), ["1.10", "1.9"])
        self.assertEqual(self.tool().find_binary(), new)                       # not on $PATH: the newest
        self.assertEqual(self.tool(version="latest").find_binary(), new)
        self.assertEqual(self.tool(version="1.9").find_binary(), old)
        self.assertEqual(self.tool(version="v1.9").find_binary(), old)
        self.assertIsNone(self.tool(version="3.0").find_binary())              # never some other version
        self.assertEqual(self.tool(version="1.9").name, "tool@1.9")
        self.assertEqual(self.tool(version="1.9", name="mine").name, "mine")

    @needs_sh
    def test_path_beats_installed_unless_a_version_is_asked_for(self):
        installed = self.make("1.0")
        bin_dir = self.tmp / "onpath"
        bin_dir.mkdir()
        (bin_dir / "tool").write_bytes(SCRIPT)
        (bin_dir / "tool").chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}):
            self.assertEqual(self.tool().find_binary(), str(bin_dir / "tool"))
            self.assertEqual(self.tool(version="1.0").find_binary(), installed)
            self.assertEqual(self.tool(version="latest").find_binary(), installed)

    @needs_sh
    def test_platform_chooses_the_folder(self):
        other = Platform("linux", "arm64")
        found = self.make("1.0", other)
        self.assertIsNone(self.tool().find_binary() if Platform.detect() != other else None)
        self.assertEqual(self.tool(platform=other).find_binary(), found)


# ---------------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------------


def tool_class(**attrs):
    return type("Tool", (SubprocessProver,), {"binary": "tool", **attrs})


class InstallDownloads(TempCase):
    def cls(self, **attrs):
        return tool_class(**attrs)

    def prover(self, cls, **kw):
        return cls(install_dir=self.tmp / "installed", **kw)

    def zip(self, name="p.zip", inner="pkg/tool"):
        archive = self.tmp / name
        with zipfile.ZipFile(archive, "w") as z:
            info = zipfile.ZipInfo(inner)
            info.external_attr = 0o644 << 16           # as in a zip made without the executable bit
            z.writestr(info, SCRIPT)
        return archive

    def test_no_installer(self):
        with self.assertRaisesRegex(NotImplementedError, "no download_url"):
            self.prover(tool_class()).install()
        with self.assertRaises(NotImplementedError):
            SubprocessProver("whatever-prover-xyz").install()

    @needs_sh
    def test_a_bare_binary_and_checksums(self):
        source = self.tmp / "tool"
        source.write_bytes(SCRIPT)
        good = hashlib.sha256(SCRIPT).hexdigest()
        cls = self.cls(download_url=source.as_uri(), default_version="1.0", checksums={"1.0": good})
        prover = self.prover(cls)
        self.assertFalse(prover.available())
        messages = []
        path = prover.install(log=messages.append)
        self.assertEqual(Path(path), self.tmp / "installed" / f"1.0-{Platform.detect()}" / "bin" / "tool")
        self.assertTrue(os.access(path, os.X_OK))
        self.assertTrue(prover.available())
        self.assertEqual(prover.run(Problem.from_text("cnf(a,axiom,p).")).status, Status.PROVED)
        self.assertTrue(any("downloading" in m for m in messages))
        self.assertFalse(any("not be verified" in m for m in messages))
        self.assertEqual(prover.install(), path)                        # already there: nothing to do
        self.assertEqual(prover.install(force=True), path)
        self.assertEqual(sorted(p.name for p in (self.tmp / "installed").iterdir()), [f"1.0-{Platform.detect()}"])

    @needs_sh
    def test_bad_checksum_leaves_nothing_behind(self):
        source = self.tmp / "tool"
        source.write_bytes(SCRIPT)
        cls = self.cls(download_url=source.as_uri(), default_version="1.0", checksums={"1.0": "0" * 64})
        with self.assertRaisesRegex(ProverError, "SHA-256"):
            self.prover(cls).install()
        self.assertEqual(list((self.tmp / "installed").iterdir()), [])
        path = self.prover(cls).install(sha256=hashlib.sha256(SCRIPT).hexdigest())      # an override wins
        self.assertTrue(os.path.isfile(path))

    @needs_sh
    def test_checksum_lookup_is_by_platform_then_version(self):
        source = self.tmp / "tool"
        source.write_bytes(SCRIPT)
        good = hashlib.sha256(SCRIPT).hexdigest()
        cls = self.cls(download_url=source.as_uri(), default_version="1.0",
                       checksums={"1.0": "0" * 64, f"1.0/{Platform.detect()}": good})
        self.assertTrue(os.path.isfile(self.prover(cls).install()))

    @needs_sh
    def test_unverified_downloads_are_said_so(self):
        source = self.tmp / "tool"
        source.write_bytes(SCRIPT)
        messages = []
        self.prover(self.cls(download_url=source.as_uri())).install(log=messages.append)
        self.assertTrue(any("not be verified" in m for m in messages))

    @needs_sh
    def test_archive_version_and_platform_in_the_url(self):
        self.zip("tool-v2.1-linux-x86_64.zip")
        base = self.tmp.as_uri()
        cls = self.cls(download_url=base + "/tool-{tag}-{platform}.zip", release_tag="v{version}",
                       default_version="2.0")
        prover = self.prover(cls, version="2.1", platform=LINUX)
        path = prover.install()                                         # a Linux download, kept apart
        self.assertEqual(Path(path), self.tmp / "installed" / "2.1-linux-x86_64" / "pkg" / "tool")
        self.assertTrue(os.access(path, os.X_OK))                        # the zip had no executable bit
        self.assertTrue(prover.available())
        self.assertEqual(prover.name, "tool@2.1")
        with self.assertRaisesRegex(ProverError, "cannot download"):    # no 2.0 on offer
            self.prover(cls, platform=LINUX).install()
        with self.assertRaisesRegex(ProverError, "cannot download"):    # nor 2.1 for another platform
            self.prover(cls, version="2.1", platform="macos-arm64").install()

    def test_bad_downloads(self):
        with self.assertRaisesRegex(ProverError, "cannot download"):
            self.prover(self.cls(download_url=(self.tmp / "missing").as_uri())).install()
        empty = self.zip("e.zip", inner="pkg/other")
        with self.assertRaisesRegex(ProverError, "found no 'tool'"):
            self.prover(self.cls(download_url=empty.as_uri())).install()
        self.assertEqual(list((self.tmp / "installed").iterdir()), [])

    def test_a_platform_the_vendor_does_not_build_for(self):
        with self.assertRaisesRegex(ProverError, "no build for"):
            self.prover(self.cls(download_url="x", os_names={"some-other-os": "X"})).install()

    def test_unsafe_archives_are_refused(self):
        archive = self.tmp / "evil.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("../escape", "x")
        with self.assertRaisesRegex(ProverError, "unsafe path"):
            self.prover(self.cls(download_url=archive.as_uri())).install()
        self.assertFalse((self.tmp / "escape").exists())

    def test_override(self):
        class Custom(SubprocessProver):
            binary = "custom"

            def install(self, **_):
                return "/somewhere"

        self.assertEqual(Custom().install(), "/somewhere")


@unittest.skipUnless(shutil.which("make") and os.name != "nt", "needs make and a POSIX shell")
class InstallBuilds(TempCase):
    """A prover distributed as source: unpack, ``./configure && make``, keep the binary."""

    CONFIGURE = b"#!/bin/sh\necho configured > config.mk\n"
    MAKEFILE = (b"all:\n\t@test -f config.mk\n\tmkdir -p PROVER\n"
                b"\tprintf '#!/bin/sh\\necho \"%% SZS status Theorem\"\\n' > PROVER/tool\n\tchmod +x PROVER/tool\n")

    def tarball(self, name="tool-1.2.tar.gz", top="tool-tool-1.2", files=None):
        files = files if files is not None else {"configure": (self.CONFIGURE, 0o755), "Makefile": (self.MAKEFILE, 0o644)}
        archive = self.tmp / name
        with tarfile.open(archive, "w:gz") as tar:
            for rel, (data, mode) in files.items():
                info = tarfile.TarInfo(f"{top}/{rel}")
                info.size, info.mode = len(data), mode
                tar.addfile(info, io.BytesIO(data))
        return archive

    def cls(self, **attrs):
        return tool_class(
            github_repo="o/tool", release_tag="tool-{version}", default_version="1.2",
            download_url=self.tmp.as_uri() + "/{tag}.tar.gz",
            build_commands=(("./configure",), ("make", "-j{jobs}")), build_tools=("make", ("cc", "gcc", "clang", "sh")),
            binary_path="PROVER/tool", **attrs)

    def prover(self, **kw):
        return self.cls()(install_dir=self.tmp / "installed", **kw)

    def test_build_and_install(self):
        self.tarball("tool-1.2.tar.gz")
        prover = self.prover()
        messages = []
        path = prover.install(jobs=2, log=messages.append)
        self.assertEqual(Path(path), self.tmp / "installed" / f"1.2-{Platform.detect()}" / "bin" / "tool")
        self.assertEqual([m for m in messages if m.startswith("running")], ["running ./configure", "running make -j2"])
        self.assertEqual(prover.run(Problem.from_text("cnf(a,axiom,p).")).status, Status.PROVED)
        root = self.tmp / "installed" / f"1.2-{Platform.detect()}"
        self.assertEqual(sorted(p.name for p in root.iterdir()), ["bin"])             # the source tree is gone
        self.assertEqual([p.name for p in (self.tmp / "installed").iterdir()], [root.name])   # and no build log
        self.assertEqual(prover.install(), path)

    def test_keep_build_tree(self):
        self.tarball("tool-1.2.tar.gz")
        cls = self.cls()
        cls.keep_build_tree = True
        path = cls(install_dir=self.tmp / "installed").install()
        self.assertTrue((Path(path).parent.parent / "src" / "tool-tool-1.2" / "config.mk").is_file())

    def test_version_picks_the_tarball(self):
        self.tarball("tool-1.3.tar.gz", top="tool-tool-1.3")
        path = self.prover(version="1.3").install()
        self.assertIn("1.3-", path)
        with self.assertRaisesRegex(ProverError, "cannot download"):
            self.prover(version="9.9").install()

    def test_a_failing_build_reports_its_output_and_keeps_the_log(self):
        self.tarball("tool-1.2.tar.gz", files={
            "configure": (b"#!/bin/sh\necho 'no C compiler found, giving up' >&2\nexit 1\n", 0o755)})
        with self.assertRaises(ProverError) as caught:
            self.prover().install()
        self.assertIn("exit status 1", str(caught.exception))
        self.assertIn("no C compiler found", str(caught.exception))
        log = self.tmp / "installed" / f"1.2-{Platform.detect()}.build.log"
        self.assertIn("no C compiler found", log.read_text())
        self.assertEqual([p.name for p in (self.tmp / "installed").iterdir()], [log.name])   # nothing half-installed

    def test_a_build_that_makes_nothing(self):
        self.tarball("tool-1.2.tar.gz", files={"configure": (b"#!/bin/sh\n", 0o755),
                                                    "Makefile": (b"all:\n\t@true\n", 0o644)})
        with self.assertRaisesRegex(ProverError, "made no 'PROVER/tool'"):
            self.prover().install()

    def test_missing_build_tools(self):
        self.tarball("tool-1.2.tar.gz")
        cls = tool_class(download_url="x", build_commands=(("make",),), build_tools=("make", ("no-such-cc-1", "no-such-cc-2")))
        with self.assertRaisesRegex(ProverError, "needs no-such-cc-1 or no-such-cc-2"):
            cls(install_dir=self.tmp / "installed").install()

    def test_cannot_build_for_another_platform(self):
        self.tarball("tool-1.2.tar.gz")
        other = "linux-arm64" if Platform.detect() != Platform("linux", "arm64") else "macos-arm64"
        with self.assertRaisesRegex(ProverError, "can only be installed for this machine"):
            self.prover(platform=other).install()

    def test_e_prover_is_described_for_a_source_build(self):
        e = EProver()
        self.assertEqual((e.build_commands, e.binary_path), ((("./configure",), ("make", "-j{jobs}")), "PROVER/eprover"))
        self.assertIn("make", e.build_tools)


# ---------------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------------


@unittest.skipIf(os.name == "nt", "the installed test programs are shell scripts")
class InstallCommand(TempCase):
    def setUp(self):
        super().setUp()
        self.source = self.tmp / "tool-1.0"
        self.source.write_bytes(SCRIPT)
        self.second = self.tmp / "tool-2.0"
        self.second.write_bytes(SCRIPT + b"# two\n")

        class Tool(SubprocessProver):
            binary = "faketool"
            default_version = "1.0"
            download_url = self.tmp.as_uri() + "/tool-{version}"
            os_names = {"linux": "linux", "macos": "macos"}
            github_repo = "o/faketool"

        patch = mock.patch.dict(provers.PRESETS, {"faketool": Tool})
        patch.start()
        self.addCleanup(patch.stop)

    def test_list(self):
        status, out, _ = run("install", "--list")
        self.assertEqual(status, 0)
        self.assertIn(f"platform: {Platform.detect()}", out)
        for name in ("eprover", "vampire", "faketool"):
            self.assertIn(name, out)
        self.assertIn("builds from source", out)
        self.assertIn("downloads a release", out)
        status, out, err = run("install")
        self.assertEqual(status, 1)
        self.assertIn("name a prover", err)

    def test_install_a_version(self):
        status, out, err = run("install", "faketool")
        self.assertEqual(status, 0, err)
        self.assertTrue(out.strip().endswith(os.path.join(f"1.0-{Platform.detect()}", "bin", "faketool")))
        self.assertIn("downloading", err)
        status, out2, _ = run("install", "faketool", "-q")
        self.assertEqual(out2, out)
        status, out, err = run("install", "faketool", "--version", "2.0", "-q")
        self.assertIn(f"2.0-{Platform.detect()}", out)
        self.assertEqual(err, "")
        _, listing, _ = run("install", "--list")
        self.assertIn("installed: 2.0, 1.0", listing)

    def test_install_for_another_platform(self):
        other = "linux-x86_64" if Platform.detect() != LINUX else "macos-arm64"
        status, out, err = run("install", "faketool", "--platform", other, "-q")
        self.assertEqual(status, 0, err)
        self.assertIn(f"1.0-{other}", out)

    def test_errors(self):
        status, _, err = run("install", "nonesuch")
        self.assertEqual(status, 1)
        self.assertIn("known: eprover, vampire, faketool", err)
        status, _, err = run("install", "faketool", "--platform", "plan9")
        self.assertEqual(status, 1)
        self.assertIn("cannot read platform", err)
        status, _, err = run("install", "faketool", "--version", "9.9", "-q")
        self.assertEqual(status, 1)
        self.assertIn("cannot download", err)
        status, _, err = run("install", "faketool", "--platform", "windows-x86_64", "-q")
        self.assertIn("no build for windows-x86_64", err)

    def test_run_with_a_pinned_version(self):
        library = self.tmp / "tptp"
        (library / "Problems" / "SET").mkdir(parents=True)
        (library / "Problems" / "SET" / "SET001-1.p").write_text("cnf(a,axiom,p).\n")
        args = ("run", "-L", library, "SET001-1", "-p", "faketool@2.0", "-t", "20", "--format", "csv", "-q")
        status, _, err = run(*args)
        self.assertEqual(status, 1)
        self.assertIn("prover not found: faketool@2.0", err)
        self.assertIn("--install", err)
        status, out, err = run(*args, "--install")
        self.assertEqual(status, 0, err)
        self.assertEqual(out.splitlines()[1].split(",")[:3], ["SET001-1", "faketool@2.0", "proved"])
        status, _, err = run(*args)                                                   # installed now
        self.assertEqual(status, 0, err)

    def test_install_option_with_nothing_to_install(self):
        status, _, err = run("run", "SET001-1", "-p", "no-such-prover-anywhere", "--install", "-q")
        self.assertEqual(status, 1)
        self.assertIn("cannot be installed automatically", err)


@unittest.skipUnless(os.environ.get("THOUSANDPROBLEMS_LIVE"), "set THOUSANDPROBLEMS_LIVE=1 to download and build real provers")
class Live(TempCase):
    """Fetches Vampire's release and builds E from its GitHub source, then proves a problem with each."""

    problem = Problem.from_text("fof(a,axiom,![X]:(p(X)=>q(X))).\nfof(b,axiom,p(a)).\nfof(c,conjecture,q(a)).\n", "t")

    def check(self, prover):
        path = prover.install()
        self.assertTrue(os.access(path, os.X_OK))
        self.assertTrue(prover.available())
        result = prover.run(self.problem, timeout=60)
        self.assertEqual((result.status, result.szs), (Status.PROVED, "Theorem"), result.stderr)

    def test_vampire_release(self):
        self.check(Vampire(version="5.1.0"))

    @unittest.skipUnless(shutil.which("make") and shutil.which("cc"), "needs make and a C compiler")
    def test_e_from_source(self):
        self.check(EProver(version="3.5.10"))

    def test_latest_versions_resolve(self):
        self.assertRegex(Vampire().latest_version(), r"^\d+\.\d+")
        self.assertRegex(EProver().latest_version(), r"^\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
