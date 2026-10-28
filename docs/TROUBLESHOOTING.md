# Troubleshooting

Start with the log. It says what bgpcanary is doing and why something failed:

```bash
sudo journalctl -u bgpcanary -n 100        # last 100 lines
sudo journalctl -u bgpcanary -f            # follow live
systemctl status bgpcanary nginx
curl -s http://127.0.0.1:8080/healthz      # {"ok": true, "version": ...} when the app is up
```

In a container, the log is `docker logs bgpcanary` (`-f` to follow), and `docker ps` shows whether its health
check passes.

## The web page doesn't load

- **"502 Bad Gateway":** nginx is up but the app isn't. Check `systemctl status bgpcanary` and the log.
- **Nothing at all / timeout:**
  - check the firewall: `sudo firewall-cmd --list-services` should include http and https;
  - check that DNS points at the server.
- **Certificate warning:** expected with the self-signed certificate. See [INSTALL.md](INSTALL.md#using-a-real-certificate).

## A "Loading history" banner on every page

On a first start, bgpcanary loads up to 90 days of history from RIPE RIS before live monitoring begins. The
banner shows how far it has got and, after a minute or so, an estimate. It usually takes a few minutes; prefixes
with a lot of routing activity take longer. Until it finishes, the dashboard shows the history as it's replayed.
Events found in that history are marked "(backfill)" and don't send alerts.

If the banner stops moving on:
- the log lists each week as it loads (`backfill chunk 3/13: … events`) and any failed requests (`bgplay … failed`);
- the server needs outbound HTTPS to `stat.ripe.net` for the history.

If **"Connecting to RIS Live"** lasts more than a couple of minutes, the connection to `ris-live.ripe.net` isn't
getting through: see [No data / "RIS Live disconnected"](#no-data--ris-live-disconnected).

## "Config file ... has a syntax error"

`/etc/bgpcanary/config.toml` was edited and isn't valid any more. The message shows the lines around the
problem. The mistake is usually on the line shown *or just above it*, because a list or text that isn't closed is
only noticed later. Common fixes:

- Lists need square brackets and commas: `owned_space = ["198.51.100.0/22", "203.0.113.0/24"]`, `expected_upstreams = [64501, 64502]`
- Text needs straight double quotes `"..."`, not curly “quotes” from a word processor.

Edit it with `sudo nano /etc/bgpcanary/config.toml`, then `sudo systemctl restart bgpcanary`. Almost every setting
can be changed in the web UI instead (Settings), which avoids editing this file at all.

## I can't log in

- **Forgot the admin password, or locked out:** `sudo bgpcanary-admin useradd admin` asks for a new password and sets it. (Add `--must-change` to force another change at the next login.) Editing `/etc/bgpcanary/initial-admin-password` does *not* change the password; that file is only a record of the one the installer generated.
- **The "choose a new password" page keeps coming back (1.0.0 and 1.1.0 only):** a bug on fresh installs where the ASN isn't set up yet. Update to 1.1.1 or later (`sudo bgpcanary-update`), or set the password from the command line as above.
- **"Too many attempts":** logins are throttled after repeated failures. Wait a few minutes.

## "Too many failed attempts" when signing in

Login attempts are limited per address (5 per 15 minutes) and per account (20 per 15 minutes from browsers
that haven't signed in to that account before). A browser that has signed in to the account before keeps
working even while someone else is guessing at it. To clear every limit at once:

```bash
sudo systemctl restart bgpcanary
```

In a container, `docker restart bgpcanary` does the same. If *everyone* is refused at once behind a reverse
proxy, bgpcanary is probably counting every browser as the proxy's address: the proxy isn't sending
X-Forwarded-For, or its address isn't covered by `BGPCANARY_TRUSTED_PROXIES` (see
[DOCKER.md](DOCKER.md#which-addresses-may-send-these-headers)).

## Signing in does nothing, or the login page says it "came over plain HTTP"

bgpcanary's sign-in cookies are HTTPS-only, so a browser on plain `http://` won't keep them. The login page
says so when that happens. Open the site over HTTPS, through a reverse proxy in front of the container (see
[DOCKER.md](DOCKER.md#https-a-reverse-proxy-in-front)). For a quick test on a trusted network, start the container
with `BGPCANARY_INSECURE_COOKIES=1` instead. `http://localhost` works in most browsers either way, because they
keep HTTPS-only cookies there.

If the page appears over HTTPS and still says this, the proxy isn't sending `X-Forwarded-Proto: https`, or its
address isn't trusted (see the section above).

## The container stops: "... isn't writable by this container's user"

The data directory you mounted belongs to someone else. The app runs as uid 10001; give it the directory:

```bash
sudo chown -R 10001:10001 ./data              # Docker, or Podman as root
podman unshare chown -R 10001:10001 ./data    # rootless Podman
```

Or use a named volume (`-v bgpcanary-data:/var/lib/bgpcanary`), which needs nothing. On SELinux systems, add `:Z`
to a directory mount: `-v ./data:/var/lib/bgpcanary:Z`.

## "Refused: this request didn't come from this site's own pages"

Changes (form submissions) are only accepted from bgpcanary's own address. If you reach it through another
proxy or a different name than the one in your browser's address bar, set the Site URL to that address, in
Settings › Alert channels or in `/etc/bgpcanary/config.toml`:

```toml
[web]
base_url = "https://bgp.example.com"
```

Then `sudo systemctl restart bgpcanary`.

In front of the container, the usual cause is a proxy that doesn't pass on the browser's Host header (nginx needs
`proxy_set_header Host $http_host;`). Fix the proxy, or set the Site URL as above.

## Lots of "unexpected upstream" warnings

Each one names a network seen right next to yours in BGP paths that isn't one of your expected upstreams.
Usually it's a peer: a network you exchange routes with at an internet exchange, or over a private link.

- **Peers at an exchange** are recognized automatically if your exchange connections are in PeeringDB.
  Settings › Monitored network shows which exchanges PeeringDB lists for you. If one is missing, add your
  connection to PeeringDB and press **Look up again now**.
- **Private peers**, or peers at an exchange PeeringDB doesn't list, need to be added to **Peers** on the same page.
- **Quickest:** open the warning's event page and answer **One of my transit providers** or **A peer**. The
  warning closes by itself a minute or two later.

If a network you've never dealt with shows up next to you, especially for many RIS peers, take it seriously:
someone may be leaking or hijacking your route.

## The tree says a network is "not carrying you"

Under each of your upstreams, the connectivity tree shows that upstream's own transits and peers (from
RIPEstat), including the ones that carry none of your paths *through that upstream*.

- **Red, "not carrying you via AS64501":** RIS sees that network carrying you nowhere, neither through that
  upstream nor any other way. If it's one of the upstream's transits, that's usually a prefix filter between
  them; the transit filter check reports the same thing as a *Transit not carrying you* notice, and the tree's
  tooltip shows what the check found for that pair. If RIS barely sees the pair at all, even for the upstream's
  own prefixes, the tooltip says so: RIS has no vantage point there, and it may well be fine.
- **Faint blue dotted, "reaches you directly" / "reaches you via AS64502":** it has a path to you it prefers,
  your own peer or another upstream of yours, so it never routes to you through this one. That's a network
  using its best route, not a problem. The *Upstream neighbours* control can fold these into one "+N reach you
  another way" line per upstream, or hide the upstreams' neighbours altogether.
- **Faint blue dotted, "moved to via AS64502":** it carried you under this upstream at the comparison point and
  now carries the same under another one; its new branch says "moved from via AS64501". A network using its
  best route, not a loss. Red dashed "dropped" is kept for a network that carries you nowhere any more.
- The Registry page's *who carries you* table and the transit filter check read the same pairs the same way: a
  transit that reaches you another way isn't a gap ("via AS64502"), and one that carries you only the long way
  round, through that upstream but not straight from it, is reported with the path it takes. The direct path would
  normally win, so your prefix is probably not accepted on that session.
- A neighbour that peers with you privately where RIS has no vantage point can still show red. List it under
  **Peers** in Settings › Monitored network and it's treated as your direct neighbour.

## No data / "RIS Live disconnected"

- The server needs outbound HTTPS to `ris-live.ripe.net`. Test it:

  ```bash
  curl -sI https://ris-live.ripe.net/v1/stream/ | head -1
  ```

- Short disconnects are normal. bgpcanary reconnects on its own and fills the gap from RIPE's archive a few hours later. Events found that way are marked "(backfill)" and don't send alerts; a gap fill reports only what began during the gap, not what was already open.

## An outage that isn't real

bgpcanary checks outages against Route Views before calling them critical, and watches control prefixes to
spot problems with RIPE's data (see [Checking RIPE's data](CONFIGURATION.md#checking-ripes-data)). The
dashboard's **Data sources** panel shows how RIS, its looking glass and Route Views look right now.

If bgpcanary still reports your prefix down or nearly invisible but your network is reachable (RIPE Atlas, your
own tests), compare with RIPE's own view:

```bash
curl -s "https://stat.ripe.net/data/looking-glass/data.json?resource=192.0.2.0/24" | grep -o '"peer":' | wc -l
```

That counts the RIS peers that have a route to it right now. If it's far more than bgpcanary shows, its view
has drifted from RIPE's. Restart it (`sudo systemctl restart bgpcanary`, or `docker restart bgpcanary`): at startup
the routes are rebuilt from RIS's own data. Before 1.3.3, a looking glass that gave incomplete answers could cause
this; the log then shows `looking-glass resync: 300 changes` or similar while nothing was wrong.

## Route Views shows errors or "rate-limited"

- Port 23 (telnet) outbound to `*.routeviews.org` must be allowed.
- If a collector is rate-limited, bgpcanary pauses it for 24 h by itself. Don't lower the polling intervals below the defaults.
- A traceroute that ends early with "target answers ping" is fine: routers near your network just don't answer traceroute.

## Path finder: the traceroute shows nothing, or stops early

The traceroute runs from the bgpcanary server itself, without root: ICMP echo where the system lets ordinary users
send it, otherwise UDP to ports 33434 and up. The routers' "time exceeded" answers have to get back to the server.

- **Every hop is `*`:** a firewall drops the outgoing probes (ICMP echo, or UDP to high ports), or the ICMP answers
  coming back. Allow ICMP "time exceeded" and "destination unreachable" in.
- **"Every probe reached the target in one hop":** something between the server and the internet sends the
  packets on itself instead of forwarding them, so their TTL is lost (some container and VPN networking does
  this). It works on a server install, or in a container with rootful networking or `--network host`.
- **"Network is unreachable" for an IPv6 address:** the server has no IPv6 route.
- **"This server reaches the internet from ..., not from your prefixes":** the server's traffic leaves from an
  address that isn't in your prefixes, for example your router's address on its link to an upstream (NAT), or the
  server is hosted somewhere else. If the trace leaves through one of your upstreams, it's still marked on the tree
  as how traffic leaves through that upstream; otherwise it isn't marked.
- **A network missing from the trace:** routers that don't answer traceroute show as `*`, so their network can be
  missing. The page still matches the trace to a known path if the missing networks fit in those silent hops, and
  says which ones it didn't see.
- **Your upstream missing, its provider right next to you:** your upstream's router answered from an address of its
  provider's (a link numbered from the provider's space), or not at all. Your neighbour has to be there, so the page
  puts in the one RIS shows that provider reaching you through, in brackets, as not seen by the trace.

## Alerts don't arrive

- Use **Settings › Alert channels › Send test email / Test** and read the error shown.
- Check that the alert type is switched on under **What to alert on**.
- Check the event page: its **Alerts sent** box shows whether and where each alert went.

**Email problems:**
- *Microsoft 365:* the mailbox needs Authenticated SMTP enabled.
- *Gmail / Google Workspace:* use an app password.
- *Your own relay:* it must allow this server's IP.
- *Delivery:* the receiving side may reject mail from a server without reverse DNS, SPF or DKIM.

## RIPE Atlas

- **Test connection** on Settings › RIPE Atlas checks the key, each permission, your credits and a real ping.
- "Not enough credit" means your balance is 0 or your daily cap is too low. The settings page shows how many credits your choices need.
- New periodic measurements take up to one interval (e.g. 30 min) to produce their first results.

## The database is getting big

Since 1.3.2 the visibility history takes about 0.1–0.2 GB per prefix per year, even for a prefix that RIS peers
reach through hundreds of different paths. Before that, every 30-second bucket stored the upstream chains in full
again, which reached several GB per prefix per year for such prefixes.

After updating to 1.3.2, bgpcanary compacts the history it already has, in the background (a few minutes for a big
database). Then it rewrites the database file once to give the space back, which pauses it for a few seconds. The
log shows both steps:

```bash
sudo journalctl -u bgpcanary | grep -i compact
docker logs bgpcanary 2>&1 | grep -i compact       # in a container
```

- **"there isn't room to compact it now":** rewriting the file needs free disk space of about twice the size of
  the compacted database, part of it in the temporary directory. Free the space the message names, then restart
  bgpcanary (`sudo systemctl restart bgpcanary`, or `docker restart bgpcanary`). It tries again at every start until
  it's done. In a container the temporary directory is `/tmp`, which is in memory: if that's what's short, set
  `SQLITE_TMPDIR=/var/lib/bgpcanary` in the container's environment to use the data volume instead.
- **The backups taken before the update** keep their old size. Once you're happy with the new version, delete the
  older ones from `/var/backups/bgpcanary` (or `backups/` in the container's volume).

The rest of the database is mostly raw BGP updates and, if you use it, RIPE Atlas results. To keep raw updates
for less time, lower **Keep raw BGP updates for (days)** under Settings › Detection & data sources. Visibility
history and events are kept regardless.

## Something else

[Open an issue](https://github.com/gregsowell/bgpcanary/issues) with:
- your bgpcanary version (bottom of every page);
- your OS version;
- what you expected and what happened;
- the relevant log lines.

**Remove passwords, API keys and anything you don't want public first.**
