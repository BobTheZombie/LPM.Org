# Automatic GitLab recipe refresh

By default, LPM checks GitLab for updated .lpmbuild recipes once per buildpkg
invocation. The recipe checkout remains cached across SAT dependencies.

- Managed Git snapshots bypass their one-hour TTL on the first recipe request.
- Operator-managed checkouts fast-forward with `git pull --ff-only`.
- Dirty local checkouts are not overwritten. Offline mode uses cached files.
- Changed recipes restart only their own build checkpoint, not all packages.
- Explicit `buildpkg --resume` of a changed root recipe remains protected.

To disable automatic remote checks, set in `/etc/lpm/lpm.conf`:

```ini
LPMBUILD_RECIPE_AUTO_REFRESH=false
```

For builds offline, use `LPMBUILD_RECIPE_TRANSPORT=offline`.

Build the updated LPM executable (including Nuitka deployments) before
expecting these changes to take effect.
