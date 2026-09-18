"""Mabolo Memory: a memory made of Markdown and Git.

What exists so far is the format and the skeleton around it: the Open Knowledge
Format v0.2 entry schema, a validator, configuration and `mabolo init`.
"""

from __future__ import annotations

__version__ = "0.1.0"

#: What goes into OKF `generated.by` for anything this package produces.
PRODUCER = f"mabolo/{__version__}"

__all__ = ["__version__", "PRODUCER"]
