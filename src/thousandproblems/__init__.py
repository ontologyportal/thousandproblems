"""Parser for the TPTP problem and solution syntax (grammar v9.3.1.x).

Handles every TPTP language -- ``thf``, ``tff``, ``tcf``, ``fof``, ``cnf`` and
``tpi`` -- including TF1/TH1 polymorphism, TXF/non-classical extensions, ``include``
directives, and annotations (sources, inference records, useful info).

    >>> import thousandproblems as tptp
    >>> problem = tptp.parse("fof(ax1, axiom, ! [X] : (p(X) => q(X))).")
    >>> ax = problem.formulae[0]
    >>> ax.language, ax.name, ax.role
    ('fof', 'ax1', 'axiom')
    >>> ax.formula.quantifier, ax.formula.variables
    ('!', (Variable(name='X', index=1),))
    >>> print(ax)
    fof(ax1,axiom,! [X] : (p(X) => q(X))).

Entry points are :func:`parse`, :func:`parse_file` and :func:`parse_formula`. Bad
input raises :class:`ParseError`, which carries the line and column. ``str(node)``
prints any node back as valid TPTP, and :func:`walk` iterates over a subtree.

The AST is the same for all languages:

* ``|``, ``&`` and THF ``@``, ``*``, ``+`` chains are flat :class:`NAry` nodes, as
  they are lists in the grammar. Parentheses are not represented, so ``a | (b | c)``
  is an ``NAry`` nested inside an ``NAry``.
* Other infix operators (``=>``, ``=``, ``>``, ``==``, ``:=``, ``-->`` ...) are
  :class:`Binary`. ``>`` is right-associative.
* Constants are :class:`Function` with no arguments. Defined and system symbols keep
  their prefix, as in ``Function('$true')``.
* ``Variable.index`` numbers the distinct variables of a formula 1, 2, 3 ... in order of
  first appearance, so in ``f(X) | f(Y) | g(X, f(Y))`` every ``X`` has index 1 and every
  ``Y`` index 2. A quantifier counts as the appearance of the variables it binds, and each
  binding is its own variable: the two ``X`` of ``(! [X] : p(X)) & (! [X] : q(X))`` get
  indexes 1 and 2. Numbering starts again for each formula; a variable outside a formula (in
  an annotation, or built by hand) has index 0.
* ``TPTPFile.symbols`` is the symbol table: a tuple of :class:`Symbol` ``(name, arity)``,
  one for each distinct function, constant or predicate the formulae use, in order of first
  appearance, so ``f/1`` and ``f/2`` are different symbols. Variables, equality, numbers,
  distinct objects, ``$`` and ``$$`` symbols, type declarations and annotations are not
  symbols. Each ``AnnotatedFormula`` has its own ``symbols`` too, and the file's table follows
  ``expand_includes``.
* Quotes are removed from a word when the result is still a valid lower-case word,
  so ``'cat'`` and ``cat`` compare equal, as the specification requires. Any other
  quoted word keeps its quotes; use :func:`unquote` to get the raw text.

This is a syntax parser: it does not check sorts, arities, roles, or that variables
are bound. Requires Python 3.11+.

Problems can be fetched from a TPTP library, given as a folder, a tarball or the TPTP
website, with :func:`fetch_problem`; see :mod:`thousandproblems.library`.

Parsing runs in a C extension when one is available, and in pure Python otherwise;
both build identical trees and raise identical errors. ``thousandproblems.BACKEND`` is
``"c"`` or ``"python"``. Set the environment variable ``THOUSANDPROBLEMS_PURE_PYTHON=1``
before importing to force the pure-Python parser.

The standard comment header of a problem file is available as ``TPTPFile.header``,
with the expected SZS ``status`` and the difficulty ``rating``::

    >>> header = parse_file("SET001-1.p").header      # doctest: +SKIP
    >>> header.status, header.rating                  # doctest: +SKIP
    ('Unsatisfiable', 0.0)

"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Union

from . import _pyparser
from .nodes import *  # noqa: F403
from .nodes import _error_at, _symbol_table, __all__ as _node_names

_LIBRARY_NAMES = ("DirectoryLibrary", "Library", "LibraryError", "OnlineLibrary", "ProblemNotFound",
                  "RateLimited", "TarballLibrary", "fetch_problem", "open_library")

__all__ = [*_node_names, "BACKEND", "parse", "parse_file", "parse_formula", *_LIBRARY_NAMES]

try:
    from importlib.metadata import version as _version
    __version__ = _version("thousandproblems")
except Exception:  # not installed, e.g. running from a source checkout
    __version__ = "0+unknown"

_native = None
if not os.environ.get("THOUSANDPROBLEMS_PURE_PYTHON"):
    try:
        from . import _cparser as _native
    except ImportError:
        _native = None

#: ``"c"`` if the compiled parser is in use, otherwise ``"python"``.
BACKEND = "python" if _native is None else "c"


def parse(text: str, filename: Optional[str] = None) -> TPTPFile:
    """Parse the text of a TPTP file. Includes are kept as :class:`Include` nodes."""
    if _native is not None:
        result = _native.parse(text, filename)
        if result is not NotImplemented:
            return result
    return _pyparser.parse(text, filename)


def parse_formula(text: str, language: str = "fof") -> Node:
    """Parse one bare formula, without the ``fof(name,role,...).`` wrapper."""
    if language not in _LANGUAGES:
        raise ValueError(f"unknown TPTP language {language!r}")
    if _native is not None:
        result = _native.parse_formula(text, language)
        if result is not NotImplemented:
            return result
    return _pyparser.parse_formula(text, language)


def __getattr__(name: str):
    # The library code (tarfile, urllib) is only imported when it is used.
    if name == "library" or name in _LIBRARY_NAMES:
        import importlib
        library = importlib.import_module(".library", __name__)
        return library if name == "library" else getattr(library, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_LANGUAGES = frozenset({"thf", "tff", "tcf", "fof", "cnf", "tpi"})


def parse_file(path: Union[str, os.PathLike], *, expand_includes: bool = False,
               tptp_root: Union[str, os.PathLike, None] = None) -> TPTPFile:
    """Parse a TPTP file.

    With ``expand_includes``, each include is replaced by the formulae it selects.
    Include paths are tried relative to the including file, then relative to
    ``tptp_root`` (default: the ``$TPTP`` environment variable). The returned
    ``comments`` are those of the top-level file only.
    """
    path = Path(path)
    root = tptp_root if tptp_root is not None else os.environ.get("TPTP")
    return _load(path, Path(root) if root else None, expand_includes, ())


def _load(path: Path, root: Optional[Path], expand: bool, stack: tuple[Path, ...]) -> TPTPFile:
    text = path.read_text(encoding="utf-8", errors="replace")
    parsed = parse(text, str(path))
    if not expand:
        return parsed
    here = path.resolve()
    inputs: list[Union[AnnotatedFormula, Include]] = []
    for item in parsed.inputs:
        if isinstance(item, AnnotatedFormula):
            inputs.append(item)
            continue
        candidates = [path.parent / item.path]
        if root is not None:
            candidates.append(root / item.path)
        target = next((c for c in candidates if c.is_file()), None)
        fail = lambda message: _error_at(message, text, item.line, 1, str(path))  # noqa: E731
        if target is None:
            tried = ", ".join(str(c) for c in candidates)
            raise fail(f"included file {item.path!r} not found (tried {tried})")
        if target.resolve() in (*stack, here):
            raise fail(f"circular include of {item.path!r}")
        formulae = _load(target, root, True, (*stack, here)).formulae
        if isinstance(item.selection, tuple):
            selected = [f for f in formulae if f.name in item.selection]
            missing = set(item.selection) - {f.name for f in selected}
            if missing:
                raise fail(f"{item.path!r} has no formulae named {sorted(missing)}")
            formulae = tuple(selected)
        inputs.extend(formulae)
    return TPTPFile(tuple(inputs), parsed.comments, parsed.filename, _symbol_table(i for i in inputs if isinstance(i, AnnotatedFormula)))
