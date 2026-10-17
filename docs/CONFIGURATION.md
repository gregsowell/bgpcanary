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
- **Unknown neighbors** still raise "unexpected upstream". The event's page asks what the network is to you:
  **One of my transit providers** or **A peer**. One click files it, the warning closes by itself, and no
  restart is needed.

Settings › Monitored network shows which exchanges PeeringDB lists for you, with a **Look up again now** button.

## Alerts (Settings › Alert channels)

- **Email:** any SMTP server: Microsoft 365, Google Workspace (app password), or an internal relay on port 25. Use *Send test email* to check.
- **Webhooks:** Slack incoming webhooks, Microsoft Teams (a channel's "Send webhook alerts to a channel" workflow), or generic JSON for PagerDuty-style receivers or your own scripts. Settings › Alerts has step-by-step instructions for getting each URL.
- **What to alert on:** every alert type, each with its own **Notify** choice (All / Warning and up / Critical only / Off) and its own thresholds. Events are always recorded on the Events page; this only controls notifications.
- **Site URL for alert links:** set it so alerts link back to the event page.

| Alert | Severity | Notes |
|---|---|---|
| Outage | warning / critical | Visibility below a % of the 24 h baseline for the sustain time; critical below half |
| Brief dip | info | Shorter than the sustain time |
| Reachability loss | critical | Most RIPE Atlas probes got no reply from one of your addresses (needs Atlas) |
| Expected upstream missing | warning | |
| Upstream shift | warning | A large share of peers moved between your upstreams within minutes; triggers Route Views and Atlas checks |
| Unexpected / new upstream | warning | A neighbor that's neither an expected upstream nor a peer |
| New upstream chain | info | A transit → upstream combination seen for the first time |
| Exchange peers lost | warning | Many of one exchange's peers stopped carrying a prefix at once (default: half of them) |
| Peer leak | warning | A peer carries a prefix to at least 10% of RIS peers and three times its usual reach; triggers Route Views checks |
| New peer | info | A peer at one of your exchanges (or a listed peer) carries a prefix for the first time |
| Origin hijack | critical | Another AS originates your prefix |
| More-specific | warning / critical | Critical when another AS originates it |
| Covering announcement | warning | |
| Your ASN in someone else's path | warning | |
| Unmonitored prefix from your ASN | warning | |
| Transit not carrying you | info | An upstream's transit carries the upstream's routes but not yours, usually a prefix filter |
| Registry problem / filter simulation / IRR / as-set change | info–critical | From the registry watch |

## Detection & data sources (Settings › Detection & data sources)

Outage thresholds, the baseline window, bucket size, upstream-shift sensitivity, covering-prefix filters,
check intervals, the **Route Views collectors** to poll, the polling and probe schedules, and how long raw
updates are kept. Each setting is explained on the page.

**Route Views limits.** Route Views allows about 100 telnet connections per collector per day from one IP
address. bgpcanary keeps scheduled work under half of that, saves the rest for events, and pauses a collector
for 24 h if it ever gets rate-limited. Keep the defaults unless you know you need more.

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
