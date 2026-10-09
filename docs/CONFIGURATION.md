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
| Owned space | Larger blocks you hold (optional). Announcements inside them that you don't monitor are flagged. |
| Probe targets | Addresses in your space that answer ping: used by Route Views ping/traceroute and as default RIPE Atlas targets |
| Site name | Shown in the header and alert subjects |

Adding a prefix loads up to 90 days of its history in the background.

## Alerts (Settings › Alert channels)

- **Email:** any SMTP server: Microsoft 365, Google Workspace (app password), or an internal relay on port 25. Use *Send test email* to check.
- **Webhooks:** Slack incoming webhooks, or generic JSON for Teams (via Power Automate), PagerDuty-style receivers or your own scripts.
- **What to alert on:** every alert type, each with its own **Notify** choice (All / Warning and up / Critical only / Off) and its own thresholds. Events are always recorded on the Events page; this only controls notifications.
- **Site URL for alert links:** set it so alerts link back to the event page.

| Alert | Severity | Notes |
|---|---|---|
| Outage | warning / critical | Visibility below a % of the 24 h baseline for the sustain time; critical below half |
| Brief dip | info | Shorter than the sustain time |
| Reachability loss | critical | Most RIPE Atlas probes got no reply from one of your addresses (needs Atlas) |
| Expected upstream missing | warning | |
| Upstream shift | warning | A large share of peers moved between your upstreams within minutes; triggers Route Views and Atlas checks |
| Unexpected / new upstream | warning | |
| New upstream chain | info | A transit → upstream combination seen for the first time |
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

## Command line

```bash
sudo bgpcanary-admin users                               # list accounts
sudo bgpcanary-admin useradd NAME --role viewer --must-change
sudo bgpcanary-admin useradd admin                       # reset a password (e.g. if you're locked out)
sudo bgpcanary-admin userdel NAME
sudo bgpcanary-admin test-alert                          # send a test alert on every channel
sudo bgpcanary-admin version
sudo bgpcanary-update [--check | --rollback | --version X.Y.Z]
sudo journalctl -u bgpcanary -f                          # live log
```

## Where things live

| What | Where |
|---|---|
| Program | `/opt/bgpcanary` (code, Python environment, deploy scripts) |
| Database | `/var/lib/bgpcanary/bgpcanary.db` (SQLite) |
| Backups | `/var/lib/bgpcanary/backups/` |
| Settings file | `/etc/bgpcanary/config.toml` |
| First admin password | `/etc/bgpcanary/initial-admin-password` |
| Web server config | `/etc/nginx/conf.d/bgpcanary.conf` |
| Certificate | `/etc/pki/tls/certs/bgpcanary.crt` and `/etc/pki/tls/private/bgpcanary.key` |
| Service | `bgpcanary.service` (runs as user `bgpcanary`, behind nginx on 127.0.0.1:8080) |
