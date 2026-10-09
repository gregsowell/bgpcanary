# Changelog

All notable changes are listed here, newest first. Versions follow [semantic versioning](https://semver.org/):
`MAJOR.MINOR.PATCH`, where patch releases only fix bugs, minor releases add features without breaking
anything, and major releases may need you to read the notes before upgrading.

## [Unreleased]

## [1.0.0] - 2026-10-09

First public release.

### Added
- Real-time visibility monitoring from RIPE RIS Live, with 90 days of history from RIPEstat BGPlay and automatic gap filling.
- Connectivity tree with time slider, playback, GIF export, multi-prefix comparison and fit-to-screen sizing.
- Detection of:
  - outages and brief dips;
  - origin hijacks, more-specific and covering announcements, and your ASN in someone else's path;
  - missing or unexpected upstreams and upstream shifts;
  - reachability loss, using RIPE Atlas.
- Transit filter check and registry watch (IRR, as-sets, RPKI, simulated upstream filters).
- Route Views snapshots, ping and traceroute, on a schedule and on events, within Route Views' limits.
- Optional RIPE Atlas integration:
  - baseline measurements and outage / shift bursts;
  - radial traceroute maps, per-carrier latency and loss, multi-target comparison, one-off tests;
  - credit budgeting, and installing a software probe from the installer.
- Event pages with a consolidated evidence section and a plain-text evidence report for providers.
- Email and webhook (Slack / JSON) alerts with per-alert-type notification settings and thresholds.
- Admin and viewer roles; all settings in the web UI.
- Installer for Rocky / AlmaLinux / RHEL 9 and 10, `bgpcanary-update` with backups and rollback, `bgpcanary-uninstall`, and an in-app update notice.

[Unreleased]: https://github.com/gregsowell/bgpcanary/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/gregsowell/bgpcanary/releases/tag/v1.0.0
