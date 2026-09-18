# infra

What holds across machines: network, services, accounts.

## Entries

* [Backups run at 03:00 and are restored once a month](backups-run-nightly.md) - A snapshot every night, and a rehearsed restore on the first Monday
* [The build runs out of memory above four workers](ci-memory-limit.md) - The CI image caps at 4 GB, so more than -j4 gets the runner killed
* [Deploy from main only](deploy-from-main.md) - Releases are cut from main; tags are labels, not sources
* [Secrets live in the password store](secrets-in-the-password-store.md) - Keys and tokens are kept in the password store, never in a repository
* [Staging mirrors production](staging-mirrors-production.md) - Staging runs the same image and the same migrations, on smaller data
