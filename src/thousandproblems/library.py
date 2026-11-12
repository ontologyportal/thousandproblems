"""Fetch problems from a TPTP library: an unpacked folder, a tarball, or the TPTP website.

    >>> import thousandproblems as tptp
    >>> problem = tptp.fetch_problem("SET001-1", "/data/TPTP-v9.0.0")        # doctest: +SKIP
    >>> problem = tptp.fetch_problem("SET001-1", "/data/TPTP-v9.0.0.tgz")    # doctest: +SKIP
    >>> problem = tptp.fetch_problem("SET001-1")                              # doctest: +SKIP

All three understand the standard layout, in which problem ``SET001-1`` lives at
``Problems/SET/SET001-1.p`` and includes such as ``include('Axioms/SET001-0.ax')`` are
relative to the library root::

    TPTP-v9.0.0/
        Problems/SET/SET001-1.p
        Axioms/SET001-0.ax

Use :func:`open_library` to get a :class:`Library` when you want to read several
problems, and :func:`fetch_problem` for a single one. With no source given, the folder
named by the ``TPTP`` environment variable is used if there is one, and the website
otherwise.

Online access
-------------
The website allows only a few requests per minute (at the time of writing, 6), so
the online library fetches a problem *together with the axiom files it includes* in
one request, and keeps everything it downloads in a cache directory
(``$THOUSANDPROBLEMS_CACHE``, or the platform's usual cache location). Reading a problem
twice, or an axiom file shared by several problems, costs one request. If the server
refuses a request for going too fast, :class:`RateLimited` is raised, with the time
to wait; nothing is retried automatically. Only the current release is available
online; for a particular version, download its tarball and open that.

Only the standard library is used, and nothing in this module is imported until you
ask for it.
"""

from __future__ import annotations

import io
import os
import posixpath
import re
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional, Union

from .nodes import AnnotatedFormula, ParseError, TPTPFile, _error_at

__all__ = [
    "DirectoryLibrary", "Library", "LibraryError", "OnlineLibrary", "ProblemNotFound",
    "RateLimited", "TarballLibrary", "fetch_problem", "open_library",
]

DEFAULT_URL = "https://seetptp.tptp.org"

#: ``SET001-1``, ``SEU140+3``, ``SYN000_1``, ``SYO000^1``, ``SYN000-1.002``: three
#: capitals for the domain, three digits, then whatever the naming scheme allows.
_NAME_RE = re.compile(r"[A-Z]{3}[0-9]{3}[A-Za-z0-9_+\-^=#.]*")


class LibraryError(Exception):
    """A problem with reaching or reading a TPTP library."""


class ProblemNotFound(LibraryError, FileNotFoundError):
    """The library has no such problem or file."""


class RateLimited(LibraryError):
    """The TPTP server refused a request for coming too fast.

    ``retry_after`` is the number of seconds the server asked us to wait, if it said.
    """

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


# ---------------------------------------------------------------------------------
# Names and paths
# ---------------------------------------------------------------------------------


def problem_path(name: str) -> str:
    """The library path of a problem: ``'SET001-1'`` -> ``'Problems/SET/SET001-1.p'``."""
    stem = name[:-2] if name.endswith(".p") else name
    if not _NAME_RE.fullmatch(stem) or ".." in stem:
        raise ValueError(
            f"{name!r} is not a TPTP problem name (expected something like 'SET001-1' or 'SEU140+3')")
    return f"Problems/{stem[:3]}/{stem}.p"


def _clean(path: str) -> Optional[str]:
    """A library-relative path in normal form, or None if it points outside the library."""
    path = path.replace("\\", "/")
    if "\x00" in path or path.startswith("/") or re.match(r"[A-Za-z]:", path):
        return None
    norm = posixpath.normpath(path)
    if norm == "." or norm == ".." or norm.startswith("../"):
        return None
    return norm


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------------
# Libraries
# ---------------------------------------------------------------------------------


class Library:
    """A TPTP library. Paths are relative to its root, with forward slashes."""

    #: Shown in error messages and set as the ``filename`` of what is parsed.
    label = "TPTP"

    def read_optional(self, path: str) -> Optional[str]:
        """The text of the file at ``path``, or ``None`` if the library has no such file."""
        raise NotImplementedError

    def read(self, path: str) -> str:
        """The text of the file at ``path``; raises :class:`ProblemNotFound` if absent."""
        clean = _clean(path)
        text = None if clean is None else self.read_optional(clean)
        if text is None:
            raise ProblemNotFound(f"{path!r} is not in {self.label}")
        return text

    def problem_text(self, name: str) -> str:
        """The source of problem ``name``, e.g. ``'SET001-1'``."""
        try:
            return self.read(problem_path(name))
        except ProblemNotFound:
            raise ProblemNotFound(f"problem {name!r} is not in {self.label}") from None

    def problem(self, name: str, *, expand_includes: bool = False) -> TPTPFile:
        """Parse problem ``name``.

        With ``expand_includes``, each ``include`` is replaced by the formulae it selects,
        as in :func:`thousandproblems.parse_file`. Include paths are read relative to the
        library root, then relative to the including file.
        """
        from . import parse

        path = problem_path(name)
        try:
            text = self.read(path)
        except ProblemNotFound:
            raise ProblemNotFound(f"problem {name!r} is not in {self.label}") from None
        parsed = parse(text, path)
        return self._expand(path, text, parsed, ()) if expand_includes else parsed

    def problem_names(self, domain: Optional[str] = None) -> list[str]:
        """The names of the problems in the library, sorted; optionally only one domain."""
        raise NotImplementedError(f"{type(self).__name__} cannot list its problems")

    def close(self) -> None:
        """Release any open files."""

    def __enter__(self) -> "Library":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.label}>"

    def _expand(self, path: str, text: str, parsed: TPTPFile, stack: tuple[str, ...]) -> TPTPFile:
        from . import parse

        inputs = []
        for item in parsed.inputs:
            if isinstance(item, AnnotatedFormula):
                inputs.append(item)
                continue

            def fail(message: str, item=item) -> ParseError:
                return _error_at(message, text, item.line, 1, path)

            candidates = []
            for candidate in (item.path, posixpath.join(posixpath.dirname(path), item.path)):
                clean = _clean(candidate)
                if clean is not None and clean not in candidates:
                    candidates.append(clean)
            target = body = None
            for candidate in candidates:
                body = self.read_optional(candidate)
                if body is not None:
                    target = candidate
                    break
            if target is None:
                tried = ", ".join(candidates) or item.path
                raise fail(f"included file {item.path!r} not found (tried {tried})")
            if target == path or target in stack:
                raise fail(f"circular include of {item.path!r}")
            child = parse(body, target)
            formulae = self._expand(target, body, child, (*stack, path)).formulae
            if isinstance(item.selection, tuple):
                selected = [f for f in formulae if f.name in item.selection]
                missing = set(item.selection) - {f.name for f in selected}
                if missing:
                    raise fail(f"{item.path!r} has no formulae named {sorted(missing)}")
                formulae = tuple(selected)
            inputs.extend(formulae)
        return TPTPFile(tuple(inputs), parsed.comments, parsed.filename)


class DirectoryLibrary(Library):
    """An unpacked TPTP distribution, e.g. ``TPTP-v9.0.0/`` (the folder that holds ``Problems``)."""

    def __init__(self, root: Union[str, os.PathLike]):
        self.root = Path(root)
        if not (self.root / "Problems").is_dir():
            raise LibraryError(
                f"{str(self.root)!r} does not look like a TPTP library: it has no 'Problems' folder. "
                "Point at the folder that contains Problems/ and Axioms/.")
        self.label = str(self.root)

    def read_optional(self, path: str) -> Optional[str]:
        clean = _clean(path)
        if clean is None:
            return None
        file = self.root / clean
        try:
            return file.read_text(encoding="utf-8", errors="replace") if file.is_file() else None
        except OSError:
            return None

    def problem(self, name: str, *, expand_includes: bool = False) -> TPTPFile:
        from . import parse_file

        file = self.root / problem_path(name)
        if not file.is_file():
            raise ProblemNotFound(f"problem {name!r} is not in {self.label}")
        return parse_file(file, expand_includes=expand_includes, tptp_root=self.root)

    def problem_names(self, domain: Optional[str] = None) -> list[str]:
        pattern = f"{domain}/*.p" if domain else "*/*.p"
        return sorted(p.stem for p in (self.root / "Problems").glob(pattern))


class TarballLibrary(Library):
    """A TPTP distribution tarball (``.tar``, ``.tgz``, ``.tar.gz``, ``.tar.bz2``, ``.tar.xz``).

    The archive may have a top-level folder such as ``TPTP-v9.0.0/``, or none.

    The first lookup reads the whole archive to index it, which takes a while for the
    full distribution when it is compressed; after that each lookup seeks to its file,
    which in a compressed archive means decompressing up to that point again. For
    heavy use, unpack the tarball and use :class:`DirectoryLibrary`.
    """

    def __init__(self, path: Union[str, os.PathLike]):
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"no such tarball: {str(self.path)!r}")
        self.label = str(self.path)
        self._tar: Optional[tarfile.TarFile] = None
        self._index: Optional[dict[str, tarfile.TarInfo]] = None

    def _members(self) -> dict[str, tarfile.TarInfo]:
        if self._index is not None:
            return self._index
        try:
            self._tar = tarfile.open(self.path, "r:*")
            files = {}
            for member in self._tar:
                if member.isfile():
                    files[member.name[2:] if member.name.startswith("./") else member.name] = member
        except (tarfile.TarError, OSError, EOFError) as e:
            raise LibraryError(f"cannot read {self.label}: {e}") from e
        prefix = None
        for name in files:
            parts = name.split("/")
            if "Problems" in parts[:-1]:
                prefix = "/".join(parts[:parts.index("Problems")])
                break
        if prefix is None:
            raise LibraryError(f"{self.label} has no 'Problems' folder; is it a TPTP distribution?")
        cut = len(prefix) + 1 if prefix else 0
        self._index = {name[cut:]: m for name, m in files.items() if name.startswith(prefix + "/" if prefix else "")}
        return self._index

    def read_optional(self, path: str) -> Optional[str]:
        member = self._members().get(path)
        if member is None:
            return None
        handle = self._tar.extractfile(member)
        if handle is None:
            return None
        with handle:
            return _decode(handle.read())

    def problem_names(self, domain: Optional[str] = None) -> list[str]:
        names = []
        for path in self._members():
            parts = path.split("/")
            if len(parts) == 3 and parts[0] == "Problems" and parts[2].endswith(".p"):
                if domain is None or parts[1] == domain:
                    names.append(parts[2][:-2])
        return sorted(names)

    def close(self) -> None:
        if self._tar is not None:
            self._tar.close()
        self._tar = self._index = None


def default_cache_dir() -> Path:
    """Where downloads are kept unless told otherwise."""
    if os.environ.get("THOUSANDPROBLEMS_CACHE"):
        return Path(os.environ["THOUSANDPROBLEMS_CACHE"])
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "thousandproblems"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "thousandproblems" / "Cache"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "thousandproblems"


class OnlineLibrary(Library):
    """The current TPTP release, fetched over HTTP from the TPTP website.

    :param base_url: the server; the default is the TPTP project's own.
    :param cache: ``True`` (default) to keep downloads in :func:`default_cache_dir`,
        ``False`` to keep them in memory only, or a directory to use as the cache.
    :param timeout: seconds to wait for the server.
    """

    def __init__(self, base_url: str = DEFAULT_URL, *, cache: Union[bool, str, os.PathLike] = True,
                 timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.label = self.base_url
        if cache is True:
            cache = default_cache_dir()
        host = re.sub(r"[^A-Za-z0-9._-]+", "_", urllib.parse.urlsplit(self.base_url).netloc or "server")
        self.cache_dir: Optional[Path] = Path(cache) / host if cache else None
        self._memory: dict[str, str] = {}

    # The server hands out a .tgz holding the requested file and, for a problem, every
    # axiom file it includes, laid out from the library root.
    def _download_url(self, path: str) -> str:
        return f"{self.base_url}/download/TPTPWorld/TPTP/{urllib.parse.quote(path, safe='/+')}"

    def _fetch(self, path: str) -> Optional[dict[str, str]]:
        from . import __version__

        request = urllib.request.Request(
            self._download_url(path), headers={"User-Agent": f"thousandproblems/{__version__}"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = response.read()
        except urllib.error.HTTPError as e:
            e.close()
            if e.code == 429:
                retry = e.headers.get("Retry-After")
                wait = float(retry) if retry and retry.replace(".", "", 1).isdigit() else None
                hint = f"; wait {wait:g} s" if wait else "; the TPTP server allows only a few requests a minute"
                raise RateLimited(f"the TPTP server is rate limiting requests{hint}", wait) from None
            if e.code == 404:
                return None
            raise LibraryError(f"{self._download_url(path)}: HTTP {e.code} {e.reason}") from e
        except (urllib.error.URLError, OSError) as e:
            raise LibraryError(f"cannot reach {self.base_url}: {getattr(e, 'reason', e)}") from e
        if data[:2] != b"\x1f\x8b":       # a missing file comes back as an HTML page, status 200
            return None
        files = {}
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                for member in tar:
                    if not member.isfile():
                        continue
                    name = member.name[2:] if member.name.startswith("./") else member.name
                    # the requested file is stored under its bare name
                    key = path if "/" not in name and name == posixpath.basename(path) else name
                    clean = _clean(key)
                    if clean is not None:
                        files[clean] = _decode(tar.extractfile(member).read())
        except (tarfile.TarError, EOFError) as e:
            raise LibraryError(f"unreadable download for {path!r}: {e}") from e
        return files if path in files else None

    def _cache_file(self, path: str) -> Optional[Path]:
        return self.cache_dir / path if self.cache_dir else None

    def read_optional(self, path: str) -> Optional[str]:
        if path in self._memory:
            return self._memory[path]
        cached = self._cache_file(path)
        if cached is not None:
            try:
                text = _decode(cached.read_bytes())
                self._memory[path] = text
                return text
            except OSError:
                pass
        files = self._fetch(path)
        if files is None:
            return None
        for name, text in files.items():
            self._memory[name] = text
            target = self._cache_file(name)
            if target is not None:
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
                    temporary.write_bytes(text.encode("utf-8"))      # bytes: no newline translation on Windows
                    os.replace(temporary, target)
                except OSError:
                    pass        # a cache that can't be written is not an error
        return files[path]

    def problem_names(self, domain: Optional[str] = None) -> list[str]:
        raise NotImplementedError("the TPTP website cannot be listed; use a local copy of the library")


# ---------------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------------


def open_library(source: Union[None, str, os.PathLike, Library] = None) -> Library:
    """A :class:`Library` for ``source``.

    ``source`` can be a folder, a tarball, an ``http(s)://`` URL of a TPTP server, an
    existing :class:`Library` (returned as is), or ``None`` for the folder named by the
    ``TPTP`` environment variable if that is set, else the TPTP website.
    """
    if isinstance(source, Library):
        return source
    if source is None:
        root = os.environ.get("TPTP")
        return DirectoryLibrary(root) if root and Path(root).is_dir() else OnlineLibrary()
    text = os.fspath(source)
    if re.match(r"https?://", text):
        return OnlineLibrary(text)
    path = Path(text)
    if path.is_dir():
        return DirectoryLibrary(path)
    if path.is_file():
        return TarballLibrary(path)
    raise FileNotFoundError(f"no such TPTP library (folder or tarball): {text!r}")


def fetch_problem(name: str, source: Union[None, str, os.PathLike, Library] = None, *,
                  expand_includes: bool = False) -> TPTPFile:
    """Parse problem ``name`` (e.g. ``'SET001-1'``) from a TPTP library; see :func:`open_library`.

    A library opened here is closed again before returning.
    """
    library = open_library(source)
    try:
        return library.problem(name, expand_includes=expand_includes)
    finally:
        if library is not source:
            library.close()
