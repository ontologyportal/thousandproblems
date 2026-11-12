"""Compare the pure-Python and C parsers on generated TPTP.

    python benchmarks/bench.py [formulae-per-language] [repeats]
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

import corpus  # noqa: E402
from thousandproblems import _pyparser  # noqa: E402

try:
    from thousandproblems import _cparser
except ImportError:
    _cparser = None


def best(fn, repeats):
    times = []
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        times.append(time.perf_counter() - start)
    return min(times)


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    repeats = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    sets = {
        "cnf": corpus.Gen(1).file(count, ("cnf",)),
        "fof": corpus.Gen(2).file(count, ("fof",)),
        "tff": corpus.Gen(3).file(count, ("tff",)),
        "thf": corpus.Gen(4).file(count, ("thf",)),
    }
    sets["mixed"] = "".join(sets.values())

    print(f"{'':6}{'size':>9}  {'lex (py)':>9}  {'python':>9}  {'C':>9}  {'speedup':>8}")
    for name, text in sets.items():
        size = len(text) / 1e6
        lex = best(lambda: _pyparser._tokenize(text, None), repeats)
        py = best(lambda: _pyparser.parse(text, None), repeats)
        if _cparser is None:
            print(f"{name:6}{size:8.2f}M  {lex:8.3f}s  {py:8.3f}s  {'-':>9}  {'-':>8}")
            continue
        c = best(lambda: _cparser.parse(text, None), repeats)
        print(f"{name:6}{size:8.2f}M  {lex:8.3f}s  {py:8.3f}s  {c:8.3f}s  {py / c:7.1f}x")


if __name__ == "__main__":
    main()
