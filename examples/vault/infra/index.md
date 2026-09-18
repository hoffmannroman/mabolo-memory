# infra

What holds across machines: network, services, accounts.

## Entries

* [The build runs out of memory above four workers](ci-memory-limit.md) - The CI image caps at 4 GB, so more than -j4 gets the runner killed
* [Deploy from main only](deploy-from-main.md) - Releases are cut from main; tags are labels, not sources
