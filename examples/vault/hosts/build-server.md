---
type: reference
title: Build server
description: Four cores, 4 GB of RAM, runs the nightly build
generated:
  by: mabolo/0.1.0
  at: '2026-08-14T10:22:00+03:00'
status: stable
mabolo:
  area: hosts
  pin: false
---

The build server is small on purpose: if a build needs more than four
cores it will not run here, and that is the signal to fix the build.
