# Configuring bgpcanary

Almost everything is set in the web UI under **Settings** (admins only), and takes effect immediately or
after an automatic restart of a few seconds. The config file only holds what the service needs before it can
start.

## Monitored network (Settings › Monitored network)

| Setting | What it's for |
|---|---|
| ASN | Your AS number; paths must end in it to count as "seeing" your prefix |
| Prefixes | Every prefix you announce, IPv4 and IPv6, aggregates and more-specifics |
| Expected upstreams | The ASes you buy transit from. Enables "expected upstream missing", "unexpected upstream" and the upstream-shift view. |
| Peers | Networks you peer with rather than buy transit from (optional). Peers at your internet exchanges are found automatically (below); list private peers here. |
| Owned space | Larger blocks you hold (optional). Announcements inside them that you don't monitor are flagged. |
| Probe targets | Addresses in your space that answer ping: used by Route Views ping/traceroute and as default RIPE Atlas targets |
| Site name | Shown in the header and alert subjects |

Adding a prefix loads up to 90 days of its history in the background.

### Exchange peers

If you peer at internet exchanges, many of the networks right next to you in BGP paths are peers, not transit
providers. A peer only passes your routes to its own customers, so each one reaches just a few RIS peers.
bgpcanary keeps them apart from your upstreams:

- **How it knows:** it looks up the exchanges your ASN is on in [PeeringDB](https://www.peeringdb.com/) once a
  day, along with their member lists. A neighbor that's a member of one of your exchanges is an exchange peer.
  So is any ASN under **Peers** (private peering, or an exchange PeeringDB doesn't know). Your expected
  upstreams always count as transit, even when they're at the same exchange.
- **What changes:**
  - peers don't raise "unexpected upstream" warnings;
  - the dashboard lists them per exchange, under your upstreams;
  - the connectivity tree puts them on one **Exchanges** branch, split per exchange;
  - the upstream chains chart shows one "Peers at …" series per exchange;
  - upstream-shift detection ignores them.
- **Checks of their own:**
  - **Exchange peers lost:** many peers at one exchange stop carrying a prefix at once (your port, the
    route servers, or the exchange itself). It closes once they've been back for 15 minutes, so a flapping
    session is one event. Private peers aren't part of it, since each has a link of its own.
  - **Peer leak:** one peer suddenly carries a prefix to a large share of the internet, far beyond its usual
    reach. That usual reach is learned first, so a big peer with many customers of its own isn't flagged.
  - **New peer** (information): a peer carries a prefix for the first time.
- **Unknown neighbors** still raise "unexpected upstream". The event's page, and the **unexpected** badge next to
  the network on the dashboard, ask what it is to you: **Add as a peer** or **One of my transit providers**. One
  click files it, the warning closes by itself, and no restart is needed. A network you don't peer with directly
  can still show up as your neighbor: route servers at an exchange don't add their own ASN to the path, so every
  network they hand your routes to looks adjacent to you, and some exchanges pass their route-server routes on to
  partner exchanges. If you peer with route servers, that's almost always what it is: a peer.

Settings › Monitored network shows which exchanges PeeringDB lists for you, with a **Look up again now** button.

## Alerts (Settings › Alert channels)

- **Email:** any SMTP server: Microsoft 365, Google Workspace (app password), or an internal relay on port 25. Use *Send test email* to check.
- **Webhooks:** Slack incoming webhooks, Microsoft Teams (a channel's "Send webhook alerts to a channel" workflow), or generic JSON for PagerDuty-style receivers or your own scripts. Settings › Alerts has step-by-step instructions for getting each URL.
- **What to alert on:** every alert type, each with its own **Notify** choice (All / Warning and up / Critical only / Off) and its own thresholds. Events are always recorded on the Events page; this only controls notifications.
- **Site URL for alert links:** set it so alerts link back to the event page.

| Alert | Severity | Notes |
|---|---|---|
| Outage | warning / critical | Visibility below a % of the 24 h baseline for the sustain time; critical below half, unless Route Views still sees it ([Checking RIPE's data](#checking-ripes-data)). Route Views decides nothing when it normally holds fewer than five paths for the prefix |
| Brief dip | info | Shorter than the sustain time |
| Reachability loss | critical | Most RIPE Atlas probes got no reply from one of your addresses (needs Atlas) |
| Expected upstream missing | warning | |
| Upstream down | warning | One of your upstreams carries almost none of a prefix's paths (default: a tenth of its typical share) while you stay visible through the others. Says what changed behind it: which of its own upstreams stopped carrying you, which of its transits don't carry your prefix (transit filter check), and whether its own network is still reachable (RIPEstat). Replaces the upstream-shift alert for that move; triggers Route Views and Atlas checks |
| Upstream shift | warning | A large share of peers moved between your upstreams within minutes; names the network behind your upstreams whose way to you changed ("AS64510 switched from via AS64520 to via AS64530"); triggers Route Views and Atlas checks |
| Unexpected / new upstream | warning | A neighbor that's neither an expected upstream nor a peer |
| New upstream chain | info | A transit → upstream combination seen for the first time |
| Exchange peers lost | warning | Many of one exchange's peers stopped carrying a prefix at once (default: half of them); says how many still reach you through your transit and how many carry you nowhere now |
| Peer leak | warning | A peer carries a prefix to at least 10% of RIS peers and three times its usual reach; triggers Route Views checks |
| New peer | info | A peer at one of your exchanges (or a listed peer) carries a prefix for the first time |
| Origin hijack | critical | Another AS originates your prefix |
| More-specific | warning / critical | Critical when another AS originates it |
| Covering announcement | warning | |
| Your ASN in someone else's path | warning | |
| Unmonitored prefix from your ASN | warning | |
| Transit not carrying you | info | An upstream's transit carries the upstream's routes straight from it but yours not at all, or only the long way round (through that upstream, but not straight from it): usually a prefix filter. One that reaches you another way, your own neighbour or a network behind another upstream of yours, is using its best route and isn't reported. A neighbour with an open "unexpected upstream" warning isn't judged until it's filed as a transit |
| Registry problem / filter simulation / IRR / as-set change | info–critical | From the registry watch |
| Watched path changed | info | A network on the [Path finder](#path-finder)'s watch list: the path to it (or back from it) changed in two checks in a row |

## Detection & data sources (Settings › Detection & data sources)

Outage thresholds, the baseline window, bucket size, upstream-shift sensitivity, covering-prefix filters,
check intervals, the **Route Views collectors** to poll, the polling and probe schedules, and how long raw
updates are kept. Each setting is explained on the page.

**Route Views limits.** Route Views allows about 100 telnet connections per collector per day from one IP
address. bgpcanary keeps scheduled work under half of that, saves the rest for events, and pauses a collector
for 24 h if it ever gets rate-limited. Keep the defaults unless you know you need more.

### Checking RIPE's data

Almost everything bgpcanary sees comes from RIPE RIS. When RIPE has a bad day, the data can say your prefix is
gone while it's fine. So bgpcanary doesn't take RIS's word alone for an outage:

- **Outages are checked against Route Views** before they're reported as critical. As soon as one opens,
  bgpcanary asks every Route Views collector at once (they're run independently of RIPE, by the University of
  Oregon) and compares what they see with their usual view. RIPE Atlas's latest results count too, if you use
  Atlas. The alert waits up to 2 minutes for the answer.
  - **Route Views still sees your paths** (or Atlas probes still reach you): the outage is reported as a
    *warning*, "probably a problem with RIPE's data", and it won't turn critical.
  - **Route Views lost them too:** it's confirmed, and alerted as usual.
  - **No answer** (for example, the collectors' daily limit is used up): it's alerted as RIS shows it, saying it
    couldn't be checked.

  The event page lists what each collector saw. Turn it off with *Check outages against Route Views before
  calling them critical*.
- **Control prefixes:** bgpcanary also watches a couple of other networks' prefixes the same way as yours, by
  default 1.1.1.0/24 (Cloudflare) and 8.8.8.0/24 (Google). If RIS loses most of them at the same moment as
  yours, the problem is RIPE's data, and the outage is reported that way even without Route Views. They aren't
  stored or alerted on. Change them under *Control prefixes*; empty turns them off.
- **Data sources panel** (bottom of the dashboard): RIS Live's connection and drops, how old and how complete
  RIPE's looking-glass answers are, the control prefixes, and which Route Views collectors answered. While
  something looks wrong, a banner says so on every page. Alerts about routes going missing (dips, missing
  upstreams, an upstream down, upstream shifts, exchange peers lost) say "while RIPE's data looked unreliable", and the looking
  glass may not withdraw routes from bgpcanary's view.

## RIPE Atlas

Optional, and off until you add an API key. **Settings › RIPE Atlas** explains each option and estimates
daily credit use as you type.

1. Create a free account at [atlas.ripe.net](https://atlas.ripe.net/).
2. Get **credits**:
   - host a probe; a software probe on any Linux machine earns ~21,600 credits a day. On this server: `sudo /opt/bgpcanary/deploy/install.sh --atlas-probe-only`, then register the key it prints at [atlas.ripe.net/register/swprobe](https://atlas.ripe.net/register/swprobe);
   - or ask another Atlas user to transfer some.
3. Create an **API key** under *My Atlas › API keys* with three permissions:
   - *Schedule a new measurement*;
   - *Stop a running measurement*;
   - *Get information about your credits*.
4. Paste it into Settings › RIPE Atlas, choose your **baseline targets**, turn it on, and use **Test connection**.

**Baseline targets:**
- One per line, optionally followed by a label and a `[group]`, e.g. `203.0.113.2  Carrier A peering [border-1]`.
- Your side of each carrier's peering link makes a good target: traffic to it arrives over that carrier, so each carrier gets its own latency/loss record.
- Each target costs roughly 10,000 credits/day at the defaults. Set the **daily credit cap** to match.

## Path finder

The **Path finder** page shows the known AS paths between your network and another one: type an IP address, a
prefix, an AS number or a host name. The paths are put together from what RIS peers see toward each of you (the
page explains how); the shortest is highlighted, and the ones in use are marked.

The paths follow how networks pass traffic on: up to a provider for anywhere, but across to a peer only for that
peer's own customers. So it matters which of your neighbours are peers. Your exchange peers (from PeeringDB, see
[Exchange peers](#exchange-peers)) and the peers listed under Settings › Monitored network count as peers,
and your expected upstreams as transit. A peering rarely shows in RIS, so where PeeringDB has you, or one of your
transits, at an exchange with them, that direct link is added as a *possible peering* (dotted) until a traceroute
shows whether it's used. A listed peer always gets its direct path.

What marks the paths in use:

- **Your traffic to them:** a traceroute from the bgpcanary server, run straight away for an address or host name,
  and on request toward an address in the prefix for an AS or prefix. It needs no root or extra packages (ICMP
  where ordinary users may send it, else UDP), shows each hop's network, its exchange (from PeeringDB) and its
  reverse DNS name, and which network adds how much delay. If the server reaches the internet from an address
  that isn't in your prefixes (behind NAT on your upstream's link, say), the page says so.
- **Their traffic back to you:** on request (the purple button next to the tree), a one-off RIPE Atlas traceroute
  to your probe target (Settings › Monitored network) from up to 3 probes inside their network, or, if Atlas has
  none there, inside one of their main upstreams: the path back from there, drawn on the tree in purple. About 60
  credits a probe, counted against your daily cap. Needs [RIPE Atlas](#ripe-atlas).
- **Route Views too:** tick *also ask Route Views* to add each collector's view of their prefix (one of its ~100
  daily connections per lookup, reused for 30 minutes, and never more than 80% of a collector's day) and the
  paths toward you from the latest scheduled snapshots.

**History:** every lookup, traceroute and trace back is kept per target, as periods ("this path from 09:00 to
17:30"), for 400 days; the page lists what changed.

**Watch list** (admins): *Watch* a target to have it checked every 15 minutes to 24 hours. Each check looks it up
and traces it; a path that's different in two checks in a row counts as a change (a router that answers
traceroutes only some of the time doesn't make one), goes into its history and, if the watch alerts, sends a
*Watched path changed* alert. A watch can also trace back from them with RIPE Atlas, at most every 6 hours (~180
credits each time).

**Who can run checks:** traceroutes, RIPE Atlas traces and Route Views lookups send traffic from the server or use
your credits and Route Views' allowance, so the bottom of the Path finder page (admins only) sets who may run
them: everyone signed in (the default), admins only, or nobody. Looking paths up is always open to everyone
signed in.

## API tokens (Settings › API tokens)

Your own scripts and dashboards can read bgpcanary through a small JSON API (`/api/v1/`): the monitor's status
with each prefix's visibility and state, the events with the Events page's filters, and what a neighbouring
network is to you. Any signed-in user creates tokens under **Settings › API tokens**; a token is shown once (only
a hash is kept), can read what its owner can, and is sent as `Authorization: Bearer …`. Revoke tokens there, or
as an admin on the Users page. Endpoints, examples and what's promised to stay the same: [API.md](API.md).

## The config file

`/etc/bgpcanary/config.toml` holds the settings needed before the service starts:
- `[storage] db_path`;
- `[web] listen_host`, `listen_port`, `secret_key` (signs login cookies; keep it secret);
- first-start defaults for everything else.

Values saved in the web UI take precedence. After editing the file, run `sudo systemctl restart bgpcanary`.

In the container image, the file is `/var/lib/bgpcanary/config.toml` in the data volume. It's created on the
first start, and the listen address comes from environment variables instead; see
[DOCKER.md](DOCKER.md#environment-variables). Restart with `docker restart bgpcanary` after editing it.

## Command line

```bash
sudo bgpcanary-admin users                               # list accounts
sudo bgpcanary-admin useradd NAME --role viewer --must-change
sudo bgpcanary-admin useradd admin                       # reset a password (e.g. if you're locked out)
sudo bgpcanary-admin userdel NAME
sudo bgpcanary-admin test-alert                          # send a test alert on every channel
sudo bgpcanary-admin version
sudo bgpcanary-admin backup - > bgpcanary.db             # a consistent copy of the database, while it runs
sudo bgpcanary-update [--check | --rollback | --version X.Y.Z]
sudo journalctl -u bgpcanary -f                          # live log
```

In a container, run them as `docker exec -it bgpcanary bgpcanary-admin ...`, without `sudo`. For `backup -`, leave
out `-it` (a terminal would mangle the data).

## Where things live

| What | Where |
|---|---|
| Program | `/opt/bgpcanary` (code, Python environment, deploy scripts) |
| Database | `/var/lib/bgpcanary/bgpcanary.db` (SQLite) |
| Backups | `/var/backups/bgpcanary/` (root only; before 1.2.2: `/var/lib/bgpcanary/backups/`) |
| Settings file | `/etc/bgpcanary/config.toml` |
| First admin password | `/etc/bgpcanary/initial-admin-password` |
| Web server config | `/etc/nginx/conf.d/bgpcanary.conf` |
| Certificate | `/etc/pki/tls/certs/bgpcanary.crt` and `/etc/pki/tls/private/bgpcanary.key` |
| Service | `bgpcanary.service` (runs as user `bgpcanary`, behind nginx on 127.0.0.1:8080) |

In the container image, everything bgpcanary keeps is in the data volume, `/var/lib/bgpcanary`; see
[DOCKER.md](DOCKER.md#the-data-volume).
