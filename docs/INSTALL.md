# Installing bgpcanary

## Requirements

- **Operating system:** Rocky Linux, AlmaLinux or Red Hat Enterprise Linux **9 or 10**, x86-64. A fresh minimal install is ideal; bgpcanary brings its own nginx configuration.
- **Size:** 2 vCPU, 2 GB RAM and 20 GB disk is plenty for a few prefixes. The database grows by roughly 0.5–1 GB per prefix per year, and the installer keeps the last three backups in `/var/backups/bgpcanary`.
- **Outbound network access:**

  | To | Port | For |
  |---|---|---|
  | `ris-live.ripe.net`, `stat.ripe.net`, `atlas.ripe.net` | 443 | RIPE RIS, RIPEstat, RIPE Atlas |
  | `www.peeringdb.com`, `api.github.com`, `github.com`, `cdnjs.cloudflare.com` | 443 | PeeringDB, update check / updates, chart libraries |
  | `*.routeviews.org` | 23 (telnet) | Route Views snapshots, ping, traceroute |
  | `whois.radb.net`, `whois.cymru.com` | 43 (whois) | IRR registry watch, IP-to-ASN lookups |

- **Inbound:** 80 and 443 from wherever you'll open the web UI. The installer opens them in firewalld if it's running.
- **A DNS name** for the server (e.g. `bgp.example.com`). This is optional but recommended, and needed for a real certificate.

## Install

```bash
sudo dnf -y install git
git clone https://github.com/gregsowell/bgpcanary.git
cd bgpcanary
sudo ./deploy/install.sh --fqdn bgp.example.com
```

The installer takes a few minutes. It:
- installs Python, nginx and SQLite from your OS repositories;
- creates a `bgpcanary` system user;
- installs the app in `/opt/bgpcanary`;
- creates a self-signed certificate;
- configures nginx;
- starts the `bgpcanary` service;
- creates an `admin` login with a random password, which it prints and also saves in `/etc/bgpcanary/initial-admin-password`.

Prefer not to use git? Download the release `.tar.gz` from the [Releases page](https://github.com/gregsowell/bgpcanary/releases), then:

```bash
tar xzf bgpcanary-*.tar.gz && cd bgpcanary-* && sudo ./deploy/install.sh --fqdn bgp.example.com
```

### Pre-filling your settings (optional)

You can enter everything in the web UI, or pass it to the installer:

```bash
sudo ./deploy/install.sh --fqdn bgp.example.com --asn 64500 \
     --prefix 192.0.2.0/24 --prefix 2001:db8::/48 \
     --upstream 64501 --upstream 64502 --probe 192.0.2.1
```

| Option | Meaning |
|---|---|
| `--fqdn` | Host name for the web UI and certificate |
| `--asn` | Your AS number |
| `--prefix` | A prefix you announce (repeat for each; IPv4 and IPv6) |
| `--upstream` | The AS number of a provider you buy transit from (repeat for each) |
| `--probe` | An address in your space that answers ping, for Route Views and RIPE Atlas tests |
| `--atlas-probe` | Also install the RIPE Atlas software probe (see [Configuration](CONFIGURATION.md#ripe-atlas)) |

## First login

1. Open `https://<your host>/`. Your browser will warn about the self-signed certificate until you replace it (see below).
2. Sign in as **admin** with the password the installer printed.
3. Choose a new password; you'll be asked to straight away.
4. Enter your **ASN**, **prefixes**, **upstreams** and **probe targets**, then save.

Monitoring starts about 10 seconds later. Up to 90 days of history load in the background over the next few
minutes; the dashboard shows progress.

Next steps:
- **Settings › Alert channels:** add an email address or a Slack webhook. Otherwise nobody gets told when something breaks. Use "Send test alert" to check delivery.
- **Settings › Alert channels › What to alert on:** choose which alert types notify you.
- **Users:** add accounts for your team; viewers are read-only.
- *(Optional)* **Settings › RIPE Atlas:** see [Configuration](CONFIGURATION.md#ripe-atlas).

## Using a real certificate

The certificate lives at `/etc/pki/tls/certs/bgpcanary.crt`, with its key at `/etc/pki/tls/private/bgpcanary.key`.
Replace both files with your own (full chain in the `.crt`), then run `sudo systemctl reload nginx`.
Updates never overwrite them.

**Let's Encrypt** works if the server is reachable from the internet on port 80:

```bash
sudo dnf -y install epel-release && sudo dnf -y install certbot
sudo certbot certonly --webroot -w /usr/share/nginx/html -d bgp.example.com
sudo ln -sf /etc/letsencrypt/live/bgp.example.com/fullchain.pem /etc/pki/tls/certs/bgpcanary.crt
sudo ln -sf /etc/letsencrypt/live/bgp.example.com/privkey.pem /etc/pki/tls/private/bgpcanary.key
sudo systemctl reload nginx
```

certbot renews the certificate automatically; to make nginx pick up renewals, add
`--deploy-hook "systemctl reload nginx"` to the `certonly` command.

## Uninstalling

```bash
sudo bgpcanary-uninstall           # removes the program; keeps your database and settings
sudo bgpcanary-uninstall --purge   # also deletes the database, backups, settings and certificate
```

nginx, Python and SQLite stay installed, because other software may use them.
