"""Mabolo Memory: a memory made of Markdown and Git.

What exists so far is the format, the skeleton around it and the measurement:
the Open Knowledge Format v0.2 entry schema, a validator, configuration,
`mabolo init`, a BM25 index over the files, and `mabolo eval`, which is how
anything built on top of the index has to prove that it did not lose something.
"""

from __future__ import annotations

__version__ = "0.1.0"

#: What goes into OKF `generated.by` for anything this package produces.
PRODUCER = f"mabolo/{__version__}"

__all__ = ["__version__", "PRODUCER"]
