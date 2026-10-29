# Cached Git recipes and GitLab 429 mitigation

LPM defaults to a persistent shallow checkout under
\`/var/lib/lpm/cache/recipes\`. It refreshes no more than once per hour and
uses the previous checkout if Git fetch fails.

To use an operator-managed checkout immediately:

\`\`\`sh
mkdir -p /var/cache/lpm/git
git clone --depth 1 --branch main \
  https://gitlab.com/lpm-org/packages.git /var/cache/lpm/git/packages
lpm buildpkg plasma6-full --resume
\`\`\`

This checkout is read-only to LPM. To update it, use
\`git -C /var/cache/lpm/git/packages pull --ff-only\`.

Settings in \`/etc/lpm/lpm.conf\`:

\`\`\`ini
LPMBUILD_RECIPE_TRANSPORT=git
LPMBUILD_RECIPE_LOCAL_PATH=/var/cache/lpm/git/packages
LPMBUILD_RECIPE_REFRESH_SECONDS=3600
# LPMBUILD_RECIPE_GIT_URL=git@gitlab.com:lpm-org/packages.git
\`\`\`

Transport modes:
- \`git\` (default): use operator-managed or managed checkout; never fall back
  to GitLab raw/API requests when Git cloning fails.
- \`offline\`: only existing checked-out recipes; no network requests.
- \`hybrid\`: Git first, opt-in raw/API fallback (429 cooldown still applies).
- \`http\`: legacy GitLab API and per-recipe HTTP requests only.

Use Git's credential helper or SSH keys to authenticate; don't put passwords
or access tokens in repository URLs. The SAT solver is unchanged.


## Companion files in SOURCE and PATCHES

When a Git snapshot supplies a recipe, relative SOURCE entries and PATCHES
(for example, \`perl/detect-old-perl-modules.hook\`) are read from that same
snapshot instead of GitLab's unauthenticated raw HTTP endpoint. Aliased
sources (\`new-name::original-file\`) retain their requested staged names.
Missing companion files fail with a clear local-checkout error in \`git\` or
\`offline\` mode; \`hybrid\` explicitly allows the previous HTTP fallback.
External upstream tarballs still use the ordinary source download/cache.
