---
type: reference
title: Atlas stores everything in Postgres
description: One Postgres database, no second store and no cache that outlives a request
generated:
  by: mabolo/0.1.0
  at: '2026-08-14T10:22:00+03:00'
status: stable
mabolo:
  area: project/atlas
  aliases:
  - atlas database
---

Atlas keeps everything in one Postgres database. A second store would need its
own backup, its own migration story and its own answer to what happens when the
two disagree, and Atlas is not large enough to pay for that three times.
