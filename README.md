# bgpcanary

**An early warning for your BGP routes.** bgpcanary watches how the rest of the internet sees the prefixes
your network announces. It alerts you when they disappear, get hijacked, leak, or suddenly move between your
upstream providers. With the optional RIPE Atlas checks it also tests whether traffic actually reaches you.
For every incident it builds an evidence pack you can send to the provider responsible.

It's for networks with their own ASN: ISPs, hosting companies, enterprises, universities. It needs **no BGP
session and no access to your routers**: everything comes from public route collectors and measurement
networks.

<!-- Screenshots: add images to docs/images/ and reference them here, e.g.
![Dashboard](docs/images/dashboard.png)
![Connectivity tree during an outage](docs/images/tree.png) -->

## What it does

- **Live visibility:** how many of RIPE RIS's ~330 peers see each prefix, in 30-second steps, with 90 days of history loaded on day one.
- **Connectivity tree:**
  - An interactive map from your ASN out through your upstreams and their carriers, showing who carries you and who doesn't.
  - All your prefixes in one tree by default (one colored strand each), or tick just the ones you want.
  - Side-by-side comparison of several prefixes.
- **Detection and alerts** (email, Slack, Microsoft Teams or any webhook), each type switchable with its own thresholds:
  - outages and brief dips;
  - origin hijacks, more-specific and covering announcements, your ASN in someone else's path;
  - missing or unexpected upstreams;
  - **upstream shifts**: a large share of the internet switching providers within minutes, such as a session flap;
  - **reachability loss**: RIPE Atlas probes can't reach your addresses, even when BGP looks fine.
- **Background checks:**
  - transits that carry your upstream's routes but not yours, usually a prefix-filter problem;
  - IRR route objects, as-sets, RPKI/ROA validity, and simulated upstream filters, with a change log.
- **Route Views:** raw `show ip bgp` from several collectors, plus ping and traceroute toward you, on a schedule and when something breaks.
- **RIPE Atlas (optional):**
  - ping and traceroute from probes in thousands of networks;
  - latency and loss per carrier, radial traceroute maps;
  - several targets compared side by side (for example, each border router's peering address);
  - one-off tests to any address.
- **Evidence for providers:** every event page collects the timeline, upstream mix, raw AS paths, Route Views output and Atlas results. One click turns it into a plain-text report to attach to a ticket.
- **Multi-prefix and IPv6.** Viewer and admin roles. Everything configurable in the web UI.

## Quick start

On a fresh **Rocky Linux, AlmaLinux or RHEL 9 or 10** server (2 vCPU / 2 GB RAM / 20 GB disk is plenty):

```bash
sudo dnf -y install git
git clone https://github.com/gregsowell/bgpcanary.git
cd bgpcanary
sudo ./deploy/install.sh --fqdn bgp.example.com
```

Open `https://bgp.example.com/`, sign in as `admin` with the password the installer printed, choose a new
password, and enter your ASN and prefixes. Monitoring starts within seconds; 90 days of history load in the
background.

Full instructions, including DNS, certificates and firewall: **[docs/INSTALL.md](docs/INSTALL.md)**.

## Updating

```bash
sudo bgpcanary-update
```

It backs up your database and settings, installs the latest release, and checks it's healthy;
`--rollback` undoes it. Admins see an "update available" badge in the web UI when a new release is out.
See **[docs/UPGRADING.md](docs/UPGRADING.md)**.

## Documentation

| | |
|---|---|
| [Installation](docs/INSTALL.md) | Requirements, install, first login, certificates, uninstalling |
| [Upgrading](docs/UPGRADING.md) | Updates, backups, rolling back |
| [Configuration](docs/CONFIGURATION.md) | Every setting, alerts, RIPE Atlas, the command line, where files live |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | Common problems and how to fix them |
| [Changelog](CHANGELOG.md) | What changed in each release |
| [Contributing](CONTRIBUTING.md) | Reporting bugs, suggesting features, sending changes |
| [Security](SECURITY.md) | Reporting a vulnerability |

## Data sources

bgpcanary is built on public infrastructure run by the networking community. It uses each source politely:
rate limits are respected, and the RIPEstat requests identify themselves. Please use them the same way.

| Source | Used for |
|---|---|
| [RIPE RIS Live](https://ris-live.ripe.net/) | Real-time BGP updates from ~330 peers (the main detector) |
| [RIPEstat](https://stat.ripe.net/) | Current state (looking glass), per-second history (BGPlay), AS names and neighbours |
| [Route Views](https://www.routeviews.org/) | `show ip bgp`, ping and traceroute from their collectors (telnet; kept within their daily limits) |
| [RADb IRR](https://www.radb.net/), [PeeringDB](https://www.peeringdb.com/), RPKI via RIPEstat | Registry watch |
| [RIPE Atlas](https://atlas.ripe.net/) (optional) | Ping and traceroute from probes worldwide; needs your own API key and credits |
| [Team Cymru](https://www.team-cymru.com/ip-asn-mapping) | IP-to-ASN lookups for traceroute hops |

## Support

bgpcanary is made by [The Brothers WISP](https://thebrotherswisp.com). If it helps your network, please
support us on [Patreon](https://www.patreon.com/thebrotherswisp).

## License

bgpcanary is **source-available** under the [Functional Source License 1.1 with an Apache 2.0 future license](LICENSE.md) (FSL-1.1-ALv2):

- **You may** use it for free, including in your business to monitor your own networks, modify it, and run it for clients as part of professional services.
- **You may not** sell it, or offer it (or something substantially similar built from it) as a commercial product or hosted service that competes with bgpcanary, without a separate agreement. For commercial licensing, contact the author.
- **Two years after each release**, that release becomes available under the Apache License 2.0.

Copyright 2026 Greg Sowell. Third-party components downloaded at install time keep their own licenses:
Chart.js (MIT) and gif.js (MIT).
