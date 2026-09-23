# Contributing

One person maintains this, so the useful kinds of help are narrow and specific.

**Welcome:** bug reports with the command you ran and its output, corrections to
the documentation, and reports that an entry format or a client integration
broke.

**Usually declined:** feature requests. A small tool that one person maintains
stays useful; a large one does not. If you need a different shape, fork it, that
is what the open format and the MIT licence are for.

## Working on Mabolo itself

The awkward part of developing a memory is that the thing under test is the
thing you rely on. Two environment variables keep the two apart.

**`MABOLO_VAULT`** points the server at another vault. Set it to a scratch
directory and nothing you try can reach the real entries:

```bash
export MABOLO_VAULT=/tmp/mabolo-scratch
uv run pytest -q
```

**`MABOLO_SESSION_ID`** names the conversation the consent notes belong to.
Set it when you are testing the write path by hand; otherwise the server reads
the host's session id, and failing that falls back to the directory and the
window (see `docs/write.md`).

**Every test runs once without its fix.** Take the fix out, watch the test go
red, put it back. A test that was green both ways is decoration, and it will
still be green the day the behaviour breaks. Take the fix back out with a copy
you made beforehand, not with `git checkout` — that would take the rest of your
uncommitted work with it.

**The server is an installed copy, not the repo.** Editing `src/` changes
nothing for a running client until you reinstall and the client restarts it:

```bash
uv tool install --force --reinstall .
```

`--reinstall` is not optional. uv keys its build cache on `pyproject.toml`,
not on `src/`, so while the version number stays the same `--force` alone
installs the wheel it built last time and says it succeeded.

A server whose code changed on disk after it started ends every answer with a
line saying so, so a session left open across an update finds out and can be
reconnected.

**Before opening an issue:** say which operating system and which version, what
you expected and what happened. A vault is personal, so never paste entries you
would not publish.
