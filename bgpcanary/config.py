"""Configuration loading (TOML)."""
import ipaddress
import os
import re
import tomllib
from dataclasses import dataclass, field

DEFAULT_PATH = os.environ.get("BGPCANARY_CONFIG", "/etc/bgpcanary/config.toml")


@dataclass
class EmailCfg:
    enabled: bool = False
    smtp_host: str = "localhost"
    smtp_port: int = 587
    starttls: bool = True
    ssl: bool = False
    username: str = ""
    password: str = ""
    sender: str = "bgpcanary@localhost"
    recipients: list = field(default_factory=list)


@dataclass
class WebhookCfg:
    url: str
    format: str = "slack"  # slack | json
    name: str = ""
    enabled: bool = True


@dataclass
class Config:
    asn: int
    prefixes: list
    expected_upstreams: list
    owned_space: list
    max_unmonitored: int
    db_path: str
    listen_host: str
    listen_port: int
    secret_key: str
    site_name: str
    bucket_seconds: int
    outage_threshold_pct: float
    recover_threshold_pct: float
    sustain_seconds: int
    shift_pct: float
    shift_window_seconds: int
    shift_quiet_seconds: int
    loss_alert_pct: float
    baseline_hours: int
    reconcile_minutes: int
    stale_days: int
    backfill_days: int
    transit_filter_check: bool
    transit_filter_min_peers: int
    transit_filter_minutes: int
    registry_watch: bool
    registry_minutes: int
    covering_ignore: list
    covering_min_length: int
    raw_retention_days: int
    rv_servers: list
    rv_poll_minutes: int
    probe_targets: list
    probe_minutes: int
    probe_on_event: bool
    rv_followup_change_pct: float
    alert_min_severity: str
    alert_kinds: dict   # alert type -> ALERT_MODES choice (saved in the UI; missing = from alert_min_severity)
    base_url: str
    email: EmailCfg
    webhooks: list

    def prefix_nets(self):
        return [ipaddress.ip_network(p) for p in self.prefixes]

    @property
    def configured(self):
        """False until an ASN and at least one prefix are set (fresh install -> first-run setup page)."""
        return bool(self.asn) and bool(self.prefixes)


def normalize_prefix(p: str) -> str:
    return str(ipaddress.ip_network(p.strip(), strict=False))


# Monitoring settings edited in the web UI (kv "monitor_settings") override [monitor] / probe targets /
# site name from the config file. They take effect on the service restart that saving triggers.
MONITOR_KV = "monitor_settings"


def monitor_settings_dict(cfg) -> dict:
    return {"asn": cfg.asn, "prefixes": list(cfg.prefixes), "expected_upstreams": list(cfg.expected_upstreams),
            "owned_space": list(cfg.owned_space), "probe_targets": list(cfg.probe_targets),
            "site_name": cfg.site_name}


def apply_monitor_settings(cfg, data: dict | None) -> None:
    if not data:
        return
    cfg.asn = int(data.get("asn", cfg.asn) or 0)
    cfg.prefixes = [normalize_prefix(x) for x in data.get("prefixes", cfg.prefixes)]
    cfg.expected_upstreams = [int(x) for x in data.get("expected_upstreams", cfg.expected_upstreams)]
    cfg.owned_space = [normalize_prefix(x) for x in data.get("owned_space", cfg.owned_space)]
    cfg.probe_targets = list(data.get("probe_targets", cfg.probe_targets))
    cfg.site_name = data.get("site_name") or cfg.site_name


# Detection / data-source tuning edited in the web UI (kv "tuning_settings"). Everything except the bootstrap
# settings (db_path, listen address/port, secret key) can be changed there; the config file only supplies
# defaults. Each entry: attribute, section, label, kind, options. kinds: int, float, bool, choice, prefixes, hosts
TUNING_KV = "tuning_settings"
TUNING = [
    ("outage_threshold_pct", "Outage detection", "Declare an outage below (% of normal visibility)", "float",
     {"min": 10, "max": 99, "help": "80 means an outage opens when fewer than 80% of the RIS peers that normally "
                                    "see a prefix still see it, i.e. a drop of more than 20%. Lower = less "
                                    "sensitive.", "pct_of_baseline": True}),
    ("sustain_seconds", "Outage detection", "Sustain time (seconds)", "int",
     {"min": 0, "max": 3600, "help": "How long visibility must stay below the threshold before an outage opens "
                                     "(and above the recovery level before it closes). Shorter drops are logged "
                                     "as brief dips."}),
    ("recover_threshold_pct", "Outage detection", "Consider it recovered at (% of normal visibility)", "float",
     {"min": 10, "max": 100, "help": "An outage closes once visibility is back at or above this share of normal "
                                     "(for the sustain time).", "pct_of_baseline": True}),
    ("shift_pct", "Upstream shift detection", "Warn when this share of peers moves between upstreams (% of normal)",
     "float", {"min": 0, "max": 100, "help": "A big, fast move of RIS peers from one of your upstreams to another while "
                                             "visibility stays up: a session flap or re-route that an outage alert "
                                             "wouldn't catch. Opens a warning and runs checks (Route Views, and RIPE "
                                             "Atlas pings/traceroutes if Atlas is on). 0 = off.",
                                             "pct_of_baseline": True}),
    ("shift_window_seconds", "Upstream shift detection", "...within (seconds)", "int",
     {"min": 60, "max": 3600, "help": "How quickly the move has to happen to count as a shift."}),
    ("shift_quiet_seconds", "Upstream shift detection", "Consider it over after stable for (seconds)", "int",
     {"min": 60, "max": 7200, "help": "The shift event closes once the upstream mix stops moving for this long."}),
    ("loss_alert_pct", "Upstream shift detection", "Critical alert when RIPE Atlas probes can't reach you (% of probes)",
     "float", {"min": 0, "max": 100, "help": "If at least this share of the probes in one round of Atlas pings to your "
                                             "own addresses (baseline targets, shift checks) gets no reply at all, a "
                                             "critical 'reachability loss' event opens, whatever BGP shows. Needs RIPE "
                                             "Atlas. 0 = off."}),
    ("baseline_hours", "Outage detection", "Baseline window (hours)", "int",
     {"min": 1, "max": 168, "help": "The baseline is the 95th percentile of visibility over this window."}),
    ("bucket_seconds", "Outage detection", "Chart and detection bucket size (seconds)", "choice",
     {"choices": [10, 15, 30, 60], "help": "Resolution of the stored visibility history. Changing it only "
                                           "affects data recorded from now on."}),
    ("covering_min_length", "Hijack and leak detection", "Ignore covering announcements shorter than /", "int",
     {"min": 0, "max": 64, "help": "Default routes and very short prefixes leaked into RIS feeds are noise, "
                                   "not a covering hijack."}),
    ("covering_ignore", "Hijack and leak detection", "Covering prefixes to ignore", "prefixes",
     {"help": "Known aggregates that contain your space (e.g. your provider's block), one per line."}),
    ("max_unmonitored", "Hijack and leak detection", "Max unmonitored prefixes to track", "int",
     {"min": 10, "max": 5000, "help": "Cap on prefixes your ASN appears in but you don't monitor. Protects "
                                      "large networks from flooding the database."}),
    ("transit_filter_min_peers", "Transit filter check", "Minimum peers for a transit to count", "int",
     {"min": 1, "max": 200, "help": "Ignore an upstream's transits that carry its own prefixes to fewer RIS "
                                    "peers than this."}),
    ("transit_filter_minutes", "Transit filter check", "Check every (minutes)", "int",
     {"min": 2, "max": 1440, "help": "The check is switched on or off with its alert type (Alert channels › What to alert on)."}),
    ("registry_minutes", "Registry watch", "Check every (minutes)", "int",
     {"min": 10, "max": 1440, "help": "IRR, as-set, filter simulation and RPKI checks. Switched on or off "
                                      "with its alert types (Alert channels) or on the Registry page."}),
    ("reconcile_minutes", "Data sources", "Looking-glass resync every (minutes)", "int",
     {"min": 1, "max": 120, "help": "When the stream is quiet, re-check state against the RIPEstat looking "
                                    "glass."}),
    ("stale_days", "Data sources", "Drop routes not refreshed for (days)", "int",
     {"min": 0, "max": 365, "help": "A RIS peer's route that hasn't been updated for this long AND isn't confirmed by "
                                    "the RIPEstat looking glass is treated as stale (a missed withdrawal or a 'zombie' "
                                    "route) and removed, so it doesn't show as a live path. 0 = never."}),
    ("rv_servers", "Data sources", "Route Views collectors", "hosts",
     {"help": "One per line. The first one is also used for ping and traceroute (route-views.routeviews.org "
              "allows both)."}),
    ("rv_poll_minutes", "Data sources", "\"show ip bgp\" snapshots every (minutes)", "int",
     {"min": 0, "max": 1440, "help": "0 turns scheduled snapshots off (they still run on outages). Route Views "
                                     "rate-limits each IP to 100 connections and 500 KB per collector per day, "
                                     "and bgpcanary keeps scheduled use under half of that. Routine polling is "
                                     "kept slow (default 12 hours) because outages trigger fresh snapshots "
                                     "automatically."}),
    ("probe_minutes", "Data sources", "Ping and traceroute every (minutes)", "int",
     {"min": 0, "max": 1440, "help": "0 turns scheduled probes off. Targets are set under Monitoring."}),
    ("probe_on_event", "Data sources", "Query Route Views immediately when an outage or other problem is detected",
     "bool", {"help": "Snapshots from every collector plus ping and traceroute when an outage, missing upstream, "
                      "hijack, more-specific or covering announcement opens, and again when it clears."}),
    ("rv_followup_change_pct", "Data sources", "During an outage, re-query only on a change of at least (% of normal)",
     "float", {"min": 0, "max": 100, "help": "While an outage is open bgpcanary watches the live RIS data (free) and "
                                             "queries Route Views again only when visibility moves by this much, "
                                             "or an upstream path appears or disappears. At least 5 min apart, "
                                             "max 12 per outage. 0 = only when it opens and when it clears."}),
    ("backfill_days", "History and storage", "History to load for new prefixes (days)", "int",
     {"min": 0, "max": 365, "help": "Loaded from RIPEstat BGPlay when a prefix is first monitored."}),
    ("raw_retention_days", "History and storage", "Keep raw BGP updates for (days)", "int",
     {"min": 7, "max": 3650, "help": "Visibility history and events are kept forever. Raw updates are the "
                                     "bulk of the database."}),
]


def tuning_dict(cfg) -> dict:
    return {attr: getattr(cfg, attr) for attr, *_ in TUNING}


def coerce_tuning(attr, kind, opts, value):
    """Validate/convert one tuning value (from the web form or the database). Raises ValueError."""
    if kind in ("int", "float"):
        try:
            v = int(value) if kind == "int" else float(value)
        except (TypeError, ValueError):
            raise ValueError("must be a whole number" if kind == "int" else "must be a number") from None
    elif kind == "bool":
        return bool(value) and value not in ("0", "false", "off")
    elif kind == "choice":
        v = int(value)
        if v not in opts["choices"]:
            raise ValueError(f"must be one of {opts['choices']}")
        return v
    elif kind == "prefixes":
        items = value if isinstance(value, list) else re.split(r"[\s,;]+", value or "")
        return [normalize_prefix(x) for x in items if x]
    elif kind == "hosts":
        items = value if isinstance(value, list) else re.split(r"[\s,;]+", value or "")
        out = [x.strip().lower() for x in items if x.strip()]
        for h in out:
            if not re.match(r"^[a-z0-9.-]+$", h):
                raise ValueError(f"{h} isn't a hostname")
        return out
    else:
        raise ValueError("unknown kind")
    if "min" in opts and v < opts["min"] or "max" in opts and v > opts["max"]:
        raise ValueError(f"must be between {opts['min']} and {opts['max']}")
    return v


def apply_tuning(cfg, data: dict | None) -> None:
    if not data:
        return
    for attr, _sec, _label, kind, opts in TUNING:
        if attr in data:
            try:
                setattr(cfg, attr, coerce_tuning(attr, kind, opts, data[attr]))
            except (ValueError, TypeError):
                pass   # keep the file/default value if a stored value is no longer valid


# Alert settings edited in the web UI are stored in the database (kv "alert_settings") and override the
# [alerts] section of the config file. The service can't write the config file (root-owned, sandboxed).
ALERT_KV = "alert_settings"

# Every kind of alert bgpcanary can send, for the "What to alert on" settings. sev = the severities it can have;
# tuning = TUNING settings that shape it; check = the background check that produces it (on/off in the UI).
ALERT_TYPES = [
    {"kind": "outage", "group": "Reachability", "name": "Outage", "sev": ("warning", "critical"),
     "desc": "A prefix's visibility drops below the threshold for the sustain time (critical below half of normal).",
     "tuning": ["outage_threshold_pct", "sustain_seconds", "recover_threshold_pct", "baseline_hours"]},
    {"kind": "blip", "group": "Reachability", "name": "Brief dip", "sev": ("info",),
     "desc": "Visibility dipped below the outage threshold but recovered within the sustain time.",
     "tuning": []},
    {"kind": "reachability_loss", "group": "Reachability", "name": "Reachability loss (RIPE Atlas)",
     "sev": ("critical",), "atlas": True,
     "desc": "Most RIPE Atlas probes got no reply from one of your own addresses in a round of pings, whatever BGP "
             "shows.", "tuning": ["loss_alert_pct"]},
    {"kind": "upstream_missing", "group": "Upstreams", "name": "Expected upstream missing", "sev": ("warning",),
     "desc": "An upstream listed under Monitored network carries none of a prefix's paths while the prefix is "
             "otherwise visible.", "tuning": []},
    {"kind": "upstream_shift", "group": "Upstreams", "name": "Upstream shift", "sev": ("warning",),
     "desc": "A large share of the internet switches which of your upstreams it uses within minutes while "
             "visibility holds (a session flap or re-route). Triggers Route Views and Atlas checks.",
     "tuning": ["shift_pct", "shift_window_seconds", "shift_quiet_seconds"]},
    {"kind": "unexpected_upstream", "group": "Upstreams", "name": "Unexpected upstream", "sev": ("warning",),
     "desc": "Your prefix is reached through an AS adjacent to you that isn't one of your expected upstreams.",
     "tuning": []},
    {"kind": "new_upstream", "group": "Upstreams", "name": "New upstream", "sev": ("warning",),
     "desc": "A new upstream appears (only used when no expected upstreams are configured).", "tuning": []},
    {"kind": "chain_change", "group": "Upstreams", "name": "New upstream chain", "sev": ("info",),
     "desc": "A transit → upstream combination appears for the first time (e.g. a new carrier picks you up).",
     "tuning": []},
    {"kind": "origin_hijack", "group": "Hijacks and leaks", "name": "Origin hijack", "sev": ("critical",),
     "desc": "Another AS originates one of your prefixes.", "tuning": []},
    {"kind": "more_specific", "group": "Hijacks and leaks", "name": "More-specific announcement",
     "sev": ("warning", "critical"),
     "desc": "A more-specific of your prefix is announced (critical when another AS originates it).", "tuning": []},
    {"kind": "covering_prefix", "group": "Hijacks and leaks", "name": "Covering announcement", "sev": ("warning",),
     "desc": "A shorter prefix covering your space is announced by someone else.",
     "tuning": ["covering_min_length", "covering_ignore"]},
    {"kind": "asn_in_path", "group": "Hijacks and leaks", "name": "Your ASN in someone else's path",
     "sev": ("warning",), "desc": "Your ASN appears in the middle of a path for space you don't originate (a leak "
                                  "or path manipulation).", "tuning": []},
    {"kind": "unexpected_prefix", "group": "Hijacks and leaks", "name": "Unmonitored prefix from your ASN",
     "sev": ("warning",), "desc": "Your ASN originates a prefix you don't monitor.", "tuning": ["max_unmonitored"]},
    {"kind": "transit_filter", "group": "Background checks", "name": "Transit not carrying you (FYI)",
     "sev": ("info",), "check": "transit",
     "desc": "One of your upstream's transits carries the upstream's own prefixes but not yours: usually a prefix "
             "filter or IRR issue.", "tuning": ["transit_filter_min_peers", "transit_filter_minutes"]},
    {"kind": "registry", "group": "Background checks", "name": "Registry problem", "sev": ("info", "warning", "critical"),
     "check": "registry", "desc": "IRR route objects or RPKI problems for your space (missing, conflicting, invalid).",
     "tuning": ["registry_minutes"]},
    {"kind": "filter_sim", "group": "Background checks", "name": "Upstream filter simulation (FYI)", "sev": ("info",),
     "check": "registry", "desc": "A filter built from an upstream's as-set wouldn't include your prefix.", "tuning": []},
    {"kind": "irr_change", "group": "Background checks", "name": "IRR change (FYI)", "sev": ("info",),
     "check": "registry", "desc": "A route object for your space was added, changed or removed.", "tuning": []},
    {"kind": "asset_change", "group": "Background checks", "name": "as-set change (FYI)", "sev": ("info",),
     "check": "registry", "desc": "Your ASN was added to or removed from an as-set.", "tuning": []},
]
ALERT_MODES = ("on", "warning", "critical", "off")   # on = every severity; warning / critical = that and above
FYI_ALERT_KINDS = ("transit_filter", "filter_sim", "irr_change", "asset_change")
_SEV = {"info": 0, "warning": 1, "critical": 2}


def alert_mode(cfg, kind):
    """The notify choice for an alert type: the saved one, else derived from the old single "notify on"
    level (FYI notices were always sent while their check is on)."""
    if kind in cfg.alert_kinds:
        return cfg.alert_kinds[kind]
    if kind in FYI_ALERT_KINDS:
        return "on"
    t = next((t for t in ALERT_TYPES if t["kind"] == kind), None)
    sevs = t["sev"] if t else ("warning",)
    floor = _SEV.get(cfg.alert_min_severity, 1)
    if all(_SEV[s] >= floor for s in sevs):
        return "on"
    if any(_SEV[s] >= floor for s in sevs):
        return {1: "warning", 2: "critical"}[floor]
    return "off"


def should_notify(cfg, ev, phase):
    """Whether an event sends an alert in this phase. Recoveries follow the opening alert."""
    if phase == "close" and ev.get("alerted"):
        return True
    mode = alert_mode(cfg, ev.get("kind"))
    if mode == "off":
        return False
    return mode == "on" or _SEV.get(ev.get("severity"), 0) >= _SEV[mode]


def alert_settings_dict(cfg) -> dict:
    e = cfg.email
    return {"min_severity": cfg.alert_min_severity, "base_url": cfg.base_url, "kinds": dict(cfg.alert_kinds),
            "email": {"enabled": e.enabled, "smtp_host": e.smtp_host, "smtp_port": e.smtp_port,
                      "starttls": e.starttls, "ssl": e.ssl, "username": e.username, "password": e.password,
                      "from": e.sender, "to": list(e.recipients)},
            "webhooks": [{"name": w.name, "format": w.format, "url": w.url, "enabled": w.enabled}
                         for w in cfg.webhooks]}


def apply_alert_settings(cfg, data: dict | None) -> None:
    """Apply saved alert settings (from the database) onto the in-memory config."""
    if not data:
        return
    cfg.alert_min_severity = data.get("min_severity", cfg.alert_min_severity)
    if data.get("kinds") is not None:
        cfg.alert_kinds = {k: v for k, v in data["kinds"].items() if v in ALERT_MODES}
    cfg.base_url = data.get("base_url", cfg.base_url)
    em = data.get("email")
    if em is not None:
        cfg.email = EmailCfg(enabled=bool(em.get("enabled")), smtp_host=em.get("smtp_host", ""),
                             smtp_port=int(em.get("smtp_port") or 587), starttls=bool(em.get("starttls")),
                             ssl=bool(em.get("ssl")), username=em.get("username", ""),
                             password=em.get("password", ""), sender=em.get("from", ""),
                             recipients=list(em.get("to", [])))
    if data.get("webhooks") is not None:
        cfg.webhooks = [WebhookCfg(url=w["url"], format=w.get("format", "slack"), name=w.get("name", ""),
                                   enabled=bool(w.get("enabled", True))) for w in data["webhooks"] if w.get("url")]


class ConfigError(Exception):
    """The config file is missing or not valid TOML; the message says where and how to fix it."""


def _read(path):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"Config file {path} not found. Re-run the installer to recreate it.") from None
    except tomllib.TOMLDecodeError as ex:
        m = re.search(r"at line (\d+), column (\d+)", str(ex))
        where, shown = "", ""
        if m:
            n = int(m.group(1))
            where = f", line {n}"
            try:
                lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
                # show a few lines before too: an unclosed [ or " is reported where the parser gives up
                shown = "\n\n" + "\n".join(f"    {i:>4} | {lines[i - 1].rstrip()}"
                                             for i in range(max(1, n - 3), min(n, len(lines)) + 1)) + "\n" + \
                        "The mistake is on that line or just above it (e.g. a [ or \" that isn't closed).\n"
            except OSError:
                pass
        why = re.sub(r"\s*\(at line.*\)$", "", str(ex))
        raise ConfigError(
            f"Config file {path} has a syntax error{where}: {why}.{shown}\n"
            "Lists need square brackets and commas, e.g. owned_space = [\"198.51.100.0/22\", \"203.0.113.0/24\"]; "
            "text needs straight double quotes. Fix it with: sudo nano " + path + "\n"
            "(Most settings can be changed in the web UI instead; values saved there override this file.)") from None


def load(path: str = DEFAULT_PATH) -> Config:
    raw = _read(path)
    mon = raw.get("monitor", {})
    web = raw.get("web", {})
    det = raw.get("detection", {})
    rv = raw.get("routeviews", {})
    al = raw.get("alerts", {})
    em = al.get("email", {})
    return Config(
        asn=int(mon.get("asn") or 0),
        prefixes=[normalize_prefix(x) for x in mon.get("prefixes", [])],
        expected_upstreams=[int(x) for x in mon.get("expected_upstreams", [])],
        owned_space=[normalize_prefix(x) for x in mon.get("owned_space", [])],
        max_unmonitored=int(mon.get("max_unmonitored", 200)),
        db_path=raw.get("storage", {}).get("db_path", "/var/lib/bgpcanary/bgpcanary.db"),
        listen_host=web.get("listen_host", "127.0.0.1"),
        listen_port=int(web.get("listen_port", 8080)),
        secret_key=web["secret_key"],
        site_name=web.get("site_name", "BGP Canary"),
        base_url=web.get("base_url", ""),
        bucket_seconds=int(det.get("bucket_seconds", 30)),
        outage_threshold_pct=float(det.get("outage_threshold_pct", 80)),
        recover_threshold_pct=float(det.get("recover_threshold_pct", 90)),
        sustain_seconds=int(det.get("sustain_seconds", 60)),
        shift_pct=float(det.get("shift_pct", 25)),
        shift_window_seconds=int(det.get("shift_window_seconds", 300)),
        shift_quiet_seconds=int(det.get("shift_quiet_seconds", 300)),
        loss_alert_pct=float(det.get("loss_alert_pct", 50)),
        baseline_hours=int(det.get("baseline_hours", 24)),
        reconcile_minutes=int(det.get("reconcile_minutes", 5)),
        stale_days=int(det.get("stale_days", 14)),
        backfill_days=int(det.get("backfill_days", 90)),
        transit_filter_check=bool(det.get("transit_filter_check", True)),
        transit_filter_min_peers=int(det.get("transit_filter_min_peers", 5)),
        transit_filter_minutes=int(det.get("transit_filter_minutes", 10)),
        registry_watch=bool(det.get("registry_watch", True)),
        registry_minutes=int(det.get("registry_minutes", 60)),
        covering_ignore=list(det.get("covering_ignore", [])),
        covering_min_length=int(det.get("covering_min_length", 8)),
        raw_retention_days=int(raw.get("storage", {}).get("raw_retention_days", 365)),
        rv_servers=list(rv.get("servers", ["route-views.routeviews.org"])),
        rv_poll_minutes=int(rv.get("poll_minutes", 720)),
        probe_targets=list(rv.get("probe_targets", [])),
        probe_minutes=int(rv.get("probe_minutes", 360)),
        probe_on_event=bool(rv.get("probe_on_event", True)),
        rv_followup_change_pct=float(rv.get("followup_change_pct", 10)),
        alert_min_severity=al.get("min_severity", "warning"),
        alert_kinds=dict(al.get("kinds", {})),
        email=EmailCfg(
            enabled=bool(em.get("enabled", False)),
            smtp_host=em.get("smtp_host", "localhost"),
            smtp_port=int(em.get("smtp_port", 587)),
            starttls=bool(em.get("starttls", True)),
            ssl=bool(em.get("ssl", False)),
            username=em.get("username", ""),
            password=em.get("password", ""),
            sender=em.get("from", "bgpcanary@localhost"),
            recipients=list(em.get("to", [])),
        ),
        webhooks=[WebhookCfg(url=w["url"], format=w.get("format", "slack"), name=w.get("name", ""))
                  for w in al.get("webhooks", []) if w.get("url")],
    )
