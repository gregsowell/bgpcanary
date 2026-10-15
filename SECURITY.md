# Security policy

## Reporting a vulnerability

Please **don't open a public issue** for security problems. Report them privately through GitHub:
**Security → Report a vulnerability** on [the repository](https://github.com/gregsowell/bgpcanary/security/advisories/new).

Include what you found, how to reproduce it, and what an attacker could do with it. You'll get a reply within
a few days. Fixes are released as a patch version, and the advisory is published once users have had time to
update. You'll be credited unless you'd rather not be.

## Supported versions

Security fixes go into the latest release. Update with `sudo bgpcanary-update`, or pull the latest container
image (`docker compose pull && docker compose up -d`).

## How bgpcanary is built to be safe

- **Unprivileged service:** the app runs as an unprivileged `bgpcanary` user under systemd sandboxing (`NoNewPrivileges`, read-only system, private /tmp), behind nginx. It never needs root at run time; installing and updating are done on the server with sudo.
- **Container image:** runs as an unprivileged user (uid 10001) with no Linux capabilities, and works on a read-only root filesystem. Images are built from the tagged source by GitHub Actions and carry a signed build provenance: `gh attestation verify oci://ghcr.io/gregsowell/bgpcanary:<version> --owner gregsowell`.
- **Passwords:** hashed with scrypt; login attempts are throttled; the first admin password is random and must be changed at first login.
- **Secrets:** API keys and SMTP passwords are stored server-side and never shown back in the web UI.
- **Outbound access only:** it only talks to the public data sources listed in the README, plus GitHub for the daily update check, which can be switched off. Nothing about your installation is sent anywhere.
