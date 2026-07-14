#!/usr/bin/env python3
"""
Scraper engine — writes data/jobs.json for The Vitrine web page.

Sources (all Singapore):
  • MyCareersFuture — free gov.sg API. Broadest SG coverage (legally-mandated postings), gives
    the official MCF link. Powers the "All Singapore" view. No key needed.
  • SmartRecruiters — direct ATS API (free). LVMH Beauty + Kering houses, with full JD.
  • Google Jobs via SerpApi — adds LinkedIn + international breadth (needs SERPAPI_KEY, quota-limited).

Each role is tagged: type (house / agency / other) and sector (beauty_luxury / general), gets a
normalised posting date for sorting, and separate careers / LinkedIn / MyCareersFuture links.

Usage:  python scrape.py [--dry-run]
Env:    SERPAPI_KEY (optional)
"""

from __future__ import annotations
import argparse, html, json, os, re, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

# ------------------------------- CONFIG ------------------------------- #

OUT = Path("data/jobs.json")
UA = "vitrine-scraper/1.0 (personal job search)"
# Some ATS endpoints (e.g. Kering's Workday) block obvious bot UAs — use a browser UA + Origin/Referer there.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TIMEOUT = 25

# Free keyword searches on MyCareersFuture (broad SG coverage, all sectors).
MCF_QUERIES = ["ecommerce", "product owner", "product manager", "digital project manager",
               "omnichannel", "digital consultant", "ui ux", "business analyst", "performance analyst"]

# SmartRecruiters slugs (LVMH2 confirmed). Kering is NOT here — it's on Workday (see below).
SR_COMPANIES = {"LVMH Beauty": "LVMH2"}
SR_FETCH_DETAIL = True

# Workday tenants (direct from the source). host + site come from the "Apply" URL, e.g.
# richemont.wd3.myworkdayjobs.com/richemont/... -> host=richemont.wd3.myworkdayjobs.com, site=richemont
WORKDAY_TENANTS = {
    # Richemont group (host tenant "richemont", site "richemont") — open, works well.
    "Richemont / Cartier": {"host": "richemont.wd3.myworkdayjobs.com", "site": "richemont"},
    # NOTE: Kering (kering.wd3.myworkdayjobs.com — Balenciaga/Gucci/Saint Laurent/etc.) blocks
    # automated access at the server level (robots-disallowed / bot wall), so the API returns
    # errors from any datacenter IP. We reach Kering roles via Google Jobs instead (they're
    # syndicated to Indeed/FashionJobs/Jobstreet, which Google indexes). See GJ_POOL below.
}
WORKDAY_FETCH_DETAIL = True

# Eightfold tenants (Estée Lauder Companies). EXPERIMENTAL — verify on first run.
EIGHTFOLD_TENANTS = {
    "Estée Lauder": {"host": "elcompanies.eightfold.ai", "domain": "elcompanies.com"},
}

# SuccessFactors career sites (SAP). All share the same /search/ + /job/<slug>/<id>/ structure.
# Location is baked into each job's URL slug, so we filter to Singapore on the slug.
SF_SITES = {
    "Sephora":  "jobs.sephora.com",
    "Shiseido": "careers.shiseido.com",
    "Clarins":  "careers.groupeclarins.com",
}
SF_MAX_PAGES = 8            # 50 roles/page, newest first

# Drop roles whose UPPER salary band is below this (monthly SGD). Roles with no salary are kept.
MIN_SALARY_MAX = 9000

# Per-source diagnostics, written into jobs.json so the page can show a "Sources" panel.
STATS = []
def record(label, fetched, kept, error=False):
    STATS.append({"label": label, "fetched": fetched, "kept": kept, "error": bool(error)})

# Shared keyword list for keyword-driven sources (Workday needs a search term).
KEYWORDS = ["ecommerce", "e-commerce", "product owner", "product manager", "digital project manager",
            "omnichannel", "digital consultant", "ui ux", "business analyst", "performance analyst"]

# Google Jobs (SerpApi) — free quota is 250/month. We rotate a pool so we cover the bot-walled
# houses (Kering) by name without exceeding budget: GJ_PER_RUN x 2 runs/day x 30 = must stay <250.
# House names are queried directly because Kering roles live on Indeed/FashionJobs/Jobstreet,
# which Google indexes even though Kering's own site blocks us. relevant() then filters titles.
GJ_POOL = [
    "Balenciaga Singapore", "Gucci Singapore", "Saint Laurent Singapore",
    "Bottega Veneta Singapore", "Alexander McQueen Singapore", "Boucheron Singapore",
    "e-commerce Singapore luxury", "digital product manager Singapore",
    "omnichannel manager Singapore", "product owner Singapore beauty",
]
GJ_PER_RUN = 4              # 4 x 2/day x 30 = 240 < 250 free quota
GJ_PAGES = 1

def gj_queries_for_this_run():
    """Rotate a window through GJ_POOL so every query runs regularly without blowing quota."""
    n=datetime.now(timezone.utc)
    slot=n.timetuple().tm_yday*2 + (0 if n.hour < 4 else 1)   # two runs/day: 9am & 3pm SGT
    start=(slot*GJ_PER_RUN) % len(GJ_POOL)
    return [GJ_POOL[(start+i) % len(GJ_POOL)] for i in range(GJ_PER_RUN)]

PREFERRED_BRANDS = [b.lower() for b in [
    "lvmh","sephora","l'oreal","loreal","clarins","estee lauder","estée lauder","kering","gucci",
    "saint laurent","bottega","balenciaga","richemont","cartier","shiseido","amorepacific","puig",
    "guerlain","dior","chanel","hermes","hermès","charlotte tilbury","net-a-porter","ynap","lancome",
]]

# Recruitment agencies — tagged, not dropped.
AGENCIES = [
    "michael page","pagegroup","robert walters","randstad","adecco","hays","kelly services","manpower",
    "morgan mckinley","charterhouse","kerry consulting","recruit express","persolkelly","robert half",
    "hudson","ambition","links international","jac recruitment","achieve group","good job creations",
    "reeracoen","supreme hr","gmp recruitment","the edge partnership","cornerstone global","peoplebank",
]

# Signals that a role is beauty / luxury (for the sector tab).
LUX_SIGNALS = [
    "beauty","cosmetic","skincare","skin care","fragrance","perfume","makeup","make-up","luxury",
    "couture","maison","jewellery","jewelry","watchmaking","fashion house","prestige","parfum","luxasia",
]

INCLUDE = re.compile("|".join([
    r"product owner", r"digital project manager", r"e-?commerce", r"e-?comm", r"omni-?channel",
    r"digital consultant", r"product manager", r"product management", r"ui\s*/?\s*ux", r"ux\s*/?\s*ui",
    r"business analyst", r"performance analyst",
]), re.I)
EXCLUDE = re.compile("|".join([
    r"controller", r"controlling", r"treasury", r"finance", r"accountant", r"\btax\b", r"\baudit",
    r"payroll", r"clienteling", r"\bintern\b", r"internship", r"apprentice", r"working student",
    r"fresh graduate", r"beauty advisor", r"client advisor", r"sales associate", r"retail associate",
    r"store manager", r"boutique manager", r"\bcounter\b", r"cashier", r"supply chain", r"logistics",
    r"warehouse", r"customer service", r"service assistant", r"call cent", r"receptionist",
    r"data entry", r"telesales",
]), re.I)

GOOD_LINK = ("smartrecruiters","myworkdayjobs","eightfold","avature","successfactors","taleo","icims",
             "workday","lever.co","greenhouse","careers.","jobs.","/careers")
AGGREGATORS = ("trabajo","jobrapido","neuvoo","talent.com","learn4good","jooble","adzuna","whatjobs",
               "jobsora","recruit.net","mncjobz","kitjob","simplyhired","jobcloud")

# ------------------------------- HELPERS ------------------------------- #

def preferred(company): 
    c=(company or "").lower(); return any(b in c for b in PREFERRED_BRANDS)

def is_agency(company):
    c=(company or "").lower(); return any(a in c for a in AGENCIES)

def type_of(company):
    if is_agency(company): return "agency"
    if preferred(company): return "house"
    return "other"

def sector_of(company, title, desc):
    if preferred(company): return "beauty_luxury"
    blob=f"{company} {title} {desc}".lower()
    return "beauty_luxury" if any(w in blob for w in LUX_SIGNALS) else "general"

def clean_text(raw):
    if not raw: return ""
    txt=re.sub(r"<\s*(br|/p|/div|/li)\s*>","\n",raw,flags=re.I)
    txt=re.sub(r"<[^>]+>"," ",txt)
    txt=html.unescape(txt).replace("\xa0"," ")
    txt=re.sub(r"[ \t]+"," ",txt); txt=re.sub(r"\n[ \t]+","\n",txt); txt=re.sub(r"\n{3,}","\n\n",txt)
    return txt.strip()

def relevant(title):
    t=title or ""
    return bool(INCLUDE.search(t)) and not EXCLUDE.search(t)

# Collapse company name variants into a house-group, so the same role from different sources
# (e.g. Google "Cartier" + Google "Richemont" + Workday "Richemont / Cartier") dedupes together.
GROUP_MAP = [
    (("cartier","richemont","van cleef","jaeger","iwc","piaget","vacheron","panerai","montblanc",
      "chloe","chloé","alaia","alaïa","dunhill","baume","buccellati"),"richemont"),
    (("kering","gucci","saint laurent","ysl","bottega","balenciaga","mcqueen","boucheron","pomellato",
      "brioni","qeelin","ginori"),"kering"),
    (("lvmh","sephora","dior","guerlain","givenchy","fenty","benefit","make up for ever","loewe",
      "celine","fendi","louis vuitton","bulgari","tiffany","acqua di parma","kenzo"),"lvmh"),
    (("l'oreal","loreal","lancome","lancôme","kiehl","biotherm","urban decay","kerastase","kérastase"),"loreal"),
    (("estee","estée","clinique","la mer","jo malone","aveda","bobbi brown","origins","tom ford"),"elc"),
    (("clarins","myblend"),"clarins"),
    (("shiseido","nars","drunk elephant","cle de peau","clé de peau"),"shiseido"),
    (("amorepacific","sulwhasoo","laneige","innisfree"),"amorepacific"),
    (("puig","charlotte tilbury","carolina herrera","jean paul gaultier","byredo","rabanne"),"puig"),
]
def group_of(company):
    c=(company or "").lower()
    for kws,g in GROUP_MAP:
        if any(k in c for k in kws): return g
    return c

def norm_title(t):
    return re.sub(r"\s+"," ",re.sub(r"[^a-z0-9 ]"," ",(t or "").lower())).strip()

SOURCE_RANK={"Workday":5,"SuccessFactors":5,"SmartRecruiters":5,"Eightfold":4,"MyCareersFuture":2,"Google Jobs":1}

def dedupe(items):
    best={}
    for j in items:
        key=f"{norm_title(j['title'])}|{group_of(j['company'])}"
        cur=best.get(key)
        if cur is None:
            best[key]=j; continue
        hi,lo=(j,cur) if SOURCE_RANK.get(j["source"],0)>=SOURCE_RANK.get(cur["source"],0) else (cur,j)
        if not hi.get("salary") and lo.get("salary"):
            hi["salary"]=lo["salary"]; hi["salary_max"]=lo.get("salary_max")
        for k in ("careers","linkedin","mcf","primary"):
            if not hi["links"].get(k) and lo["links"].get(k): hi["links"][k]=lo["links"][k]
        hi["url"]=hi["links"].get("primary") or hi.get("url","")
        if not hi.get("posted_date") and lo.get("posted_date"):
            hi["posted_date"]=lo["posted_date"]; hi["posted"]=lo.get("posted",hi.get("posted",""))
        if lo.get("first_seen","z")<hi.get("first_seen","z"): hi["first_seen"]=lo["first_seen"]
        best[key]=hi
    return list(best.values())

def to_date(s):
    """Normalise a posting date to ISO (YYYY-MM-DD) for sorting."""
    if not s: return ""
    s=str(s).strip().lower()
    m=re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m: return m.group(1)
    today=datetime.now(timezone.utc).date()
    if any(w in s for w in ("today","hour","just","min","moment")): return today.isoformat()
    if "yesterday" in s: return (today-timedelta(days=1)).isoformat()
    for pat,mult in ((r"(\d+)\s*day",1),(r"(\d+)\s*week",7),(r"(\d+)\s*month",30)):
        m=re.search(pat,s)
        if m: return (today-timedelta(days=int(m.group(1))*mult)).isoformat()
    return ""

def best_apply_link(options, share=""):
    if not options: return share
    def score(o):
        link=(o.get("link") or "").lower(); title=(o.get("title") or "").lower(); s=0
        if any(g in link for g in GOOD_LINK): s+=5
        if "career" in title: s+=4
        if "linkedin" in link: s+=3
        if any(a in link or a in title for a in AGGREGATORS): s-=6
        return s
    return max(options,key=score).get("link") or share

def extract_links(options, share=""):
    careers=linkedin=mcf=""
    for o in options:
        link=o.get("link") or ""; low=link.lower()
        if "linkedin.com" in low and not linkedin: linkedin=link
        elif "mycareersfuture" in low and not mcf: mcf=link
        elif any(g in low for g in GOOD_LINK) and not careers: careers=link
    primary=careers or linkedin or best_apply_link(options,share)
    return {"primary":primary,"careers":careers,"linkedin":linkedin,"mcf":mcf}

def job(id_, title, company, source, links, posted="", description="", salary="", salary_max=None):
    company=(company or "").strip()
    return {"id":str(id_), "title":(title or "").strip(), "company":company, "source":source,
            "type":type_of(company), "sector":sector_of(company,title,description),
            "posted":posted or "", "posted_date":to_date(posted),
            "salary":salary or "", "salary_max":salary_max,
            "links":links, "url":links.get("primary",""),
            "description":(description or "").strip(), "preferred":preferred(company)}

# ------------------------------- CONNECTORS ------------------------------- #

def connector_mycareersfuture():
    out=[]; hdr={"User-Agent":UA,"Content-Type":"application/json","Accept":"application/json"}
    for q in MCF_QUERIES:
        try:
            r=requests.post("https://api.mycareersfuture.gov.sg/v2/search",
                            params={"limit":100,"page":0},
                            json={"search":q,"sortBy":["new_posting_date"]},
                            headers=hdr,timeout=TIMEOUT)
            if r.status_code!=200:
                print(f"  ! MCF '{q}': HTTP {r.status_code}",file=sys.stderr); continue
            for j in r.json().get("results",[]):
                title=j.get("title","")
                comp=((j.get("postedCompany") or {}) or (j.get("hiringCompany") or {})).get("name","")
                uuid=j.get("uuid","")
                posted=(j.get("metadata") or {}).get("newPostingDate") or j.get("newPostingDate","")
                sal=j.get("salary") or {}
                lo, hi = sal.get("minimum"), sal.get("maximum")
                stype=((sal.get("type") or {}).get("salaryType") or "").lower()
                per="yr" if "annual" in stype else "mo"
                salary=f"S${lo:,}–{hi:,}/{per}" if isinstance(lo,int) and isinstance(hi,int) else ""
                salary_max=(hi//12 if per=="yr" else hi) if isinstance(hi,int) else None
                url=f"https://www.mycareersfuture.gov.sg/job/{uuid}" if uuid else ""
                links={"primary":url,"careers":"","linkedin":"","mcf":url}
                out.append(job(f"mcf-{uuid}",title,comp,"MyCareersFuture",links,
                               posted,clean_text(j.get("description","")),salary,salary_max))
            time.sleep(0.3)
        except requests.RequestException as e:
            print(f"  ! MCF '{q}': {e}",file=sys.stderr)
    return out

def connector_smartrecruiters():
    out=[]; base="https://api.smartrecruiters.com/v1/companies/{c}/postings"
    hdr={"User-Agent":UA,"Accept":"application/json"}
    for label,slug in SR_COMPANIES.items():
        offset=0
        while True:
            try:
                r=requests.get(base.format(c=slug),params={"country":"sg","limit":100,"offset":offset},
                               headers=hdr,timeout=TIMEOUT)
            except requests.RequestException as e:
                print(f"  ! SR {label}: {e}",file=sys.stderr); break
            if r.status_code!=200:
                print(f"  ! SR {label}: HTTP {r.status_code}",file=sys.stderr); break
            data=r.json()
            for p in data.get("content",[]):
                pid=p.get("id","")
                desc=sr_detail(slug,pid,hdr) if SR_FETCH_DETAIL else ""
                if SR_FETCH_DETAIL: time.sleep(0.25)
                apply_url=(((p.get("actions") or {}).get("apply") or {}).get("url") or
                           f"https://jobs.smartrecruiters.com/{slug}/{pid}")
                links={"primary":apply_url,"careers":apply_url,"linkedin":"","mcf":""}
                out.append(job(pid,p.get("name",""),label,"SmartRecruiters",links,
                               (p.get("releasedDate") or "")[:10],desc))
            offset+=100
            if offset>=data.get("totalFound",0): break
            time.sleep(0.3)
    return out

def sr_detail(slug,pid,hdr):
    if not pid: return ""
    try:
        r=requests.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{pid}",
                       headers=hdr,timeout=TIMEOUT)
        if r.status_code!=200: return ""
        sec=(((r.json().get("jobAd") or {}).get("sections")) or {})
        parts=[clean_text((sec.get(k) or {}).get("text","")) for k in
               ("companyDescription","jobDescription","qualifications","additionalInformation")]
        return "\n\n".join(p for p in parts if p)
    except requests.RequestException:
        return ""

def connector_google_jobs():
    key=os.getenv("SERPAPI_KEY")
    if not key: print("  · Google Jobs skipped (no SERPAPI_KEY)"); return []
    out=[]
    queries=gj_queries_for_this_run()
    print(f"  · Google Jobs queries this run: {queries}")
    for q in queries:
        token,page=None,0
        while page<GJ_PAGES:
            params={"engine":"google_jobs","q":q,"location":"Singapore","hl":"en","gl":"sg","api_key":key}
            if token: params["next_page_token"]=token
            try:
                r=requests.get("https://serpapi.com/search.json",params=params,timeout=TIMEOUT)
            except requests.RequestException as e:
                print(f"  ! GJ '{q}': {e}",file=sys.stderr); break
            if r.status_code!=200:
                print(f"  ! GJ '{q}': HTTP {r.status_code}",file=sys.stderr); break
            data=r.json()
            for j in data.get("jobs_results",[]):
                ext=j.get("detected_extensions") or {}
                links=extract_links(j.get("apply_options") or [], j.get("share_link",""))
                out.append(job(j.get("job_id","")[:120],j.get("title"),j.get("company_name"),
                               "Google Jobs",links,ext.get("posted_at",""),
                               clean_text(j.get("description",""))))
            token=(data.get("serpapi_pagination") or {}).get("next_page_token")
            page+=1
            if not token: break
            time.sleep(1.0)
    return out

def connector_workday():
    out=[]
    for label,cfg in WORKDAY_TENANTS.items():
        host=cfg["host"]; site=cfg["site"]; tenant=host.split(".")[0]
        api=f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
        hdr={"User-Agent":BROWSER_UA,"Content-Type":"application/json","Accept":"application/json",
             "Origin":f"https://{host}","Referer":f"https://{host}/{site}/"}
        seen_paths=set(); fetched=0; kept=0; errored=False
        try:
            for q in KEYWORDS:
                try:
                    r=requests.post(api,json={"appliedFacets":{},"limit":20,"offset":0,"searchText":q},
                                    headers=hdr,timeout=TIMEOUT)
                except requests.RequestException as e:
                    print(f"  ! WD {label} '{q}': {e}",file=sys.stderr); errored=True; continue
                if r.status_code!=200:
                    print(f"  ! WD {label} '{q}': HTTP {r.status_code}",file=sys.stderr); errored=True; continue
                try:
                    postings=r.json().get("jobPostings",[])
                except ValueError:
                    print(f"  ! WD {label} '{q}': non-JSON response",file=sys.stderr); errored=True; continue
                for p in postings:
                    path=p.get("externalPath","")
                    if not path or path in seen_paths:
                        continue
                    seen_paths.add(path); fetched+=1
                    lt=(p.get("locationsText") or "").lower()
                    desc=""
                    if "singapore" not in lt:
                        if not re.search(r"\d+\s+location", lt):   # clearly another city
                            continue
                        desc,locs=wd_detail(host,tenant,site,path,hdr)  # ambiguous: confirm city
                        time.sleep(0.2)
                        if "singapore" not in locs.lower():
                            continue
                    url=f"https://{host}/{site}{path}"
                    links={"primary":url,"careers":url,"linkedin":"","mcf":""}
                    out.append(job(f"wd-{path}",p.get("title"),label,"Workday",links,p.get("postedOn",""),desc))
                    kept+=1
                time.sleep(0.3)
        except Exception as e:
            print(f"  ! WD {label} crashed: {e}",file=sys.stderr); errored=True
        record(f"Workday · {label}", fetched, kept, error=errored)
    return out

def wd_detail(host,tenant,site,path,hdr):
    try:
        r=requests.get(f"https://{host}/wday/cxs/{tenant}/{site}{path}",headers=hdr,timeout=TIMEOUT)
        if r.status_code!=200: return "",""
        info=r.json().get("jobPostingInfo") or {}
        locs=" ".join([info.get("location","") or ""]
                      +[str(x) for x in (info.get("additionalLocations") or [])]
                      +[info.get("country","") or ""])
        return clean_text(info.get("jobDescription","")), locs
    except Exception:
        return "",""

def connector_eightfold():
    out=[]; hdr={"User-Agent":UA,"Accept":"application/json"}
    for label,cfg in EIGHTFOLD_TENANTS.items():
        try:
            r=requests.get(f"https://{cfg['host']}/api/apply/v2/jobs",
                params={"domain":cfg["domain"],"location":"Singapore","start":0,"num":100},
                headers=hdr,timeout=TIMEOUT)
        except requests.RequestException as e:
            print(f"  ! Eightfold {label}: {e}",file=sys.stderr); continue
        if r.status_code!=200:
            print(f"  ! Eightfold {label}: HTTP {r.status_code} (may need endpoint tweak)",file=sys.stderr); continue
        data=r.json()
        for p in (data.get("positions") or data.get("data") or []):
            loc=p.get("location") or (", ".join(p.get("locations",[])) if isinstance(p.get("locations"),list) else "")
            if "singapore" not in (loc or "").lower():
                continue
            pid=p.get("id") or p.get("position_id") or p.get("displayJobId","")
            url=p.get("canonicalPositionUrl") or f"https://{cfg['host']}/careers?pid={pid}"
            links={"primary":url,"careers":url,"linkedin":"","mcf":""}
            out.append(job(f"ef-{pid}",p.get("name") or p.get("title"),label,"Eightfold",links,
                           "",clean_text(p.get("job_description",""))))
    return out

def connector_successfactors():
    out=[]; hdr={"User-Agent":UA}
    # job title links look like  <a href="/job/<slug>/<id>/">Title</a>  — slug starts with the city
    job_re=re.compile(r'href="(/job/([^"/]+?)/(\d+)/)"[^>]*>\s*([^<]{3,160}?)\s*</a>', re.I)
    for label,host in SF_SITES.items():
        seen=set()
        for page in range(SF_MAX_PAGES):
            try:
                r=requests.get(f"https://{host}/search/",
                               params={"q":"","sortColumn":"referencedate","sortDirection":"desc",
                                       "startrow":page*50},
                               headers=hdr,timeout=TIMEOUT)
            except requests.RequestException as e:
                print(f"  ! SF {label}: {e}",file=sys.stderr); break
            if r.status_code!=200:
                print(f"  ! SF {label}: HTTP {r.status_code}",file=sys.stderr); break
            matches=job_re.findall(r.text)
            if not matches:
                break                                   # past the last page
            for href,slug,jid,title in matches:
                if jid in seen:
                    continue
                seen.add(jid)
                if not slug.lower().startswith("singapore"):
                    continue
                url=f"https://{host}{href}"
                links={"primary":url,"careers":url,"linkedin":"","mcf":""}
                out.append(job(f"sf-{jid}",clean_text(title),label,"SuccessFactors",links,"",""))
            time.sleep(0.4)
    return out

CONNECTORS=[connector_mycareersfuture, connector_smartrecruiters, connector_successfactors,
            connector_workday, connector_eightfold, connector_google_jobs]

# ------------------------------- MAIN ------------------------------- #

def load_previous():
    if OUT.exists():
        try: return {j["id"]:j for j in json.loads(OUT.read_text()).get("jobs",[])}
        except (json.JSONDecodeError,KeyError): return {}
    return {}

def run(dry_run=False):
    prev=load_previous(); now=datetime.now(timezone.utc).isoformat()
    STATS.clear()
    collected={}
    for conn in CONNECTORS:
        print(f"→ {conn.__name__}")
        try:
            got=conn()
            kept=[j for j in got if relevant(j["title"])]
            for j in kept:
                j["first_seen"]=prev.get(j["id"],{}).get("first_seen",now)
                collected[j["id"]]=j
            print(f"   {len(got)} fetched · {len(kept)} relevant")
            if conn.__name__!="connector_workday":     # workday records per-site itself
                record(conn.__name__.replace("connector_",""), len(got), len(kept))
        except Exception as e:
            print(f"  ! {conn.__name__} crashed: {e}",file=sys.stderr)
            record(conn.__name__.replace("connector_",""), 0, 0, error=True)
    # Carry forward roles seen in the last CARRY_DAYS that weren't re-fetched this run, so the
    # Google Jobs query rotation (and any transient fetch failure) doesn't make roles flicker away.
    CARRY_DAYS=12
    cutoff=(datetime.now(timezone.utc)-timedelta(days=CARRY_DAYS)).isoformat()
    carried=0
    for jid,pj in prev.items():
        if jid in collected: continue
        if (pj.get("first_seen") or "") >= cutoff and relevant(pj.get("title","")):
            collected[jid]=pj; carried+=1
    if carried: print(f"   carried forward {carried} recent roles not re-fetched this run")
    # newest first by posting date, then preferred houses, then company
    jobs=dedupe(list(collected.values()))
    before=len(jobs)
    jobs=[j for j in jobs if not j.get("salary_max") or j["salary_max"]>=MIN_SALARY_MAX]
    dropped=before-len(jobs)
    jobs.sort(key=lambda j:(j.get("posted_date") or "0000-00-00", j["type"]=="house"), reverse=True)
    print(f"\n{len(jobs)} relevant SG roles after dedupe "
          f"({sum(1 for j in jobs if j['type']=='house')} houses, "
          f"{sum(1 for j in jobs if j['sector']=='beauty_luxury')} beauty/luxury; "
          f"dropped {dropped} below S${MIN_SALARY_MAX:,}/mo)")
    if dry_run:
        for j in jobs[:30]:
            print(f"  [{j['type'][:5]:5}|{j['sector'][:6]:6}] {j['posted_date']}  {j['title']} — {j['company']}")
        return
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps({"generated_at":now,"sources":STATS,"jobs":jobs},indent=2,ensure_ascii=False))
    print(f"Wrote {OUT} ({len(jobs)} roles).")

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--dry-run",action="store_true")
    run(dry_run=ap.parse_args().dry_run)

if __name__=="__main__":
    main()
