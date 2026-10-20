"""Daily job digest: searches job sources and emails new roles.

Edit the SETTINGS block below to change search terms, filters or recipient.
Secrets (ADZUNA_APP_ID, ADZUNA_APP_KEY, GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
come from GitHub repository secrets, never from this file.
"""
import csv
import html
import json
import os
import re
import smtplib
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import requests

# ----------------------------- SETTINGS ------------------------------
RECIPIENT = "adamfitzsimons.pm@gmail.com"
SEARCH_TERMS = [
    "project manager",
    "project coordinator",
    "operations",
    "field operations",
    "logistics",
]
# Titles containing any of these words are dropped (whole words only).
EXCLUDE_WORDS = ["senior", "sr", "head", "director", "principal", "vp",
                 "vice president", "chief"]
# Locations accepted for company feeds and Arbeitnow (Adzuna is searched
# by Berlin and Ireland directly).
LOCATION_WORDS = ["berlin", "ireland", "dublin", "cork", "galway",
                  "limerick", "waterford"]
DAYS_BACK = 3            # how far back the search sources look
KEEP_SEEN_DAYS = 60      # how long a sent job is remembered
SEND_HOUR_BERLIN = 16    # scheduled runs only send during this Berlin hour
COMPANIES_FILE = "companies.csv"
SEEN_FILE = "seen.json"
# ---------------------------------------------------------------------

TITLE_RE = re.compile("|".join(re.escape(t) for t in SEARCH_TERMS), re.I)
EXCLUDE_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in EXCLUDE_WORDS) + r")\b", re.I)
LOCATION_RE = re.compile("|".join(re.escape(w) for w in LOCATION_WORDS), re.I)

HEADERS = {"User-Agent": "job-digest/1.0 (personal use)"}
errors = []


# ----------------------------- helpers -------------------------------
def redact(msg):
    """Remove query strings (which hold API keys) from error text."""
    return re.sub(r"(https?://[^\s?]+)\?\S*", r"\1", str(msg))


def clean(text):
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def get_json(url, **kwargs):
    r = requests.get(url, headers=HEADERS, timeout=20, **kwargs)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


GERMAN_WORDS = {
    "und", "der", "die", "das", "wir", "mit", "für", "ein", "eine", "sie",
    "ist", "von", "zu", "im", "auf", "bei", "dein", "deine", "du", "unser",
    "unsere", "erfahrung", "kenntnisse", "aufgaben", "bewerbung", "suchen",
}
REQUIRES_GERMAN_RE = re.compile(
    r"(fluent|native|good|business[- ]level|excellent|strong|proficient|"
    r"advanced|solid)\s+(\w+\s+){0,3}german"
    r"|german\s+(language\s+)?(skills|proficiency|fluency)"
    r"|german\s*\(?\s*(b2|c1|c2)"
    r"|german\s+(is\s+)?(required|mandatory|a must|essential)"
    r"|deutschkenntnisse|fließend|fliessend|verhandlungssicher|muttersprachl",
    re.I,
)
OPTIONAL_GERMAN_RE = re.compile(
    r"(german|deutsch)[^.]{0,40}(plus|advantage|nice to have|beneficial|desirable|bonus)"
    r"|(plus|advantage|nice to have|beneficial|desirable|bonus)[^.]{0,40}(german|deutsch)",
    re.I,
)


def needs_german(text):
    words = re.findall(r"[a-zäöüß]+", text.lower())
    if len(words) >= 12:
        hits = sum(1 for w in words if w in GERMAN_WORDS)
        if hits / len(words) > 0.08:
            return True  # the posting itself is written in German
    return bool(REQUIRES_GERMAN_RE.search(text) and not OPTIONAL_GERMAN_RE.search(text))


def keep(job, check_location):
    title = job["title"]
    if not TITLE_RE.search(title) or EXCLUDE_RE.search(title):
        return False
    loc = job.get("location", "")
    if check_location and loc and not LOCATION_RE.search(loc):
        return False
    if needs_german(title + " " + job.get("desc", "")):
        return False
    return True


def job_key(job):
    return f"{job['company'].lower().strip()}|{job['title'].lower().strip()}"


def company_base(name):
    base = re.sub(r"[^a-z0-9 ]", "", name.lower())
    base = re.sub(r"\b(gmbh|ag|se|inc|ltd|limited|kg|ug|co)\b", "", base)
    return re.sub(r"\s+", " ", base).strip()


# --------------------------- search sources --------------------------
def adzuna_search(country, term, where=None):
    params = {
        "app_id": os.environ["ADZUNA_APP_ID"],
        "app_key": os.environ["ADZUNA_APP_KEY"],
        "results_per_page": 50,
        "what": term,
        "sort_by": "date",
        "max_days_old": DAYS_BACK,
        "content-type": "application/json",
    }
    if where:
        params["where"] = where
        params["distance"] = 25
    time.sleep(2.5)  # stay under Adzuna's free rate limit
    data = get_json(f"https://api.adzuna.com/v1/api/jobs/{country}/search/1", params=params)
    jobs = []
    for r in (data or {}).get("results", []):
        jobs.append({
            "title": clean(r.get("title")),
            "company": clean((r.get("company") or {}).get("display_name", "Unknown")),
            "location": clean((r.get("location") or {}).get("display_name", "")),
            "url": r.get("redirect_url", ""),
            "desc": clean(r.get("description")),
            "source": "Adzuna",
        })
    return jobs


def fetch_adzuna():
    jobs = []
    for country, where in (("de", "Berlin"), ("ie", None)):
        for term in SEARCH_TERMS:
            try:
                jobs += adzuna_search(country, term, where)
            except Exception as e:  # keep going if one query fails
                errors.append(f"Adzuna {country} '{term}': {e}")
    return [j for j in jobs if keep(j, check_location=False)]


def fetch_arbeitnow():
    jobs = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)
    try:
        for page in range(1, 6):
            data = get_json("https://www.arbeitnow.com/api/job-board-api", params={"page": page})
            for j in (data or {}).get("data", []):
                created = datetime.fromtimestamp(j.get("created_at", 0), timezone.utc)
                if created < cutoff:
                    continue
                jobs.append({
                    "title": clean(j.get("title")),
                    "company": clean(j.get("company_name")),
                    "location": clean(j.get("location")),
                    "url": j.get("url", ""),
                    "desc": clean(j.get("description")),
                    "source": "Arbeitnow",
                })
    except Exception as e:
        errors.append(f"Arbeitnow: {e}")
    return [j for j in jobs if keep(j, check_location=True)]


# ------------------------- company watchlist -------------------------
def read_companies():
    if not os.path.exists(COMPANIES_FILE):
        return []
    names = []
    with open(COMPANIES_FILE, newline="", encoding="utf-8-sig") as f:
        for i, row in enumerate(csv.reader(f)):
            if not row or not row[0].strip() or row[0].strip().startswith("#"):
                continue
            name = row[0].strip()
            if i == 0 and re.match(r"compan", name, re.I):
                continue  # header row
            names.append(name)
    return names


def greenhouse(slug, name):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
    if data is None:
        return None
    return [{"title": clean(j["title"]), "company": name,
             "location": clean((j.get("location") or {}).get("name", "")),
             "url": j.get("absolute_url", ""), "desc": clean(j.get("content")),
             "source": "Greenhouse"} for j in data.get("jobs", [])]


def lever(slug, name):
    for host in ("api.lever.co", "api.eu.lever.co"):
        data = get_json(f"https://{host}/v0/postings/{slug}", params={"mode": "json"})
        if isinstance(data, list) and data:
            return [{"title": clean(j["text"]), "company": name,
                     "location": clean((j.get("categories") or {}).get("location", "")),
                     "url": j.get("hostedUrl", ""), "desc": clean(j.get("descriptionPlain")),
                     "source": "Lever"} for j in data]
    return None


def personio(slug, name):
    for domain in ("jobs.personio.de", "jobs.personio.com"):
        r = requests.get(f"https://{slug}.{domain}/xml", params={"language": "en"},
                         headers=HEADERS, timeout=20)
        if r.status_code != 200 or "<workzag-jobs" not in r.text:
            continue
        jobs = []
        for p in ET.fromstring(r.content).findall("position"):
            desc = " ".join(v.text or "" for v in p.iter("value"))
            jobs.append({"title": clean(p.findtext("name")), "company": name,
                         "location": clean(p.findtext("office")),
                         "url": f"https://{slug}.{domain}/job/{p.findtext('id')}",
                         "desc": clean(desc), "source": "Personio"})
        return jobs
    return None


def smartrecruiters(slug, name):
    data = get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", params={"limit": 100})
    if not data or not data.get("content"):
        return None
    jobs = []
    for j in data["content"]:
        loc = j.get("location") or {}
        jobs.append({"title": clean(j.get("name")), "company": name,
                     "location": clean(f"{loc.get('city', '')} {loc.get('country', '')}"),
                     "url": f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
                     "desc": "", "source": "SmartRecruiters"})
    return jobs


def workable(slug, name):
    data = get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", params={"details": "true"})
    if not data or not data.get("jobs"):
        return None
    return [{"title": clean(j.get("title")), "company": name,
             "location": clean(f"{j.get('city', '')} {j.get('country', '')}"),
             "url": j.get("url", ""), "desc": clean(j.get("description")),
             "source": "Workable"} for j in data["jobs"]]


PLATFORMS = [greenhouse, lever, personio, smartrecruiters, workable]


def fetch_company(name):
    """Return (jobs, found_feed). Tries common job platforms by name guess."""
    base = company_base(name)
    slugs = {base.replace(" ", ""), base.replace(" ", "-")}
    for slug in sorted(s for s in slugs if s):
        for platform in PLATFORMS:
            try:
                jobs = platform(slug, name)
            except Exception:
                continue
            if jobs is not None:
                return [j for j in jobs if keep(j, check_location=True)], True
    return [], False


def fetch_company_fallback(name):
    """Search Adzuna by company name when no job feed was found."""
    base = company_base(name)
    jobs = []
    for country, where in (("de", "Berlin"), ("ie", None)):
        try:
            for j in adzuna_search(country, name, where):
                if base and base in company_base(j["company"]):
                    j["source"] = "Adzuna (company search)"
                    jobs.append(j)
        except Exception as e:
            errors.append(f"Adzuna company search '{name}': {e}")
    return [j for j in jobs if keep(j, check_location=False)]


# ------------------------------ email --------------------------------
def render(watch_jobs, search_jobs, with_feed, no_feed):
    def block(jobs):
        rows = []
        for j in jobs:
            rows.append(
                f'<li><a href="{html.escape(j["url"])}"><b>{html.escape(j["title"])}</b></a><br>'
                f'{html.escape(j["company"])} &middot; {html.escape(j["location"] or "location not listed")}'
                f' <span style="color:#888">({html.escape(j["source"])})</span></li>'
            )
        return "<ul>" + "".join(rows) + "</ul>"

    parts = []
    if watch_jobs:
        parts.append(f"<h3>New roles at your watchlist companies ({len(watch_jobs)})</h3>" + block(watch_jobs))
    if search_jobs:
        parts.append(f"<h3>New roles from search ({len(search_jobs)})</h3>" + block(search_jobs))
    if not parts:
        parts.append("<p>No new matching roles today.</p>")
    foot = []
    if with_feed:
        foot.append("Company job feeds found for: " + ", ".join(with_feed))
    if no_feed:
        foot.append("No job feed found (searched via Adzuna only, so roles may be missed): " + ", ".join(no_feed))
    if errors:
        foot.append("Problems during this run: " + " | ".join(redact(e)[:200] for e in errors[:10]))
    parts.append('<hr><p style="color:#888;font-size:12px">' + "<br>".join(html.escape(f) for f in foot) + "</p>")
    return "".join(parts)


def send_email(subject, body_html):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["GMAIL_ADDRESS"]
    msg["To"] = RECIPIENT
    msg.set_content("Your email client does not show HTML. Open this message in Gmail.")
    msg.add_alternative(body_html, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"].replace(" ", ""))
        s.send_message(msg)


# ------------------------------- main --------------------------------
def should_run():
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return True  # manual test runs always go ahead
    now = datetime.now(ZoneInfo("Europe/Berlin"))
    return now.weekday() < 5 and now.hour == SEND_HOUR_BERLIN


def main():
    if not os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "w") as f:
            json.dump({}, f)
    if not should_run():
        print("Not the Berlin send hour. Skipping.")
        return

    with open(SEEN_FILE) as f:
        seen = json.load(f)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_SEEN_DAYS)).date().isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}

    watch_jobs, with_feed, no_feed = [], [], []
    for name in read_companies():
        jobs, found = fetch_company(name)
        if found:
            with_feed.append(name)
        else:
            no_feed.append(name)
            jobs = fetch_company_fallback(name)
        watch_jobs += jobs
    search_jobs = fetch_adzuna() + fetch_arbeitnow()

    def fresh(jobs):
        out = []
        for j in jobs:
            k = job_key(j)
            if k in seen:
                continue
            seen[k] = datetime.now(timezone.utc).date().isoformat()
            out.append(j)
        return out

    watch_new = fresh(watch_jobs)
    search_new = fresh(search_jobs)

    today = datetime.now(ZoneInfo("Europe/Berlin")).strftime("%a %d %b")
    total = len(watch_new) + len(search_new)
    send_email(f"Job digest {today}: {total} new", render(watch_new, search_new, with_feed, no_feed))
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, indent=0)
    print(f"Sent digest with {total} roles.")


if __name__ == "__main__":
    main()
