"""Builds the optional C parser. Everything else is configured in pyproject.toml.

Environment variables read at build time:

``THOUSANDPROBLEMS_PURE_PYTHON=1``
    Don't build the C extension at all; the result is a pure-Python (``py3-none-any``)
    wheel.
``THOUSANDPROBLEMS_REQUIRE_EXT=1``
    Fail the build if the C extension can't be compiled. Without it a compile failure
    is only a warning and the package installs as pure Python. Release builds set this
    so that a broken compiler can never produce a binary wheel without the extension.
"""

import os

from setuptools import Extension, setup

ext_modules = []
if not os.environ.get("THOUSANDPROBLEMS_PURE_PYTHON"):
    ext_modules.append(
        Extension(
            "thousandproblems._cparser",
            sources=["src/thousandproblems/_cparser.c"],
            optional=not os.environ.get("THOUSANDPROBLEMS_REQUIRE_EXT"),
        )
    )

setup(ext_modules=ext_modules)
