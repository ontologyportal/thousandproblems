# Changelog

## 0.1.0

- `Variable.index` and `TypedVariable.index`: the distinct variables of a formula are numbered
  1, 2, 3 ... in order of first appearance (scoped by quantifier; 0 outside a formula).
- `TPTPFile.symbols` and `AnnotatedFormula.symbols`: the symbol table, a tuple of
  `Symbol(name, arity)` in order of first appearance, without `$` and `$$` symbols.
- First release: parser and printer for every TPTP language (`thf`, `tff`, `tcf`, `fof`,
  `cnf`, `tpi`), with a C extension and a pure-Python fallback that build identical trees.
- `fetch_problem` / `open_library`: read problems, and expand their includes, from a TPTP
  folder, a tarball or the TPTP website.
