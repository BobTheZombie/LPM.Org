# lpm-filesystem

This package owns LPM's static configuration, system account definitions, and
persistent directory layout.

Runtime directories and permissions are applied through
`systemd-sysusers` and `systemd-tmpfiles`. The `lpm` program package owns
the executable, hooks, and build metadata; it must not replace files owned by
this package.

Repository enrollment is intentionally not enabled here. Until the official
binary repository is published, LPM initializes and manages its own repository
state under `/var/lib/lpm`.
