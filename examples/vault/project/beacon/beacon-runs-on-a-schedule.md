---
type: reference
title: Beacon runs every ten minutes
description: A timer starts Beacon; it has no queue and no worker of its own
generated:
  by: mabolo/0.1.0
  at: '2026-08-14T10:22:00+03:00'
status: stable
mabolo:
  area: project/beacon
---

A timer starts Beacon every ten minutes. There is no queue and no long running
worker: a run that is still going when the next one starts is a bug, not a
backlog.
