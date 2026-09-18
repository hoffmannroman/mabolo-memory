---
type: reference
title: Gateway
description: Terminates TLS and routes to the application hosts, nothing else runs on it
generated:
  by: mabolo/0.1.0
  at: '2026-08-14T10:22:00+03:00'
status: stable
mabolo:
  area: hosts
---

The gateway terminates TLS and routes to the application hosts. Nothing else
runs here, so a bad deploy anywhere cannot take the routing down with it.
