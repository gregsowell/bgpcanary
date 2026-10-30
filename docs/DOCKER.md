# Running bgpcanary in a container

bgpcanary is also published as a container image, **`ghcr.io/gregsowell/bgpcanary`**, for **amd64** and
**arm64** (for example a Raspberry Pi 4 or 5 with a 64-bit OS, or Ampere and Graviton servers). It's Alpine
Linux with Python and bgpcanary, about 100 MB, and runs with Docker or Podman (rootful or rootless).

How it differs from a [server install](INSTALL.md):
- **No nginx inside.** The container serves plain HTTP on port 8080. HTTPS comes from a reverse proxy in front
  of it ([examples below](#https-a-reverse-proxy-in-front)). The app sends the security headers the bundled
  nginx would add.
- **Everything bgpcanary keeps is in one volume,** `/var/lib/bgpcanary`: the database, the config file and
  backups.
- **Updating** means pulling a new image ([below](#updating)) instead of running `bgpcanary-update`.
- **No RIPE Atlas software probe inside.** All the Atlas features work. To earn credits, host a probe on the
  Docker host or anywhere else.

| Tag | What you get |
|---|---|
| `latest` | the newest release |
| `1`, `1.3` | the newest 1.x or 1.3.x release |
| `1.3.0` | exactly that release |

## Quick try (plain HTTP)

To look around on a trusted network, without HTTPS:

```bash
docker run -d --name bgpcanary --restart unless-stopped -p 8080:8080 -e BGPCANARY_INSECURE_COOKIES=1 \
  -v bgpcanary-data:/var/lib/bgpcanary ghcr.io/gregsowell/bgpcanary:latest
docker logs bgpcanary
```

The log shows the initial admin password. Open `http://<host>:8080/`. To keep it out of logs from the start,
set `BGPCANARY_LOG_INITIAL_PASSWORD=0` in the container environment before its first start. Setting it later
does not remove the password from earlier logs. It remains in the owner-only `initial-admin-password` file in
the data volume until it is changed.

Over plain HTTP, passwords and session cookies cross the network unencrypted. That's why signing in needs
`BGPCANARY_INSECURE_COOKIES=1`. For real use, put it behind HTTPS as described below. To remove the trial
afterwards: `docker rm -f bgpcanary && docker volume rm bgpcanary-data`.

## With Docker Compose

[deploy/docker/compose.yaml](../deploy/docker/compose.yaml) runs it with hardening options: a read-only
filesystem and no Linux capabilities. It publishes port 8080 on the host's loopback address only, ready for a
reverse proxy on the same host:

```bash
mkdir bgpcanary && cd bgpcanary
curl -fsSLO https://raw.githubusercontent.com/gregsowell/bgpcanary/main/deploy/docker/compose.yaml
docker compose up -d
docker compose logs bgpcanary     # shows the initial admin password
```

With Podman, `podman run` takes the same options as `docker run`.

## HTTPS: a reverse proxy in front

Whichever proxy you use, it must:
- pass on the **Host** header the browser sent. Otherwise set the Site URL; see
  [Troubleshooting](TROUBLESHOOTING.md#refused-this-request-didnt-come-from-this-sites-own-pages).
- send **X-Forwarded-Proto** (`https`) and **X-Forwarded-For**, *replacing* any X-Forwarded-For the browser sent
  rather than adding to it. The login limits count attempts per client address, and this is how bgpcanary learns
  that address.

Caddy and Traefik do all of this by default.

### Caddy in the same Compose project, with automatic certificates

The two files are in [deploy/docker/caddy/](../deploy/docker/caddy/). Replace `bgp.example.com` in both with
your host name, then run `docker compose up -d` in their folder.

`compose.yaml`:

```yaml
services:
  bgpcanary:
    image: ghcr.io/gregsowell/bgpcanary:latest
    container_name: bgpcanary
    restart: unless-stopped
    volumes:
      - bgpcanary-data:/var/lib/bgpcanary
    environment:
      BGPCANARY_BASE_URL: "https://bgp.example.com"
    read_only: true
    tmpfs:
      - /tmp
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    stop_grace_period: 30s

  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data

volumes:
  bgpcanary-data:
  caddy-data:
```

`Caddyfile`, next to it:

```
bgp.example.com {
	reverse_proxy bgpcanary:8080
}
```

bgpcanary publishes no port here, so only Caddy can reach it. Caddy gets a Let's Encrypt certificate by itself
when `bgp.example.com` points at this host and ports 80 and 443 are reachable from the internet. For a name that's
only reachable internally, add a `tls` line inside the braces:
- `tls internal` uses Caddy's own certificate authority. Browsers warn until you trust it.
- `tls /path/to/cert.pem /path/to/key.pem` uses your own certificate (mount the files into the Caddy container).

### nginx on the host

Use it with [compose.yaml](../deploy/docker/compose.yaml), which publishes port 8080 on 127.0.0.1:

```nginx
server {
    listen 443 ssl;
    server_name bgp.example.com;
    ssl_certificate     /etc/ssl/bgp.example.com/fullchain.pem;
    ssl_certificate_key /etc/ssl/bgp.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 120s;
    }
}
```

Use `$remote_addr`, not `$proxy_add_x_forwarded_for`: that way a browser can't add addresses of its own. Other
proxies (Traefik, HAProxy, Nginx Proxy Manager...) need the same three headers.

### Which addresses may send these headers

`BGPCANARY_TRUSTED_PROXIES` lists the proxies bgpcanary believes. The default is the private address ranges
(`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `fc00::/7`). That covers a proxy in another container or on
the Docker host. Connections from the machine itself are always believed: from loopback, or from the container's
own address. With rootless Podman, that's also how a proxy on the host arrives, so it works however the host is
addressed.

The catch: *anything* that can reach port 8080 from those addresses can claim to be any client, and so get
around the per-address login limit. The per-account limits still apply. To close that:
- don't publish port 8080 more widely than your proxy needs. The compose file publishes it on 127.0.0.1 only;
  with the proxy in the same Compose project, don't publish it at all.
- or set `BGPCANARY_TRUSTED_PROXIES` to your proxy's address or network, for example `172.18.0.0/16`.
  `docker network inspect` shows a network's subnet.

## First start

1. Get the initial admin password from `docker logs bgpcanary`, unless you set
   `BGPCANARY_LOG_INITIAL_PASSWORD=0` before first start. In either case, until you change it, the password is
   also in the volume: `docker exec bgpcanary cat /var/lib/bgpcanary/initial-admin-password`.
2. Open the site, sign in as **admin** and choose a new password.
3. Enter your ASN and prefixes. bgpcanary restarts itself inside the container to apply them; the container keeps
   running. It then loads 90 days of history, usually in a few minutes, with a progress bar on every page. Live
   monitoring starts when that's done, and the dashboard switches over by itself.

Then carry on with the [next steps in INSTALL.md](INSTALL.md#first-login): alert channels, users and RIPE Atlas.

## Environment variables

All are optional. Everything else is set in the web UI, as on a server install.

| Variable | Default | Meaning |
|---|---|---|
| `BGPCANARY_BASE_URL` | (none) | The address people use, e.g. `https://bgp.example.com`, for links in alerts. It's read only when the config file is first created. To change it later, use Settings › Alert channels. |
| `BGPCANARY_TRUSTED_PROXIES` | the private ranges | Reverse proxies whose X-Forwarded-For and X-Forwarded-Proto headers are believed, besides the machine itself: addresses or networks, comma-separated. `none` means only the machine itself. See [above](#which-addresses-may-send-these-headers). |
| `BGPCANARY_INSECURE_COOKIES` | off | `1` allows signing in over plain HTTP. Only for testing. |
| `BGPCANARY_LOG_INITIAL_PASSWORD` | on | Set to `0` before first start to keep the initial admin password out of the container log. The owner-only password file remains available until the password is changed. |
| `BGPCANARY_HOST`, `BGPCANARY_PORT` | `0.0.0.0`, `8080` | Where the app listens inside the container (useful with host networking). |
| `BGPCANARY_CONFIG` | `/var/lib/bgpcanary/config.toml` | The config file. It's created on first start if it doesn't exist. Point this at your own file, mounted read-only, if you prefer. |
| `TZ` | `UTC` | Time zone for the few local times the command line prints. Log lines are in UTC; the web UI uses your browser's time zone. |

## The data volume

| File | What it is |
|---|---|
| `bgpcanary.db` (with `-wal` and `-shm`) | the database (SQLite) |
| `config.toml` | settings needed before the app starts, including the random session key ([details](CONFIGURATION.md#the-config-file)) |
| `backups/before-<version>-<date>.db` | a copy taken before each new version first starts; the newest 3 are kept |
| `initial-admin-password` | the generated admin password, until it's changed |

Plan on roughly 0.1–0.3 GB of disk per prefix per year, plus RIPE Atlas results if you use Atlas, plus the
backups.

**A host directory instead of a named volume** has to be writable by the container's user, uid 10001:

```bash
mkdir data && sudo chown 10001:10001 data         # Docker, or Podman as root
podman unshare chown 10001:10001 data             # rootless Podman
```

Then mount it with `-v ./data:/var/lib/bgpcanary`, or `-v ./data:/var/lib/bgpcanary:Z` on systems with
SELinux (Fedora, RHEL and its clones). If the directory isn't writable, the container stops, and `docker logs`
says what to do.

## Admin commands

```bash
docker exec -it bgpcanary bgpcanary-admin users                      # list accounts
docker exec -it bgpcanary bgpcanary-admin useradd NAME --role viewer
docker exec -it bgpcanary bgpcanary-admin useradd admin              # reset a password
docker exec -it bgpcanary bgpcanary-admin userdel NAME
docker exec -it bgpcanary bgpcanary-admin test-alert                 # send a test alert on every channel
docker logs -f bgpcanary                                              # live log
```

`bgpcanary` here is the container's name. `docker compose exec bgpcanary bgpcanary-admin ...` works whatever the
container is called.

## Updating

```bash
docker compose pull && docker compose up -d
```

Without Compose: `docker pull ghcr.io/gregsowell/bgpcanary:latest`, then remove the container and start it again
with the same options.

Before a new version first starts, it backs the database up into `backups/`, then makes any database changes it
needs. The web UI is away for a few seconds. As on a server install, the update badge and Settings › About &
updates tell admins when a release is out.

To choose when you move to new features, use a tag like `1.3` (bug fixes only) or an exact version.

## Going back to an earlier version

Run the previous version's image, e.g. `image: ghcr.io/gregsowell/bgpcanary:1.3.0` in compose.yaml, then
`docker compose up -d`. Database changes are only ever additive, so an older version works with a newer
database. The exception is the upstream chains in the visibility history: since 1.3.2 they're stored only when
they change, so 1.3.1 and earlier show the upstream chains chart and the event pages' "who carried" table mostly
empty. Restore the backup taken before the update to get them back.

If you'd rather have the database exactly as it was before the update, restore the backup taken then. Anything
recorded since that backup is lost, so keep a copy of the current database first ([Backups](#backups)).

```bash
docker compose stop bgpcanary
docker compose run --rm bgpcanary ls /var/lib/bgpcanary/backups
docker compose run --rm bgpcanary sh -c 'umask 077 && cd /var/lib/bgpcanary && rm -f bgpcanary.db-wal bgpcanary.db-shm && cp backups/before-1.3.1-20261101-120000.db bgpcanary.db'
docker compose up -d
```

## Backups

To copy the database while bgpcanary runs, straight to the Docker host, and the config file with it:

```bash
umask 077
docker exec bgpcanary bgpcanary-admin backup - > bgpcanary-$(date +%F).db
docker exec bgpcanary cat /var/lib/bgpcanary/config.toml > bgpcanary-config.toml
```

Don't add `-t` to these commands: a terminal would mangle the data. Keep both files private, because the
database holds password hashes and API keys. The config file holds the session key; a database restored without
it works, but everyone has to sign in again.

To restore one:

```bash
docker compose stop bgpcanary
docker compose run --rm -T bgpcanary sh -c 'umask 077 && rm -f /var/lib/bgpcanary/bgpcanary.db-wal /var/lib/bgpcanary/bgpcanary.db-shm && cat > /var/lib/bgpcanary/bgpcanary.db' < bgpcanary-2026-11-01.db
docker compose up -d
```

## Moving a server install into a container

1. On the server (1.3.0 or later):

   ```bash
   umask 077
   sudo bgpcanary-admin backup - > bgpcanary.db
   sudo cat /etc/bgpcanary/config.toml > config.toml
   ```

2. Copy both files to the Docker host, into the folder with compose.yaml. If you started the container there
   already, stop it first (`docker compose stop bgpcanary`); these commands replace its database.

   ```bash
   docker compose run --rm -T bgpcanary sh -c 'umask 077 && rm -f /var/lib/bgpcanary/bgpcanary.db-wal /var/lib/bgpcanary/bgpcanary.db-shm && cat > /var/lib/bgpcanary/bgpcanary.db' < bgpcanary.db
   docker compose run --rm -T bgpcanary sh -c 'umask 077 && cat > /var/lib/bgpcanary/config.toml' < config.toml
   docker compose up -d
   ```

   Users, settings, history and alert channels all come along. The server's config file works as it is: the
   database path is the same inside the container, and the listen address in it isn't used there. If people
   will reach the container at a different address, update the Site URL under Settings › Alert channels.

3. Once it works, stop the server install: `sudo systemctl disable --now bgpcanary`, or remove it with
   `sudo bgpcanary-uninstall`.

## Health and monitoring

The image has a health check, so `docker ps` shows `(healthy)`. For outside monitoring, `GET /healthz` returns
`{"ok": true, "version": ...}`.

## Security

- The app runs as uid 10001 with no Linux capabilities, and works on a read-only root filesystem (the compose
  file sets that up). It writes only to its volume and `/tmp`.
- The Path finder's traceroute needs no capabilities either: it sends UDP probes (ICMP where the container allows
  unprivileged ping) and reads the routers' answers from the socket. It works on the default bridge network and in
  rootless Podman. Networking that re-sends packets instead of forwarding them hides the hops, and the page says so;
  see [Troubleshooting](TROUBLESHOOTING.md#path-finder-the-traceroute-shows-nothing-or-stops-early).
- Images are built by GitHub Actions from the tagged source, tested on both architectures, and published with a
  signed build provenance. To check that an image came from this repository:

  ```bash
  gh attestation verify oci://ghcr.io/gregsowell/bgpcanary:1.3.0 --owner gregsowell
  ```

- Each release's image includes the Alpine and Python security fixes available when it was built. Later fixes
  arrive with the next release, so keep updating.

## Building the image yourself

```bash
git clone https://github.com/gregsowell/bgpcanary.git && cd bgpcanary
docker build -t bgpcanary .
deploy/docker/smoke-test.sh bgpcanary      # optional: the checks CI runs on every change
```
