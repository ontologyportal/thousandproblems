"""Download the whole TPTP library from tptp.org.

    >>> from thousandproblems.download import download_library
    >>> root = download_library("/data")            # doctest: +SKIP
    >>> root                                         # doctest: +SKIP
    PosixPath('/data/TPTP-v10.0.0')

The current release is at ``https://tptp.org/TPTP/Distribution/TPTP-v<version>.tgz`` and
older ones are under ``https://tptp.org/TPTP/Archive/``. The tarball is close to 1 GB, and
unpacked the library takes several GB.
"""

from __future__ import annotations

import os
import re
import tarfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional, Union

from .library import LibraryError

__all__ = ["DISTRIBUTION_URL", "ARCHIVE_URL", "latest_version", "release_url", "download_library", "save_files"]

DISTRIBUTION_URL = "https://tptp.org/TPTP/Distribution/"
ARCHIVE_URL = "https://tptp.org/TPTP/Archive/"

_TARBALL_RE = re.compile(r"TPTP-v(\d+\.\d+\.\d+)\.tgz")


def _user_agent() -> dict[str, str]:
    from . import __version__
    return {"User-Agent": f"thousandproblems/{__version__}"}


def _get(url: str, timeout: float = 60.0):
    try:
        return urllib.request.urlopen(urllib.request.Request(url, headers=_user_agent()), timeout=timeout)
    except urllib.error.HTTPError as e:
        e.close()
        raise LibraryError(f"{url}: HTTP {e.code} {e.reason}") from e
    except (urllib.error.URLError, OSError) as e:
        raise LibraryError(f"cannot reach {url}: {getattr(e, 'reason', e)}") from e


def _versions(listing_url: str) -> list[str]:
    with _get(listing_url) as response:
        page = response.read().decode("utf-8", errors="replace")
    found = set(_TARBALL_RE.findall(page))
    return sorted(found, key=lambda v: tuple(int(x) for x in v.split(".")))


def latest_version(base_url: str = DISTRIBUTION_URL) -> str:
    """The version of the current release, e.g. ``'10.0.0'``, read from the download page."""
    versions = _versions(base_url)
    if not versions:
        raise LibraryError(f"no TPTP-v*.tgz found at {base_url}")
    return versions[-1]


def release_url(version: Optional[str] = None, *, distribution_url: str = DISTRIBUTION_URL,
                archive_url: str = ARCHIVE_URL) -> str:
    """The tarball URL for ``version`` (``'9.0.0'`` or ``'v9.0.0'``), or the latest release."""
    if version in (None, "", "latest"):
        return f"{distribution_url}TPTP-v{latest_version(distribution_url)}.tgz"
    version = version.lstrip("vV")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"{version!r} is not a TPTP version such as 9.0.0")
    if version in _versions(distribution_url):
        return f"{distribution_url}TPTP-v{version}.tgz"
    return f"{archive_url}TPTP-v{version}.tgz"


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    if hasattr(tarfile, "data_filter"):                  # Python 3.11.4+
        tar.extractall(dest, filter="data")
        return
    root = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if not (member.isfile() or member.isdir()) or not target.is_relative_to(root):
            continue
        tar.extract(member, dest)


def download_library(dest: Union[str, os.PathLike] = ".", version: Optional[str] = None, *,
                     extract: bool = True, keep_tarball: bool = False, url: Optional[str] = None,
                     progress: Optional[Callable[[int, Optional[int]], None]] = None) -> Path:
    """Download a TPTP release into the folder ``dest``.

    Returns the unpacked library folder (e.g. ``dest/TPTP-v10.0.0``), or the tarball with
    ``extract=False``. ``progress(bytes_done, bytes_total)`` is called while downloading.
    ``url`` overrides where the tarball comes from.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    url = url or release_url(version)
    tarball = dest / url.rsplit("/", 1)[-1]
    partial = tarball.with_name(tarball.name + ".part")
    with _get(url, timeout=120.0) as response:
        total = response.headers.get("Content-Length")
        total = int(total) if total and total.isdigit() else None
        done = 0
        with open(partial, "wb") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    if total is not None and done != total:
        raise LibraryError(f"download of {url} stopped after {done} of {total} bytes")
    os.replace(partial, tarball)
    if not extract:
        return tarball
    try:
        with tarfile.open(tarball, "r:*") as tar:
            names = (m.name[2:] if m.name.startswith("./") else m.name for m in tar.getmembers())
            tops = {n.split("/")[0] for n in names if n not in ("", ".")}
            _safe_extract(tar, dest)
    except (tarfile.TarError, EOFError) as e:
        raise LibraryError(f"{tarball} is not a readable tarball: {e}") from e
    if not keep_tarball:
        tarball.unlink()
    if len(tops) == 1:
        return dest / tops.pop()
    return dest


def save_files(files: dict[str, str], dest: Union[str, os.PathLike], *, overwrite: bool = True) -> list[Path]:
    """Write ``{library path: text}`` (from :meth:`Library.problem_files`) under ``dest``,
    keeping the standard layout. Returns the files written."""
    from .library import _clean

    root = Path(dest)
    written = []
    for rel, text in files.items():
        clean = _clean(rel)
        if clean is None:
            continue
        target = root / clean
        if target.exists() and not overwrite:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
        written.append(target)
    return written
