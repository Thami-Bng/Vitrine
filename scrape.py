#!/usr/bin/env python3
"""
Scraper engine — writes data/jobs.json for The Vitrine web page.

Pulls from multiple sources through a common connector interface, normalises everything to one
schema (with the FULL job description where the source allows), de-duplicates, remembers when each
role was first seen (so the page can badge "new"), and writes data/jobs.json.

Sources included:
  • SmartRecruiters — direct ATS API (free, no auth). Fetches the full JD per posting.
  • Google Jobs via SerpApi — broad aggregator across LinkedIn + every ATS (needs SERPAPI_KEY).

Add a connector by writing one function that returns a list of Job dicts and appending it to
CONNECTORS. That's the whole extension model.

Usage:
    python scrape.py            # write data/jobs.json
    python scrape.py --dry-run  # print a summary, write nothing

Env: SERPAPI_KEY (optional — enables the Google Jobs connector)
"""

from __future__ import annotations
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ------------------------------- CONFIG ------------------------------- #

OUT = Path("data/jobs.json")
COUNTRY = "sg"
UA = "vitrine-scraper/1.0 (personal job search)"
TIMEOUT = 25

# SmartRecruiters company slugs (verify Kering ones with the connector-level 404 handling).
SR_COMPANIES = {
    "LVMH Beauty": "LVMH2",
    "Gucci": "Gucci",
    "Saint Laurent": "SaintLaurent",
    "Bottega Veneta": "BottegaVeneta",
    "Balenciaga": "Balenciaga",
}
SR_FETCH_DETAIL = True          # fetch full JD per posting (more requests, richer data)

# Google Jobs role searches (each sees every company at once).
GJ_QUERIES = [
    "e-commerce manager Singapore",
    "e-commerce product owner Singapore",
    "digital product manager Singapore beauty",
    "e-commerce project manager Singapore luxury",
    "omnichannel manager Singapore beauty",
    "CRM ecommerce Singapore luxury",
]
GJ_PAGES = 1

PREFERRED_BRANDS = [b.lower() for b in [
    "lvmh","sephora","l'oreal","loreal","clarins","estee lauder","estée lauder","kering","gucci",
    "saint laurent","bottega","balenciaga","richemont","cartier","shiseido","amorepacific","puig",
    "guerlain","dior","chanel","hermes","hermès","charlotte tilbury","net-a-porter","ynap","lancome",
]]

INCLUDE = re.compile("|".join([
    r"e-?commerce", r"e-?comm", r"e-?boutique", r"product owner", r"product manager",
    r"digital product", r"\bdigital\b", r"\bomnichannel\b", r"\bdtc\b", r"\bd2c\b",
    r"business analyst", r"\bcrm\b", r"marketplace", r"\bonline\b",
]), re.I)
EXCLUDE = re.compile("|".join([
    r"\bintern\b", r"internship", r"apprentice", r"working student", r"fresh graduate",
    r"beauty advisor", r"client advisor", r"sales associate", r"retail associate",
    r"store manager", r"boutique manager", r"\bcounter\b", r"cashier",
]), re.I)


# ------------------------------- HELPERS ------------------------------- #

def preferred(company: str) -> bool:
    c = (company or "").lower()
    return any(b in c for b in PREFERRED_BRANDS)

def relevant(title: str, desc: str) -> bool:
    blob = f"{title} {desc}"
    return bool(INCLUDE.search(blob)) and not EXCLUDE.search(blob)

def job(id_, title, company, location, source, url, posted="", description=""):
    return {
        "id": str(id_), "title": (title or "").strip(), "company": (company or "").strip(),
        "location": (location or "").strip(), "source": source, "url": url or "",
        "posted": posted or "", "description": (description or "").strip(),
        "preferred": preferred(company),
    }


# ------------------------------- CONNECTORS ------------------------------- #

def connector_smartrecruiters() -> list[dict]:
    out, base = [], "https://api.smartrecruiters.com/v1/companies/{c}/postings"
    hdr = {"User-Agent": UA, "Accept": "application/json"}
    for label, slug in SR_COMPANIES.items():
        offset = 0
        while True:
            try:
                r = requests.get(base.format(c=slug),
                                 params={"country": COUNTRY, "limit": 100, "offset": offset},
                                 headers=hdr, timeout=TIMEOUT)
            except requests.RequestException as e:
                print(f"  ! SR {label}: {e}", file=sys.stderr); break
            if r.status_code != 200:
                print(f"  ! SR {label}: HTTP {r.status_code}", file=sys.stderr); break
            data = r.json()
            for p in data.get("content", []):
                loc = p.get("location", {}) or {}
                title = p.get("name", "")
                desc = ""
                if SR_FETCH_DETAIL:
                    desc = sr_detail(slug, p.get("id", ""), hdr)
                    time.sleep(0.25)
                apply_url = (((p.get("actions") or {}).get("apply") or {}).get("url") or
                             f"https://jobs.smartrecruiters.com/{slug}/{p.get('id','')}")
                out.append(job(p.get("id"), title, label, loc.get("city", "Singapore"),
                               "SmartRecruiters", apply_url,
                               (p.get("releasedDate") or "")[:10], desc))
            total = data.get("totalFound", 0)
            offset += 100
            if offset >= total:
                break
            time.sleep(0.3)
    return out

def sr_detail(slug, pid, hdr) -> str:
    """Fetch the full posting and stitch its sections into one description string."""
    if not pid:
        return ""
    try:
        r = requests.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{pid}",
                         headers=hdr, timeout=TIMEOUT)
        if r.status_code != 200:
            return ""
        sections = (((r.json().get("jobAd") or {}).get("sections")) or {})
        parts = []
        for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
            txt = (sections.get(key) or {}).get("text", "")
            if txt:
                parts.append(re.sub(r"<[^>]+>", "", txt).strip())   # strip HTML tags
        return "\n\n".join(parts)
    except requests.RequestException:
        return ""

def connector_google_jobs() -> list[dict]:
    key = os.getenv("SERPAPI_KEY")
    if not key:
        print("  · Google Jobs skipped (no SERPAPI_KEY)")
        return []
    out = []
    for q in GJ_QUERIES:
        token, page = None, 0
        while page < GJ_PAGES:
            params = {"engine": "google_jobs", "q": q, "location": "Singapore",
                      "hl": "en", "gl": "sg", "api_key": key}
            if token:
                params["next_page_token"] = token
            try:
                r = requests.get("https://serpapi.com/search.json", params=params, timeout=TIMEOUT)
            except requests.RequestException as e:
                print(f"  ! GJ '{q}': {e}", file=sys.stderr); break
            if r.status_code != 200:
                print(f"  ! GJ '{q}': HTTP {r.status_code}", file=sys.stderr); break
            data = r.json()
            for j in data.get("jobs_results", []):
                ext = j.get("detected_extensions") or {}
                opts = j.get("apply_options") or []
                url = (opts[0].get("link") if opts else "") or j.get("share_link", "")
                out.append(job(j.get("job_id", "")[:120], j.get("title"), j.get("company_name"),
                               j.get("location"), "Google Jobs", url,
                               ext.get("posted_at", ""), j.get("description", "")))
            token = (data.get("serpapi_pagination") or {}).get("next_page_token")
            page += 1
            if not token:
                break
            time.sleep(1.0)
    return out

CONNECTORS = [connector_smartrecruiters, connector_google_jobs]


# ------------------------------- MAIN ------------------------------- #

def load_previous() -> dict:
    if OUT.exists():
        try:
            prev = json.loads(OUT.read_text())
            return {j["id"]: j for j in prev.get("jobs", [])}
        except (json.JSONDecodeError, KeyError):
            return {}
    return {}

def run(dry_run: bool = False) -> None:
    prev = load_previous()
    now = datetime.now(timezone.utc).isoformat()

    collected = {}
    for conn in CONNECTORS:
        print(f"→ {conn.__name__}")
        try:
            for j in conn():
                if "singapore" not in j["location"].lower():
                    continue
                if not relevant(j["title"], j["description"]):
                    continue
                # carry over first_seen if we've seen this id before
                j["first_seen"] = prev.get(j["id"], {}).get("first_seen", now)
                collected[j["id"]] = j
        except Exception as e:
            print(f"  ! {conn.__name__} crashed: {e}", file=sys.stderr)

    jobs = sorted(collected.values(),
                  key=lambda j: (not j["preferred"], j["first_seen"] < now, j["company"]))
    new_count = sum(1 for j in jobs if j["first_seen"] == now and prev)
    print(f"\n{len(jobs)} relevant SG roles · {new_count} new since last run")

    if dry_run:
        for j in jobs[:25]:
            tag = "⭐" if j["preferred"] else " "
            print(f"  {tag} {j['title']} — {j['company']} ({j['source']})")
        return

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"generated_at": now, "jobs": jobs}, indent=2, ensure_ascii=False))
    print(f"Wrote {OUT} ({len(jobs)} roles).")

def main() -> None:
    ap = argparse.ArgumentParser(description="Scrape SG luxury/beauty roles into data/jobs.json")
    ap.add_argument("--dry-run", action="store_true")
    run(dry_run=ap.parse_args().dry_run)

if __name__ == "__main__":
    main()
