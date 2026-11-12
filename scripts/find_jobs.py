#!/usr/bin/env python3
"""Find new jobs every morning using only free services.

Where jobs come from (each source is optional and fails on its own without stopping the run):
  1. Tavily web search (free plan, no card) for private, bank and government postings
  2. Free remote-job feeds (Remotive, Jobicy)
  3. Official recruitment pages (Bangladesh Bank e-recruitment, Teletalk all government jobs)

Google's Gemini free tier then reads each result, keeps the real open postings that match,
pulls out the details (deadline, age limit, fee, how to apply...) and scores the fit.
Links always come from the sources themselves, never from the AI.

Only the Python standard library is used, so nothing needs installing.
"""

import datetime as dt
import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
JOBS_FILE = ROOT / "docs" / "jobs.json"
SEEN_FILE = ROOT / "data" / "seen.json"
TZ = dt.timezone(dt.timedelta(hours=6))  # Dhaka
NOW = dt.datetime.now(TZ)
TODAY = NOW.date().isoformat()
CATEGORIES = ["Private", "Bank", "Government", "Remote", "Abroad"]
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
RUN_LOG: list[dict] = []


def log(source: str, message: str) -> None:
    print(f"[{source}] {message}", flush=True)
    RUN_LOG.append({"source": source, "status": message})


# ---------------------------------------------------------------- HTTP

class HTTPFail(Exception):
    def __init__(self, code: int, body: str = ""):
        super().__init__(f"HTTP {code}")
        self.code = code
        self.body = body


def request(url, payload=None, headers=None, timeout=45):
    data = json.dumps(payload).encode() if payload is not None else None
    h = {"User-Agent": UA, "Accept": "*/*"}
    if data is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), (r.headers.get_content_charset() or "utf-8"), r.geturl()
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:600]
        except Exception:
            pass
        raise HTTPFail(e.code, body) from None


def get_json(url, payload=None, headers=None, timeout=45):
    raw, charset, _ = request(url, payload, headers, timeout)
    return json.loads(raw.decode(charset, "replace"))


# ---------------------------------------------------------------- text helpers

class PageText(HTMLParser):
    """Turns HTML into readable text and collects the page's links."""
    SKIP = {"script", "style", "noscript", "svg", "template"}
    BLOCK = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table", "ul", "ol", "td"}

    def __init__(self, base=""):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.skip = 0
        self._href = None
        self._atext: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
            return
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href")
            if href and not href.startswith(("javascript:", "mailto:", "tel:", "#")):
                self._href = urllib.parse.urljoin(self.base, href)
                self._atext = []

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if tag == "a" and self._href:
            text = " ".join("".join(self._atext).split())
            if text:
                self.links.append((text[:150], self._href))
            self._href = None

    def handle_data(self, data):
        if self.skip:
            return
        self.parts.append(data)
        if self._href is not None:
            self._atext.append(data)

    def text(self) -> str:
        t = "".join(self.parts)
        t = re.sub(r"[ \t\r\f\v\u00a0]+", " ", t)
        t = re.sub(r"\n\s*\n+", "\n", t)
        return t.strip()


def strip_html(s: str) -> str:
    p = PageText()
    try:
        p.feed(s or "")
    except Exception:
        return re.sub(r"<[^>]+>", " ", s or "")
    return p.text()


def norm_url(u: str) -> str:
    try:
        p = urllib.parse.urlsplit((u or "").strip())
    except ValueError:
        return ""
    host = p.netloc.lower().removeprefix("www.")
    return (host + p.path).rstrip("/").lower()


def site_of(u: str) -> str:
    try:
        return urllib.parse.urlsplit(u).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def make_id(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def same_job_key(org, title) -> str:
    clean = lambda s: re.sub(r"[^a-z0-9]+", "", (s or "").lower())
    return clean(org) + "|" + clean(title)


def valid_date(s):
    if not isinstance(s, str):
        return None
    try:
        return dt.date.fromisoformat(s.strip()[:10]).isoformat()
    except ValueError:
        return None


def title_matches(title: str) -> bool:
    t = (title or "").lower()
    if any(w in t for w in CONFIG.get("exclude_title_words", [])):
        return False
    return any(k.lower() in t for k in CONFIG["target_titles"])


def parse_json(text: str):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"(\[.*\]|\{.*\})", t, re.S)
        if m:
            return json.loads(m.group(1))
        raise


# ---------------------------------------------------------------- sources

def from_web_search() -> list[dict]:
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    ws = CONFIG.get("web_search", {})
    if not key:
        log("Web search", "Off. Add the TAVILY_API_KEY secret to turn it on.")
        return []
    found, done = [], 0
    for q in ws.get("queries", [])[: ws.get("max_queries_per_run", 10)]:
        payload = {
            "query": q["q"],
            "search_depth": "basic",
            "max_results": ws.get("results_per_query", 8),
            "time_range": q.get("time_range", ws.get("time_range", "week")),
            "include_raw_content": True,
        }
        if q.get("include_domains"):
            payload["include_domains"] = q["include_domains"]
        try:
            data = get_json("https://api.tavily.com/search", payload, {"Authorization": f"Bearer {key}"})
        except HTTPFail as e:
            log("Web search", f"Stopped at “{q['q']}”: HTTP {e.code} {e.body[:150]}")
            if e.code in (401, 403, 429, 432, 433):  # bad key, rate or free-plan limit: stop for today
                break
            continue
        except Exception as e:
            log("Web search", f"“{q['q']}” failed: {type(e).__name__}")
            continue
        done += 1
        for r in data.get("results", []):
            url = r.get("url") or ""
            if not url.startswith("http"):
                continue
            text = r.get("raw_content") or r.get("content") or ""
            found.append({
                "url": url,
                "title": (r.get("title") or "").strip(),
                "text": strip_html(text)[:6000] if "<" in text[:200] else text[:6000],
                "category_hint": q.get("category"),
                "source": site_of(url),
                "published": valid_date(r.get("published_date") or ""),
            })
        time.sleep(1)
    log("Web search", f"{done} searches, {len(found)} results")
    return found


REMOTE_OK = ("worldwide", "anywhere", "global", "asia", "apac", "bangladesh", "south asia", "emea, apac")


def remote_ok(location: str) -> bool:
    loc = (location or "").lower()
    return (not loc) or any(w in loc for w in REMOTE_OK)


def from_free_feeds() -> list[dict]:
    out = []
    for f in CONFIG.get("free_feeds", []):
        name = f.get("name", f.get("type", "Feed"))
        try:
            if f["type"] == "remotive":
                data = get_json("https://remotive.com/api/remote-jobs?" + urllib.parse.urlencode(
                    {"search": f.get("search", "product manager"), "limit": 100}))
                items = [{
                    "url": j.get("url"), "title": j.get("title", ""), "org": j.get("company_name", ""),
                    "loc": j.get("candidate_required_location", ""), "salary": j.get("salary") or "",
                    "text": strip_html(j.get("description", "")), "published": (j.get("publication_date") or "")[:10],
                } for j in data.get("jobs", [])]
            elif f["type"] == "jobicy":
                data = get_json("https://jobicy.com/api/v2/remote-jobs?" + urllib.parse.urlencode(
                    {"count": 50, "tag": f.get("tag", "product manager")}))
                items = [{
                    "url": j.get("url"), "title": j.get("jobTitle", ""), "org": j.get("companyName", ""),
                    "loc": j.get("jobGeo", ""), "salary": "",
                    "text": strip_html(j.get("jobDescription") or j.get("jobExcerpt") or ""),
                    "published": (j.get("pubDate") or "")[:10],
                } for j in data.get("jobs", [])]
            else:
                continue
        except Exception as e:
            log(name, f"Couldn’t load the feed ({e})")
            continue
        kept = [i for i in items if i["url"] and title_matches(i["title"]) and remote_ok(i["loc"])]
        for i in kept:
            out.append({
                "url": i["url"],
                "title": i["title"],
                "text": (f"Job title: {i['title']}\nCompany: {i['org']}\nWho can apply: {i['loc'] or 'not stated'}\n"
                         f"Salary: {i['salary'] or 'not stated'}\nPosted: {i['published']}\n\n{i['text']}")[:6000],
                "category_hint": "Remote",
                "source": name,
                "published": valid_date(i["published"]),
                "fallback": {"title": i["title"], "organization": i["org"], "location": i["loc"] or "Remote"},
            })
        log(name, f"{len(items)} listings, {len(kept)} match your titles and accept Bangladesh")
    return out


def read_official_pages() -> list[dict]:
    pages = []
    for p in CONFIG.get("official_pages", []):
        try:
            raw, charset, final = request(p["url"], timeout=40)
            parser = PageText(final)
            parser.feed(raw.decode(charset, "replace"))
            text = parser.text()
            if len(text) < 200:
                log(p["name"], "The page has no readable text here (it may need a browser to load).")
                continue
            pages.append({**p, "text": text[:15000], "links": parser.links[:250]})
            log(p["name"], f"Read the page ({len(parser.links)} links)")
        except HTTPFail as e:
            log(p["name"], f"Couldn’t open the page (HTTP {e.code})")
        except Exception as e:
            log(p["name"], f"Couldn’t open the page ({type(e).__name__})")
    return pages


# ---------------------------------------------------------------- Gemini (free tier)

class Gemini:
    def __init__(self, key: str):
        ai = CONFIG.get("ai", {})
        self.key = key
        self.models = ai.get("models", ["gemini-2.5-flash-lite", "gemini-2.5-flash"])
        self.exhausted: set[str] = set()
        self.calls = 0
        self.budget = ai.get("max_calls_per_run", 14)
        self.pause = ai.get("pause_seconds", 6)
        self.used: set[str] = set()

    @property
    def available(self) -> bool:
        return bool(self.key) and self.calls < self.budget and len(self.exhausted) < len(self.models)

    def ask(self, prompt: str):
        if not self.available:
            return None
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1}}
        for model in self.models:
            if model in self.exhausted:
                continue
            for attempt in range(2):
                if self.calls >= self.budget:
                    return None
                self.calls += 1
                try:
                    data = get_json(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                        body, {"x-goog-api-key": self.key}, timeout=150)
                    parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
                    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                    time.sleep(self.pause)
                    self.used.add(model)
                    return parse_json(text)
                except HTTPFail as e:
                    if e.code in (404, 429) or (e.code == 400 and "not found" in e.body.lower()):
                        # 404: model name retired. 429: this model's free quota is used up for now.
                        self.exhausted.add(model)
                        log("AI", f"{model}: {'not available' if e.code != 429 else 'free limit reached for now'}, trying the next model")
                        break
                    if e.code in (400, 403) and ("API key" in e.body or "API_KEY" in e.body or "PERMISSION" in e.body):
                        log("AI", "Gemini rejected the key. Check the GEMINI_API_KEY secret.")
                        self.key = ""
                        return None
                    if e.code >= 500 and attempt == 0:
                        time.sleep(10)
                        continue
                    log("AI", f"{model}: HTTP {e.code} {e.body[:150]}")
                    break
                except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                    log("AI", f"{model}: reply wasn’t readable")
                    break
                except Exception as e:
                    log("AI", f"{model}: {type(e).__name__}")
                    break
        return None


FIELDS = """{"i": <item number>, "match": true,
 "category": "Private" | "Bank" | "Government" | "Remote" | "Abroad",
 "title": "job title", "organization": "employer", "location": "city/country or Remote" or null,
 "work_type": "On-site" | "Hybrid" | "Remote" | null,
 "posted": "YYYY-MM-DD" or null, "application_start": "YYYY-MM-DD" or null, "deadline": "YYYY-MM-DD" or null,
 "vacancies": "as written" or null, "salary": "as written" or null, "experience": "as written" or null,
 "age_limit": "as written" or null, "max_age_years": number or null, "age_as_of": "YYYY-MM-DD" or null,
 "fee": "as written" or null, "apply_url": "a URL copied exactly from the item" or null,
 "how_to_apply": "one sentence" or null, "summary": "two plain sentences on the role",
 "requirements": ["up to 6 short items"], "documents": ["documents or uploads asked for, up to 6"],
 "fit": 1-5, "fit_reason": "one sentence"}"""


def targets_text() -> str:
    return "\n".join(f"- {k}: {v}" for k, v in CONFIG["targets"].items())


def profile_text() -> str:
    return (os.environ.get("CANDIDATE_PROFILE") or CONFIG.get("profile") or "").strip()


def batch_prompt(batch: list[dict]) -> str:
    items = "\n\n".join(
        f"### ITEM {n}\nURL: {c['url']}\nTITLE: {c['title']}\nFOUND VIA: {c['source']}\n{c['text'][:3500]}"
        for n, c in enumerate(batch, 1))
    return f"""Today is {TODAY}. You screen job search results for one person in Bangladesh.

What they are looking for:
{targets_text()}

About them, for the fit score:
{profile_text()}

For each ITEM decide whether it is ONE specific job posting or recruitment circular that is still open (or opening soon) and matches what they are looking for. Pages listing many unrelated jobs, news stories, exam results, admit cards, seat plans, salary guides and expired postings do NOT match. A page about one specific circular on a job-news site does match.

Use only facts written in the item. If something is not stated, use null. Never guess a date or a number. Convert Bengali dates and numbers. Write every field in English.

Reply with a JSON array with one object per item, in item order, shaped like:
{FIELDS}
For an item that does not match, return only {{"i": <item number>, "match": false}}.

{items}"""


def page_prompt(page: dict) -> str:
    links = "\n".join(f"[{n}] {t} -> {u}" for n, (t, u) in enumerate(page["links"], 1))
    return f"""Today is {TODAY}. Below is the text of an official recruitment page ({page['name']}, {page['url']}) and a numbered list of its links.

What this person is looking for:
{targets_text()}

About them, for the fit score:
{profile_text()}

List every recruitment notice on this page that is open (or opening soon) and matches what they are looking for. Use only facts on the page; use null when something is not stated. Write every field in English.

Reply with a JSON array (use [] if nothing matches). Each object is shaped like the one below, except that instead of "i" it has "link": the number of the link that opens that notice, or null:
{FIELDS}

PAGE TEXT:
{page['text']}

LINKS:
{links}"""


def build_job(fields: dict, url: str, source: str, text: str, category_hint: str) -> dict | None:
    title = str(fields.get("title") or "").strip()
    org = str(fields.get("organization") or "").strip()
    if not title:
        return None
    apply_url = fields.get("apply_url")
    if isinstance(apply_url, str):
        bare = re.sub(r"^https?://", "", apply_url.strip()).rstrip("/")
        if not apply_url.startswith("http") or bare.lower() not in text.lower():
            apply_url = None  # only keep links that are really written in the posting
    else:
        apply_url = None
    category = fields.get("category") if fields.get("category") in CATEGORIES else (category_hint or "Private")
    fit = fields.get("fit")
    try:
        max_age = float(fields.get("max_age_years")) if fields.get("max_age_years") not in (None, "") else None
    except (TypeError, ValueError):
        max_age = None
    as_list = lambda v: [str(x)[:200] for x in v][:6] if isinstance(v, list) else []
    return {
        "id": make_id(norm_url(url), same_job_key(org, title)),
        "category": category,
        "title": title[:160],
        "organization": org[:160],
        "location": fields.get("location"),
        "work_type": fields.get("work_type"),
        "posted": valid_date(fields.get("posted")),
        "application_start": valid_date(fields.get("application_start")),
        "deadline": valid_date(fields.get("deadline")),
        "vacancies": fields.get("vacancies"),
        "salary": fields.get("salary"),
        "experience": fields.get("experience"),
        "age_limit": fields.get("age_limit"),
        "max_age_years": max_age,
        "age_as_of": valid_date(fields.get("age_as_of")),
        "fee": fields.get("fee"),
        "url": url,
        "apply_url": apply_url,
        "how_to_apply": fields.get("how_to_apply"),
        "summary": fields.get("summary"),
        "requirements": as_list(fields.get("requirements")),
        "documents": as_list(fields.get("documents")),
        "fit": fit if isinstance(fit, int) and 1 <= fit <= 5 else None,
        "fit_reason": fields.get("fit_reason"),
        "source": source,
        "jd_text": text[:3000],
        "found_on": TODAY,
        "ai": True,
    }


def plain_job(c: dict) -> dict:
    """A job saved without the AI check (used when the free AI limit is reached)."""
    fb = c.get("fallback") or {}
    return {
        "id": make_id(norm_url(c["url"]), same_job_key(fb.get("organization", ""), c["title"])),
        "category": c.get("category_hint") or "Private",
        "title": (fb.get("title") or c["title"])[:160],
        "organization": fb.get("organization") or c["source"],
        "location": fb.get("location"),
        "posted": c.get("published"),
        "deadline": None,
        "url": c["url"],
        "apply_url": None,
        "summary": None,
        "requirements": [],
        "documents": [],
        "fit": None,
        "source": c["source"],
        "jd_text": c["text"][:3000],
        "found_on": TODAY,
        "ai": False,
    }


def looks_like_job(c: dict) -> bool:
    blob = (c["title"] + " " + c["url"]).lower()
    return title_matches(c["title"]) or bool(re.search(r"circular|recruit|vacanc|career|job", blob))


# ---------------------------------------------------------------- alerts

def site_url() -> str:
    if os.environ.get("SITE_URL"):
        return os.environ["SITE_URL"]
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/"
    return ""


def notify(new_jobs: list[dict]) -> None:
    if not new_jobs:
        return
    top = sorted(new_jobs, key=lambda j: (-(j.get("fit") or 0), j.get("deadline") or "9999"))[:10]
    link = site_url()

    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        lines = [f"<b>{len(new_jobs)} new job{'s' if len(new_jobs) != 1 else ''} today</b>"]
        for j in top:
            bits = [j["category"]]
            if j.get("fit"):
                bits.append(f"fit {j['fit']}/5")
            if j.get("deadline"):
                bits.append(f"deadline {j['deadline']}")
            lines.append(f"\n• <a href=\"{html.escape(j['url'])}\">{html.escape(j['title'])}</a>, "
                         f"{html.escape(j.get('organization') or '')} ({html.escape(', '.join(bits))})")
        if link:
            lines.append(f"\n<a href=\"{html.escape(link)}\">Open your job hunt desk</a>")
        try:
            get_json(f"https://api.telegram.org/bot{token}/sendMessage",
                     {"chat_id": chat, "text": "".join(lines)[:4000], "parse_mode": "HTML", "disable_web_page_preview": True})
            log("Alerts", "Sent the Telegram message")
        except Exception as e:
            log("Alerts", f"Telegram failed ({e})")

    user, password = os.environ.get("EMAIL_USER"), os.environ.get("EMAIL_APP_PASSWORD")
    if user and password:
        msg = EmailMessage()
        msg["Subject"] = f"{len(new_jobs)} new job{'s' if len(new_jobs) != 1 else ''} for you today"
        msg["From"] = user
        msg["To"] = os.environ.get("EMAIL_TO") or user
        body = []
        for j in top:
            extra = ", ".join(x for x in [j["category"], f"fit {j['fit']}/5" if j.get("fit") else "",
                                          f"deadline {j['deadline']}" if j.get("deadline") else ""] if x)
            body.append(f"{j['title']}, {j.get('organization') or ''} ({extra})\n{j['url']}\n")
        if link:
            body.append(f"Open your job hunt desk: {link}")
        msg.set_content("\n".join(body))
        try:
            host = os.environ.get("EMAIL_SMTP_HOST", "smtp.gmail.com")
            with smtplib.SMTP_SSL(host, 465, context=ssl.create_default_context(), timeout=30) as s:
                s.login(user, password)
                s.send_message(msg)
            log("Alerts", "Sent the email")
        except Exception as e:
            log("Alerts", f"Email failed ({type(e).__name__})")


# ---------------------------------------------------------------- main

def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def main() -> int:
    store = load(JOBS_FILE, {"jobs": []})
    seen: dict = load(SEEN_FILE, {})
    jobs = {j["id"]: j for j in store.get("jobs", [])}
    known_urls = {norm_url(j["url"]) for j in jobs.values()}
    known_keys = {same_job_key(j.get("organization"), j.get("title")) for j in jobs.values()}

    ai = Gemini(os.environ.get("GEMINI_API_KEY", "").strip())
    if not ai.key:
        log("AI", "Off. Add the GEMINI_API_KEY secret so results get checked and scored.")

    new_jobs: list[dict] = []

    def add(job, is_new=True):
        if not job:
            return
        if job.get("deadline") and job["deadline"] < TODAY:
            return
        u, k = norm_url(job["url"]), same_job_key(job.get("organization"), job.get("title"))
        if u in known_urls or (k in known_keys and k != "|"):
            return
        known_urls.add(u)
        known_keys.add(k)
        jobs[job["id"]] = job
        if is_new:
            new_jobs.append(job)

    # 1. Official pages first: they are the most reliable source for bank and government jobs.
    for page in read_official_pages():
        result = ai.ask(page_prompt(page))
        if result is None:
            log(page["name"], "Skipped the AI check (no key or free limit reached)")
            continue
        added = 0
        for f in result if isinstance(result, list) else []:
            if not isinstance(f, dict):
                continue
            idx = f.get("link")
            url = page["links"][idx - 1][1] if isinstance(idx, int) and 1 <= idx <= len(page["links"]) else page["url"]
            before = len(new_jobs)
            add(build_job(f, url, page["name"], page["text"], page.get("category")))
            added += len(new_jobs) - before
        log(page["name"], f"{added} new matching notice{'s' if added != 1 else ''}")

    # 2. Search results and free feeds.
    candidates = from_web_search() + from_free_feeds()
    fresh, batch_urls = [], set()
    for c in candidates:
        u = norm_url(c["url"])
        if not u or u in seen or u in known_urls or u in batch_urls:
            continue
        batch_urls.add(u)
        fresh.append(c)
    # Earlier jobs saved without the AI check get another chance.
    for j in list(jobs.values()):
        if not j.get("ai") and j.get("jd_text"):
            fresh.append({"url": j["url"], "title": j["title"], "text": j["jd_text"], "source": j["source"],
                          "category_hint": j["category"], "published": j.get("posted"), "retry_of": j["id"]})
    # Most promising first, so the free AI allowance is spent where it matters.
    fresh.sort(key=lambda c: (0 if title_matches(c["title"]) else 1,
                              0 if c.get("category_hint") in ("Bank", "Government") else 1))

    size = CONFIG.get("ai", {}).get("batch_size", 6)
    checked, unchecked = 0, []
    for start in range(0, len(fresh), size):
        batch = fresh[start:start + size]
        result = ai.ask(batch_prompt(batch)) if ai.available else None
        if result is None:
            unchecked.extend(batch)
            continue
        checked += len(batch)
        by_i = {r.get("i"): r for r in result if isinstance(r, dict)} if isinstance(result, list) else {}
        for n, c in enumerate(batch, 1):
            r = by_i.get(n)
            if c.get("retry_of"):
                old = jobs.pop(c["retry_of"], None)  # replace the unchecked copy with the checked one
                if old:
                    known_urls.discard(norm_url(old["url"]))
                    known_keys.discard(same_job_key(old.get("organization"), old.get("title")))
            seen[norm_url(c["url"])] = TODAY
            if r and r.get("match"):
                job = build_job(r, c["url"], c["source"], c["text"], c.get("category_hint"))
                if job and c.get("retry_of"):
                    job["found_on"] = old["found_on"] if old else TODAY
                add(job, is_new=not c.get("retry_of"))
    # Without the AI, keep only results that clearly look like matching jobs.
    kept_plain = 0
    for c in unchecked:
        if c.get("retry_of"):
            continue
        if looks_like_job(c) and (c.get("fallback") or title_matches(c["title"])):
            before = len(new_jobs)
            add(plain_job(c))
            kept_plain += len(new_jobs) - before
    if fresh:
        log("AI", f"Checked {checked} of {len(fresh)} results with {ai.calls} AI call{'s' if ai.calls != 1 else ''}"
                  + (f" ({', '.join(sorted(ai.used))})" if ai.used else "")
                  + (f". Saved {kept_plain} unchecked; they’ll be checked tomorrow." if kept_plain else ""))

    # 3. Tidy up.
    keep_days = CONFIG.get("keep_jobs_for_days", 45)
    cutoff = (NOW.date() - dt.timedelta(days=keep_days)).isoformat()
    final = [j for j in jobs.values()
             if j.get("found_on", TODAY) >= cutoff and not (j.get("deadline") and j["deadline"] < TODAY)]
    final.sort(key=lambda j: (j.get("found_on", ""), j.get("fit") or 0), reverse=True)
    seen_cut = (NOW.date() - dt.timedelta(days=60)).isoformat()
    seen = {u: d for u, d in seen.items() if d >= seen_cut}

    notify(new_jobs)

    stamp = NOW.isoformat(timespec="minutes")
    JOBS_FILE.write_text(json.dumps({
        "updated_at": stamp if new_jobs else store.get("updated_at"),
        "last_run": {"at": stamp, "new": len(new_jobs), "sources": RUN_LOG},
        "jobs": final,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, indent=0), encoding="utf-8")
    print(f"Done: {len(new_jobs)} new, {len(final)} in the list.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
