# Changelog

All notable changes are listed here, newest first. Versions follow [semantic versioning](https://semver.org/):
`MAJOR.MINOR.PATCH`, where patch releases only fix bugs, minor releases add features without breaking
anything, and major releases may need you to read the notes before upgrading.

## [Unreleased]

## [1.7.0] - 2026-10-29

### Added
- **A JSON API for your own scripts and dashboards** (`/api/v1/`), read-only, with per-user tokens: the monitor's status with each prefix's visibility, state and neighbours and how many events are open; the events with the Events page's filters, a prefix or a time window; one event with the alerts sent for it and its text report; and a neighbouring network with what it is to you and the events about it. Tokens are created under **Settings › API tokens** (shown once; only a hash is kept), carry their owner's role, and can be revoked there or, by an admin, on the Users page. The shapes are a promise: within v1 a field is only ever added. Documented in [docs/API.md](docs/API.md).
- **Tests** (`tests/`): the first automated tests, run by CI on every push. They hold the API's contract.

### Fixed
- **The tab icon and title follow the outage state again.** Since 1.3.1 the "not live yet" banner's poll had taken over the `/api/status` address that the once-a-minute check used, so a tab left open kept the plain icon and title when an outage began (and the alert ones after it ended) until the page was reloaded. The banner polls `/api/loading` now, and `/api/status` answers with the monitor's status and each prefix's summary, as before.

### Changed
- README: screenshots of the Path finder (the tree and the traceroute), BGP data (Paths and Changes), the Events page with its filters, an upstream-down event, several events replayed together and a neighbouring network's page; the connectivity tree screenshot now shows the "moved" and "reaches you another way" states.

## [1.6.3] - 2026-10-28

### Added
- **A page for each neighbouring network** (`/neighbours/<ASN>`): what it is to you with the one-click answer (**Add as a peer**, or one of your transits), how many RIS peers reach you through it right now, and the events about it. The dashboard's "unexpected" badge leads there, so filing a network as a peer no longer needs an open event.

### Changed
- **"What is this network to you?"** now explains why a network you don't peer with directly can appear as your neighbour (route servers don't add their own ASN to the path, and some exchanges pass route-server routes on to partner exchanges), is shown to viewers too (the buttons stay admin-only), and its peer button reads **Add as a peer**.

## [1.6.2] - 2026-10-28

### Added
- **Events page: open or closed.** A new first filter shows only the events still going on (no end yet) or only the ones that have ended, alone or together with the type and severity filters. The filters apply as soon as you change one; the count says what you're looking at ("3 open events"). Coming back to the page shows the filters its address says, not the ones the browser remembers picking. The dashboard's recent-events panel links to the open ones.
- **Events page: a type in the list is clickable** and filters to that type, keeping the other filters.

### Changed
- **Container image:** the base image (`python:3.13-alpine3.24`) refreshed to its current build for the latest Alpine and Python fixes.

## [1.6.1] - 2026-10-27

### Fixed
- **The same warning again after every RIS Live reconnect, marked "(backfill)".** The gap filler replayed each gap in an engine that knew nothing of what was already open, so an unexpected upstream, an outage or a shift that existed before the gap was reported again as a short backfill event every time the feed dropped, several times a day on some networks. It now knows what the live engine has open and what the gap's starting state shows, and reports only what begins inside the gap. Events already recorded stay as they are.

### Changed
- **BGP data › Paths:** each way in is plainly expandable now: a chevron that turns when open, a highlight under the pointer, and a hint in the heading. Opening one shows the next network out and the peers behind it, as before.

## [1.6.0] - 2026-10-27

### Added
- **BGP data page**, replacing Paths and Updates (the old addresses redirect to it):
  - *Paths*: the RIS peers' current paths grouped by the way in (transit → upstream), then by the next network out, with each way's share, how many Route Views collectors see it, and the peers that joined or left it this week; the peers under each, with their last change. 250 distinct paths become three ways in.
  - *Changes*: one row per RIS peer whose path changed, old → new with the changed hops marked, grouped into bursts with a summary ("152 moved from … to …; 34 withdrawn"), the events that overlapped, and a link to the connectivity tree at that moment. The rows that only correct the stored state (the looking glass's resync, seed rows after a restart, stale withdrawals) are hidden unless asked for.
  - *Raw updates*: the update log as before. *Route Views tables*: the collectors' "show ip bgp", the snapshot history, and today's use of their daily limits.
- **Reachability page**, replacing RIPE Atlas and Route Views (the old addresses redirect): the Atlas views, with the Route Views collectors' ping and traceroute below them. The same question, does traffic arrive and where does it stop, from both sets of vantage points.
- The dashboard's connectivity tree can be opened around a moment (`/?prefix=…&at=<unix time>`); the Changes tab links there.

### Changed
- **Upstream shift alerts say who moved.** The move behind a shift is usually one network behind your upstreams changing which way it reaches you. The alert now names it ("Behind it: AS64510 switched from via AS64520 to via AS64530 (180 peers)"), so does the closing alert when the mix stays, and the event's evidence and plain-text report carry it.
- **The tree tells "moved" from "dropped".** A network that stops carrying you under one upstream and carries the same under another has moved: faint blue, "moved to via AS64502", and its new branch says "moved from via AS64501", instead of a red "dropped" and a green "new" for the same network. The alert-email images follow.
- **Exchange-peers-lost alerts say where the peers went**: how many still reach you through your transit (your side of the exchange) and how many carry you nowhere now (their side, or your route filtered further on), naming them, instead of assuming they all use your transit now.
- The transit filter check no longer judges a neighbour that has an open "unexpected upstream" warning (a leak, or a neighbour you haven't filed yet); it's judged once it's filed as a transit.
- An outage's Route Views check says "too few to tell" when Route Views normally holds fewer than five paths for the prefix, instead of "probably RIPE's data" (RIPE Atlas decides when it's set up).
- The Events list shows alert types by name, the prefix, the lowest visibility as n / N (%), and a line between days.
- The event page's raw BGP updates table is collapsed, with a link to the changes around the event. The Route Views budget table moved from Paths to the dashboard's Data sources panel.
- The nav: Dashboard · Events · Path finder · BGP data · Reachability · Registry · Settings.

## [1.5.8] - 2026-10-27

### Changed
- **The transit filter check and the Registry page read "not carrying you" the way the tree does.** Both treated every transit of an upstream that carries none of your paths straight from it as a gap, and the Registry table explained each one as a filter. A transit that reaches you another way, your own neighbour or a network behind another upstream of yours, prefers that path and passes on only its best route, so that said nothing. Now:
  - the Registry table says how it reaches you instead ("via AS64502", "directly") and that the pair's filters can't be seen from there;
  - the check no longer opens a notice for those (open ones close by themselves, two checks after the update);
  - a transit that carries you only the long way round, through that upstream but not straight from it, although it carries the upstream's own prefixes straight from it, is still reported, and the notice, the table and the tree's tooltip say so: the direct path would normally win, so your prefix is probably not accepted on that session;
  - where RIS barely sees the pair at all, even for the upstream's own prefixes, the table says it can't tell.

## [1.5.7] - 2026-10-26

### Changed
- **Events replayed together keep only the time that matters.** The replay kept five minutes around each event and everything between events less than ten minutes apart, so most of it was quiet. Now it keeps a minute around each event and the moments when a path actually changed (with 45 seconds either side), and cuts every quiet stretch longer than a minute and a half.
- **The connectivity tree's faint states are told apart by pattern as well as colour.** *Dropped* is red dashed and thick, *not carrying you* red dotted, *reaches you another way* blue dotted (it does carry you, just not through here), *no longer peered* grey dash-dot. The alert-email images follow.
- **Upstream neighbours are shown in full by default.** 1.5.6 folded the ones that reach you another way into one line per upstream; that's now the opt-in *fold* choice of the *Upstream neighbours* control, off by default.

### Fixed
- The tree's tooltips were cut off at the right and bottom of the panel; they wrap now and stay inside it.

## [1.5.6] - 2026-10-26

### Changed
- **The connectivity tree's "not carrying you" means it now.** Under each of your upstreams the tree shows that upstream's own transits and peers, and painted every one that carries none of your paths through it red, "not carrying you". Most of those have a path to you they prefer, your own peer for one, so they never route to you through that upstream, and the red was misleading. Now:
  - a neighbour is red only when RIS sees it carrying you nowhere, and the label says through which upstream ("not carrying you via AS64501");
  - one that's your own neighbour, or on your paths some other way, is grey: "reaches you directly" / "reaches you via AS64502". By default these are folded into one line per upstream, "+12 reach you another way" (hover for the list);
  - a new *Upstream neighbours* control next to Depth: problems only (the default), all, or none;
  - the tooltip of a red one adds what the transit filter check knows about that pair: whether the transit carries the upstream's own prefixes widely (then a filter is likely) or RIS barely sees the pair at all.

  The same goes for the tree images in alert emails.

## [1.5.5] - 2026-10-26

### Added
- **Upstream down alert.** When one of your upstreams stops carrying almost all of a prefix's paths while you stay visible through the others, bgpcanary now says so, and what changed behind it, instead of a bare "upstream shift":
  - which of the upstream's own upstreams stopped carrying you on from it;
  - which of its other transits don't carry your prefix (from the transit filter check). Without them your routes had nowhere else to go through it, and the alert says what to ask your upstream for;
  - whether its own network is still reachable, from RIPEstat's looking glass (the alert waits a few seconds for it): gone dark too ("it seems to have lost all of its upstreams"), moved to transit that won't carry you, or unchanged (more likely your link to it).

  It's judged against the upstream's typical share of your paths (the median over 6 hours), so a backup upstream going back to normal after carrying everything for a while isn't an alarm. It replaces the upstream-shift alert for that move and the move back, and closes once the upstream has carried you again for 10 minutes. On by default (Upstreams › Upstream down); its threshold is under Settings › Detection & data sources (10% of the typical share; 0 turns it off).
- **Events replayed together.** Tick two or more events on the Events page and choose *Replay selected together* for one path-tree replay of them, oldest first, with the quiet time between them cut out. ✂ on the timeline marks each cut and how much it skipped; previous/next-event buttons jump between them, and GIF export works as usual. Events on different prefixes share the tree, one strand each.

## [1.5.4] - 2026-10-26

### Changed
- **Event pages show the path tree replay first,** right under the event's details and above the troubleshooting evidence: it's usually the first thing to look at.

## [1.5.3] - 2026-10-23

### Changed
- **Path finder: tracing their path back to you is easy to find.** Their traffic's way back to you only gets onto the tree when RIPE Atlas traces it, and the button for that sat at the bottom of the page. Now a purple button right by the tree offers it (the path is drawn in purple too), with the most it can cost, and says when it's under way. The RIPE Atlas panel says what the trace does. An admin whose bgpcanary has no RIPE Atlas set up gets a link to set it up instead.

## [1.5.2] - 2026-10-23

### Fixed
- **Path finder: a traceroute that didn't show your upstream drew the next network as your neighbour.** Your upstream's routers may not answer a traceroute, or may answer from the next network's addresses (a link numbered from that network's space). Then the traceroute, from this server or a RIPE Atlas trace back from their side, seems to go straight from that network to you. When it wasn't one of the known paths, the page drew it that way, for example your upstream's provider connected straight to you. Now:
  - the network RIS shows it reaching you through is put in, shown in brackets as not seen by the traceroute;
  - on a path RIS doesn't know, networks hidden behind silent hops further along are filled in the same way, from the known path it follows that far.
- **Path finder watches: false "path changed" alerts from a router that answers traceroutes only some of the time.** When it answered, the path showed its network; when it didn't, the path seemed to skip it, and two checks in a row either way counted as a change. A traceroute's path now counts as the same when what it showed fits the other one, with the networks it didn't show hidden behind its silent hops. The history keeps one period for it, with the fullest view. A network put in as above doesn't count as a change either, so updating doesn't set off an alert.
- A trace back from a probe in their upstream was labelled "traceroute stopped here" where it starts. It now says "traced back from here".

## [1.5.1] - 2026-10-22

### Fixed
- **Path finder: paths through a peer that wouldn't carry the traffic.** A network sends traffic to its providers for anywhere, but to a peer only for that peer's own customers. The Path finder didn't allow for that, so it could show your traffic going through one of your exchange peers, or one of your upstream's peers, on to a network that peer merely peers with: a path no traffic takes. Now:
  - your exchange peers (from PeeringDB) and the peers listed under Settings › Monitored network count as peers, and your expected upstreams as transit. A peer's route is used only when it stays among that peer's own customers;
  - the same goes for your upstreams' other neighbours (judged by how much of each upstream's traffic they carry, from RIPEstat) and for the other network's side;
  - which network is in front of which is now counted once per RIS peer's network, not per session (some networks feed RIS from a dozen sessions), and a RIS peer's own direct link no longer counts as a sign that it's anyone's provider.

### Added
- **Possible peerings** on the Path finder. RIS rarely sees a peering, so where PeeringDB has you, or one of your transits, at an internet exchange with the other network, that direct link is added as a possible path, drawn dotted. When the traceroute goes through it, the page says so, and at which exchange. A peer you listed yourself always gets its direct path.

### Changed
- When RIS doesn't show their own route to you, their traffic's paths now also come through a peer of theirs that has you among its customers, not only through their main upstreams. A network that peers with almost everyone no longer shows only your side.

## [1.5.0] - 2026-10-22

### Added
- **Path finder** (new page in the menu): enter an IP address, prefix, AS number or host name, and see the known AS paths between your network and theirs as a tree, from your AS out to theirs.
  - RIS can't show the paths between two networks directly, so they're put together from what RIS peers see toward each of you. Your traffic's paths climb from your AS through the networks that carry your prefix (and your upstreams' other neighbours, as on the connectivity tree) to the first one with a route of its own to theirs. Their traffic's paths are their own route to you when RIS sees it, or the same climb through their main upstreams.
  - The shortest known path is highlighted, and every path is listed with which way it goes and how many RIS paths point along it. For an AS, its biggest prefix is used, or the one you pick; for a prefix announced by several networks, the origin you pick.
  - **Your traffic:** a traceroute from the bgpcanary server marks the path it actually takes (or adds it, dashed, when it's one RIS doesn't show). Each hop shows its network, its reverse DNS name and, on an internet exchange's LAN, the exchange and whose port it is (from PeeringDB, instead of whoever announces that LAN). A breakdown shows which network adds how much delay. Networks hidden behind routers that don't answer, or that answer from another network's addresses, are allowed for when matching.
  - **Their traffic back to you:** on request, a RIPE Atlas traceroute to your address from probes inside their network, or, if Atlas has none there, inside their main upstream (about 60 credits a probe, up to 3 probes). Marked on the tree in its own color.
  - **Route Views too**, on request: each collector's view of their prefix (one connection per collector per lookup, reused for 30 minutes, at most 80% of a collector's daily allowance) and the paths toward you from the latest scheduled snapshots.
  - **History:** what every lookup, traceroute and trace back showed is kept per target for 400 days, as periods, and listed on the page.
  - **Watch list** (admins): targets checked again every 15 minutes to 24 hours, optionally traced back from their side every 6 hours. A different path in two checks in a row counts as a change: it's kept in the history and, if the watch alerts, sent as a new alert type, *Watched path changed* (Settings › Alert channels).
  - **Who can run checks:** an admin chooses who may run traceroutes, RIPE Atlas traces and Route Views lookups: everyone signed in (the default), admins only, or nobody. Looking paths up stays open to everyone.
  - The traceroute needs no root or extra packages: it works under the systemd sandbox and in the container image (ICMP where ordinary users may ping, else UDP). At most 10 a minute from people, one at a time, to public addresses only.

### Changed
- Signing in from a link (for example a shared Path finder page) brings you back to that exact page, query and all.
- The container image is built on the latest Alpine and Python 3.13 security fixes.

## [1.4.0] - 2026-10-20

### Added
- **Outages are checked against Route Views before they're called critical.** As soon as RIS shows an outage, bgpcanary asks every Route Views collector at once (an independent set of route collectors, not RIPE's) and compares what they see with their usual view; RIPE Atlas's latest results count too. The alert waits up to 2 minutes for the answer. If Route Views still sees your paths, the outage is a warning, "probably a problem with RIPE's data", and it won't turn critical. If Route Views lost them too, it's confirmed as usual. The event page lists what each collector saw. On by default; Settings › Detection & data sources.
- **Control prefixes:** two other networks' prefixes, 1.1.1.0/24 and 8.8.8.0/24 by default, watched the same way as yours (not stored or alerted on). If RIS loses most of them at the same moment as yours, the outage is reported as a problem with RIPE's data. Configurable, or off.
- **An upstream's lost neighbours stay on the tree, as "no longer peered".** The connectivity tree shows each of your upstreams' own transits and peers, including ones that don't carry you. When one stops being seen next to your upstream at all, it now stays on the tree for 30 days, marked "no longer peered" with when it was last seen and how big the link was, instead of silently disappearing. On the first start after updating, the past month is looked up in RIPEstat, so a recent loss shows straight away.
- **Data sources panel and banner.** The dashboard shows RIS Live's connection and drops, how old and complete RIPE's looking-glass answers are, the control prefixes, and which Route Views collectors answered. While something looks wrong, a banner says so on every page, alerts about routes going missing say "while RIPE's data looked unreliable", and the looking glass can't withdraw routes.

## [1.3.3] - 2026-10-20

### Fixed
- **A false outage when RIPE's looking glass gave incomplete answers.** Every few minutes bgpcanary checks its routes against RIPEstat's looking glass, which catches withdrawals RIS Live missed. On 2026-10-20 the looking glass listed almost none of a prefix's routes for about an hour, with current timestamps. bgpcanary withdrew them all and reported an outage while the prefix was fine. When the looking glass recovered, its data was older than those withdrawals, so the routes never came back, and routes RIS Live announced again were withdrawn again at the next check. Now:
  - one looking-glass answer can withdraw at most a tenth of a prefix's routes (or 10). An answer missing more is treated as incomplete, logged, and nothing is withdrawn from it. Real withdrawals arrive through RIS Live as they happen;
  - a route the looking glass withdrew comes back as soon as it lists the route again, even from older data.

  On an older version, `sudo systemctl restart bgpcanary` (or `docker restart bgpcanary`) clears such a false outage: at startup the routes are rebuilt from RIS's own data.
- An event that cleared through data stamped earlier than its start (such as a looking-glass answer older than the outage) was recorded as ending before it began, with a negative duration. It now ends no earlier than it started.

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

[Unreleased]: https://github.com/gregsowell/bgpcanary/compare/v1.7.0...HEAD
[1.7.0]: https://github.com/gregsowell/bgpcanary/compare/v1.6.3...v1.7.0
[1.6.3]: https://github.com/gregsowell/bgpcanary/compare/v1.6.2...v1.6.3
[1.6.2]: https://github.com/gregsowell/bgpcanary/compare/v1.6.1...v1.6.2
[1.6.1]: https://github.com/gregsowell/bgpcanary/compare/v1.6.0...v1.6.1
[1.6.0]: https://github.com/gregsowell/bgpcanary/compare/v1.5.8...v1.6.0
[1.5.8]: https://github.com/gregsowell/bgpcanary/compare/v1.5.7...v1.5.8
[1.5.7]: https://github.com/gregsowell/bgpcanary/compare/v1.5.6...v1.5.7
[1.5.6]: https://github.com/gregsowell/bgpcanary/compare/v1.5.5...v1.5.6
[1.5.5]: https://github.com/gregsowell/bgpcanary/compare/v1.5.4...v1.5.5
[1.5.4]: https://github.com/gregsowell/bgpcanary/compare/v1.5.3...v1.5.4
[1.5.3]: https://github.com/gregsowell/bgpcanary/compare/v1.5.2...v1.5.3
[1.5.2]: https://github.com/gregsowell/bgpcanary/compare/v1.5.1...v1.5.2
[1.5.1]: https://github.com/gregsowell/bgpcanary/compare/v1.5.0...v1.5.1
[1.5.0]: https://github.com/gregsowell/bgpcanary/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/gregsowell/bgpcanary/compare/v1.3.3...v1.4.0
[1.3.3]: https://github.com/gregsowell/bgpcanary/compare/v1.3.2...v1.3.3
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
