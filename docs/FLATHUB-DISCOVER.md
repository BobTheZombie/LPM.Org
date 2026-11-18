# Flathub and KDE Discover integration

LPM Linux supports **two independent application systems** in KDE Discover:

- **Native LPM packages**: Discover's PackageKit plugin talks to the LPM backend.
  The LPM SAT resolver, signatures and installed-package database remain authoritative.
- **Flatpak apps**: Discover's Flatpak plugin talks directly to Flatpak/libflatpak
  and OSTree. Flatpak does not go through LPM's PackageKit backend.

## Install the integration

Build the `flatpak`, `flatpak-flathub`, `packagekit`, `packagekit-qt6`,
and `discover` recipes in LPMBUILDS. The `discover` build now requires and
verifies both backend plugins. `plasma6-full` includes the integration.

The existing `flatpak-flathub` recipe installs the official Flathub
`flathub.flatpakrepo` under `/usr/share/flatpak/remotes.d/`. Flatpak reads
that directory for system-wide static remotes. To configure a user-specific
Flathub remote, run `lpm flatpak setup --user`. The setup operation is
idempotent and retains existing remotes.

## LPM command-line frontend

Installed Flatpak applications also appear in the regular native inventory command:

```sh
lpm list                   # Native LPM packages + all visible Flatpak apps
lpm list --native          # Native LPM SQLite inventory only
lpm list --flatpak         # Flatpak apps only (current user + system installs)
```

Flatpak rows have a `flatpak:` prefix and show their installation
(`user`, `system`, or a named installation) and origin remote. The combined
listing is read-only and does not insert Flatpaks into LPM's SQLite database.
Use `lpm list` as the desktop user to include user-installed Flatpaks;
running it with `sudo` would instead inspect root's per-user installation.

```sh
lpm flatpak remotes
lpm flatpak search firefox
lpm flatpak install org.mozilla.firefox
lpm flatpak list
lpm flatpak info org.mozilla.firefox
lpm flatpak update
lpm flatpak remove org.mozilla.firefox
lpm flatpak run org.mozilla.firefox
```

Operations use the system installation by default (polkit may prompt).
Add `--user` after an action to use a per-user installation, for example
`lpm flatpak install --user org.kde.krita`. Add `--yes` to install, update
or remove to skip normal Flatpak confirmation prompts. Flatpak retains its own
signature verification, sandbox permissions, application lifecycle and updates.
This command family does not mutate the native LPM package database.

To use the explicit PackageKit bridge, run `pkcon resolve flatpak:org.mozilla.firefox`.
Discover itself uses its dedicated Flatpak backend to prevent duplicate entries.

## Short native-style Flatpak commands

The `flatpak:` prefix explicitly routes operations to Flatpak, not LPM's
SAT solver or the native installed-package database:

```sh
lpm search --flatpak firefox
lpm install flatpak:org.mozilla.firefox
lpm remove flatpak:org.mozilla.firefox
lpm update --flatpak
lpm update --flatpak --user
```

The install/remove shorthand targets the system installation and accepts
`--yes` for noninteractive confirmation. To use per-user installation,
run `lpm flatpak install --user APP_ID` or
`lpm flatpak remove --user APP_ID`. Mixed native + Flatpak
install/remove transactions and native-only transaction flags such as
`--root` are deliberately rejected. For native updates keep using
`lpm upgrade`; the `lpm update --flatpak` shorthand only updates Flatpak.

## Verify in Discover

After installing rebuilt packages and restarting Discover:

1. `flatpak remotes --system` should list `flathub`.
2. `find /usr/lib -type f \( -name 'flatpak-backend.so' -o -name 'packagekit-backend.so' \)`
   should find **both** Discover plugins.
3. Open Discover and search for an app published on Flathub.
   Choose its Flathub source, then install and uninstall it.
4. Confirm native LPM software remains available from the PackageKit source.

If the Flatpak plugin fails to load, inspect
`QT_LOGGING_RULES='org.kde.plasma.libdiscover.backend.flatpak.debug=true' plasma-discover`
and ensure the user session has a working D-Bus and polkit agent. Native
PackageKit installs should continue to work even when Flatpak is offline.
