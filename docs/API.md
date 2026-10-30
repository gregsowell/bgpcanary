# The API

bgpcanary has a small JSON API for your own scripts and dashboards: is anything down, what happened, what is
this network to me. It's read-only, it lives under `/api/v1/`, and it's used with a per-user token.

```bash
curl -s -H "Authorization: Bearer bgpc_…" https://bgp.example.com/api/v1/status | jq '.prefixes[] | {prefix, state, pct}'
```

## Tokens

1. **Settings › API tokens** (any signed-in user): name the token after what it's for and create it.
2. **Copy it when it's shown.** bgpcanary keeps only a hash of it, so it can't be shown again; if it's lost,
   revoke it and create another.
3. Send it in the `Authorization` header of every request: `Authorization: Bearer bgpc_…`.

A token can read what its owner can: a viewer's token is read-only like the viewer, and in this version the
whole API is read-only. Up to 20 tokens per account. Changing a password doesn't revoke tokens; they stop at
once when revoked (Settings › API tokens, or by an admin on the Users page), when their owner is deleted, and
while an account must change its password. Bad tokens are rate-limited per address (HTTP 429 for a while).

Tokens are accepted only on `/api/v1/…`, only in the header: never put one in a URL (it would end up in logs and
browser histories), and don't try it on the pages. A browser that's signed in can open the API's addresses
directly, which is handy for a first look at the shapes; the session cookie works there too.

## Conventions

- **JSON** objects. Times are **unix seconds** (`start_ts`, `end_ts`, `last_message`), as in the database and the
  webhook payload; a client turns them into local time.
- **Errors** are `{"error": "…"}` with the status code: 400 a bad parameter (the message names it), 401 no or
  invalid token, 403 the account must change its password, 404, 429 too many bad tokens from your address,
  503 bgpcanary isn't set up yet.
- **Lists** take `limit` (default 100, at most 500) and `offset`, and say `total`.
- The unauthenticated `GET /healthz` says whether bgpcanary is up at all (below).

### What's promised

Within v1, a field is only ever **added**, never renamed, removed or given another type. A release that must
break that starts a v2 beside v1 and says so in the changelog. Three things are deliberately *not* promised:

- an event's `detail` is kind-specific and changes as detection improves; read the documented fields, and treat
  `detail` as extra;
- `phase` is the status line people read in the header, free text; use `live` and each prefix's `state`;
- the pages' own routes (`/api/status`, `/api/series`, `/api/replay`, … without `v1`) are internal, undocumented,
  and change with the pages.

The test suite holds this contract (`tests/test_api.py`).

## Endpoints

### `GET /healthz` (no token)

Is bgpcanary up: `{"ok": true, "version": "1.7.0", "started": 1761782400.1, "phase": "live", "configured": true}`.
`configured` is false until an ASN and a prefix are set. The container's health check and `bgpcanary-update` use
it.

### `GET /api/v1/me`

Who the token is: `{"user": "grafana", "role": "viewer", "auth": "token", "token": "grafana panel"}`. `auth` is
`token` or `session`; `token` is the token's name, or null for a browser session.

### `GET /api/v1/status`

The monitor right now: the dashboard's table.

```json
{
  "version": "1.7.0",
  "phase": "live",
  "live": true,
  "ris_connected": true,
  "last_message": 1761782400.5,
  "asn": 64500,
  "expected_upstreams": [{"asn": 64496, "name": "UPSTREAM-ONE"}, {"asn": 64497, "name": "UPSTREAM-TWO"}],
  "open_events": 1,
  "prefixes": [
    {
      "prefix": "192.0.2.0/24",
      "state": "ok",
      "visible": 318,
      "baseline": 320.0,
      "pct": 99.4,
      "upstreams": [{"asn": 64496, "name": "UPSTREAM-ONE", "paths": 190}, {"asn": 64497, "name": "UPSTREAM-TWO", "paths": 120}],
      "peers": [{"asn": 64498, "name": "PEER-ONE", "exchange": "Metro-IX", "paths": 8}],
      "missing": []
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `live` | Live monitoring is running. False while history loads or RIS Live is reconnecting (`phase` says what's going on); every prefix's `state` is then `starting`. |
| `ris_connected`, `last_message` | The RIS Live feed, and when its last message arrived. |
| `open_events` | Warning and critical events still going on (the Events page's "open" filter). |
| `state` | `ok`, `degraded` (visibility below the threshold, not yet for the sustain time), `down` (an outage is open) or `starting`. |
| `visible`, `baseline`, `pct` | RIS peers seeing the prefix now, the normal count (the 95th percentile of the last 24 h), and the first as a percentage of the second. Null until there is a baseline. |
| `upstreams` | The neighbours your paths come in through that aren't peers: your transits, plus any network you haven't filed as anything (the dashboard shows those as "unexpected"). `paths` is how many RIS peers reach you through it. Most paths first. |
| `peers` | Peers at your exchanges (from PeeringDB) and listed peers (`exchange` is then "Private peering"). |
| `missing` | Expected upstreams carrying none of your paths right now. |

### `GET /api/v1/events`

The events, newest first, with the Events page's filters:

| Parameter | |
|---|---|
| `state` | `open` (still going on) or `closed` |
| `kind` | One kind: `outage`, `blip`, `upstream_down`, `unexpected_upstream`, `origin_hijack`, … (the `kind` values the list shows) |
| `severity` | `info`, `warning` or `critical` |
| `info` | `1` to include info events, which are left out unless asked for, as on the page (`severity=info` asks for them too) |
| `prefix` | One monitored prefix |
| `since`, `until` | Unix seconds: the events going on at some point in that window (an open event counts as lasting until now) |
| `limit`, `offset` | Paging; `limit` at most 500 |

```json
{
  "total": 3,
  "limit": 100,
  "offset": 0,
  "events": [
    {
      "id": 412,
      "kind": "upstream_down",
      "kind_name": "Upstream down",
      "severity": "warning",
      "prefix": "192.0.2.0/24",
      "start_ts": 1761780000.0,
      "end_ts": null,
      "open": true,
      "title": "Upstream AS64497 (UPSTREAM-TWO) down: carrying 2 of its usual 120 paths",
      "min_visible": 196,
      "baseline": 320.0,
      "historical": false,
      "url": "https://bgp.example.com/events/412",
      "neighbour": null,
      "detail": {"…": "kind-specific"}
    }
  ]
}
```

`id`, `kind`, `severity`, `prefix`, `start_ts`, `end_ts`, `title`, `min_visible` and `baseline` are the same
fields the webhook payload carries. `open` is `end_ts == null`; `historical` marks events found in a history load
or gap fill rather than live; `url` is the event page (a full link when the Site URL is set in Settings › Alert
channels, else the path); `neighbour` is `{"asn", "name"}` for the events about one neighbouring network
(unexpected or new upstream, new peer, peer leak) and null otherwise; `detail` is the kind-specific record (not
part of the promise, see above).

### `GET /api/v1/events/{id}`

One event, as above, plus:

- `alerts`: what was sent for it, `[{"ts": 1761780060.2, "channel": "email", "ok": true, "error": null}, …]`
  (`channel` is `email` or `webhook:<name>`);
- `report_url`: where its text report is.

404 `{"error": "no such event"}` for an unknown id.

### `GET /api/v1/events/{id}/report`

The event's evidence as plain text (`text/plain`), the same report the event page offers to attach to a ticket
with a provider: the timeline, the upstream mix before and during, the raw AS paths, Route Views output and
RIPE Atlas results.

### `GET /api/v1/neighbours/{asn}`

A neighbouring network: what it is to you, and the events about it.

```json
{
  "asn": 64499,
  "name": "STRANGER",
  "role": null,
  "exchange": null,
  "paths_now": 3,
  "events": [{"id": 390, "kind": "unexpected_upstream", "…": "…"}]
}
```

`role` is `transit` (one of your expected upstreams), `peer` (a listed peer), `exchange_peer` (a peer at one of
your exchanges, per PeeringDB) or null for a network you haven't filed as anything, which is what the dashboard's
"unexpected" badge means; `exchange` names the exchange(s) for a peer; `paths_now` is how many RIS peers reach you
through it right now; `events` are the latest 20 about it, in the list's shape.

## Examples

Is anything down? (exit status 1 if so)

```bash
curl -sf -H "Authorization: Bearer $BGPCANARY_TOKEN" https://bgp.example.com/api/v1/status \
  | jq -e '[.prefixes[] | select(.state == "down")] | length == 0' > /dev/null
```

The open events, one line each:

```bash
curl -s -H "Authorization: Bearer $BGPCANARY_TOKEN" "https://bgp.example.com/api/v1/events?state=open" \
  | jq -r '.events[] | "\(.severity)\t\(.prefix)\t\(.title)\t\(.url)"'
```

Python:

```python
import os
import requests

api = "https://bgp.example.com/api/v1"
headers = {"Authorization": f"Bearer {os.environ['BGPCANARY_TOKEN']}"}
status = requests.get(f"{api}/status", headers=headers, timeout=10).json()
for p in status["prefixes"]:
    print(p["prefix"], p["state"], p["visible"], "of", p["baseline"])
```

A Grafana panel: the Infinity data source (or any JSON data source) reads `/api/v1/status` with the header above;
`$.prefixes[*].pct` is one gauge per prefix, `$.open_events` a stat.

## The push side: webhooks

bgpcanary also pushes: **Settings › Alert channels** sends every alert to Slack, Microsoft Teams, or as generic
JSON to any URL, when an event opens, escalates, or ends:

```json
{
  "phase": "open",
  "state": "WARNING",
  "subject": "[BGP WARNING] Upstream AS64497 (UPSTREAM-TWO) down: carrying 2 of its usual 120 paths",
  "text": "…the alert's text…",
  "link": "https://bgp.example.com/events/412",
  "event": {
    "id": 412, "kind": "upstream_down", "severity": "warning", "prefix": "192.0.2.0/24",
    "start_ts": 1761780000.0, "end_ts": null, "title": "Upstream AS64497 (UPSTREAM-TWO) down: carrying 2 of its usual 120 paths",
    "min_visible": 196, "baseline": 320.0
  }
}
```

`event` carries the same fields the API's events do, so a receiver can follow up with `/api/v1/events/{id}` for
the alerts sent and the report. `phase` is `open`, `escalate` or `close` (`test` for a test alert); `state` is the
severity in capitals (`WARNING`, `CRITICAL`), `FYI` for an info event, `ESCALATED`, `RESOLVED` or `TEST`; `subject`
starts with the same word in brackets, `[BGP WARNING] …`.

## What isn't in the API

Acting on bgpcanary (filing a neighbour as a peer, starting a traceroute, changing settings) stays in the web UI
for now, as do the replay trees, the visibility series and the Path finder: those are the pages' own data and
change with them. If your script needs something that isn't here, [open an issue](https://github.com/gregsowell/bgpcanary/issues)
and say what you'd do with it.
