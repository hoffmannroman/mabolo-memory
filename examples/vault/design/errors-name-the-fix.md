---
type: feedback
title: An error message names the fix
description: Every error says what to do next, not only what went wrong
generated:
  by: mabolo/0.1.0
  at: '2026-08-14T10:22:00+03:00'
status: stable
mabolo:
  area: design
  applies_to:
  - '*.py'
  - '*.ts'
  scope: global
  instead_of: a stack trace handed to the person as if it were an explanation
---

An error names what went wrong, which file it happened in, and what the person
can do about it. The third part is the one that gets left out, and it is the
only one the reader was looking for.
