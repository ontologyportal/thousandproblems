# Changelog

## 0.1.0

- First release: parser and printer for every TPTP language (`thf`, `tff`, `tcf`, `fof`,
  `cnf`, `tpi`), with a C extension and a pure-Python fallback that build identical trees.
- `fetch_problem` / `open_library`: read problems, and expand their includes, from a TPTP
  folder, a tarball or the TPTP website.
