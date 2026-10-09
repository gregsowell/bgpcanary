# Releasing a new version (maintainer guide)

This is the checklist for publishing a release. Users get it with `sudo bgpcanary-update`, and admins see an
"update available" badge within a day.

## How the pieces fit

- **The version number** lives in one place: `__version__` in [`bgpcanary/__init__.py`](../bgpcanary/__init__.py).
- **A release** is a git tag `vX.Y.Z` plus a GitHub *Release* with notes. GitHub automatically attaches a
  source `.tar.gz` to every release, and that's exactly what `bgpcanary-update` downloads and installs. There
  is nothing to build.
- **[`.github/workflows/release.yml`](../.github/workflows/release.yml)** does the GitHub part for you. When
  you push a tag, it checks that the tag matches `__version__`, then publishes the release using that version's
  section of the changelog as the notes.
- **[`.github/workflows/ci.yml`](../.github/workflows/ci.yml)** runs on every push and pull request. It catches
  syntax errors in the Python, the shell scripts and the page templates.

## Choosing the next number

| You changed... | Bump | Example |
|---|---|---|
| Only bug fixes | patch | 1.0.0 → 1.0.1 |
| Added features; nothing existing breaks | minor | 1.0.1 → 1.1.0 |
| Something users must act on (a removed setting, a new OS requirement...) | major | 1.4.2 → 2.0.0 |

## Checklist

1. **Test on a real server.** Use a test VM, not production:
   - `deploy/push.sh user@testhost` installs your working tree there;
   - also test the upgrade path: install the *previous* release, then `sudo bgpcanary-update --from /path/to/new/source`.
2. **Update [CHANGELOG.md](../CHANGELOG.md):**
   - rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`;
   - add a fresh empty `## [Unreleased]` above it;
   - write the notes for users ("Fixed: alert emails had no link when…"), not for developers;
   - update the comparison links at the bottom.
3. **Bump the version** in `bgpcanary/__init__.py`: `__version__ = "X.Y.Z"`.
4. **Commit, tag and push:**

   ```bash
   git add -A
   git commit -m "Release X.Y.Z"
   git tag vX.Y.Z
   git push && git push --tags
   ```

5. **Check the result:**
   - the *Actions* tab shows the release workflow succeeded;
   - the *Releases* page shows **vX.Y.Z** with your notes;
   - on your own server, `sudo bgpcanary-update --check` reports it, and `sudo bgpcanary-update` installs it.

If the workflow fails because the tag and `__version__` don't match:
1. delete the tag (`git tag -d vX.Y.Z && git push --delete origin vX.Y.Z`);
2. fix the version and commit;
3. tag again.

## Fixing a bad release

**Never re-use a version number.** If X.Y.Z has a problem, release X.Y.(Z+1) with the fix. Anyone affected
can run `sudo bgpcanary-update --rollback` in the meantime. If a release is dangerous, mark it as a
*pre-release* on GitHub so the update check and `bgpcanary-update` stop offering it, then publish the fix.

## Rules that keep upgrades painless

- **Database:** only ever *add* tables or columns, and add the change to `init()` in `bgpcanary/db.py` so it runs automatically at startup. Never require users to run SQL.
- **Settings:** new settings need sensible defaults. If you rename or remove one, keep reading the old name for at least one minor version.
- **Installer:** `deploy/install.sh` must stay safe to re-run on an existing install. It's what every update runs.

## Pre-releases (optional)

To let volunteers try a version first:
1. tag it `vX.Y.Z-rc1`, with `__version__ = "X.Y.Z-rc1"`;
2. tick *Set as a pre-release* on the GitHub release.

Testers install it with `sudo bgpcanary-update --version X.Y.Z-rc1`. Nobody else is offered it.

## Licensing questions

The license is the Functional Source License (see [LICENSE.md](../LICENSE.md)). Each release becomes Apache 2.0
two years after its release date, so the date you publish matters. Commercial licensing requests (companies
wanting to resell or host it) come to you directly. Contributors agree to the terms in
[CONTRIBUTING.md](../CONTRIBUTING.md), which let you include their work in commercial licenses too.
