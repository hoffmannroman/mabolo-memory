"""One error class for anything the user is meant to read.

Everything the CLI can fail with on purpose raises `MaboloError`. The command
line turns it into a message and exit code 2; a traceback means a bug.
"""

from __future__ import annotations


class MaboloError(Exception):
    """An error with a message written for the person running the command."""
