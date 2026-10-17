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

## No data / "RIS Live disconnected"

- The server needs outbound HTTPS to `ris-live.ripe.net`. Test it:

  ```bash
  curl -sI https://ris-live.ripe.net/v1/stream/ | head -1
  ```

- Short disconnects are normal. bgpcanary reconnects on its own and fills the gap from RIPE's archive a few hours later.

## Route Views shows errors or "rate-limited"

- Port 23 (telnet) outbound to `*.routeviews.org` must be allowed.
- If a collector is rate-limited, bgpcanary pauses it for 24 h by itself. Don't lower the polling intervals below the defaults.
- A traceroute that ends early with "target answers ping" is fine: routers near your network just don't answer traceroute.

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

Raw BGP updates are the bulk of the database. Lower **Keep raw BGP updates for (days)** under Settings ›
Detection & data sources. Visibility history and events are kept regardless.

## Something else

[Open an issue](https://github.com/gregsowell/bgpcanary/issues) with:
- your bgpcanary version (bottom of every page);
- your OS version;
- what you expected and what happened;
- the relevant log lines.

**Remove passwords, API keys and anything you don't want public first.**
