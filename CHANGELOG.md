# Changelog

All notable changes are listed here, newest first. Versions follow [semantic versioning](https://semver.org/):
`MAJOR.MINOR.PATCH`, where patch releases only fix bugs, minor releases add features without breaking
anything, and major releases may need you to read the notes before upgrading.

## [Unreleased]

## [1.1.7] - 2026-10-14

### Fixed
- Long unbroken text (for example a long as-set name in an event title or a "Related events" link) made the event page wider than the screen, especially on phones. It now wraps.
- After an update, browsers could keep using the previous version's stylesheet and scripts until a hard refresh, so layout fixes didn't show. Each release now loads its own copies.

## [1.1.6] - 2026-10-14

### Fixed
- On an event page, long details or a registry object pushed the text past the panel and under "Alerts sent", cutting it off. The details panel now takes two thirds of the width, wraps long text, and scrolls a long registry object sideways instead of overflowing.
- Conflicting route objects generated from RPKI ROAs showed "(maintainer )" and said to ask the maintainer to delete them. They now explain that the object comes from a ROA, which the address holder changes in their RIR's RPKI portal. Open registry events get the corrected wording at the next registry check.

## [1.1.5] - 2026-10-13

### Added
- Microsoft Teams webhooks: choose the **Microsoft Teams** format under Settings › Alerts › Webhooks and paste the URL from a channel's "Send webhook alerts to a channel" workflow. Alerts arrive as cards with a colored status line, the event details and an "Open in bgpcanary" button. Step-by-step instructions are on the settings page.

### Changed
- The help for the generic JSON format no longer points Teams users to a hand-built Power Automate flow.

## [1.1.4] - 2026-10-13

### Fixed
- Connectivity trees were a very long, empty scroll when an upstream has thousands of neighbours (for example Hurricane Electric, AS6939, with over 8,000): every neighbour got its own row and the tree became too tall for the browser to draw. Each upstream now always shows up to 20 neighbours, major transit networks first; any other neighbour still appears when it carries your routes, and the rest are folded into "+N more". This also keeps alert-email tree images, the Registry page and RIPE Atlas probe selection to a sensible size.
- Very tall trees are drawn at a lower resolution instead of not at all.

## [1.1.3] - 2026-10-12

### Fixed
- `bgpcanary-admin test-alert` with no alert channels enabled now says so and how to add one, instead of printing `none configured: FAILED`; it also exits with an error when any channel fails, so scripts can tell.
- `bgpcanary-update --check` run without sudo quietly exited with an error; it now does the check.
- `bgpcanary-admin useradd` with the password piped in (from a script) no longer prints a warning about the password being shown.

## [1.1.2] - 2026-10-11

### Fixed
- A syntax error in `/etc/bgpcanary/config.toml` (for example a list missing its `]`) now produces a clear message that shows the lines involved and how to fix them, from `bgpcanary-admin`, the installer and the service log, instead of a Python traceback.
- Re-running the installer or `bgpcanary-update` with a broken config file now stops before changing anything, instead of restarting the service into an error.

### Changed
- The installer accepts lists separated by commas or spaces (`--prefix "192.0.2.0/24, 2001:db8::/48"`, `--upstream "64501 64502"`) and rejects invalid ASNs, prefixes and probe addresses up front.

## [1.1.1] - 2026-10-11

### Fixed
- On a fresh install (before the ASN and prefixes were set up), the required first-login password change never saved and the page kept coming back. Existing installs that already completed setup weren't affected.
- The installer's `initial-admin-password` file now says that editing it doesn't change the password, and how to set a new one.

## [1.1.0] - 2026-10-11

### Added
- A small "Support The Brothers WISP on Patreon" line under the header (dismissible for 90 days per browser), a bold "Project Support" link in the navigation, and a support section on the About page.

### Changed
- The default site name (shown in the header and alert subjects) is now "BGP Canary". Existing installs keep whatever name they have; change it under Settings › Monitored network.

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

[Unreleased]: https://github.com/gregsowell/bgpcanary/compare/v1.1.7...HEAD
[1.1.7]: https://github.com/gregsowell/bgpcanary/compare/v1.1.6...v1.1.7
[1.1.6]: https://github.com/gregsowell/bgpcanary/compare/v1.1.5...v1.1.6
[1.1.5]: https://github.com/gregsowell/bgpcanary/compare/v1.1.4...v1.1.5
[1.1.4]: https://github.com/gregsowell/bgpcanary/compare/v1.1.3...v1.1.4
[1.1.3]: https://github.com/gregsowell/bgpcanary/compare/v1.1.2...v1.1.3
[1.1.2]: https://github.com/gregsowell/bgpcanary/compare/v1.1.1...v1.1.2
[1.1.1]: https://github.com/gregsowell/bgpcanary/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/gregsowell/bgpcanary/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/gregsowell/bgpcanary/releases/tag/v1.0.0
