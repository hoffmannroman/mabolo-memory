---
type: feedback
title: Dates are written as ISO-8601
description: Dates sort as text, so they are written 2026-09-18 and never 18/09/2026
generated:
  by: mabolo/0.1.0
  at: '2026-08-14T10:22:00+03:00'
status: stable
mabolo:
  area: design
  applies_to:
  - '*.py'
  - '*.ts'
  - '*.sql'
  - '*.md'
  scope: global
  instead_of: a date format that depends on where the reader lives
---

Dates are written as 2026-09-18. That spelling sorts correctly as plain text,
means the same thing in every country, and needs no locale to be read.

Times carry their offset. A timestamp without one is a guess that looks precise.
