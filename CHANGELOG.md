# Changelog

## Unreleased

- Python 3.15: binary wheels with the C parser, and 3.15 in the test matrix.
- A `thousandproblems` command (also `python -m thousandproblems`) with the subcommands
  `fetch` (problems with their axioms), `download` (the whole library), `validate` (check a
  local install), `find`, `stats` and `search`.
- `thousandproblems.search`: an offline version of TPTP2T's problem-property search over a
  local library, using a cached index of the problem headers.
- `thousandproblems.stats`: `syntax_stats` and `SPC` read a problem's `% Syntax` and `% SPC`
  header lines; `compute_stats` works out dialect, roles, atoms, predicates, functions,
  variables and depths from the formulae.
- `thousandproblems.validate.validate_library` and `thousandproblems.download.download_library`.
- `Library.problem_files(name)`: a problem and every file it includes.
- `thousandproblems discover`: find TPTP libraries on this machine, check them, and print the
  command that sets `$TPTP`, or add it to the shell's startup file (`--set`; `setx` on Windows).
- `thousandproblems refresh` (and `thousandproblems.updates.check_for_updates`): look up the
  latest TPTP release, clear cached website downloads from older releases, and compare a local
  library with it. Other commands remind you after 30 days without a check (y / n / snooze);
  `THOUSANDPROBLEMS_NO_UPDATE_CHECK=1` turns that off.

## 0.1.0

- `Variable.index` and `TypedVariable.index`: the distinct variables of a formula are numbered
  1, 2, 3 ... in order of first appearance (scoped by quantifier; 0 outside a formula).
- `TPTPFile.symbols` and `AnnotatedFormula.symbols`: the symbol table, a tuple of
  `Symbol(name, arity)` in order of first appearance, without `$` and `$$` symbols.
- First release: parser and printer for every TPTP language (`thf`, `tff`, `tcf`, `fof`,
  `cnf`, `tpi`), with a C extension and a pure-Python fallback that build identical trees.
- `fetch_problem` / `open_library`: read problems, and expand their includes, from a TPTP
  folder, a tarball or the TPTP website.
