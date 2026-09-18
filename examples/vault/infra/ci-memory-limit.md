---
type: reference
title: The build runs out of memory above four workers
description: The CI image caps at 4 GB, so more than -j4 gets the runner killed
generated:
  by: mabolo/0.1.0
  at: '2026-08-14T10:22:00+03:00'
status: stable
stale_after: '2027-02-01T00:00:00+03:00'
mabolo:
  area: infra
  anchor: .github/workflows/ci.yml
---

Four workers is the practical ceiling. Above that the runner is killed
without a message, which looks like a flaky test and is not one.
