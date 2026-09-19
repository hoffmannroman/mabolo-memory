"""Mabolo Memory: a memory made of Markdown and Git.

The format and the skeleton around it: the Open Knowledge Format v0.2 entry
schema, a validator, configuration and `mabolo init`. The measurement: a BM25
index over the files and `mabolo eval`, which is how everything built on top of
the index proves that it did not quietly lose something. The reading tiers: a
session index, quiet recall, standing rules raised by the file they are about,
and an entry read in full through the MCP server. The writing: a transaction
per change with a person's own sentence in front of it, and proposals for
everything that has no such sentence. And the four commands a person reaches
for when they want to check: `why`, `doctor`, `lint` and `recover`.
"""

from __future__ import annotations

__version__ = "0.1.0"

#: What goes into OKF `generated.by` for anything this package produces.
PRODUCER = f"mabolo/{__version__}"

__all__ = ["__version__", "PRODUCER"]
