# Releasing a new version (maintainer guide)

This is the checklist for publishing a release. Users get it with `sudo bgpcanary-update` or by pulling the new
container image, and admins see an "update available" badge within a day.

## How the pieces fit

- **The version number** lives in one place: `__version__` in [`bgpcanary/__init__.py`](../bgpcanary/__init__.py).
- **A release** is a git tag `vX.Y.Z` plus a GitHub *Release* with notes. GitHub automatically attaches a
  source `.tar.gz` to every release, and that's exactly what `bgpcanary-update` downloads and installs.
- **[`.github/workflows/release.yml`](../.github/workflows/release.yml)** does the GitHub part for you. When
  you push a tag, it checks that the tag matches `__version__`, then publishes the release using that version's
  section of the changelog as the notes. There's nothing to build by hand.
- **The container image** is built by the same workflow, before the release is published. It's built for amd64
  and arm64 and tested on both with [`deploy/docker/smoke-test.sh`](../deploy/docker/smoke-test.sh). Then it's
  pushed to `ghcr.io/gregsowell/bgpcanary` as `X.Y.Z`, `X.Y`, `X` and `latest` (a pre-release gets only its own
  tag), with a signed build provenance. If the image fails, no release is published.
- **[`.github/workflows/ci.yml`](../.github/workflows/ci.yml)** runs on every push and pull request. It catches
  syntax errors in the Python, the shell scripts and the page templates. It also builds the image and runs the
  smoke test, the Compose example, the documented backup and restore commands, and the Caddy example.

## Choosing the next number

| You changed... | Bump | Example |
|---|---|---|
| Only bug fixes | patch | 1.0.0 → 1.0.1 |
| Added features; nothing existing breaks | minor | 1.0.1 → 1.1.0 |
| Something users must act on (a removed setting, a new OS requirement...) | major | 1.4.2 → 2.0.0 |

## Checklist

1. **Dependencies:** `requirements.txt` pins exact versions with hashes, and the installer refuses anything else.
   To pick up newer libraries, install from `requirements.in` on a test server, test, then regenerate the lock
   from that server:
   `ssh testhost '/opt/bgpcanary/venv/bin/pip freeze' > /tmp/freeze.txt && python3 deploy/lock-requirements.py /tmp/freeze.txt > requirements.txt`.
2. **Base image:** the [Dockerfile](../Dockerfile) pins `python:3.13-alpine3.24` by digest. To pick up the latest
   Alpine and Python fixes, refresh the digest each release: `docker buildx imagetools inspect python:3.13-alpine3.24`
   prints the current one (the `Digest:` line); put it in the `ARG BASE=` line. To move to a newer Alpine or Python
   version, first check that every native package in `requirements.txt` has a `musllinux` wheel for it on PyPI.
   The build only uses pre-built wheels, so it fails rather than compiling anything.
3. **Test on a real server.** Use a test VM, not production:
   - `deploy/push.sh user@testhost` installs your working tree there;
   - also test the upgrade path: install the *previous* release, then `sudo bgpcanary-update --from /path/to/new/source`;
   - and the image: `docker build -t bgpcanary:test . && deploy/docker/smoke-test.sh bgpcanary:test`. CI runs that
     on every push too.
4. **Update [CHANGELOG.md](../CHANGELOG.md):**
   - rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`;
   - add a fresh empty `## [Unreleased]` above it;
   - write the notes for users ("Fixed: alert emails had no link when…"), not for developers;
   - update the comparison links at the bottom.
5. **Bump the version** in `bgpcanary/__init__.py`: `__version__ = "X.Y.Z"`.
6. **Commit, tag and push:**

   ```bash
   git add -A
   git commit -m "Release X.Y.Z"
   git tag vX.Y.Z
   git push && git push --tags
   ```

7. **Check the result:**
   - the *Actions* tab shows the release workflow succeeded;
   - the *Releases* page shows **vX.Y.Z** with your notes;
   - on your own server, `sudo bgpcanary-update --check` reports it, and `sudo bgpcanary-update` installs it;
   - `docker pull ghcr.io/gregsowell/bgpcanary:X.Y.Z` works without signing in to GitHub. The first time an image is
     published, GitHub may create the package as *private*. If so, open the package (your profile › *Packages* ›
     bgpcanary › *Package settings*) and change its visibility to *Public* once. Later releases keep that.

If the workflow fails because the tag and `__version__` don't match:
1. delete the tag (`git tag -d vX.Y.Z && git push --delete origin vX.Y.Z`);
2. fix the version and commit;
3. tag again.

## Fixing a bad release

**Never re-use a version number.** If X.Y.Z has a problem, release X.Y.(Z+1) with the fix. Anyone affected
can run `sudo bgpcanary-update --rollback` in the meantime, or run the previous image tag. The image's `latest`,
`X` and `X.Y` tags move on to the fix when it's released. If a release is dangerous, mark it as a
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
