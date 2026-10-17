# Changelog

All notable changes are listed here, newest first. Versions follow [semantic versioning](https://semver.org/):
`MAJOR.MINOR.PATCH`, where patch releases only fix bugs, minor releases add features without breaking
anything, and major releases may need you to read the notes before upgrading.

## [Unreleased]

## [1.3.2] - 2026-10-16

### Fixed
- **The visibility history took far more disk space than it should.** Every 30-second bucket stored the prefix's upstream chains and upstream counts in full again, though they change in only about 1 bucket in 100. For a prefix that RIS peers reach through hundreds of different paths, that came to about 6 KB per bucket: 8–9 GB per prefix per year, where the docs said 0.5–1 GB. They're now stored only when they change (and once a day), and the charts and event pages show exactly what they did. For two such prefixes, 90 days of history now takes 0.1 GB instead of 4.3 GB, and the dashboard's charts load 4 to 14 times faster: 0.2 s instead of 1 s for the last 24 hours, and 5 s instead of a minute for both prefixes over 90 days.
  - History stored by older versions is compacted once, in the background, after the update: a few minutes for a big database. Then the database file is rewritten to give the space back, which pauses bgpcanary for a few seconds. That needs free disk space of about twice the compacted database; without it, the log says so and it's tried again at every start. See [UPGRADING.md](docs/UPGRADING.md#updating-to-132).
  - An older version still runs on the compacted database, but shows the upstream chains chart and the event pages' "who carried" table mostly empty. `sudo bgpcanary-update --rollback` restores the database from before the update.
- Looking up AS names for the charts read the whole visibility history every 5 minutes.
- The docs' disk figures: plan on roughly 0.1–0.3 GB per prefix per year.

## [1.3.1] - 2026-10-16

### Added
- **Exchange peers.** Networks you peer with at internet exchanges are now recognized and kept apart from your transit providers.
  - bgpcanary looks up the exchanges your ASN is on in PeeringDB once a day, and treats their members as peers. List private peers under Settings › Monitored network › Peers. Your expected upstreams always count as transit, even at the same exchange.
  - Peers no longer raise "unexpected upstream" warnings.
  - The dashboard lists them per exchange under your upstreams, and the prefix table shows "+N peers at …".
  - The connectivity tree has one **Exchanges** branch, split per exchange, with every peer shown. The tree images in alert emails do too.
  - The upstream chains chart and table show one "Peers at …" entry per exchange.
  - Upstream-shift detection and the transit filter check leave peers out.
- **New alert types** (group "Exchange peering"):
  - **Exchange peers lost** (warning): many of one exchange's peers stop carrying a prefix at once, by default half of them. That usually means your port, the exchange's route servers, or the exchange itself. A flapping session is one event: it closes once the peers have been back for 15 minutes.
  - **Peer leak** (warning): a peer carries a prefix to a large share of the internet, by default at least 10% of RIS peers and three times its usual reach. That usually means it's leaking your routes to its own transits or peers. Each peer's usual reach is learned first, so a big peer with many customers of its own isn't flagged. Like an outage, it triggers a Route Views snapshot, ping and traceroute for the evidence.
  - **New peer** (information).
- **"What is this network to you?"** Event pages for unexpected or new upstreams, new peers and peer leaks have two buttons: **One of my transit providers** and **A peer**. One click files the network, and the warning closes by itself, with no restart.
- Settings › Monitored network shows which exchanges PeeringDB lists for you, with a button to look them up again.

### Changed
- **The first start says what it's doing.** Before live monitoring begins, bgpcanary loads up to 90 days of history, and the pages now make that obvious:
  - a banner on every page shows the progress and an estimate;
  - the dashboard's figures are marked as replayed history;
  - the header's status dot is amber instead of red;
  - once the history is loaded, the dashboard switches to live data by itself.

  The banner also covers connecting to RIS Live (with a hint if that takes long), restarts, and history loading for prefixes added later.
- **Loading history is about 6× faster.** Baselines and path counts were worked out again for every 30-second step of the replay, even when nothing had changed. Now they're only redone after a change, with identical results. For two busy prefixes, 90 days of history took about 17 minutes and now take under 3. Live monitoring uses less CPU too.

### Fixed
- For a network at an exchange, the connectivity tree drew every peer as an upstream with up to 20 of its own neighbors. The tree was very tall and mostly empty, and the dashboard fetched each peer's neighbors from RIPEstat.
- The transit filter check treated exchange peers as upstreams. It queried RIPEstat for each one, then flagged their transits for not carrying your routes, which is exactly what a peer should do.
- Charts turned solid orange when many warnings were open at once: each open event added its shading on top of the others. Overlapping events now shade as one.
- The docs said live monitoring starts within seconds of the first setup, with history loading in the background. On a first start it begins once the history has loaded; the docs now say so.

## [1.3.0] - 2026-10-15

### Added
- **A container image**, `ghcr.io/gregsowell/bgpcanary`, for Docker and Podman on amd64 and arm64. See [docs/DOCKER.md](docs/DOCKER.md).
  - Alpine Linux, about 100 MB. It runs as an unprivileged user with no Linux capabilities and works on a read-only filesystem. Everything it keeps is in one volume.
  - On first start it creates the config file, with a random session key, and the initial admin account. The admin password goes to the container log.
  - Before a new version first starts, it backs up the database inside the volume, keeping the newest 3 backups.
  - Settings that need a restart restart the app inside the container, which keeps running.
  - Examples for Docker Compose, Caddy and nginx, and how to move a server install into a container.
  - Each release's image is built and tested by GitHub Actions on both architectures, with a signed build provenance.
- `bgpcanary-admin backup FILE` makes a consistent copy of the database while bgpcanary runs, readable only by its owner. `backup -` writes it to standard output.

### Changed
- The client address used by the login limits comes from `X-Real-IP` only when the connection is from the same machine (the bundled nginx). Otherwise it comes from `X-Forwarded-For`, and only when that's sent by a trusted proxy. Nothing changes for server installs behind the bundled nginx.
- If the login page is opened over plain HTTP, where the HTTPS-only sign-in cookie can't work, it now says so.
- Settings pages show where the config file actually is.

### Fixed
- `bgpcanary-admin` run without sudo printed its "run as root" message on standard output instead of standard error.

## [1.2.3] - 2026-10-14

### Security
- **Signing out now ends the session on the server,** so a copy of the session cookie stops working too. Sessions are tracked server-side: a password change, an admin's reset, a role change, "Sign out everywhere else" and deleting the account all end them, sessions not used for 7 days end, and everyone signs in again at least every 30 days. **After updating, everyone signs in once more** (cookies from earlier versions aren't accepted).
- **Webhook failures are recorded without anything from the webhook URL:** the HTTP status, or the kind of failure with a cleaned-up detail. This applies to the alert log, the settings and event pages, and the logs, however bgpcanary is run.
- **Monitored prefixes, like probe targets, must be plain prefixes,** and the Route Views client only ever sends a single printable command line.

### Fixed
- An outage that opened as a warning and then became critical sent no alert to "Critical only" subscribers (and no "escalated" notice to anyone). Escalations now go through the same per-type alert settings ([#1](https://github.com/gregsowell/bgpcanary/issues/1)).
- An outage that lasted longer than the baseline window (24 hours by default) could never end, even after every peer came back. After a restart during an outage, recovery could also be judged against the outage's own low readings. An outage now keeps the baseline it opened with until it recovers ([#2](https://github.com/gregsowell/bgpcanary/issues/2)).
- Turning RIPE Atlas off marked its periodic measurements stopped even when Atlas refused, and clearing the API key at the same time sent the stop requests without it. Measurements are now stopped with the key they were made with, recorded as stopped only once Atlas confirms, and anything not confirmed keeps counting against the daily credit cap, is retried, and is listed on the Atlas settings page ([#3](https://github.com/gregsowell/bgpcanary/issues/3)).
- IPv6 Route Views traceroutes that reached their target were recorded as not reaching it, with no last hop ([#4](https://github.com/gregsowell/bgpcanary/issues/4)).

## [1.2.2] - 2026-10-14

### Security
Fixes from an independent security review.
- **Backups moved to `/var/backups/bgpcanary` (root only).** They used to live in `/var/lib/bgpcanary/backups`, a directory the service account controls, where `bgpcanary-update`, `--rollback` and the installer worked as root. If the service account were ever compromised, it could have used that to reach other files on the server. The database is now read and restored as the service user, and root only writes into its own directory. Existing backups are moved automatically and keep their dates. (The update to 1.2.2 itself is still run by your installed updater. To use the new one for this update too, see [Upgrading](docs/UPGRADING.md#updating-to-122).)
- **Webhook URLs no longer appear in error messages.** A failed webhook's error text could include its URL (which is the secret for Slack and Teams), and it was visible to read-only users under Settings and on event pages. Errors now show only the HTTP status, and URLs already in the alert log are masked.
- **Changing a password signs that account out everywhere else.** So do an admin's password reset and role changes. There's also a new "Sign out everywhere else" button on the Change password page. A deleted account's old sessions don't carry over to a new account with the same name.
- **Stronger login throttling:**
  - attempts are counted before the password is checked, so sending many at once doesn't get around the limit;
  - IPv6 clients are counted per /64;
  - each account has its own limit (20 failures per 15 minutes), against guessing from many addresses. Browsers that have signed in to the account before ("known devices", remembered with a cookie) have their own budget instead, so someone guessing at a known username can't lock its owner out;
  - logins wait for a few password checks at a time, and are turned away when too many are waiting, so a flood can't stall the monitoring;
  - nginx also rate-limits login POSTs.
  If you're ever locked out, `sudo systemctl restart bgpcanary` clears the limits.
- **Requests from other sites are refused.** A change (a form POST) is only accepted from this site's own origin (scheme, host name and port), so forms on other sites, sibling subdomains or other ports of the same host are rejected. This adds to the SameSite cookies. If you reach bgpcanary through another proxy that rewrites the host name, set the Site URL (`[web] base_url`) to the address you use.
- **The service refuses to start with the example config's placeholder `secret_key`,** or one shorter than 32 characters, which would let anyone forge login cookies. Installer-generated keys are not affected.
- **Hardening:**
  - stricter systemd sandbox for the service;
  - a Content-Security-Policy, and the nginx version is hidden;
  - Route Views ping/traceroute targets must be plain IP addresses, and Atlas targets can't carry IPv6 zone suffixes;
  - the front-end libraries are checked against fixed SHA-256 checksums when installed;
  - Python dependencies are pinned to exact, hash-checked versions and installed from pre-built packages only;
  - GitHub Actions are pinned to commits;
  - `/etc/bgpcanary/initial-admin-password` is removed by the next install or update once its temporary password has been changed;
  - the RIPE Atlas probe installer no longer writes into the data directory as root;
  - URLs in logged errors are reduced to scheme and host name (credentials included);
  - the as-set lookup cache is bounded and thread-safe;
  - repeated refused logins are logged once per 15 minutes, not per attempt.

### Fixed
- Opening `/settings/alerts?form=…` returned an error page.

## [1.2.1] - 2026-10-14

### Fixed
- On a new install the 90-day history load could stop partway with "history load failed; continuing live" in the log (`cannot start a transaction within a transaction`), leaving charts with only part of their history. It happened when another startup task (Route Views snapshot, AS name lookup, update check) wrote to the database at the same moment as the history load. Writes are now properly serialized. Installs that were affected keep the history they have and record everything from then on as usual.
- Visibility charts shaded long-running informational notices (transit filter checks, filter simulations, chain changes) in blue, which could tint days of the chart. Only brief dips, warnings and critical events are shaded now, as the chart captions say.

### Added
- Screenshots in the README.

## [1.2.0] - 2026-10-14

### Changed
- **The dashboard follows a prefix picker** at the top of the page (shown when you monitor more than one prefix). It starts on **All**; tick some prefixes, or click "only" next to one, to see just those. The choice is in the page address (for example `/?prefix=192.0.2.0/24`), so a view can be bookmarked or shared.
  - **One prefix:** the same detail as before (status, visibility, connectivity tree, upstream chains, churn), for that prefix only.
  - **Several or all:** one combined view instead of a copy of every section per prefix: a status table with problems first, a single visibility chart with one line per prefix as a percentage of its own baseline (the 12 that dipped lowest get a line; any others share a "lowest of the others" line), a connectivity tree with one strand per prefix, upstream chains and churn summed, and the chains table combined with how many prefixes use each chain.
  - Recent events, brief dips and Route Views tables show the selected prefixes (plus network-wide events).
- The connectivity tree's own prefix checkboxes moved to the page-level picker. Visibility lines and tree strands use the same color for each prefix.

## [1.1.8] - 2026-10-14

### Changed
- The dashboard has one **Connectivity tree** that shows all your prefixes by default, with a checkbox per prefix (and "All") to focus on one or a few. Ticked prefixes each get their own colored strand, as the old "Compare prefixes" tree did, up to 6 by default; with more than 6 prefixes, a "Separate strands for up to" setting raises that to 8, 10 or 12 (remembered per browser), and beyond it they are combined into one strand. The per-prefix labels above the tree wrap onto more lines when they don't fit. This replaces the separate "Compare prefixes" tree and the per-prefix trees. The tree has its own time range buttons.

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

[Unreleased]: https://github.com/gregsowell/bgpcanary/compare/v1.3.2...HEAD
[1.3.2]: https://github.com/gregsowell/bgpcanary/compare/v1.3.1...v1.3.2
[1.3.1]: https://github.com/gregsowell/bgpcanary/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/gregsowell/bgpcanary/compare/v1.2.3...v1.3.0
[1.2.3]: https://github.com/gregsowell/bgpcanary/compare/v1.2.2...v1.2.3
[1.2.2]: https://github.com/gregsowell/bgpcanary/compare/v1.2.1...v1.2.2
[1.2.1]: https://github.com/gregsowell/bgpcanary/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/gregsowell/bgpcanary/compare/v1.1.8...v1.2.0
[1.1.8]: https://github.com/gregsowell/bgpcanary/compare/v1.1.7...v1.1.8
[1.1.7]: https://github.com/gregsowell/bgpcanary/compare/v1.1.6...v1.1.7
[1.1.6]: https://github.com/gregsowell/bgpcanary/compare/v1.1.5...v1.1.6
[1.1.5]: https://github.com/gregsowell/bgpcanary/compare/v1.1.4...v1.1.5
[1.1.4]: https://github.com/gregsowell/bgpcanary/compare/v1.1.3...v1.1.4
[1.1.3]: https://github.com/gregsowell/bgpcanary/compare/v1.1.2...v1.1.3
[1.1.2]: https://github.com/gregsowell/bgpcanary/compare/v1.1.1...v1.1.2
[1.1.1]: https://github.com/gregsowell/bgpcanary/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/gregsowell/bgpcanary/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/gregsowell/bgpcanary/releases/tag/v1.0.0
