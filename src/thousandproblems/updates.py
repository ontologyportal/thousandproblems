"""Checking for new TPTP releases, and keeping the online cache in step with them.

The TPTP website serves only the current release, so files cached by
:class:`~thousandproblems.library.OnlineLibrary` go out of date when a new release comes
out. :func:`check_for_updates` asks tptp.org for the latest version, clears the online cache
if it holds an older one, and says whether a local library is behind.

The time of the last check is kept in ``state.json`` in the cache directory, and the
command line reminds you when it is more than :data:`CHECK_INTERVAL` old. Set
``THOUSANDPROBLEMS_NO_UPDATE_CHECK=1`` to turn the reminder off.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from .library import DEFAULT_URL, default_cache_dir

__all__ = ["CHECK_INTERVAL", "UpdateReport", "check_for_updates", "check_due", "snooze",
           "postpone", "detect_version", "online_cache_version"]

#: How long after a check the command line starts reminding you, in seconds.
CHECK_INTERVAL = 30 * 24 * 3600
#: How long answering "no" to the reminder keeps it quiet, in seconds.
POSTPONE = 24 * 3600

_VERSION_RE = re.compile(r"v(\d+\.\d+\.\d+)")
_FILE_LINE_RE = re.compile(r"^% File\s*:.*?TPTP v(\d+\.\d+\.\d+)", re.MULTILINE)


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in version.lstrip("v").split("."))


# ---------------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------------


def _state_file(cache: Optional[Path] = None) -> Path:
    return (cache or default_cache_dir()) / "state.json"


def load_state(cache: Optional[Path] = None) -> dict:
    try:
        data = json.loads(_state_file(cache).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict, cache: Optional[Path] = None) -> None:
    path = _state_file(cache)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass                # an unwritable cache only means being reminded again


def check_due(now: Optional[float] = None, cache: Optional[Path] = None) -> Optional[int]:
    """Days since the last check if a reminder is due, else ``None``.

    The clock starts the first time this is called, so a new installation is not
    reminded straight away.
    """
    if os.environ.get("THOUSANDPROBLEMS_NO_UPDATE_CHECK"):
        return None
    now = time.time() if now is None else now
    state = load_state(cache)
    if "last_check" not in state:
        state["last_check"] = now
        save_state(state, cache)
        return None
    if now < state.get("quiet_until", 0):
        return None
    age = now - state["last_check"]
    return int(age // 86400) if age >= CHECK_INTERVAL else None


def snooze(now: Optional[float] = None, cache: Optional[Path] = None, seconds: float = CHECK_INTERVAL) -> None:
    """Don't remind again for ``seconds`` (default: 30 days)."""
    now = time.time() if now is None else now
    state = load_state(cache)
    state["quiet_until"] = now + seconds
    save_state(state, cache)


def postpone(now: Optional[float] = None, cache: Optional[Path] = None) -> None:
    """Don't remind again for a day (the answer "no")."""
    snooze(now, cache, POSTPONE)


# ---------------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------------


def detect_version(source: Union[str, os.PathLike]) -> Optional[str]:
    """The TPTP version of a local library, e.g. ``'10.0.0'``, from its folder or tarball
    name (``TPTP-v10.0.0``) or else from the ``% File`` line of one of its problems."""
    path = Path(source)
    match = _VERSION_RE.search(path.name)
    if match:
        return match[1]
    if path.is_dir():
        for problem in sorted((path / "Problems").glob("*/*.p"))[:5]:
            try:
                with open(problem, encoding="utf-8", errors="replace") as handle:
                    found = _FILE_LINE_RE.search(handle.read(4096))
            except OSError:
                continue
            if found:
                return found[1]
    return None


def _host_dir(cache: Path, base_url: str = DEFAULT_URL) -> Path:
    import urllib.parse
    host = re.sub(r"[^A-Za-z0-9._-]+", "_", urllib.parse.urlsplit(base_url).netloc or "server")
    return cache / host


def online_cache_version(cache: Optional[Path] = None, base_url: str = DEFAULT_URL) -> Optional[str]:
    """The TPTP version the online cache was filled from, or ``None`` if it is empty or unknown.

    It is recorded in a ``VERSION`` file; a cache without one (from before versions were
    recorded) is judged by the ``% File`` line of a cached problem.
    """
    folder = _host_dir(cache or default_cache_dir(), base_url)
    try:
        recorded = (folder / "VERSION").read_text(encoding="utf-8").strip()
        if recorded:
            return recorded
    except OSError:
        pass
    for problem in sorted((folder / "Problems").glob("*/*.p"))[:5]:
        try:
            found = _FILE_LINE_RE.search(problem.read_text(encoding="utf-8", errors="replace")[:4096])
        except OSError:
            continue
        if found:
            return found[1]
    return None


@dataclass
class UpdateReport:
    """What :func:`check_for_updates` found."""

    latest: str
    cache_version: Optional[str] = None
    cache_cleared: int = 0                   # files removed from the online cache
    local_path: Optional[str] = None
    local_version: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    @property
    def local_outdated(self) -> bool:
        return bool(self.local_version) and _key(self.local_version) < _key(self.latest)

    def summary(self) -> str:
        lines = [f"latest TPTP release : v{self.latest}"]
        if self.cache_cleared:
            lines.append(f"online cache        : held v{self.cache_version or '?'}; cleared "
                         f"{self.cache_cleared} files, so problems are fetched again from v{self.latest}")
        else:
            lines.append(f"online cache        : {'v' + self.cache_version if self.cache_version else 'empty'}"
                         + (" (up to date)" if self.cache_version == self.latest else ""))
        if self.local_path:
            if self.local_version is None:
                lines.append(f"local library       : {self.local_path} (version unknown)")
            elif self.local_outdated:
                lines.append(f"local library       : {self.local_path} is v{self.local_version}; "
                             f"get v{self.latest} with 'thousandproblems download'")
            else:
                lines.append(f"local library       : {self.local_path} is v{self.local_version} (up to date)")
        lines += self.notes
        return "\n".join(lines)


def check_for_updates(local: Union[None, str, os.PathLike] = None, *, cache: Optional[Path] = None,
                      base_url: str = DEFAULT_URL, distribution_url: Optional[str] = None,
                      now: Optional[float] = None) -> UpdateReport:
    """Look up the latest TPTP release and bring the online cache in line with it.

    Cached downloads from an older release (or of unknown release) are deleted, since the
    website now serves the new one. ``local`` is a local library to compare. The check is
    recorded, which resets the reminder. Raises
    :class:`~thousandproblems.library.LibraryError` if tptp.org cannot be reached.
    """
    from .download import DISTRIBUTION_URL, latest_version

    cache = cache or default_cache_dir()
    latest = latest_version(distribution_url or DISTRIBUTION_URL)
    report = UpdateReport(latest)

    folder = _host_dir(cache, base_url)
    report.cache_version = online_cache_version(cache, base_url)
    cached = [p for p in folder.rglob("*") if p.is_file() and p.name != "VERSION"] if folder.is_dir() else []
    if cached and report.cache_version != latest:
        for sub in ("Problems", "Axioms"):
            shutil.rmtree(folder / sub, ignore_errors=True)
        report.cache_cleared = sum(1 for p in cached if not p.exists())
        leftover = [p for p in cached if p.exists()]
        if leftover:
            report.notes.append(f"note: {len(leftover)} other files in {folder} were left alone")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "VERSION").write_text(latest + "\n", encoding="utf-8")
    except OSError:
        pass

    if local is not None:
        report.local_path = str(local)
        report.local_version = detect_version(local)

    state = load_state(cache)
    state.update(last_check=time.time() if now is None else now, latest_version=latest)
    state.pop("quiet_until", None)
    save_state(state, cache)
    return report
