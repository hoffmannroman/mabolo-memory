---
type: reference
title: Staging mirrors production
description: Staging runs the same image and the same migrations, on smaller data
generated:
  by: mabolo/0.1.0
  at: '2026-08-14T10:22:00+03:00'
status: stable
mabolo:
  area: infra
---

Staging runs the image production will run, with the migrations already applied.
The data is smaller and anonymised; everything else is the same, because a
staging that differs only proves that staging works.
