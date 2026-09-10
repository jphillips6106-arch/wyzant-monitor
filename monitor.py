#!/usr/bin/env python3
"""Poll the Wyzant tutor job board for new organic-chemistry jobs and notify.

Runs headless against a persistent browser profile in ./profile that you log
into once via login.py. State (seen job ids) lives in ./state.json.

Each poll loads two views of the logged-in board (both limited to "My
subjects", which Wyzant sets from your approved subjects):
  * Online jobs
  * In-person jobs within `inperson_miles` of `zip_code` (config.json)

Notifications: macOS banner always; opens the job in `open_in_browser`
(default Opera GX); ntfy.sh push if `ntfy_topic` is set.
"""
from __future__ import annotations

import hashlib
import json
import os
import smtplib
from email.message import EmailMessage
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
PROFILE = ROOT / "profile"
AUTH_STATE = ROOT / "auth_state.json"   # exported session (cloud mode); profile/ is used if absent
STATE = ROOT / "state.json"
CONFIG = ROOT / "config.json"
SECRETS = ROOT / "secrets.json"
LOG = ROOT / "monitor.log"
DEBUG_DIR = ROOT / "debug"

BOARD = "https://highered.wyzant.com/tutor/jobs"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
DEFAULT_KEYWORDS = r"organic|orgo|ochem|o-chem|chem 2|chem ii|chem 24|2420"
COUNT_RE = re.compile(r"(\d+)\s+(Online|In-person)\s+Tutoring Jobs", re.I)
NO_JOBS = "Sorry, no jobs fit your current filters"


QUIET = bool(os.environ.get("MONITOR_QUIET"))  # public CI logs: ids and counts only


def log(msg: str) -> None:
    if QUIET and ("NOTIFIED" in msg or "->" in msg or "UNPARSED" in msg):
        msg = msg.split(":")[0] if "NOTIFIED" in msg else msg.split(";")[0]
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


# ---------------------------------------------------------------- notify ---

IS_MAC = sys.platform == "darwin"


def mac_notify(title: str, body: str) -> None:
    if not IS_MAC:
        return
    body = body.replace('"', "'").replace("\\", "")
    title = title.replace('"', "'")
    script = f'display notification "{body}" with title "{title}" sound name "Glass"'
    subprocess.run(["osascript", "-e", script], check=False)


def ntfy(topic: str, title: str, body: str, url: str | None = None, email: str | None = None) -> None:
    """Publish to ntfy.sh. With `email`, ntfy relays the message to that
    address (no mail credentials needed). Phone push only happens if the
    ntfy app is subscribed to the topic."""
    headers = {"Title": title, "Priority": "high", "Tags": "test_tube"}
    if url:
        headers["Click"] = url
        body = f"{body}\n\n{url}"
    if email:
        headers["Email"] = email
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode(), headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # never let a push failure kill the run
        log(f"ntfy failed: {e}")


def open_in_browser(cfg: dict, url: str) -> None:
    app = cfg.get("open_in_browser")
    if IS_MAC and app and url:
        subprocess.run(["open", "-a", app, url], check=False)


def send_email(cfg: dict, title: str, body: str, url: str | None = None, html: str | None = None) -> bool:
    """Email via Gmail SMTP using an app password from secrets.json.
    secrets.json is filled in by you, never by the monitor."""
    to = cfg.get("email_to")
    sec = load_json(SECRETS, {})
    user, pw = sec.get("smtp_user"), sec.get("smtp_app_password")
    if not (to and user and pw):
        log("email skipped: set smtp_app_password in secrets.json")
        return False
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body + (f"\n\n{url}" if url else "") + "\n\n-- Wyzant orgo job monitor")
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP_SSL(sec.get("smtp_host", "smtp.gmail.com"), int(sec.get("smtp_port", 465)), timeout=30) as smtp:
            smtp.login(user, pw.replace(" ", ""))
            smtp.send_message(msg)
        return True
    except Exception as e:  # never let mail failure kill the run
        log(f"email failed: {type(e).__name__}: {e}")
        return False


def notify(cfg: dict, title: str, body: str, url: str | None = None, email_body: str | None = None, html: str | None = None) -> None:
    mac_notify(title, body)
    if cfg.get("email_to"):
        send_email(cfg, title, email_body or body, None if email_body else url, html=html)
    if cfg.get("phone_push") and cfg.get("ntfy_topic"):
        ntfy(cfg["ntfy_topic"], title, body, url)


# ---------------------------------------------------------------- scrape ---

def board_urls(cfg: dict) -> dict[str, str]:
    zip_code = cfg.get("zip_code", "15241")
    miles = cfg.get("inperson_miles", 40)
    return {
        "online": f"{BOARD}?utf8=%E2%9C%93&lesson_type=online&sort_by=1",
        "in_person": (
            f"{BOARD}?utf8=%E2%9C%93&lesson_type=in_person&location=Specific+Location"
            f"&zip_code={zip_code}&distance={miles}&sort_by=1"
        ),
    }


EXTRACT_JS = """() => {
  // Collect anything that looks like a link to an individual job, then walk
  // up to the surrounding card so we get title/description/poster text.
  const pats = [/\\/tutor\\/jobs\\/(\\d+)/, /viewjob\\?id=(\\d+)/, /job[_-]?id=(\\d+)/i, /\\/jobs?\\/(\\d{5,})/];
  const out = {};
  for (const a of document.querySelectorAll('a[href]')) {
    const h = a.getAttribute('href') || '';
    let id = null;
    for (const p of pats) { const m = h.match(p); if (m) { id = m[1]; break; } }
    if (!id) continue;
    let el = a;
    for (let i = 0; i < 7 && el.parentElement; i++) {
      el = el.parentElement;
      if ((el.innerText || '').length > 120) break;
    }
    const raw = (el.innerText || a.innerText || '');
    const text = raw.replace(/\\s+/g, ' ').trim();
    const lines = raw.split(/\\n+/).map(x => x.trim()).filter(Boolean);
    const full = (el.textContent || '').replace(/\\s+/g, ' ').trim();  // includes collapsed "Show details" text
    const url = new URL(h, location.href).href;
    if (!out[id] || text.length > out[id].text.length) out[id] = { id, url, text, lines, full, html: el.outerHTML.slice(0, 6000) };
  }
  return Object.values(out);
}"""


def scrape_view(page, url: str) -> dict:
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(3000)
    if "login" in page.url.lower() or page.locator("input[type='password']").count():
        return {"logged_in": False}
    text = page.evaluate("() => document.body.innerText")
    if "Performing security verification" in text or "verify you are not a bot" in text:
        return {"logged_in": True, "blocked": True, "count": None, "empty": False, "jobs": [], "results_text": "", "html": None}
    m = COUNT_RE.search(text)
    count = int(m.group(1)) if m else None
    if count is None and NO_JOBS not in text and "Tutoring Jobs" not in text:
        # Neither a count nor the empty-board message: layout changed or an error page.
        return {"logged_in": True, "unexpected": True, "count": None, "empty": False, "jobs": [], "results_text": "", "html": page.content()}
    jobs = page.evaluate(EXTRACT_JS)
    # Results region text, for the fallback when we can't parse cards.
    i = text.find("Sort by")
    results_text = re.sub(r"\s+", " ", text[i:i + 1500]).strip() if i >= 0 else ""
    return {
        "logged_in": True,
        "count": count,
        "empty": NO_JOBS in text,
        "jobs": jobs,
        "results_text": results_text,
        "html": page.content() if (count and not jobs) else None,
    }


# --------------------------------------------------------------- details ---

PUBLIC_JOB = "https://www.wyzant.com/viewjob?id={id}"
AGE_RE = re.compile(r"^(\d+\s*(?:m|h|d|min|mins|hr|hrs|hour|hours|day|days)(?:\s+ago)?|just now)$", re.I)


def parse_card(j: dict) -> dict:
    """Best-effort structure from the board card's lines:
    ['5m', 'Tim', 'Organic Chemistry', 'Recommended rate: None', '<desc>', 'Show details']"""
    lines = [l for l in j.get("lines", []) if l.lower() not in ("show details", "hide details", "apply")]
    d = {"age": "", "name": "", "matched_subject": "", "rate": "", "desc": ""}
    if not lines and j.get("text"):
        m = re.match(r"^(?P<age>\d+\s*[mhd])\s+(?P<name>\S+)\s+(?P<subj>.+?)\s+Recommended rate:\s*(?P<rate>\S+)\s+(?P<desc>.*?)(?:\s+Show details)?$", j["text"])
        if m:
            return {"age": m["age"], "name": m["name"], "matched_subject": m["subj"], "rate": m["rate"], "desc": m["desc"].strip()}
    rest = []
    for l in lines:
        if not d["age"] and AGE_RE.match(l):
            d["age"] = l
        elif l.lower().startswith("recommended rate"):
            d["rate"] = l.split(":", 1)[-1].strip()
        else:
            rest.append(l)
    # After the age line: name, then subject, then description paragraphs.
    if len(rest) >= 2 and len(rest[0]) <= 40 and len(rest[1]) <= 60:
        d["name"], d["matched_subject"], rest = rest[0], rest[1], rest[2:]
    d["desc"] = " ".join(rest).strip()
    # Collapsed cards hide part of the description; textContent has it all.
    full = j.get("full", "")
    if d["desc"] and full and len(full) > len(j.get("text", "")) + 20:
        i = full.find(d["desc"][:40])
        if i >= 0:
            tail = full[i:]
            tail = re.split(r"\s(?:Show details|Hide details|Apply)\b", tail)[0]
            if len(tail) > len(d["desc"]):
                d["desc"] = tail.strip()
    return d


def fetch_public_details(page, job_id: str) -> dict:
    """Wyzant's public job page (no login) has labeled fields and the full
    description, and it stays up after the job closes."""
    out: dict = {}
    try:
        page.goto(PUBLIC_JOB.format(id=job_id), wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(1500)
        out = page.evaluate("""() => {
          const o = {};
          const h1 = document.querySelector('h1');
          o.title = h1 ? h1.innerText.trim() : '';
          const posted = [...document.querySelectorAll('*')].map(e => e.childNodes.length === 1 && e.innerText || '').find(t => /^posted on:/i.test(t.trim()));
          o.posted = posted ? posted.replace(/posted on:/i, '').trim() : '';
          const h3 = [...document.querySelectorAll('h3, h2, strong')].find(e => /^job description$/i.test(e.innerText.trim()));
          let p = h3 ? h3.nextElementSibling : null;
          while (p && p.tagName !== 'P') p = p.nextElementSibling;
          if (!p) { const box = document.querySelector('.box-gray'); p = box && box.querySelector('p'); }
          o.desc = p ? p.innerText.trim() : '';
          o.desc = o.desc.replace(/^posted on:.*$/im, '').trim();
          for (const m of document.querySelectorAll('.match-answer')) {
            const lab = m.querySelector('.text-light');
            if (!lab) continue;
            const key = lab.innerText.replace(':', '').trim().toLowerCase();
            const val = m.innerText.replace(lab.innerText, '').replace(/\\s+/g, ' ').trim();
            o[key] = val;
          }
          return o;
        }""")
    except Exception as e:
        log(f"public details failed for {job_id}: {type(e).__name__}")
    return out or {}


def _esc(x: str) -> str:
    return (x or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


DAYS = "SUNDAY|MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY"
DAY_RE = re.compile(rf"({DAYS})\s+(.*?)(?=\s+(?:{DAYS})\b|$)")


def render_html(subject: str, matched: str, lesson: str, name: str, location: str,
                posted: str, age: str, rate: str, desc: str, avail: str,
                apply_url: str, public_url: str) -> str:
    accent = "#1f6feb"
    ink, muted, line, soft = "#1b1f24", "#6b7280", "#e5e7eb", "#f6f8fa"
    headline = _esc(subject or matched or "Tutoring job")
    sub = ""
    if subject and matched and subject.lower() != matched.lower():
        sub = f'Shown to you as <b>{_esc(matched)}</b>'
    when = ""
    if age:
        when = f"posted {_esc(age)} ago"
    elif posted:
        when = f"posted {_esc(posted)}"

    def row(k, v):
        return (f'<tr><td style="padding:7px 0;color:{muted};font-size:13px;width:150px;vertical-align:top">{k}</td>'
                f'<td style="padding:7px 0;color:{ink};font-size:14px;vertical-align:top">{v}</td></tr>')

    rows = "".join([
        row("Subject posted", _esc(subject) or "&mdash;"),
        row("Matched to you as", _esc(matched) or "&mdash;"),
        row("Lesson", _esc(lesson) or "&mdash;"),
        row("Student / parent", _esc(name) or "&mdash;"),
        row("Location", _esc(location) or "&mdash;"),
        row("Posted", _esc(f"{posted} &middot; {age} ago when found".replace("&middot;", "·")) if posted and age else _esc(posted or (f"{age} ago" if age else "")) or "&mdash;"),
        row("Recommended rate", _esc(rate) if rate and rate.lower() != "none" else "not given"),
    ])

    # Availability: "SUNDAY Morning, Afternoon, Evening MONDAY ..." -> compact grid
    avail_html = ""
    days = [(d, t.strip().rstrip(",")) for d, t in DAY_RE.findall(avail or "")]
    if days and len(days) == 7 and len({t for _, t in days}) == 1:
        days = [("Every day", days[0][1])]  # same slots all week: say it once
    if days:
        cells = "".join(
            f'<tr><td style="padding:3px 12px 3px 0;color:{muted};font-size:12px;letter-spacing:.03em">{d.title()}</td>'
            f'<td style="padding:3px 0;color:{ink};font-size:13px">{_esc(t)}</td></tr>'
            for d, t in days)
        avail_html = (f'<div style="margin-top:22px"><div style="font-size:11px;font-weight:700;letter-spacing:.08em;color:{muted};text-transform:uppercase;margin-bottom:6px">Availability</div>'
                      f'<table cellpadding="0" cellspacing="0" style="border-collapse:collapse">{cells}</table></div>')
    elif avail:
        avail_html = f'<div style="margin-top:22px;color:{ink};font-size:14px"><b>Availability:</b> {_esc(avail)}</div>'

    paras = [re.sub(r"\s*\n\s*", " ", para).strip() for para in re.split(r"\n\s*\n", desc or "") if para.strip()]
    desc_html = "<br><br>".join(_esc(x) for x in paras) or "<i>No description given.</i>"

    return f"""<!doctype html><html><body style="margin:0;padding:0;background:{soft}">
<table width="100%" cellpadding="0" cellspacing="0" style="background:{soft};padding:28px 12px">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#fff;border:1px solid {line};border-radius:12px;overflow:hidden;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif">
  <tr><td style="background:{accent};height:6px;font-size:0;line-height:0">&nbsp;</td></tr>
  <tr><td style="padding:26px 30px 0 30px">
    <div style="font-size:11px;font-weight:700;letter-spacing:.08em;color:{muted};text-transform:uppercase">New Wyzant job &middot; {_esc(lesson)}</div>
    <div style="font-size:24px;font-weight:700;color:{ink};margin-top:6px;line-height:1.25">{headline}</div>
    <div style="font-size:13px;color:{muted};margin-top:6px">{sub}{(" &middot; " if sub and when else "")}{when}</div>
  </td></tr>
  <tr><td style="padding:22px 30px 0 30px">
    <table cellpadding="0" cellspacing="0" width="100%" style="border-collapse:collapse;border-top:1px solid {line};border-bottom:1px solid {line}">{rows}</table>
  </td></tr>
  <tr><td style="padding:22px 30px 0 30px">
    <div style="font-size:11px;font-weight:700;letter-spacing:.08em;color:{muted};text-transform:uppercase;margin-bottom:8px">Description</div>
    <div style="background:{soft};border-left:3px solid {accent};border-radius:6px;padding:14px 16px;font-size:15px;line-height:1.55;color:{ink}">{desc_html}</div>
    {avail_html}
  </td></tr>
  <tr><td style="padding:26px 30px 8px 30px" align="left">
    <a href="{_esc(apply_url)}" style="display:inline-block;background:{accent};color:#fff;text-decoration:none;font-weight:700;font-size:15px;padding:12px 22px;border-radius:8px">Open job &amp; apply &rarr;</a>
    <div style="font-size:12px;color:{muted};margin-top:12px">Only the first five applications reach the student.
      <a href="{_esc(public_url)}" style="color:{muted}">Public listing</a></div>
  </td></tr>
  <tr><td style="padding:18px 30px 24px 30px;font-size:11px;color:{muted};border-top:1px solid {line};margin-top:10px">Wyzant orgo job monitor &middot; checks the board every 5 minutes</td></tr>
</table>
</td></tr></table></body></html>"""


def build_alert(view: str, j: dict, det: dict) -> tuple[str, str, str, str]:
    """Return (banner_title, banner_body, email_text, email_html)."""
    card = parse_card(j)
    subject = det.get("subject") or ""
    matched = card["matched_subject"]
    title_txt = det.get("title") or ""
    if not matched and title_txt:
        # title is "<City, ST> <Subject> tutoring job" (or "Online <Subject> tutoring job")
        m = re.match(r"^(?:.*?,\s*[A-Z]{2}\s+|Online\s+)?(.+?)\s+tutoring job$", title_txt.strip(), re.I)
        if m:
            matched = m.group(1).strip()
    lesson = det.get("preferred lesson location") or view.replace("_", " ").title()
    name = det.get("name") or card["name"]
    location = det.get("location") or ""
    desc = det.get("desc") or card["desc"] or j.get("text", "")
    avail = det.get("availability") or ""
    posted = det.get("posted") or ""
    age = card["age"]

    subj_line = subject or matched or "subject unknown"
    if subject and matched and subject.lower() != matched.lower():
        subj_line = f"{subject} (shown to you as {matched})"

    title = f"Wyzant job: {subj_line} · {lesson}"
    banner = " · ".join(x for x in [name, location, f"posted {age} ago" if age else posted] if x)
    banner = (banner + " — " + re.sub(r"\s+", " ", desc))[:200]

    rows = [
        ("Subject posted", subject or "(not shown)"),
        ("Matched to your subject", matched or "(not shown)"),
        ("Lesson", lesson),
        ("Student/parent", name or "(not shown)"),
        ("Location", location or "(not shown)"),
        ("Posted", f"{posted} ({age} ago when found)" if posted and age else (posted or (f"{age} ago" if age else "(not shown)"))),
        ("Recommended rate", card["rate"] or "(not shown)"),
    ]
    width = max(len(k) for k, _ in rows)
    lines = [f"{k.ljust(width)} : {v}" for k, v in rows]
    email = "\n".join(lines)
    email += f"\n\nDescription:\n{desc}\n"
    if avail:
        email += f"\nAvailability: {avail}\n"
    public_url = PUBLIC_JOB.format(id=j['id'])
    email += f"\nApply (logged-in board): {j['url']}\nPublic page: {public_url}\n"
    html = render_html(subject, matched, lesson, name, location, posted, age, card["rate"], desc, avail, j["url"], public_url)
    return title, banner, email, html


def auth_cookie_days_left(cookies: list) -> float | None:
    """Days until Wyzant's login cookie expires (wyzauth family)."""
    exps = [c.get("expires") for c in cookies if c.get("name") in ("wyzauth", "wyzauth_v2", ".AspNet.ApplicationCookie") and c.get("expires", -1) > 0]
    if not exps:
        return None
    return round((min(exps) - time.time()) / 86400, 2)


# -------------------------------------------------------------- watchdog ---

GH_REPO = "jphillips6106-arch/wyzant-monitor"


def github_monitor_health() -> tuple[bool, str]:
    """Ask GitHub's public API whether the cloud copy is alive.
    Healthy = workflow active AND (a run is queued/in progress OR the last run
    finished less than 40 minutes ago)."""
    import urllib.request as ur
    hdr = {"Accept": "application/vnd.github+json", "User-Agent": "wyzant-monitor-watchdog"}
    def get(path):
        with ur.urlopen(ur.Request(f"https://api.github.com/repos/{GH_REPO}{path}", headers=hdr), timeout=20) as r:
            return json.loads(r.read())
    wf = [w for w in get("/actions/workflows")["workflows"] if w["path"].endswith("monitor.yml")]
    if not wf:
        return False, "workflow file not found in the repo"
    if wf[0]["state"] != "active":
        return False, f"workflow is {wf[0]['state']}"
    runs = get(f"/actions/workflows/{wf[0]['id']}/runs?per_page=5")["workflow_runs"]
    if not runs:
        return False, "no runs at all"
    if any(r["status"] in ("in_progress", "queued", "waiting", "pending") for r in runs):
        return True, "a run is live"
    last = runs[0]
    ended = datetime.strptime(last["updated_at"], "%Y-%m-%dT%H:%M:%SZ").timestamp() - time.timezone
    mins = (time.time() - ended) / 60
    if mins > 40:
        return False, f"no live run; last one ended {mins:.0f} min ago ({last['conclusion']})"
    return True, f"last run ended {mins:.0f} min ago"


def watchdog(cfg: dict, state: dict) -> None:
    """Mac side: every 10 minutes check that the GitHub copy is alive; if not,
    banner + email (email even though the Mac's normal alerts are banner-only)."""
    if not cfg.get("watch_github", True) or not IS_MAC:
        return
    state.setdefault("watchdog_checked_at", 0)
    state.setdefault("watchdog_nag_at", 0)
    if time.time() - state["watchdog_checked_at"] < 600:
        return
    state["watchdog_checked_at"] = time.time()
    try:
        ok, why = github_monitor_health()
    except Exception as e:
        log(f"watchdog: GitHub API unreachable ({type(e).__name__})")
        return
    log(f"watchdog: github copy {'OK' if ok else 'DOWN'} ({why})")
    if ok:
        state["watchdog_nag_at"] = 0
        return
    if time.time() - state["watchdog_nag_at"] > 6 * 3600:
        state["watchdog_nag_at"] = time.time()
        body = (f"The GitHub copy of the Wyzant monitor is not running: {why}. "
                f"Check https://github.com/{GH_REPO}/actions and re-enable or re-run the workflow. "
                "Until then only this Mac is watching the board (and only while awake).")
        mac_notify("Wyzant monitor: GitHub copy is DOWN", body)
        send_email({"email_to": cfg.get("watchdog_email") or "philjoe@sas.upenn.edu"},
                   "Wyzant monitor: GitHub copy is DOWN", body, f"https://github.com/{GH_REPO}/actions")


# ------------------------------------------------------------------ main ---

def main() -> int:
    cfg = load_json(CONFIG, {})
    state = load_json(STATE, {})
    seen: dict = state.setdefault("seen", {})
    state.setdefault("login_nag_at", 0)
    state.setdefault("bootstrapped", False)
    state.setdefault("fallback_hashes", [])
    kw = cfg.get("keywords", DEFAULT_KEYWORDS)
    keywords = re.compile(kw, re.I) if kw else None  # empty string = notify on every job

    use_state = AUTH_STATE.exists()
    if not use_state and not PROFILE.exists():
        log("No session yet. Run login.sh (Mac) or copy auth_state.json here (cloud).")
        return 1

    views: dict[str, dict] = {}
    with sync_playwright() as p:
        browser = None
        if use_state:
            browser = p.chromium.launch(headless=True, channel="chromium")
            ctx = browser.new_context(storage_state=str(AUTH_STATE),
                                      viewport={"width": 1280, "height": 900}, user_agent=UA)
        else:
            ctx = p.chromium.launch_persistent_context(
                str(PROFILE), headless=True, channel="chromium",
                viewport={"width": 1280, "height": 900}, user_agent=UA,
            )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            for name, url in board_urls(cfg).items():
                views[name] = scrape_view(page, url)
                if not views[name]["logged_in"]:
                    break
            if use_state and all(v["logged_in"] for v in views.values()):
                ctx.storage_state(path=str(AUTH_STATE))  # keep refreshed cookies
                AUTH_STATE.chmod(0o600)
            try:
                auth_days = auth_cookie_days_left(ctx.cookies())
            except Exception:
                auth_days = None
        finally:
            ctx.close()
            if browser:
                browser.close()
        state["auth_days_left"] = auth_days

        if any(not v["logged_in"] for v in views.values()):
            detail_page = None
            pub = None
        else:
            # Public job pages need no login; a plain context avoids touching the profile again.
            pub = p.chromium.launch(headless=True, channel="chromium")
            detail_page = pub.new_context(viewport={"width": 1280, "height": 900}, user_agent=UA).new_page()
        try:
            rc = process_views(cfg, state, seen, keywords, views, detail_page)
        finally:
            if pub:
                pub.close()
    return rc


def process_views(cfg, state, seen, keywords, views, detail_page) -> int:

    if any(not v["logged_in"] for v in views.values()):
        if time.time() - state["login_nag_at"] > 6 * 3600:
            notify(cfg, "Wyzant monitor: logged out",
                   "Not logged in. Run ~/wyzant_monitor/login.sh to sign in." if IS_MAC else
                   "Cloud session expired. On your Mac run: ~/wyzant_monitor/cloud/push.sh")
            state["login_nag_at"] = time.time()
            STATE.write_text(json.dumps(state, indent=2))
        log("Not logged in; skipping.")
        return 2

    first_run = not state["bootstrapped"]
    blocked = [n for n, v in views.items() if v.get("blocked")]
    odd = [n for n, v in views.items() if v.get("unexpected")]
    if blocked or odd:
        state.setdefault("block_nag_at", 0)
        for n in odd:
            DEBUG_DIR.mkdir(exist_ok=True)
            (DEBUG_DIR / f"unexpected_{n}_{datetime.now():%Y%m%d_%H%M%S}.html").write_text(views[n]["html"] or "")
        log(f"WARNING blocked={blocked} unexpected={odd}")
        if time.time() - state["block_nag_at"] > 6 * 3600:
            notify(cfg, "Wyzant monitor: can't read the board",
                   ("Cloudflare bot check" if blocked else "Unexpected page") + " on " + ", ".join(blocked + odd) + ". Will keep retrying.")
            state["block_nag_at"] = time.time()
        STATE.write_text(json.dumps(state, indent=2))
        return 3
    summary = []
    for name, v in views.items():
        summary.append(f"{name}: count={v['count']} cards={len(v['jobs'])}")

        # --- normal path: parsed job cards
        for j in v["jobs"]:
            if keywords and not keywords.search(j.get("full") or j["text"]):
                continue
            if j["id"] in seen:
                continue
            seen[j["id"]] = {"first_seen": datetime.now().isoformat(), "view": name, "text": j["text"][:300]}
            if first_run:
                continue
            DEBUG_DIR.mkdir(exist_ok=True)
            (DEBUG_DIR / f"card_{j['id']}.html").write_text(j.get("html", ""))
            det = fetch_public_details(detail_page, j["id"]) if detail_page else {}
            title, banner, email_body, html = build_alert(name, j, det)
            notify(cfg, title, banner, j["url"], email_body=email_body, html=html)
            open_in_browser(cfg, j["url"])
            log(f"NOTIFIED {j['id']}: {title} | {banner[:120]}")
            log(f"  -> {j['url']}")

        # --- fallback: the board says there are jobs but we parsed no cards.
        # Notify on the board text itself (once per distinct text) and save
        # the HTML so the parser can be fixed against real markup.
        if v["count"] and not v["jobs"]:
            h = hashlib.sha1(v["results_text"].encode()).hexdigest()[:12]
            DEBUG_DIR.mkdir(exist_ok=True)
            snap = DEBUG_DIR / f"board_{name}_{datetime.now():%Y%m%d_%H%M%S}.html"
            snap.write_text(v["html"] or "")
            log(f"UNPARSED: {name} shows {v['count']} job(s) but no cards parsed; saved {snap.name}")
            if h not in state["fallback_hashes"]:
                state["fallback_hashes"] = (state["fallback_hashes"] + [h])[-50:]
                if not first_run:
                    body = v["results_text"][8:188]  # skip the "Sort by" label
                    url = board_urls(cfg)[name]
                    if not keywords or keywords.search(v["results_text"]):
                        notify(cfg, f"Wyzant board changed ({name.replace('_', ' ')})", body, url)
                        open_in_browser(cfg, url)
                        log(f"NOTIFIED (fallback) {name}: {body}")

    days = state.get("auth_days_left")
    if days is not None:
        summary.append(f"session expires in {days:.1f}d")
        state.setdefault("expiry_nag_at", 0)
        if days < 2 and time.time() - state["expiry_nag_at"] > 24 * 3600:
            where = ("On the Mac run ~/wyzant_monitor/login.sh." if IS_MAC else
                     "On the Mac run ~/wyzant_monitor/github/prepare.sh, then paste the clipboard into the WYZANT_AUTH_STATE secret on GitHub.")
            notify(cfg, f"Wyzant monitor: session expires in {max(days, 0):.1f} days",
                   f"Your Wyzant login cookie runs out soon. {where}")
            state["expiry_nag_at"] = time.time()
    log(" | ".join(summary))
    watchdog(cfg, state)

    if first_run:
        state["bootstrapped"] = True
        n = len(seen)
        log(f"Bootstrapped with {n} existing job(s) (no notifications sent).")
        notify(cfg, "Wyzant monitor is live",
               f"Watching online + in-person boards for orgo jobs. {n} already listed.")

    # Keep state from growing forever: drop ids first seen more than 30 days ago.
    cutoff = time.time() - 30 * 86400
    for jid in list(seen):
        try:
            if datetime.fromisoformat(seen[jid]["first_seen"]).timestamp() < cutoff:
                del seen[jid]
        except Exception:
            pass

    STATE.write_text(json.dumps(state, indent=2))
    return 0


def loop_forever(poll_seconds: int, max_minutes: int) -> None:
    """Cloud mode: keep polling inside one long CI job (GitHub's cron can't go
    below 5 minutes, but a job can run for up to 6 hours)."""
    end = time.time() + max_minutes * 60
    n = 0
    while time.time() < end:
        n += 1
        t0 = time.time()
        try:
            main()
        except Exception as e:
            log(f"ERROR in poll {n}: {type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}")
        remaining = end - time.time()
        if remaining < poll_seconds:
            break
        time.sleep(max(5, poll_seconds - (time.time() - t0)))
    log(f"loop finished after {n} polls")


if __name__ == "__main__":
    if "--loop" in sys.argv:
        loop_forever(int(os.environ.get("POLL_SECONDS", "150")), int(os.environ.get("LOOP_MINUTES", "345")))
        sys.exit(0)
    if "--resend" in sys.argv:
        cfg = load_json(CONFIG, {})
        job_id = sys.argv[sys.argv.index("--resend") + 1]
        state = load_json(STATE, {})
        j = {"id": job_id, "url": f"{BOARD}/{job_id}", "text": state.get("seen", {}).get(job_id, {}).get("text", ""), "lines": [], "full": ""}
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, channel="chromium")
            det = fetch_public_details(b.new_context(user_agent=UA).new_page(), job_id)
            b.close()
        title, banner, email_body, html = build_alert("online", j, det)
        print(title); print(banner); print(); print(email_body)
        (ROOT / "debug").mkdir(exist_ok=True)
        (ROOT / "debug" / f"alert_{job_id}.html").write_text(html)
        if "--send" in sys.argv:
            notify(cfg, title, banner, j["url"], email_body=email_body, html=html)
            print("\n(sent)")
        sys.exit(0)
    if "--notify" in sys.argv:
        # --notify "<title>" "<body>": send a plain alert through every configured channel
        i = sys.argv.index("--notify")
        cfg = load_json(CONFIG, {})
        notify(cfg, sys.argv[i + 1], sys.argv[i + 2], f"https://github.com/{GH_REPO}/actions")
        print("notified")
        sys.exit(0)
    if "--test" in sys.argv or "--test-email" in sys.argv:
        cfg = load_json(CONFIG, {})
        url = "https://highered.wyzant.com/tutor/jobs"
        body = "Test alert from your Wyzant orgo job monitor. Real alerts include the job snippet and link."
        if "--test" in sys.argv:
            notify(cfg, "Wyzant monitor test", body, url)
            print("sent: Mac banner" + (", email" if cfg.get("email_to") else "") + (", phone push" if cfg.get("phone_push") else ""))
        else:
            print("email sent" if send_email(cfg, "Wyzant monitor test", body, url) else "email NOT sent (see monitor.log)")
        sys.exit(0)
    try:
        sys.exit(main())
    except Exception as e:
        msg = str(e).splitlines()[0] if str(e) else type(e).__name__
        log(f"ERROR {type(e).__name__}: {msg}")
        sys.exit(1)
