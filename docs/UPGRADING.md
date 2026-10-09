# Upgrading bgpcanary

## Knowing when there's an update

- Admins see a small **"update x.y.z"** badge in the web UI header when a newer release is published. **Settings › About & updates** shows what's new. bgpcanary checks GitHub once a day; you can switch that off on the same page.
- On the server: `sudo bgpcanary-update --check`
- On GitHub: *Watch → Custom → Releases* on [the repository](https://github.com/gregsowell/bgpcanary) to get an email for every release.

Version numbers follow `MAJOR.MINOR.PATCH`:

| Kind | Example | Meaning |
|---|---|---|
| Patch | 1.0.0 → 1.0.1 | Bug fixes only. Always safe. |
| Minor | 1.0.x → 1.1.0 | New features. Existing settings and data keep working. |
| Major | 1.x → 2.0.0 | Something you rely on may change. Read the release notes first. |

## Updating

```bash
sudo bgpcanary-update
```

That's all. It:
1. finds the latest release on GitHub;
2. **backs up** the database to `/var/lib/bgpcanary/backups/before-update-<version>-<date>.db`, and the settings in `/etc/bgpcanary` to a `.tar.gz` next to it;
3. downloads and installs the new release. Your configuration, data, users, alert settings and certificate are kept, and any database changes the new version needs happen automatically when it starts;
4. waits for the service to report the new version, and tells you if it doesn't.

The web UI is unavailable for about 10–30 seconds while the service restarts. Monitoring continues where it
left off, and any BGP updates missed during the restart are filled in from RIPE's archive a few hours later.

### Other options

```bash
sudo bgpcanary-update --version 1.2.0              # install a specific release (newer or older)
sudo bgpcanary-update --from bgpcanary-1.2.0.tar.gz   # install from a downloaded file (no internet needed)
sudo bgpcanary-update --from /path/to/source         # install from a source directory, e.g. a git checkout
```

With a git checkout you can also update by hand: `git pull && sudo ./deploy/install.sh`. Re-running the
installer is always safe, and it takes a database backup too.

## Rolling back

If something is wrong after an update:

```bash
sudo bgpcanary-update --rollback
```

This restores the previous version's code **and** the database from just before the update. Anything recorded
since the update is set aside, not deleted, in `/var/lib/bgpcanary/backups/rolled-back-from-*.db`. Only the most recent update can be rolled back this way.
To go further back, use `--version` with the older release number. Please also
[open an issue](https://github.com/gregsowell/bgpcanary/issues) describing what went wrong.

## Backups

- Updates keep the **last three** database backups and five settings archives in `/var/lib/bgpcanary/backups/`.
- For your own regular backups, copy the database safely while it's running:

  ```bash
  sudo sqlite3 /var/lib/bgpcanary/bgpcanary.db ".backup '/root/bgpcanary-$(date +%F).db'"
  ```

  Keep `/etc/bgpcanary/` too, because it contains the session key and initial settings.
- To restore one:

  ```bash
  sudo systemctl stop bgpcanary
  sudo cp /path/to/backup.db /var/lib/bgpcanary/bgpcanary.db
  sudo rm -f /var/lib/bgpcanary/bgpcanary.db-wal /var/lib/bgpcanary/bgpcanary.db-shm
  sudo chown bgpcanary:bgpcanary /var/lib/bgpcanary/bgpcanary.db
  sudo systemctl start bgpcanary
  ```

## Moving to a new server

1. On the old server, take a backup (above) and copy it plus `/etc/bgpcanary/config.toml` to the new one.
2. Install bgpcanary on the new server with the same `--fqdn`.
3. Stop the service, put the database and config in place (owner `bgpcanary`, config group `bgpcanary`), and start it again.

## Coming from "bgpmon"

bgpcanary was called *bgpmon* before its first public release. Running the bgpcanary installer or
`bgpcanary-update` on a bgpmon server migrates it automatically: data, settings, users, certificate and host
name are moved to the new names (`/opt/bgpcanary`, `/var/lib/bgpcanary`, `/etc/bgpcanary`, `bgpcanary.service`,
`bgpcanary-admin`).
