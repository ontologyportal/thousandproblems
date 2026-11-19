"""The ``thousandproblems`` command (also ``python -m thousandproblems``).

Subcommands::

    fetch      download problems (and the axioms they include), or print them
    download   download and unpack the whole TPTP library
    validate   check that a folder or tarball is a usable TPTP library
    discover   find TPTP libraries on this machine and set $TPTP
    find       locate problems or axiom files in a local library
    stats      print statistics about a problem: dialect, symbols, variables ...
    search     find problems by their properties, like TPTP2T on tptp.org
    run        run problems (by name, or found by a search) against theorem provers
    install    download (or build) a theorem prover for use with 'run'
    refresh    check for a new TPTP release and clear outdated cached downloads

Commands that need a library take ``-L/--library`` (a folder, a tarball or a server URL).
Without it the folder named by ``$TPTP`` is used, and, for ``fetch`` and ``stats``, the TPTP
website if ``$TPTP`` is not set.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import __version__, parse_file
from .library import (DirectoryLibrary, Library, LibraryError, OnlineLibrary, ProblemNotFound, RateLimited,
                      TarballLibrary, open_library, problem_path)
from .nodes import ParseError

_GLOB = set("*?[")


class CommandError(Exception):
    """A failure to report as ``thousandproblems: error: ...`` with exit status 1."""


# ---------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------


def _open(source: Optional[str], *, local: bool) -> Library:
    if source is None and local and not os.environ.get("TPTP"):
        raise CommandError("no TPTP library given: use -L/--library PATH or set $TPTP "
                           "(find one with 'thousandproblems discover', or get one with 'thousandproblems download')")
    try:
        library = open_library(source)
    except (FileNotFoundError, LibraryError) as e:
        raise CommandError(str(e)) from None
    if local and isinstance(library, OnlineLibrary):
        raise CommandError("this command needs a local copy of the TPTP library (a folder or tarball), "
                           "not the website; get one with 'thousandproblems download'")
    return library


def _progress(label: str, enabled: bool):
    if not enabled or not sys.stderr.isatty():
        return None
    state = {"last": -1}

    def report(done: int, total: Optional[int]) -> None:
        mark = done * 100 // total if total else done // 1000
        if mark != state["last"]:
            state["last"] = mark
            text = f"{done}/{total} ({mark}%)" if total else f"{done}"
            sys.stderr.write(f"\r{label}: {text}   ")
            sys.stderr.flush()
            if total and done >= total:
                sys.stderr.write("\n")
    return report


def _bytes_progress(enabled: bool):
    if not enabled or not sys.stderr.isatty():
        return None
    state = {"last": -1}

    def report(done: int, total: Optional[int]) -> None:
        mb = done >> 20
        if mb != state["last"]:
            state["last"] = mb
            text = f"{mb} / {total >> 20} MB ({done * 100 // total}%)" if total else f"{mb} MB"
            sys.stderr.write(f"\rdownloading: {text}   ")
            sys.stderr.flush()
    return report


def _expand_names(library: Library, names: Sequence[str]) -> list[str]:
    """Problem names, with ``-`` read from stdin and wildcards matched against the library."""
    result: list[str] = []
    for name in names:
        if name == "-":
            result.extend(_expand_names(library, [line.split()[0] for line in sys.stdin if line.strip()]))
            continue
        name = name[:-2] if name.endswith(".p") else name
        if _GLOB & set(name):
            try:
                domain = name[:3] if len(name) >= 3 and not (_GLOB & set(name[:3])) else None
                matches = [n for n in library.problem_names(domain) if fnmatch.fnmatchcase(n, name)]
            except NotImplementedError:
                raise CommandError(f"wildcards ({name!r}) need a local library; the website cannot be listed") from None
            if not matches:
                raise CommandError(f"no problem matches {name!r} in {library.label}")
            result.extend(matches)
        else:
            result.append(name)
    return list(dict.fromkeys(result))


def _location(library: Library, rel: str) -> str:
    if isinstance(library, DirectoryLibrary):
        return str(library.root / rel)
    if isinstance(library, TarballLibrary):
        return f"{library.path}:{rel}"
    return f"{library.label}/{rel}"


# ---------------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------------


def cmd_fetch(args) -> int:
    from .download import save_files

    library = _open(args.library, local=False)
    with library:
        names = _expand_names(library, args.names)
        if not names:
            raise CommandError("no problem names given")
        for name in names:
            try:
                if args.output is None:
                    if args.expand:
                        parsed = library.problem(name, expand_includes=True)
                        sys.stdout.write("\n".join(str(f) for f in parsed.formulae) + "\n")
                    else:
                        sys.stdout.write(library.problem_text(name))
                    continue
                files = library.problem_files(name) if not args.no_axioms else \
                    {problem_path(name): library.problem_text(name)}
                written = save_files(files, args.output, overwrite=not args.keep_existing)
                if not args.quiet:
                    for path in written:
                        print(path)
            except ValueError as e:      # bad name (ParseError is a ValueError too)
                raise CommandError(str(e)) from None
    return 0


# ---------------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------------


def cmd_download(args) -> int:
    from . import download
    from .validate import validate_library

    if args.list:
        current = download._versions(download.DISTRIBUTION_URL)
        archived = download._versions(download.ARCHIVE_URL)
        for v in sorted(set(current) | set(archived), key=lambda v: tuple(map(int, v.split(".")))):
            print(f"{v}{'  (current)' if v in current else ''}")
        return 0
    url = args.url or download.release_url(args.version)
    if not args.quiet:
        print(f"downloading {url} into {Path(args.output).resolve()}", file=sys.stderr)
    result = download.download_library(args.output, url=url, extract=not args.no_extract,
                                       keep_tarball=args.keep_tarball,
                                       progress=_bytes_progress(not args.quiet))
    if not args.quiet and sys.stderr.isatty():
        sys.stderr.write("\n")
    if args.no_extract:
        print(result)
        return 0
    report = validate_library(result)
    if not args.version and not args.url:        # the latest release: as good as a check
        from . import updates
        import time
        state = updates.load_state()
        state.update(last_check=time.time(), latest_version=(report.version or "").lstrip("v") or None)
        state.pop("quiet_until", None)
        updates.save_state(state)
    if not args.quiet:
        print(report.summary(), file=sys.stderr)
    print(result)
    if report.ok and not args.quiet:
        print(f"\nTo use it by default:\n    export TPTP={result.resolve()}", file=sys.stderr)
    return 0 if report.ok else 1


# ---------------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------------


def cmd_validate(args) -> int:
    from .validate import validate_library

    source = args.path or args.library or os.environ.get("TPTP")
    if not source:
        raise CommandError("give the library folder or tarball to check (or set $TPTP)")
    report = validate_library(source, check_includes=args.includes, parse=args.parse,
                              progress=_progress("checking", not args.json))
    if args.json:
        print(json.dumps({"source": report.source, "ok": report.ok, "version": report.version,
                          "domains": report.domains, "problems": report.problems, "axioms": report.axioms,
                          "errors": report.errors, "warnings": report.warnings}, indent=2))
    else:
        print(report.summary())
    failed = not report.ok or (args.strict and report.warnings)
    return 1 if failed else 0


# ---------------------------------------------------------------------------------
# find
# ---------------------------------------------------------------------------------


def cmd_find(args) -> int:
    library = _open(args.library, local=True)
    missing = 0

    def exists(rel: str) -> bool:
        if isinstance(library, DirectoryLibrary):
            return (library.root / rel).is_file()
        return library.read_optional(rel) is not None

    def show(rel: str) -> None:
        print(rel if args.relative else _location(library, rel))

    with library:
        for name in args.names:
            if name.endswith(".ax"):                         # an axiom file, SET001-0.ax
                if exists(f"Axioms/{name}"):
                    show(f"Axioms/{name}")
                    continue
                print(f"thousandproblems: Axioms/{name} is not in {library.label}", file=sys.stderr)
                missing += 1
                continue
            if _GLOB & set(name):
                try:
                    for problem in _expand_names(library, [name]):
                        show(problem_path(problem))
                except CommandError as e:
                    print(f"thousandproblems: {e}", file=sys.stderr)
                    missing += 1
                continue
            try:
                rel = problem_path(name)
            except ValueError as e:
                print(f"thousandproblems: {e}", file=sys.stderr)
                missing += 1
                continue
            if exists(rel):
                show(rel)
            elif exists(f"Axioms/{name.removesuffix('.p')}.ax"):    # SET001-0 names an axiom file
                show(f"Axioms/{name.removesuffix('.p')}.ax")
            else:
                print(f"thousandproblems: {name} is not in {library.label}", file=sys.stderr)
                missing += 1
    return 1 if missing else 0


# ---------------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------------


def _load_for_stats(args):
    target = args.target
    path = Path(target)
    if path.is_file():
        root = args.library or os.environ.get("TPTP")
        try:
            return parse_file(path, expand_includes=args.expand, tptp_root=root), str(path), None
        except ParseError as e:
            if not args.expand or "not found" not in e.message:
                raise
            print(f"thousandproblems: warning: {e.message}; statistics cover this file only "
                  "(give -L/--library or set $TPTP to resolve includes)", file=sys.stderr)
            return parse_file(path), str(path), None
    library = _open(args.library, local=False)
    with library:
        try:
            parsed = library.problem(target, expand_includes=args.expand)
        except ValueError as e:
            if isinstance(e, ParseError):
                raise
            raise CommandError(f"{target!r} is neither a file nor a TPTP problem name") from None
        return parsed, problem_path(target), library


def _fmt_details(stat) -> str:
    parts = []
    for key, value in stat.details.items():
        text = f"{value[0]}-{value[1]}" if isinstance(value, tuple) else f"{value}"
        parts.append(f"{text} {key}")
    return f"({'; '.join(parts)})" if parts else ""


def cmd_stats(args) -> int:
    from .stats import DOMAINS, SPC, compute_stats, syntax_stats

    parsed, where, library = _load_for_stats(args)
    header = parsed.header
    declared = syntax_stats(header)
    computed = compute_stats(parsed)
    spc = SPC(header.spc) if header and header.spc else None

    if args.json:
        print(json.dumps({
            "file": where,
            "problem": header.problem if header else None,
            "domain": (header.problem or "")[:3] if header and header.problem else None,
            "status": header.status if header else None,
            "rating": header.rating if header else None,
            "spc": str(spc) if spc else None,
            "form": spc.form if spc else None,
            "declared": {k: {"value": s.value, **{d: (list(v) if isinstance(v, tuple) else v)
                                                   for d, v in s.details.items()}}
                         for k, s in declared.items()},
            "computed": computed.as_dict(),
            "expanded": args.expand,
        }, indent=2))
        return 0

    rows: list[tuple[str, str]] = []
    name = header.problem if header and header.problem else Path(where).stem
    rows.append(("Problem", f"{name}  ({where})"))
    if header:
        domain = name[:3]
        if header.get("Domain") or domain in DOMAINS:
            rows.append(("Domain", f"{domain}  {header.get('Domain') or DOMAINS.get(domain, '')}".rstrip()))
        if header.get("Problem"):
            rows.append(("Title", header.get("Problem")))
        if header.status:
            rows.append(("Status", header.status))
        if header.ratings:
            rating, version = header.ratings[0]
            rows.append(("Rating", f"{rating:.2f}" + (f"  (v{version})" if version else "")))
        if spc:
            rows.append(("SPC", f"{spc}  ({spc.describe()})"))
    dialect = ", ".join(computed.dialects) or "-"
    if spc:
        dialect += f"  [{spc.form}]"
    if computed.polymorphic:
        dialect += "  polymorphic"
    rows.append(("Dialect", dialect))
    if computed.includes:
        rows.append(("Includes", ", ".join(computed.includes) + "  (not expanded)"))

    roles = ", ".join(f"{n} {r}" for r, n in sorted(computed.roles.items(), key=lambda x: -x[1]))
    rows.append(("Formulae", f"{computed.formulae}" + (f"  ({roles})" if roles else "")))
    if computed.unit_formulae is not None:
        rows.append(("Unit formulae", str(computed.unit_formulae)))
    if computed.type_declarations:
        rows.append(("Type decls", f"{computed.type_declarations}"
                     + (f"  (user types: {', '.join(computed.user_types)})" if computed.user_types else "")))
    if computed.atoms is not None:
        rows.append(("Atoms", f"{computed.atoms}  ({computed.equality_atoms} equality)"))
    elif computed.equality_atoms:
        rows.append(("Equalities", str(computed.equality_atoms)))

    def symbol_line(symbols: tuple[str, ...]) -> str:
        if not symbols:
            return "0"
        arities = sorted(int(s.rsplit("/", 1)[1]) for s in symbols)
        text = f"{len(symbols)}  (arity {arities[0]}-{arities[-1]})"
        if args.symbols:
            text += "\n" + _wrap(symbols)
        return text

    rows.append(("Symbols", symbol_line(computed.symbols)))
    if computed.atoms is not None:
        rows.append(("  predicates", symbol_line(computed.predicates)))
        rows.append(("  functions", symbol_line(computed.functions)))
        rows.append(("  constants", symbol_line(computed.constants)))
    if computed.defined_symbols:
        rows.append(("  interpreted", f"{len(computed.defined_symbols)}"
                     + (f"\n{_wrap(computed.defined_symbols)}" if args.symbols else
                        f"  ({', '.join(computed.defined_symbols[:8])}{' ...' if len(computed.defined_symbols) > 8 else ''})")))
    binders = ", ".join(f"{n} {q}" for q, n in computed.bound_variables.items())
    variables = f"{computed.variables}" + (f"  (bound: {binders})" if binders else "")
    if "cnf" in computed.dialects:
        variables += f"  ({computed.singleton_variables} singleton)"
    rows.append(("Variables", variables))
    if not declared:      # the header's figures, when present, are the authoritative ones
        rows.append(("Max depth", f"formula {computed.max_formula_depth}, term {computed.max_term_depth}"))

    width = max(len(k) for k, _ in rows) + 2
    for key, value in rows:
        lines = value.split("\n")
        print(f"{key:<{width}}: {lines[0]}")
        for more in lines[1:]:
            print(f"{'':<{width}}  {more}")
    if declared:
        print("\nDeclared in the header (% Syntax), covering the problem with its includes:")
        label_width = max(len(k) for k in declared) + 2
        for key, stat in declared.items():
            print(f"  {key.replace('_', ' '):<{label_width}}: {stat.value:>6}  {_fmt_details(stat)}")
    if computed.includes and not args.expand:
        print("\nNote: the computed figures cover this file only; its includes were not read.")
    return 0


def _wrap(items: Sequence[str], width: int = 88, indent: str = "    ") -> str:
    lines, line = [], indent
    for item in items:
        if len(line) + len(item) + 1 > width and line.strip():
            lines.append(line.rstrip())
            line = indent
        line += item + " "
    if line.strip():
        lines.append(line.rstrip())
    return "\n".join(lines)


# ---------------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------------


def _query(args):
    from .search import CRITERIA, Query

    query = Query()
    try:
        for name in CRITERIA:
            for value in getattr(args, name) or ():
                query.add(name, value)
    except ValueError as e:
        raise CommandError(str(e)) from None
    return query


def _search(args, query):
    """Search the local library; a SearchResult."""
    from .search import search

    if args.library is None and not os.environ.get("TPTP"):
        raise CommandError("no TPTP library given: use -L/--library PATH or set $TPTP "
                           "(find one with 'thousandproblems discover', or get one with 'thousandproblems download')")
    try:
        result = search(args.library, query=query, refresh=args.refresh,
                        progress=_progress("indexing problem headers", not args.quiet))
    except (FileNotFoundError, LibraryError) as e:
        raise CommandError(str(e)) from None
    if not args.quiet and sys.stderr.isatty():
        sys.stderr.write("\r" + " " * 60 + "\r")
    return result


def cmd_search(args) -> int:
    result = _search(args, _query(args))
    with result:
        hits = result.records
        shown = hits[:args.limit] if args.limit is not None else hits

        if args.count:
            print(len(hits))
            return 0 if hits else 1
        if args.format == "json":
            print(json.dumps([r.as_dict() for r in shown], indent=2))
        elif args.format == "paths":
            for r in shown:
                print(_location(result.library, r.path))
        elif args.format == "table":
            print(f"{'Problem':<18} {'Form':<4} {'Status':<4} {'Rating':>6} {'Formulae':>8} {'Atoms':>8} "
                  f"{'Symbols':>7} {'Vars':>6}  SPC")
            for r in shown:
                symbols = r.stat("symbols")
                if symbols is None and (r.stat("predicates") is not None or r.stat("functors") is not None):
                    symbols = (r.stat("predicates") or 0) + (r.stat("functors") or 0)
                cell = lambda v, w: f"{'-' if v is None else v:>{w}}"      # noqa: E731
                rating = "-" if r.rating is None else f"{r.rating:.2f}"
                print(f"{r.name:<18} {r.form or '-':<4} {r.status_code or '-':<4} {rating:>6} "
                      f"{cell(r.stat('formulae', 'clauses'), 8)} {cell(r.stat('atoms', 'literals'), 8)} "
                      f"{cell(symbols, 7)} {cell(r.stat('variables'), 6)}  {r.spc or ''}")
        else:
            for r in shown:
                print(r.name)
        if not args.quiet:
            more = f" (showing {len(shown)})" if len(shown) != len(hits) else ""
            print(f"{len(hits)} of {result.searched} problems match{more}", file=sys.stderr)
        return 0 if hits else 1


# ---------------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------------


def _install(prover, args, jobs: Optional[int] = None) -> str:
    from .provers import ProverError

    log = None if args.quiet else (lambda message: print(message, file=sys.stderr))
    try:
        return prover.install(force=getattr(args, "force", False), jobs=jobs, log=log)
    except NotImplementedError as e:
        raise CommandError(str(e)) from None
    except ProverError as e:
        raise CommandError(str(e)) from None


def cmd_install(args) -> int:
    from .install import Platform
    from .provers import PRESETS, ProverError

    try:
        platform = Platform.parse(args.platform) if args.platform else Platform.detect()
    except ValueError as e:
        raise CommandError(str(e)) from None
    if args.list or not args.prover:
        if not args.list:
            print("name a prover to install, e.g. 'thousandproblems install vampire'; the ones known:\n",
                  file=sys.stderr)
        print(f"platform: {platform}")
        for name, cls in PRESETS.items():
            prover = cls(platform=platform)
            how = ("builds from source" if cls.build_commands else "downloads a release") if cls.download_url \
                else "no installer"
            installed = ", ".join(prover.installed_versions()) or "-"
            found = shutil.which(cls.binary)
            print(f"  {name:<9} default {cls.default_version or '-':<8} {how:<21} installed: {installed}"
                  + (f"   on $PATH: {found}" if found else ""))
        return 0 if args.list else 1
    cls = PRESETS.get(args.prover)
    if cls is None:
        raise CommandError(f"don't know how to install {args.prover!r}; known: {', '.join(PRESETS)}")
    try:
        prover = cls(version=args.version, platform=platform)
    except ProverError as e:
        raise CommandError(str(e)) from None
    print(_install(prover, args, args.jobs))
    return 0


def _safe_filename(text: str) -> str:
    return "".join(c if c.isalnum() or c in "._+-^=#" else "_" for c in text)


def _save_outputs(report, folder: str) -> None:
    for r in report.results:
        target = Path(folder) / _safe_filename(r.prover)
        target.mkdir(parents=True, exist_ok=True)
        stem = _safe_filename(r.problem)
        (target / f"{stem}.out").write_text(r.stdout, encoding="utf-8")
        if r.stderr.strip():
            (target / f"{stem}.err").write_text(r.stderr, encoding="utf-8")


def cmd_run(args) -> int:
    from .provers import ProverCollection, ProverError
    from .runner import run
    from .search import CRITERIA

    if not args.prover:
        raise CommandError("give at least one prover with -p/--prover, e.g. -p eprover -p 'vampire --mode casc'")
    query = _query(args)
    if query and args.names:
        raise CommandError("give problem names or search criteria, not both "
                           "(a name pattern is a criterion too: --name 'SET00*')")
    if not query and not args.names:
        raise CommandError("no problems: give problem names or files, or search criteria (see --help)")
    try:
        provers = ProverCollection.from_specs(args.prover, input=args.input, platform=args.platform)
    except (ProverError, ValueError) as e:
        raise CommandError(str(e)) from None
    if args.install:
        for prover in provers.missing():
            _install(prover, args)
    missing = provers.missing()
    if missing:
        raise CommandError("prover not found: " + ", ".join(f"{p.name} (binary {p.binary!r})" for p in missing)
                           + "\n(install a known one with 'thousandproblems install NAME [--version X]', "
                             "or add --install)")

    searched = None
    if query:
        searched = _search(args, query)
        problems = searched[:args.limit] if args.limit is not None else searched
        library = None
    else:
        library = _open(args.library, local=False)
        names = []
        for name in args.names:
            names.extend([name] if Path(name).is_file() else _expand_names(library, [name]))
        problems = names[:args.limit] if args.limit is not None else names
    if not len(problems):
        raise CommandError("no problems match")

    def report_one(r, done, total) -> None:
        if args.quiet:
            return
        flag = "  ! contradicts the header" if r.correct is False else ""
        detail = f"  {r.error}" if r.error else ""
        print(f"[{done}/{total}] {r.problem}  {r.prover}  {r.status} {r.runtime:.2f}s{flag}{detail}", file=sys.stderr)

    try:
        report = run(problems, provers, timeout=args.timeout, jobs=args.jobs, library=library,
                     keep_output=bool(args.output_dir), on_result=report_one)
    finally:
        if searched is not None:
            searched.close()
        if library is not None:
            library.close()
    if args.output_dir:
        _save_outputs(report, args.output_dir)
    if args.format == "json":
        print(report.to_json(output=bool(args.output_dir)))
    elif args.format == "csv":
        sys.stdout.write(report.to_csv())
    else:
        print(report.table())
    return 0


# ---------------------------------------------------------------------------------
# refresh, and the reminder to run it
# ---------------------------------------------------------------------------------


def _local_source(args) -> Optional[str]:
    source = getattr(args, "library", None) or os.environ.get("TPTP")
    if source and not source.startswith(("http://", "https://")) and Path(source).exists():
        return source
    return None


def cmd_refresh(args) -> int:
    from .updates import check_for_updates

    local = _local_source(args)
    report = check_for_updates(local)
    if args.index and local:
        from .search import build_index
        records = build_index(local, refresh=True, progress=_progress("indexing problem headers", not args.json))
        if sys.stderr.isatty() and not args.json:
            sys.stderr.write("\r" + " " * 60 + "\r")
        report.notes.append(f"search index        : rebuilt ({len(records)} problems)")
    if args.json:
        print(json.dumps({"latest": report.latest, "cache_version": report.cache_version,
                          "cache_cleared": report.cache_cleared, "local_path": report.local_path,
                          "local_version": report.local_version, "local_outdated": report.local_outdated},
                         indent=2))
    else:
        print(report.summary())
    return 0


_REMINDER_SKIPPED = {"refresh", "download"}


def _remind(args) -> None:
    """Every 30 days, offer to check for a new TPTP release (or warn, without a terminal)."""
    from . import updates

    if args.command in _REMINDER_SKIPPED:
        return
    days = updates.check_due()
    if days is None:
        return
    message = f"You have not checked for a new TPTP library version in {days} days"
    interactive = sys.stdin is not None and sys.stdin.isatty() and sys.stderr.isatty()
    if not interactive:
        print(f"thousandproblems: {message.lower()[0]}{message[1:]}; run 'thousandproblems refresh' "
              "(or set THOUSANDPROBLEMS_NO_UPDATE_CHECK=1)", file=sys.stderr)
        updates.postpone()
        return
    while True:
        sys.stderr.write(f"{message}, check now? [y]es / [n]o / [s]nooze for thirty days: ")
        sys.stderr.flush()
        answer = sys.stdin.readline()
        if not answer:                       # end of input
            answer = "n"
        answer = answer.strip().lower()
        if answer in ("y", "yes"):
            try:
                report = updates.check_for_updates(_local_source(args))
            except LibraryError as e:
                print(f"thousandproblems: could not check: {e}", file=sys.stderr)
                updates.postpone()
            else:
                print(report.summary(), file=sys.stderr)
            return
        if answer in ("n", "no"):
            updates.postpone()               # ask again tomorrow, not on every command
            return
        if answer in ("s", "snooze"):
            updates.snooze()
            return


# ---------------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------------


def _interactive() -> bool:
    return sys.stdin is not None and sys.stdin.isatty() and sys.stderr.isatty()


def _ask(question: str, default: str = "n") -> str:
    """Ask on the terminal; the answer, lower-cased, or ``default`` on an empty line or end of input."""
    sys.stderr.write(question)
    sys.stderr.flush()
    answer = sys.stdin.readline()
    return (answer.strip().lower() or default) if answer else default


def cmd_discover(args) -> int:
    from . import discover

    shell = args.shell or discover.detect_shell()
    current = os.environ.get("TPTP")
    found = discover.find_libraries(args.dirs or None, depth=args.depth)
    usable = [f for f in found if f.valid and not f.packed]

    if args.json:
        chosen = usable[0] if usable else None
        print(json.dumps({
            "current": current,
            "found": [{"path": str(f.path), "version": f.version, "valid": f.valid, "packed": f.packed,
                       "problems": f.problems, "errors": f.errors} for f in found],
            "chosen": str(chosen.path) if chosen else None,
            "command": discover.export_command(chosen.path, shell) if chosen else None,
        }, indent=2))
        return 0 if usable else 1

    where = ", ".join(args.dirs) if args.dirs else "the usual places (here, your home folder, /opt ...)"
    if not found:
        print(f"No TPTP library found in {where}.")
        print("Search somewhere else with 'thousandproblems discover DIR', or get one with "
              "'thousandproblems download -o DIR'.")
        return 1

    print(f"TPTP libraries found in {where}:")
    for number, f in enumerate(found, 1):
        version = f"v{f.version}" if f.version else "v?"
        if f.packed:
            state = "tarball: unpack it to use it"
        elif f.valid:
            state = f"{f.problems} problems" + ("" if f.complete else ", partial: only Problems/ and Axioms/")
        else:
            state = f"INVALID: {f.errors[0] if f.errors else 'see thousandproblems validate'}"
        same = "  <- current $TPTP" if current and _same_path(current, f.path) else ""
        print(f"  {number:>2}. {version:<9} {f.path}  ({state}){same}")
    if not usable:
        print("\nNone of them can be used as $TPTP. 'thousandproblems validate PATH' explains what is wrong.")
        return 1

    chosen = usable[0]
    if len(usable) > 1 and _interactive() and not args.yes:
        numbers = {i: f for i, f in enumerate(found, 1) if f in usable}
        default = next(i for i, f in numbers.items() if f is chosen)
        while True:
            answer = _ask(f"Use which one? [{default}] ", str(default))
            if answer.isdigit() and int(answer) in numbers:
                chosen = numbers[int(answer)]
                break
            print(f"  choose one of {', '.join(map(str, numbers))}", file=sys.stderr)
    elif len(usable) > 1:
        print(f"\nUsing the first: {chosen.path}")

    already = current is not None and _same_path(current, chosen.path)
    command = discover.export_command(chosen.path, shell)
    if already:
        print(f"\n$TPTP is already set to it.")
    else:
        print(f"\nTo use it in this terminal, run:\n\n    {command}\n")

    if shell in ("powershell", "cmd"):
        return _set_windows(args, chosen.path)
    target = discover.startup_file(shell)
    text = target.read_text(encoding="utf-8") if target and target.exists() else ""
    if discover.MARK_BEGIN in text and command in text:
        print(f"{target} already sets it for new terminals.")
        return 0
    if not args.set:
        if not _interactive() or args.yes:
            print(f"Add that line to {target} to make it permanent, or run "
                  f"'thousandproblems discover --set'.")
            return 0
        if _ask(f"Add it to {target} so that new terminals use it? [y/N] ") not in ("y", "yes"):
            return 0
    elif not args.yes:
        if not _interactive():
            raise CommandError(f"--set would change {target}; add --yes to allow that without asking")
        if _ask(f"Add it to {target}? [y/N] ") not in ("y", "yes"):
            return 0
    discover.install(chosen.path, shell, target)
    replaced = discover.MARK_BEGIN in text
    print(f"{'Updated' if replaced else 'Added'} in {target}:\n\n"
          f"    {discover.MARK_BEGIN}\n    {command}\n    {discover.MARK_END}\n")
    for line in discover.other_settings(text):
        print(f"warning: {target} also has '{line}'; the block above comes later, so it wins")
    print(f"New terminals will use it. For this one, run: {command}"
          if not already else "New terminals will use it.")
    return 0


def _set_windows(args, path) -> int:
    import subprocess

    command = ["setx", "TPTP", str(path)]
    if not args.set:
        print(f"To make it permanent, run:  setx TPTP \"{path}\"   (or 'thousandproblems discover --set')")
        return 0
    if not args.yes:
        if not _interactive():
            raise CommandError("--set would change your user environment; add --yes to allow that without asking")
        if _ask("Set TPTP for your user account with setx? [y/N] ") not in ("y", "yes"):
            return 0
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise CommandError(f"setx failed: {(result.stderr or result.stdout).strip()}")
    print("Set for your user account. Programs started from now on will see it.")
    return 0


def _same_path(a, b) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


# ---------------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    from .runner import DEFAULT_TIMEOUT
    from .search import CRITERIA

    library_opt = argparse.ArgumentParser(add_help=False)
    library_opt.add_argument("-L", "--library", metavar="SOURCE",
                             help="TPTP library: a folder, a tarball, or a server URL (default: $TPTP)")

    parser = argparse.ArgumentParser(
        prog="thousandproblems",
        description="Fetch, find, check, describe and search TPTP problems.",
        epilog="Run 'thousandproblems COMMAND --help' for the options of each command.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    p = sub.add_parser("fetch", parents=[library_opt], help="download or print problems",
                       description="Download problems together with the axiom files they include, or print "
                                   "them. Without -L and $TPTP, problems come from the TPTP website, which "
                                   "allows only a few requests a minute (downloads are cached).")
    p.add_argument("names", nargs="+", metavar="NAME",
                   help="problem names such as SET001-1; wildcards (SET00*) need a local library; "
                        "'-' reads names from stdin, e.g. from 'search'")
    p.add_argument("-o", "--output", metavar="DIR",
                   help="save into DIR in the standard layout (Problems/..., Axioms/...); "
                        "without it the problem is printed")
    p.add_argument("--expand", action="store_true", help="when printing, replace includes by their formulae")
    p.add_argument("--no-axioms", action="store_true", help="with -o, save only the problem files")
    p.add_argument("--keep-existing", action="store_true", help="with -o, do not overwrite files")
    p.add_argument("-q", "--quiet", action="store_true", help="do not list the files written")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("download", help="download the whole TPTP library",
                       description="Download a TPTP release (about 1 GB) from tptp.org and unpack it.")
    p.add_argument("-o", "--output", default=".", metavar="DIR", help="where to put it (default: here)")
    p.add_argument("--version", dest="version", metavar="X.Y.Z", help="release to get (default: the latest)")
    p.add_argument("--list", action="store_true", help="list the releases available and exit")
    p.add_argument("--no-extract", action="store_true", help="keep the tarball packed")
    p.add_argument("--keep-tarball", action="store_true", help="keep the tarball after unpacking it")
    p.add_argument("--url", help="download from this URL instead")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("refresh", parents=[library_opt], help="check for a new TPTP release",
                       description="Look up the latest TPTP release on tptp.org. Problems cached from the "
                                   "website are deleted if they come from an older release, so they are "
                                   "fetched again, and a local library (-L or $TPTP) is compared with the "
                                   "latest release. Resets the 30-day reminder.")
    p.add_argument("--index", action="store_true", help="also rebuild the search index of the local library")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser("validate", parents=[library_opt], help="check a local TPTP library",
                       description="Check that a folder or tarball is a TPTP library in the standard "
                                   "layout. Exits with status 1 if it is not.")
    p.add_argument("path", nargs="?", help="the library folder or tarball (default: -L or $TPTP)")
    p.add_argument("--includes", action="store_true", help="also check that every problem's includes exist")
    p.add_argument("--parse", action="store_true", help="also parse every problem and axiom file (slow)")
    p.add_argument("--strict", action="store_true", help="fail on warnings too")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("discover", help="find TPTP libraries on this machine and set $TPTP",
                       description="Look for TPTP libraries (folders with Problems/ and Axioms/) and check "
                                   "them. Prints the command that sets $TPTP to the best one, and can add it "
                                   "to your shell's startup file (--set; on Windows, setx).")
    p.add_argument("dirs", nargs="*", metavar="DIR",
                   help="where to look (default: here, your home folder, and /opt, /usr/local/share, "
                        "/usr/share, /srv, /data)")
    p.add_argument("--depth", type=int, default=4, metavar="N",
                   help="how many folder levels to search below each DIR (default: 4)")
    p.add_argument("--set", action="store_true", help="make it permanent: add it to your shell's startup file")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask: take the first one listed, and allow --set")
    p.add_argument("--shell", choices=("bash", "zsh", "fish", "sh", "powershell", "cmd"),
                   help="the shell to write the command for (default: yours)")
    p.add_argument("--json", action="store_true", help="print JSON (changes nothing)")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("find", parents=[library_opt], help="locate problems in a local library",
                       description="Print where problems (SET001-1, wildcards such as 'SET00*-1') or "
                                   "axiom files (SET001-0, SET001-0.ax) are in a local library.")
    p.add_argument("names", nargs="+", metavar="NAME")
    p.add_argument("-r", "--relative", action="store_true", help="print paths relative to the library root")
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("stats", parents=[library_opt], help="print statistics about a problem",
                       description="Print the dialect, status, symbols, variables and other statistics of "
                                   "a problem (a name such as SET001-1, or a file).")
    p.add_argument("target", metavar="NAME_OR_FILE")
    p.add_argument("--no-expand", dest="expand", action="store_false",
                   help="do not read the included axiom files")
    p.add_argument("--symbols", action="store_true", help="list every symbol")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser(
        "search", parents=[library_opt], help="find problems by their properties (like TPTP2T)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Find the problems of a local library that have every property given, like the "
                    "TPTP2T form at tptp.org/cgi-bin/TPTP2T.\n\n"
                    "Lists are comma-separated and match any item. Numbers take N, MIN..MAX, MIN..,\n"
                    "..MAX, >N, >=N, <N or <=N. Prefix a value with 'not:' to negate it. An option given\n"
                    "twice must hold both times. The problem headers are indexed on first use and the\n"
                    "index is cached until the library changes.",
        epilog="examples:\n"
               "  thousandproblems search --form FOF --status THM --rating 0.5..1 --equality none\n"
               "  thousandproblems search --domain SET,SEU --form THF --format table\n"
               "  thousandproblems search --form not:CNF --predicates '>1' --function-arity 0 --count\n"
               "  thousandproblems search --form TF0 --arithmetic yes | thousandproblems fetch - -o out/")
    criteria = p.add_argument_group("problem properties")
    for name, (_, text) in CRITERIA.items():
        criteria.add_argument(f"--{name.replace('_', '-')}", dest=name, action="append", metavar="VALUE", help=text)
    out = p.add_argument_group("output")
    out.add_argument("--format", choices=("names", "table", "paths", "json"), default="names",
                     help="names (default), a table of properties, file paths, or JSON")
    out.add_argument("--count", action="store_true", help="print only the number of matches")
    out.add_argument("--limit", type=int, metavar="N", help="show at most N matches")
    out.add_argument("--refresh", action="store_true", help="rebuild the header index")
    out.add_argument("-q", "--quiet", action="store_true", help="no progress or totals on stderr")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser(
        "run", parents=[library_opt], help="run problems against theorem provers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Run problems against one or more theorem provers and report, for each, the SZS\n"
                    "status (proved, satisfiable, timeout ...) and the run time. Choose the problems by\n"
                    "name (or a file), or by the same criteria as 'search'. A prover is a command line:\n"
                    "its first word is the binary (on $PATH, or a path), the rest are options passed as\n"
                    "they are. 'eprover' and 'vampire' have presets that also set their time limit.\n"
                    "Prefix a command with LABEL= to name it.",
        epilog="examples:\n"
               "  thousandproblems run SET001-1 'SEU14*' -p eprover -p vampire -t 10\n"
               "  thousandproblems run --domain SET --status THM --rating 0 --limit 50 -p eprover -j 4\n"
               "  thousandproblems run my_problem.p -p 'casc=vampire --mode casc' -p './myprover --fast'\n"
               "  thousandproblems search --form FOF --rating 0 -q | thousandproblems run - -p eprover --format csv")
    p.add_argument("names", nargs="*", metavar="NAME",
                   help="problem names such as SET001-1 (wildcards need a local library), problem files, or "
                        "'-' to read names from stdin; or use the criteria below instead")
    run_opts = p.add_argument_group("provers and limits")
    run_opts.add_argument("-p", "--prover", action="append", metavar="COMMAND",
                          help="a prover to run, e.g. eprover, 'vampire --mode casc', "
                               "'label=./bin/prover -x', or eprover@3.5.10 for an installed version; "
                               "repeat for several")
    run_opts.add_argument("-t", "--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="SECONDS",
                          help=f"time each prover gets per problem (default: {DEFAULT_TIMEOUT:g})")
    run_opts.add_argument("-j", "--jobs", type=int, default=1, metavar="N",
                          help="runs at once (default: 1; timings are fairest with 1)")
    run_opts.add_argument("--install", action="store_true",
                          help="first install any prover that is not found (see 'install')")
    run_opts.add_argument("--platform", metavar="OS-ARCH",
                          help="the platform installed provers are for, e.g. linux-arm64 (default: this machine's)")
    run_opts.add_argument("--input", choices=("file", "stdin"),
                          help="how the problem reaches a prover: a temporary file, or its standard input "
                               "(default: a file)")
    criteria = p.add_argument_group("choose problems by their properties (as in 'search')")
    for name, (_, text) in CRITERIA.items():
        criteria.add_argument(f"--{name.replace('_', '-')}", dest=name, action="append", metavar="VALUE", help=text)
    out = p.add_argument_group("output")
    out.add_argument("--limit", type=int, metavar="N", help="run at most N problems")
    out.add_argument("--format", choices=("table", "json", "csv"), default="table",
                     help="a table of results (default), or JSON or CSV")
    out.add_argument("--output-dir", metavar="DIR",
                     help="save each prover's output to DIR/PROVER/PROBLEM.out (and .err)")
    out.add_argument("--refresh", action="store_true", help="rebuild the header index")
    out.add_argument("-q", "--quiet", action="store_true", help="no progress lines on stderr")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser(
        "install", help="download or build a theorem prover",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Install a prover into the cache directory, where 'run' finds it. Each version and\n"
                    "platform gets a folder of its own. Vampire is downloaded as a release build; E\n"
                    "publishes none, so it is built from its source on GitHub (needs make and a C\n"
                    "compiler).",
        epilog="examples:\n"
               "  thousandproblems install --list\n"
               "  thousandproblems install vampire\n"
               "  thousandproblems install eprover --version 3.5.10\n"
               "  thousandproblems install vampire --version latest --platform linux-arm64\n"
               "  thousandproblems run SET001-1 -p eprover@3.5.10 -p vampire@5.1.0")
    p.add_argument("prover", nargs="?", help="a known prover: see --list")
    p.add_argument("--version", metavar="X.Y.Z",
                   help="the version (default: the one this release knows; 'latest' asks GitHub)")
    p.add_argument("--platform", metavar="OS-ARCH",
                   help="linux, macos or windows with x86_64 or arm64, e.g. macos-arm64 "
                        "(default: this machine's; a prover built from source can only be installed for it)")
    p.add_argument("--force", action="store_true", help="install again even if that version is there")
    p.add_argument("-j", "--jobs", type=int, metavar="N", help="parallel build jobs (default: all CPUs)")
    p.add_argument("--list", action="store_true", help="list the known provers and what is installed")
    p.add_argument("-q", "--quiet", action="store_true", help="only print the path of the binary")
    p.set_defaults(func=cmd_install)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _remind(args)
        return args.func(args)
    except CommandError as e:
        print(f"thousandproblems: error: {e}", file=sys.stderr)
        return 1
    except RateLimited as e:
        print(f"thousandproblems: error: {e}", file=sys.stderr)
        return 75          # EX_TEMPFAIL
    except ProblemNotFound as e:
        print(f"thousandproblems: error: {e.args[0] if e.args else e}", file=sys.stderr)
        return 1
    except ParseError as e:
        print(f"thousandproblems: error: {e}", file=sys.stderr)
        return 1
    except (LibraryError, OSError) as e:
        print(f"thousandproblems: error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
    except BrokenPipeError:
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
