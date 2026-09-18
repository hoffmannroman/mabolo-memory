---
type: reference
title: Backups run at 03:00 and are restored once a month
description: A snapshot every night, and a rehearsed restore on the first Monday
generated:
  by: mabolo/0.1.0
  at: '2026-08-14T10:22:00+03:00'
status: stable
mabolo:
  area: infra
---

The nightly snapshot is not the backup. The restore is, and it is rehearsed on
the first Monday of the month against an empty host.

A snapshot nobody has ever restored is a hope, and hopes are not recoverable.
