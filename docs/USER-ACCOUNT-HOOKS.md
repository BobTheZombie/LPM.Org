# LPM system accounts and opt-in user groups

LPM runs account-related actions after successful package transactions. These
actions work against `LPM_ROOT` (normally `/`), including target roots during
bootstrap, and are safe to run repeatedly.

## System accounts: `systemd-sysusers`

The existing `systemd-sysusers.hook` runs when a package installs or upgrades
a declaration in `/usr/lib/sysusers.d/` or `/etc/sysusers.d/`. Use declarative
rules, shipped by the package:

```ini
# /usr/lib/sysusers.d/example-daemon.conf
g example-daemon -
u example-daemon - "Example service" /var/lib/example-daemon /usr/bin/nologin
```

This creates a locked **system** account and group only if they do not already
exist. `systemd-tmpfiles` separately creates runtime/state directories from
`tmpfiles.d` files. LPM's wrapper reports failures from `systemd-sysusers`
rather than silently ignoring them; if the tool is absent during early
bootstrap, provisioning is deferred and logged. Retry it when available.

Do not use `sysusers.d` to create interactive desktop login users. Those
require `useradd`, `passwd`, home directory setup, and PAM policy, usually
through the installer. The `m` sysusers directive can implicitly create
missing users, so it is not suitable for unrestricted login-user enrollment.

## Opt-in memberships for existing local login users

Place root-owned, non-group/world-writable rules under
`/etc/lpm/user-groups.d/*.conf`:

```text
# /etc/lpm/user-groups.d/desktop.conf
# username  supplementary-group [supplementary-group ...]
alice lpadmin
# alice wheel
```

LPM's `user-groups.hook` runs once after package install/upgrade transactions,
after systemd-sysusers. It:

- Checks `/etc/passwd` and `/etc/group` in the **target root**.
- Acts only on local users with UID >= 1000, never root/service accounts.
- Adds memberships with `usermod --root ... --append --groups ...`.
- Skips existing memberships, missing users, and missing groups. No users or
  groups are created by this hook.
- Does nothing when no administrator-defined configuration exists.
- Never modifies `/etc/sudoers`, PAM configuration, or users' passwords.

An administrator may run the hook explicitly after editing rules:

```sh
/usr/libexec/lpm/hooks/user-groups
```

For an alternate root:

```sh
LPM_ROOT=/mnt/lpm-root /usr/libexec/lpm/hooks/user-groups
```

Rules granting membership in privileged groups (such as `wheel`, `docker`,
or `disk`) should be added only deliberately. Adding a user to `wheel`
does **not** itself configure sudo authorization. Use a separately reviewed
sudoers rule, edited with `visudo`. KDE screen access and audio commonly use
logind/udev ACLs and PipeWire, so global `video` or `audio` membership is
usually unnecessary.

A user generally must log out and back in to receive new supplementary
group credentials in their session.
