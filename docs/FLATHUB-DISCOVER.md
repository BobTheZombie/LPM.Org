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

## Verify in Discover

After installing rebuilt packages and restarting Discover:

1. `flatpak remotes --system` should list `flathub`.
2. `find /usr/lib -type f \\( -name 'flatpak-backend.so' -o -name 'packagekit-backend.so' \\)`
   should find **both** Discover plugins.
3. Open Discover and search for an app published on Flathub.
   Choose its Flathub source, then install and uninstall it.
4. Confirm native LPM software remains available from the PackageKit source.

If the Flatpak plugin fails to load, inspect
`QT_LOGGING_RULES='org.kde.plasma.libdiscover.backend.flatpak.debug=true' plasma-discover`
and ensure the user session has a working D-Bus and polkit agent. Native
PackageKit installs should continue to work even when Flatpak is offline.
