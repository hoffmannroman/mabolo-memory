---
type: reference
title: Deploy from main only
description: Releases are cut from main; tags are labels, not sources
sources:
- id: s1
  resource: session://2026-08-14
generated:
  by: mabolo/0.1.0
  at: '2026-08-14T10:22:00+03:00'
verified:
- by: human:alex
  at: '2026-08-14T10:25:00+03:00'
status: stable
mabolo:
  area: infra
  pin: false
---

Releases are cut from `main`. Tags mark what shipped, they are never the
source of a deploy.[^s1]

[^s1]: "we deploy from main only, never from a tag"
