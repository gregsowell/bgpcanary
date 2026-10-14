"""Email + webhook (Slack / Microsoft Teams / generic JSON) alerting."""
import asyncio
import html
import json
import logging
import smtplib
import ssl
import time
from email.message import EmailMessage

import httpx

log = logging.getLogger("bgpcanary.alerts")

EMOJI = {"critical": ":red_circle:", "warning": ":large_orange_circle:", "info": ":information_source:",
         "resolved": ":large_green_circle:"}


def _fmt_ts(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts)) if ts else "-"


def _dur(s):
    s = int(s)
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def build(cfg, ev, phase):
    """Return (subject, text, fields) for an event row + phase (open|close|escalate|test)."""
    link = f"{cfg.base_url.rstrip('/')}/events/{ev['id']}" if cfg.base_url and ev.get("id") else ""
    if ev.get("severity") == "info" and phase in ("open", "close"):
        # informational notices (e.g. transit filter check): calm wording, no alarm styling
        state = "FYI" if phase == "open" else "FYI · cleared"
        subject = f"[BGP FYI] {ev['title']}" if phase == "open" else f"[BGP FYI] Cleared: {ev['title']}"
    elif phase == "close":
        state = "RESOLVED"
        subject = f"[BGP RESOLVED] {ev['title']}"
    elif phase == "escalate":
        state = "ESCALATED"
        subject = f"[BGP {ev['severity'].upper()}] escalated: {ev['title']}"
    elif phase == "test":
        state = "TEST"
        subject = "[BGP TEST] bgpcanary test alert"
    else:
        state = ev["severity"].upper()
        subject = f"[BGP {state}] {ev['title']}"
    fields = [("Event", ev["kind"]), ("Prefix", ev.get("prefix") or "-"), ("Started", _fmt_ts(ev["start_ts"]))]
    if ev.get("end_ts"):
        fields.append(("Ended", _fmt_ts(ev["end_ts"])))
        fields.append(("Duration", _dur(ev["end_ts"] - ev["start_ts"])))
    if ev.get("min_visible") is not None and ev.get("baseline"):
        fields.append(("Min visibility", f"{ev['min_visible']}/{ev['baseline']:.0f} RIS peers "
                                         f"({100 * ev['min_visible'] / ev['baseline']:.0f}%)"))
    try:
        note = json.loads(ev.get("detail") or "{}").get("note")
    except (TypeError, ValueError):
        note = None
    if note:
        fields.append(("Details", note))
    extra = ev.get("extra") or {}
    for k, v in extra.items():
        fields.append((k, v))
    text = f"{state}: {ev['title']}\n" + "\n".join(f"{k}: {v}" for k, v in fields)
    if link:
        text += f"\n{link}"
    return subject, text, fields, link, state


SEV_COLOR = {"critical": "#c8262b", "warning": "#c77700", "info": "#3b6fd6", "resolved": "#1a8a4a"}


def _html(subject, fields, link, state, sev, images):
    """HTML body: header bar, details table, inline tree images (cid:), link."""
    e = lambda s: html.escape(str(s))
    color = SEV_COLOR.get(sev, "#3b6fd6")
    rows = "".join(f'<tr><td style="padding:4px 12px 4px 0;color:#5f6b7a;white-space:nowrap;vertical-align:top">{e(k)}'
                   f'</td><td style="padding:4px 0">{e(v)}</td></tr>' for k, v in fields)
    imgs = "".join(f'<p style="margin:18px 0 6px;font-weight:600">{e(cap)}</p>'
                   f'<img src="cid:{cid}" alt="{e(cap)}" style="max-width:100%;border:1px solid #e1e5ea;border-radius:6px">'
                   for cid, _png, cap in images)
    btn = (f'<p style="margin-top:18px"><a href="{e(link)}" style="background:{color};color:#fff;padding:8px 14px;'
           f'border-radius:6px;text-decoration:none">Open in bgpcanary</a></p>') if link else ""
    return (f'<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;font-size:14px;color:#1b1f24;'
            f'max-width:1120px"><div style="border-left:6px solid {color};padding:6px 12px;margin-bottom:12px">'
            f'<div style="font-size:12px;font-weight:700;color:{color};letter-spacing:.04em">{e(state)}</div>'
            f'<div style="font-size:17px;font-weight:600">{e(subject)}</div></div>'
            f'<table style="border-collapse:collapse;font-size:14px">{rows}</table>{imgs}{btn}</div>')


def _send_email(cfg, subject, text, html_body=None, images=()):
    e = cfg.email
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = e.sender
    msg["To"] = ", ".join(e.recipients)
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
        part = msg.get_payload()[-1]
        for cid, png, cap in images:
            part.add_related(png, maintype="image", subtype="png", cid=f"<{cid}>", filename=f"{cid}.png")
    ctx = ssl.create_default_context()
    if e.ssl:
        s = smtplib.SMTP_SSL(e.smtp_host, e.smtp_port, timeout=30, context=ctx)
    else:
        s = smtplib.SMTP(e.smtp_host, e.smtp_port, timeout=30)
        if e.starttls:
            s.starttls(context=ctx)
    try:
        if e.username:
            s.login(e.username, e.password)
        s.send_message(msg)
    finally:
        s.quit()


def _slack_payload(ev, phase, subject, fields, link, state):
    if ev["severity"] == "info":
        sev = "info"   # informational: same calm icon whether opening or clearing
    else:
        sev = "resolved" if phase == "close" else ev["severity"]
    head = f"{EMOJI.get(sev, '')} *{state}* — {ev['title']}"
    body = "\n".join(f"*{k}:* {v}" for k, v in fields)
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": head}},
              {"type": "section", "text": {"type": "mrkdwn", "text": body}}]
    if link:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"<{link}|Open in bgpcanary>"}]})
    return {"text": f"{state}: {ev['title']}", "blocks": blocks}


TEAMS_COLOR = {"critical": "Attention", "warning": "Warning", "info": "Accent", "resolved": "Good"}


def _teams_payload(ev, phase, subject, fields, link, state):
    """An Adaptive Card, as the Teams "Send webhook alerts to a channel" workflow expects."""
    if ev["severity"] == "info":
        sev = "info"
    else:
        sev = "resolved" if phase == "close" else ev["severity"]
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4",
        "msteams": {"width": "Full"},
        "body": [
            {"type": "TextBlock", "text": state, "weight": "Bolder", "size": "Small", "color": TEAMS_COLOR.get(sev, "Accent")},
            {"type": "TextBlock", "text": ev["title"], "weight": "Bolder", "size": "Medium", "wrap": True},
            {"type": "FactSet", "facts": [{"title": str(k), "value": str(v)} for k, v in fields]},
        ],
    }
    if link:
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open in bgpcanary", "url": link}]
    return {"type": "message", "summary": subject,
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}]}


async def send(cfg, conn, ev, phase, only=None, images=()):
    """Send an alert on all enabled channels (or just `only`: "email" or "webhook:<index>").
    `ev` is a dict (event row); `images` = [(cid, png_bytes, caption)] embedded in the email.
    Returns list of (channel, ok, error)."""
    subject, text, fields, link, state = build(cfg, ev, phase)
    results = []
    if (only == "email" or (only is None and cfg.email.enabled)) and cfg.email.recipients:
        sev = "resolved" if phase == "close" and ev.get("severity") != "info" else ev.get("severity", "info")
        body = _html(subject, fields, link, state, sev, images)
        if images:
            text += "\n\n(The HTML version of this email includes connectivity-tree images: " + \
                    ", ".join(cap for _cid, _png, cap in images) + ".)"
        try:
            await asyncio.to_thread(_send_email, cfg, subject, text, body, images)
            results.append(("email", True, None))
        except Exception as e:
            log.warning("email alert failed: %s", e)
            results.append(("email", False, str(e)))
    async with httpx.AsyncClient(timeout=20) as c:
        for i, wh in enumerate(cfg.webhooks):
            if only is not None and only != f"webhook:{i}":
                continue
            if only is None and not wh.enabled:
                continue
            name = f"webhook:{wh.name or wh.format}"
            try:
                if wh.format == "slack":
                    payload = _slack_payload(ev, phase, subject, fields, link, state)
                elif wh.format == "teams":
                    payload = _teams_payload(ev, phase, subject, fields, link, state)
                else:
                    payload = {"phase": phase, "state": state, "subject": subject, "text": text, "link": link,
                               "event": {k: ev.get(k) for k in ("id", "kind", "severity", "prefix", "start_ts",
                                                                 "end_ts", "title", "min_visible", "baseline")}}
                r = await c.post(wh.url, json=payload)
                r.raise_for_status()
                results.append((name, True, None))
            except Exception as e:
                log.warning("%s alert failed: %s", name, e)
                results.append((name, False, str(e)))
    if conn is not None:
        for ch, ok, err in results:
            conn.execute("INSERT INTO alert_log(ts,event_id,channel,ok,error) VALUES(?,?,?,?,?)",
                         (time.time(), ev.get("id"), ch, 1 if ok else 0, err))
        conn.commit()
    return results
