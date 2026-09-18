---
type: reference
title: Secrets live in the password store
description: Keys and tokens are kept in the password store, never in a repository
generated:
  by: mabolo/0.1.0
  at: '2026-08-14T10:22:00+03:00'
status: stable
mabolo:
  area: infra
  aliases:
  - secrets
  - password store
---

Keys and tokens are kept in the password store and handed to a host through its
environment. Nothing of that kind belongs in a repository, not even in a file
that is ignored: an ignored file is one `git add -f` away from being published.
