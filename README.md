# thousandproblems

A fast parser and printer for the [TPTP](https://tptp.org) ("Thousands of Problems for Theorem
Provers") problem and solution syntax.

- Every TPTP language: `thf`, `tff`, `tcf`, `fof`, `cnf` and `tpi`, including TF1/TH1
  polymorphism, the TXF/non-classical extensions, `include` directives, and annotations
  (sources, inference records, useful info).
- One AST for all of them, with exact line and column in every error.
- `str(node)` prints any node back as valid TPTP.
- **A C extension does the parsing**, roughly 10 times faster than the pure-Python parser. If
  it isn't available the same parser runs in pure Python, with identical results.
- Fetches problems, with their axiom includes, from the TPTP website, a tarball or an
  unpacked library.
- No dependencies. Python 3.11+.

```
pip install thousandproblems
```

Import it as `thousandproblems`; the examples below alias it to `tptp`.

## Parsing

```python
>>> import thousandproblems as tptp
>>> problem = tptp.parse("fof(ax1, axiom, ! [X] : (p(X) => q(X))).")
>>> ax = problem.formulae[0]
>>> ax.language, ax.name, ax.role
('fof', 'ax1', 'axiom')
>>> ax.formula.quantifier, ax.formula.variables
('!', (Variable(name='X', index=1),))
>>> print(ax)
fof(ax1,axiom,! [X] : (p(X) => q(X))).
```

## Variable indexes and the symbol table

Each `Variable` has an `index`: the distinct variables of one formula are numbered 1, 2, 3 ... in
order of first appearance.

```python
>>> clause = tptp.parse("cnf(c, axiom, f(X) | f(Y) | g(X, f(Y))).").formulae[0]
>>> [(v.name, v.index) for v in tptp.walk(clause.formula) if isinstance(v, tptp.Variable)]
[('X', 1), ('Y', 2), ('X', 1), ('Y', 2)]
```

A quantifier counts as the first appearance of the variables it binds, and every binding is its
own variable, so `! [Y,X] : p(X,Y)` numbers Y 1 and X 2, and `(! [X] : p(X)) & (! [X] : q(X))`
gives its two `X` the indexes 1 and 2. Numbering restarts for each formula, and a variable that
is not part of a formula (in an annotation, or built by hand) has index 0. `index` is not
compared, so `Variable("X") == Variable("X", 1)`.

`problem.symbols` is the symbol table: a tuple of `Symbol(name, arity)`, one for each distinct
function, constant or predicate the formulae use, in order of first appearance. `f/1` and `f/2`
are different symbols, and a constant has arity 0.

```python
>>> problem = tptp.parse("cnf(a, axiom, p(f(a), X) | ~ q(f(a,b))). cnf(b, axiom, p(c, Y)).")
>>> [str(s) for s in problem.symbols]
['p/2', 'f/1', 'a/0', 'q/1', 'f/2', 'b/0', 'c/0']
>>> problem.formulae[1].symbols
(Symbol(name='p', arity=2), Symbol(name='c', arity=0))
```

Variables, equality, numbers, distinct objects, `$` and `$$` symbols (`$true`, `$less` ...),
type declarations and annotations are not symbols. In THF, `f @ a` uses the symbols `f/0` and
`a/0`. Each formula has its own `symbols`, and the file's table is rebuilt when
`expand_includes` brings other files in, so it only covers formulae that are really there.

## Functions

| Function | |
| --- | --- |
| `parse(text, filename=None)` | Parse the text of a file. |
| `parse_file(path, *, expand_includes=False, tptp_root=None)` | Parse a file; optionally replace each `include` with the formulae it selects. |
| `parse_formula(text, language="fof")` | Parse one bare formula. |
| `walk(node)` | Iterate over a subtree, depth first. |
| `unquote(word)` | The raw text of a single-quoted word. |

Bad input raises `ParseError`, which has `.line`, `.column`, `.filename` and `.message`:

```
x.p:3:9: expected ')', found '&'
      p | q & r).
            ^
```

Nodes are frozen, hashable dataclasses (`Function`, `Variable`, `Or`, `ForAll`, `Implies` ...).
`|` and `&` chains are flat `NAry` nodes; parentheses are not represented. See the module
docstring (`help(thousandproblems)`) for the full description of the tree.

This is a *syntax* parser: it does not check sorts, arities, roles, or whether variables are
bound.

## Fetching problems from a TPTP library

```python
# the TPTP website (current release)
problem = tptp.fetch_problem("SET001-1", expand_includes=True)

# an unpacked library: the folder that holds Problems/ and Axioms/
problem = tptp.fetch_problem("SET001-1", "/data/TPTP-v9.0.0", expand_includes=True)

# a tarball (.tgz, .tar.gz, .tar.bz2, .tar.xz or plain .tar)
problem = tptp.fetch_problem("SET001-1", "/data/TPTP-v9.0.0.tgz", expand_includes=True)
```

Without a source, the folder named by the `TPTP` environment variable is used if there is
one, and the website otherwise. To read several problems, open the library once:

```python
with tptp.open_library("/data/TPTP-v9.0.0.tgz") as library:
    for name in library.problem_names("SET")[:10]:
        print(name, library.problem(name).header.status)
```

Problem names follow the library's own scheme: `SET001-1`, `SEU140+3`, `SYN000_1`,
`SYO000^1`. `library.problem_text(name)` gives the raw source.

**The website allows only a few requests a minute.** The online library fetches a problem
together with the axiom files it includes in one request, and caches everything in
`$THOUSANDPROBLEMS_CACHE` (default: your platform's cache directory), so repeat reads cost nothing.
If the server says you are going too fast you get `RateLimited`, with the time to wait; nothing
is retried for you. Only the current release is available online; for another version, open its
tarball. For anything more than a handful of problems, download the library.

Reading a compressed tarball means decompressing up to each file, so unpack it
(and use the folder) if you will read many problems.

## Command line

Installing the package adds a `thousandproblems` command (`python -m thousandproblems` works
too). Commands that need a library take `-L/--library` with a folder, a tarball or a server
URL. Without it they use the folder named by `$TPTP`, and `fetch` and `stats` fall back to the
TPTP website.

```sh
# the whole library (about 1 GB): the latest release, or --version 9.0.0; --list shows them all
thousandproblems download -o ~/tptp            # prints e.g. ~/tptp/TPTP-v10.0.0
export TPTP=~/tptp/TPTP-v10.0.0

# a few problems, with the axiom files they include, saved in the standard layout
thousandproblems fetch SET001-1 'SEU14*' -o problems/
thousandproblems fetch SET001-1 --expand       # print it, includes replaced by their formulae

# already have a library somewhere? find it, and set $TPTP for new terminals
thousandproblems discover ~/datasets --set

# is this a usable TPTP library? (exit status 1 if not)
thousandproblems validate ~/tptp/TPTP-v10.0.0 --includes

# where is it?
thousandproblems find SET001-1 'SYO00?^1' SET001-0.ax

# dialect, status, symbols, variables ...
thousandproblems stats SET001-1
thousandproblems stats my_problem.p --symbols --json
```

`validate` checks the standard layout: the `Problems/` and `Axioms/` folders, three-letter
domain folders, and problem files named after their domain. `--includes` checks that every
include can be found, and `--parse` also parses every file (slow). A missing `Documents/`,
`Generators/`, `Scripts/` or `TPTP2X/` folder is only a warning, since a trimmed copy still
works; `--strict` makes warnings fail too.

`discover` looks for libraries: folders holding `Problems/` and `Axioms/`, searched four levels
deep (`--depth`) under the folders you name, or by default under the current folder, your home
folder, `/opt`, `/usr/local/share`, `/usr/share`, `/srv` and `/data`. It checks each one,
lists them (usable ones first, then newest), and prints the command that sets `$TPTP` for your
shell (`--shell bash|zsh|fish|sh|powershell|cmd` to choose another). With `--set`, or when you
say yes at the prompt, it adds that command to your shell's startup file (`~/.zshrc`,
`~/.bashrc`, or `~/.bash_profile` on macOS, a `conf.d` file for fish, `~/.profile` otherwise)
between marker comments, so running it again replaces that line rather than adding another.
On Windows it runs `setx` instead. Without a terminal, `--set` also needs `--yes`. A program
can't change the environment of the shell that started it, so the current terminal still needs
the printed command, or a new window.

`stats` shows the figures it works out from the formulae (with the included axioms, unless you
pass `--no-expand`) next to the `% Syntax` statistics that the TPTP header declares:

```
Problem        : SET001-1  (Problems/SET/SET001-1.p)
Domain         : SET  Set Theory
Title          : Set members are superset members
Status         : Unsatisfiable
Rating         : 0.11  (v10.0.0)
SPC            : CNF_UNS_RFO_NEQ_NHN  (clause normal form, Unsatisfiable, real first-order, no equality, non-Horn)
Dialect        : cnf  [CNF]
Formulae       : 9  (6 axiom, 2 hypothesis, 1 negated_conjecture)
Unit formulae  : 3
Atoms          : 17  (0 equality)
Symbols        : 7  (arity 0-2)
  predicates   : 3  (arity 2-2)
  functions    : 1  (arity 2-2)
  constants    : 3  (arity 0-0)
Variables      : 13  (bound: 13 !)  (0 singleton)

Declared in the header (% Syntax), covering the problem with its includes:
  clauses          :      9  (3 unt; 1 nHn; 8 RR)
  literals         :     17  (0 equ; 8 neg)
  ...
```

### Keeping up with new TPTP releases

The website only serves the current release, so problems cached from it go out of date when a
new release comes out. `thousandproblems refresh` looks up the latest release on tptp.org,
deletes cached downloads that come from an older one (they are fetched again when next
needed), and compares your local library (`-L` or `$TPTP`) with the latest release.
`--index` also rebuilds the search index.

```
$ thousandproblems refresh
latest TPTP release : v10.0.0
online cache        : held v9.3.0; cleared 12 files, so problems are fetched again from v10.0.0
local library       : /data/TPTP-v9.3.0 is v9.3.0; get v10.0.0 with 'thousandproblems download'
```

If you haven't checked for 30 days, the next command asks first:

```
You have not checked for a new TPTP library version in 30 days, check now? [y]es / [n]o / [s]nooze for thirty days:
```

`n` asks again the next day and `s` waits another 30 days. Without a terminal (in scripts and
pipelines) you get a one-line warning on stderr instead, at most once a day. Set
`THOUSANDPROBLEMS_NO_UPDATE_CHECK=1` to turn the reminder off, e.g. in CI. The time of the last
check is kept in `state.json` in the cache directory, and the version the online cache holds
in a `VERSION` file next to the cached files.

### Searching by problem properties (TPTP2T)

`search` covers the problem-properties part of the [TPTP2T](https://tptp.org/cgi-bin/TPTP2T)
form, run offline against a local library: domain, logical form, first-order subform, problem
version, status, rating, numbers of formulae, unit formulae, atoms, types, datatypes,
codatatypes, symbols, predicates, functions, the arities, equality, unit equality, variables
and binders, and arithmetic.

```sh
thousandproblems search --form FOF --status THM --rating 0.5..1 --equality none
thousandproblems search --domain SET,SEU --form THF --format table
thousandproblems search --form not:CNF --predicates '>1' --function-arity 0 --count
thousandproblems search --form TF0 --arithmetic yes | thousandproblems fetch - -o out/
```

Lists are comma-separated and match any item. Numbers take `N`, `MIN..MAX`, `MIN..`, `..MAX`,
`>N`, `>=N`, `<N` or `<=N`, and `not:` negates a criterion like TPTP2T's `~` box. All
criteria must hold. `thousandproblems search --help` lists them all. The answers come from the
problem headers (`% SPC`, `% Status`, `% Rating`, `% Version` and `% Syntax`), which are
indexed on first use (a couple of seconds for a folder; a tarball has to be decompressed once)
and cached in `$THOUSANDPROBLEMS_CACHE` until the library changes. TPTP2T's *solution*
properties (which systems solved a problem, and how) come from the TSTP solution library, not
the problem library, so `search` does not offer them.

The same is available from Python:

```python
from thousandproblems.search import search
for record in search("/data/TPTP-v10.0.0", form="FOF", status="THM", rating="unsolved"):
    print(record.name, record.rating, record.stat("formulae"))
```

`thousandproblems.stats` (`compute_stats`, `syntax_stats`, `SPC`),
`thousandproblems.validate.validate_library` and `thousandproblems.download.download_library`
are the other building blocks the commands use.

## Which parser is running?

```python
>>> tptp.BACKEND
'c'
```

The C extension is used when it is installed, and `BACKEND` is `"c"`. Otherwise (no wheel for
your platform and no compiler when installing from source, or you asked for it) the pure-Python
parser runs and `BACKEND` is `"python"`. Set `THOUSANDPROBLEMS_PURE_PYTHON=1` before importing to
force pure Python.

The C parser hands very deeply nested input (more than about a thousand levels of nested terms
or a few hundred of bare parentheses) to the Python parser, so that it can never overflow the
C stack, even on a thread with a small one. You get the same result either way, just more
slowly.

One thing the parser cannot control: CPython frees a very deeply nested tree recursively, using
a few hundred bytes of stack per level. Freeing a tree around a thousand levels deep on a thread
with a tiny stack (256 KB) crashed Python 3.14 on musl Linux, with either parser. If you handle
inputs like that, don't do it on threads with stacks that small; the default thread stack size on
every mainstream platform is fine.

Measured on an Apple M-series laptop, Python 3.14, on generated problems of 0.4 to 0.7 MB:

| | pure Python | C | speedup |
| --- | --- | --- | --- |
| cnf | 0.274 s | 0.026 s | 10.5x |
| fof | 0.269 s | 0.027 s | 10.0x |
| tff | 0.221 s | 0.020 s | 10.9x |
| thf | 0.160 s | 0.013 s | 12.4x |

Run `python benchmarks/bench.py` to measure on your machine.

## Development

```
python -m venv .venv && . .venv/bin/activate
pip install -e .                 # builds the C extension if there's a compiler
python -m unittest discover -s tests
THOUSANDPROBLEMS_PURE_PYTHON=1 python -m unittest discover -s tests    # the fallback
```

`tests/test_backends.py` runs the C and Python parsers side by side on the whole test corpus,
on generated problems and on thousands of randomly damaged ones, and requires identical trees
and identical error messages, lines and columns. If you change the grammar, change
`_pyparser.py` and `_cparser.c` together; that test will tell you if they drift apart.
`THOUSANDPROBLEMS_FUZZ_ROUNDS=4000` runs it harder.

Build-time switches: `THOUSANDPROBLEMS_PURE_PYTHON=1` builds without the extension (a pure
`py3-none-any` wheel), and `THOUSANDPROBLEMS_REQUIRE_EXT=1` makes a failed C compile an error
instead of a warning.
