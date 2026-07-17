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
import argparse, html, json, os, re, sys, time, unicodedata
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
    # Chanel (tenant "cc", site "ChanelCareers"). If their Workday turns out to be bot-walled like
    # Kering, this row will show red in the panel and we fall back to Google Jobs ("Chanel Singapore").
    "Chanel": {"host": "cc.wd3.myworkdayjobs.com", "site": "ChanelCareers"},
    # Luxury hospitality + big drinks house, both on open Workday tenants.
    "Aman": {"host": "aman.wd103.myworkdayjobs.com", "site": "AmanGroupExternal"},
    "Pernod Ricard": {"host": "pernodricard.wd3.myworkdayjobs.com", "site": "pernod-ricard"},
    # Chanel (host tenant "cc", site "ChanelCareers") — diagnostics panel will show if it's open.
    "Chanel": {"host": "cc.wd3.myworkdayjobs.com", "site": "ChanelCareers"},
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

# SuccessFactors career sites (SAP). Two shapes, same engine:
#  · modern Career-Site-Builder on the brand's own domain (jobs.sephora.com, …)
#  · legacy hosted portal on SF's domain, reached with ?company=X (career5.successfactors.eu, …)
# Location is baked into each job's URL slug, so we filter to Singapore on the slug.
SF_SITES = {
    "Sephora":  {"host": "jobs.sephora.com"},
    "Clarins":  {"host": "careers.groupeclarins.com"},
    "Coty":     {"host": "careers.coty.com"},
    # These three were configured as legacy hosted portals (careerN.successfactors.eu
    # with ?company=X) and returned HTTP 404 on every run. They aren't hosted portals
    # at all — each runs Career Site Builder on its own domain, exactly like Sephora,
    # so the plain /search/ path works and no company parameter is needed.
    "Shiseido": {"host": "careers.shiseido.com"},
    "Puig":     {"host": "jobs.puig.com"},
    "Changi":   {"host": "jobs.changiairport.com"},
}
SF_MAX_PAGES = 6           # 50 roles/page; we search "Singapore" so this is plenty

# Greenhouse job boards — clean public JSON API at boards-api.greenhouse.io.
GREENHOUSE_BOARDS = {"Soho House": "sohohouseco"}

# Oracle Recruiting Cloud sites. host + siteNumber (usually CX). Search endpoint is
# /hcmRestApi/resources/latest/recruitingCEJobRequisitions?finder=findReqs;siteNumber=CX,keyword=…
# Marriott's host/siteNumber are confirmed; Hermès/Tiffany use their front-end hosts (to verify).
ORACLE_SITES = {
    "Marriott": {"host": "ejwl.fa.us2.oraclecloud.com",                  "site": "CX"},
    "Hermès":   {"host": "fa-eoic-saasfaprod1.fa.ocs.oraclecloud.com",   "site": "CX_12001"},
    "Tiffany":  {"host": "eljs.fa.us2.oraclecloud.com",                  "site": "CX"},
}

# Drop roles whose UPPER salary band is below this (monthly SGD). Roles with no salary are kept.
MIN_SALARY_MAX = 9000

# Per-source diagnostics, written into jobs.json so the page can show a "Sources" panel.
STATS = []
def record(label, fetched, kept, error=False, detail=""):
    """A red dot with no reason is a dead end: it says something broke but not
    what, so the only way to learn is to read the Actions log. Carry the real
    status or message into jobs.json so the dashboard can show it."""
    STATS.append({"label": label, "fetched": fetched, "kept": kept,
                  "error": bool(error), "detail": str(detail)[:160]})

# Shared keyword list for keyword-driven sources (Workday needs a search term).
KEYWORDS = ["ecommerce", "e-commerce", "product owner", "product manager", "digital project manager",
            "omnichannel", "digital consultant", "ui ux", "business analyst", "performance analyst"]
# (replaced at runtime by apply_config() using the editable list from the Worker)

# Google Jobs (SerpApi) — free quota is 250/month. We rotate a pool so we cover the bot-walled
# houses (Kering) by name without exceeding budget: GJ_PER_RUN x 2 runs/day x 30 = must stay <250.
# House names are queried directly because Kering roles live on Indeed/FashionJobs/Jobstreet,
# which Google indexes even though Kering's own site blocks us. relevant() then filters titles.
GJ_POOL = [
    # Kering houses (own Workday is bot-walled — reached via Google's index of Indeed/FashionJobs)
    "Balenciaga Singapore", "Gucci Singapore", "Saint Laurent Singapore",
    "Bottega Veneta Singapore", "Alexander McQueen Singapore", "Boucheron Singapore",
    # brands on platforms we don't scrape directly (Oracle / Avature / legacy SF / custom)
    "L'Oreal Singapore", "Lancome Singapore", "YSL Beauty Singapore", "Kiehl's Singapore",
    "Ralph Lauren Singapore", "Hermes Singapore", "Tiffany Singapore", "Chanel Singapore",
    # Their own ATS connectors are currently failing (SuccessFactors hosted portals,
    # eightfold), leaving these houses invisible. The pool always runs GJ_PER_RUN
    # queries per scan, so a longer pool costs no extra quota — only a slower
    # rotation, which the 12-day carry-forward absorbs.
    "Shiseido Singapore", "Puig Singapore", "Estee Lauder Singapore",
    # luxury hospitality / travel on Oracle / Workable / iCIMS / custom sites
    "Marriott luxury Singapore", "Four Seasons Singapore", "Belmond Singapore",
    "Mandarin Oriental Singapore", "Banyan Tree Singapore",
    # broad nets
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
    "bulgari","bvlgari",
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
    "beauty","beaute","beauté","cosmetic","skincare","skin care","haircare","hair care","fragrance",
    "perfume","perfumery","parfum","makeup","make-up","luxury","luxe","couture","maison","jewellery",
    "jewelry","watchmaking","watches","prestige","atelier","leather goods","ready-to-wear",
    "pret-a-porter","prêt-à-porter","high-end","luxasia","fashion house","luxury retail","luxury goods",
]

# Wider list of beauty & luxury BRANDS/companies (beyond the marquee PREFERRED_BRANDS used for "Houses").
# A job at any of these is tagged beauty_luxury even when it carries no description to scan.
SECTOR_BRANDS = [b.lower() for b in [
    # luxury fashion / leather / watches / jewellery
    "coach","tapestry","kate spade","michael kors","capri","versace","jimmy choo","prada","miu miu",
    "burberry","ferragamo","tod's","tods","valentino","moncler","bally","loro piana","brunello cucinelli",
    "celine","loewe","fendi","louis vuitton","bulgari","bvlgari","tiffany","chaumet","van cleef",
    "jaeger","iwc","piaget","vacheron","panerai","montblanc","chloe","chloé","alaia","alaïa","rimowa",
    "berluti","givenchy","kenzo","marc jacobs","off-white","ralph lauren","longchamp","delvaux","goyard",
    "dunhill","zegna","thom browne","rolex","patek","audemars","richard mille","omega","tag heuer",
    "breitling","hublot","chopard","bucherer","hour glass","cortina watch","swatch","harry winston",
    "graff","de beers","boucheron","pomellato","qeelin","buccellati","boucheron","brioni","mytheresa",
    "farfetch","net-a-porter","matchesfashion","ssense","24s","moda operandi","club21","club 21","dfs",
    "valiram","fj benjamin","bluebell","on pedder","pedder","rsh",
    # beauty / cosmetics / fragrance
    "coty","wella","kenvue","beiersdorf","nivea","la prairie","aesop","fresh","glossier","charlotte tilbury",
    "rituals","l'occitane","loccitane","occitane","molton brown","jo malone","byredo","diptyque","creed",
    "kilian","penhaligon","sk-ii","sk ii","olay","kiehl","urban decay","nyx","maybelline","garnier",
    "cerave","la roche","vichy","kerastase","kérastase","redken","aveda","bumble and bumble","mac cosmetics",
    "bobbi brown","clinique","la mer","glamglow","too faced","benefit cosmetics","hourglass","laura mercier",
    "nars","shu uemura","armani beauty","prada beauty","ysl beaut","dior beaut","guerlain","make up for ever",
    "fenty","rare beauty","drunk elephant","tatcha","the ordinary","deciem","paula's choice","sulwhasoo",
    "laneige","innisfree","cosrx","clé de peau","cle de peau","ipsa","kanebo","kose","decorté","decorte",
    "escentials","escential","sasa","sa sa","luxola","tangs beauty","aveda","natura","avon","the body shop",
    "pernod","pernod ricard","moet","moët","hennessy","dom perignon","dom pérignon","krug","veuve clicquot",
]]

# Luxury hospitality, travel retail & premium travel — a separate "Travel & Hospitality" vertical.
HOSPITALITY_BRANDS = [b.lower() for b in [
    # luxury hotels & resorts
    "aman","belmond","four seasons","banyan tree","angsana","dhawa","mandarin oriental","raffles","fairmont",
    "sofitel","accor","pullman","shangri-la","shangri la","rosewood","six senses","como hotel","como hotels",
    "capella","the peninsula","peninsula hotel","anantara","hyatt","park hyatt","andaz","hilton","conrad",
    "waldorf","regent","dusit","millennium hotel","pan pacific","parkroyal","marina bay sands","resorts world",
    "kempinski","oberoi","taj hotel","jumeirah","bulgari hotel","cheval blanc","one&only","oneandonly",
    "montage","auberge","langham","corinthia","dorchester","ritz-carlton","ritz carlton","st regis","st. regis",
    "w hotels","edition hotel","westin","sheraton","le meridien","le méridien","soho house","standard hotel",
    # travel retail & duty free
    "dfs","lagardère travel","lagardere travel","dufry","avolta","lotte duty free","the shilla","king power",
    "heinemann","duty free",
    # airports / airlines / travel platforms
    "changi","jewel changi","sats","singapore airlines","scoot","cathay","emirates","qatar airways",
    "klook","booking.com","booking holdings","expedia","agoda","trip.com","traveloka","airbnb",
    # cruise & rail
    "silversea","seabourn","ponant","crystal cruises","regent seven seas","oceania cruises","cunard",
    "orient express","aman resorts",
]]
HOSP_SIGNALS = ["hotel","resort","hospitality","travel retail","duty free","duty-free","airline","aviation",
                "cruise","airport","concierge","members club","members' club","private club","spa resort"]

# For splitting the general pile into readable buckets.
TECH_SIGNALS = ["software","saas","b2b saas","fintech","technology company","platform","developer","engineer",
                "data science","machine learning","artificial intelligence","cloud","cybersecurity","blockchain",
                "crypto","startup","digital agency","gaming","semiconductor","e-wallet","super app",
                "grab","shopee","sea limited","sea ltd","lazada","gojek","goto","bytedance","tiktok","tik tok",
                "stripe","google","meta","amazon","microsoft","salesforce","adobe","atlassian","canva","razer",
                "ninja van","ninjavan","carousell","propertyguru","nium","thunes","wise","revolut","binance",
                "coinbase","circles.life","coda payments","advance intelligence","openai","anthropic"]
CONSULT_SIGNALS = ["consulting","consultancy","advisory","management consult","accenture","deloitte","mckinsey",
                   "bain","boston consulting","bcg","kpmg","pwc","pricewaterhouse","ernst & young","capgemini",
                   "cognizant","infosys","wipro","tata consultancy","oliver wyman","roland berger","kearney",
                   "l.e.k","strategy&","slalom","thoughtworks","publicis sapient"]

# ── Editable search terms ────────────────────────────────────────────────
# These are plain words, not regex, so they can be edited from the dashboard's
# ⚙ Settings by someone who doesn't write code. Matching rules:
#   "intern"  → whole word only  (won't match "International")
#   "audit*"  → prefix match     (matches "auditor", "auditing")
# The live lists come from the Cloudflare Worker (so edits sync across devices
# and reach this scraper). If the Worker is unreachable we fall back to these
# defaults — a network blip must never silently empty the dashboard.
DEFAULT_SEARCH = ["ecommerce", "e-commerce", "product owner", "product manager",
                  "digital project manager", "omnichannel", "digital consultant",
                  "ui ux", "business analyst", "performance analyst"]
DEFAULT_INCLUDE = ["product owner", "digital project manager", "ecommerce", "e-commerce",
                   "ecomm", "e-comm", "ebusiness", "e-business", "omnichannel", "omni-channel",
                   "digital consultant", "product manager", "product management",
                   "ui ux", "ui/ux", "uiux", "ux ui", "ux/ui", "business analyst",
                   "performance analyst"]
DEFAULT_EXCLUDE = ["controller", "controlling", "treasury", "finance", "accountant", "tax",
                   "audit*", "payroll", "clienteling", "intern", "internship", "apprentice",
                   "working student", "fresh graduate", "beauty advisor", "client advisor",
                   "sales associate", "retail associate", "store manager", "boutique manager",
                   "counter", "cashier", "supply chain", "logistics", "warehouse",
                   "customer service", "service assistant", "call cent*", "receptionist",
                   "data entry", "telesales"]

def fold(x):
    """Lower-case and strip accents. "estee" must find Estee Lauder and Estée
    Lauder alike — nobody should have to hunt for an é to make a search work.
    Applied to the terms AND the text, exactly like case."""
    s = str(x or "").lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.replace("\u2019", "'").replace("\u2018", "'")

def term_to_pattern(t):
    """A plain word becomes a whole-word match; a trailing * becomes a prefix match."""
    t = fold(t).strip()
    if not t: return None
    if t.endswith("*"):
        core = re.escape(t[:-1])
        return rf"\b{core}\w*" if core else None
    return rf"\b{re.escape(t)}\b"

class TermSet:
    """Any term may match. A term joined with + must have ALL its words present,
    in any order: "luxury + product" finds "Head of Product (Luxury)"."""
    def __init__(self, terms):
        self.specs = []
        for t in terms or []:
            parts = [p.strip() for p in fold(t).split("+") if p.strip()]
            pats = [term_to_pattern(p) for p in parts]
            pats = [re.compile(p, re.I) for p in pats if p]
            if pats: self.specs.append(pats)
    def __bool__(self): return bool(self.specs)
    def search(self, text):
        f = fold(text)
        return any(all(r.search(f) for r in spec) for spec in self.specs)

def compile_terms(terms, fallback):
    ts = TermSet(terms)
    if not ts:                                    # never let an empty list match nothing
        ts = TermSet(fallback)
    return ts

def load_config():
    """Read the editable term lists from the Worker. Falls back to defaults."""
    url, tok = os.getenv("SYNC_URL", "").rstrip("/"), os.getenv("SYNC_TOKEN", "")
    cfg = {}
    if url and tok:
        try:
            r = requests.get(f"{url}/config", headers={"Authorization": f"Bearer {tok}"}, timeout=15)
            if r.status_code == 200:
                cfg = r.json() or {}
                print(f"  · config loaded from Worker (updated {cfg.get('updated','?')})")
            else:
                print(f"  ! config HTTP {r.status_code} — using defaults", file=sys.stderr)
        except Exception as e:
            print(f"  ! config unreachable ({e}) — using defaults", file=sys.stderr)
    else:
        print("  · no SYNC_URL/SYNC_TOKEN — using default search terms")
    return cfg

CONFIG = {}
INCLUDE = compile_terms(DEFAULT_INCLUDE, DEFAULT_INCLUDE)
EXCLUDE = compile_terms(DEFAULT_EXCLUDE, DEFAULT_EXCLUDE)

def apply_config(cfg):
    """Point the module-level knobs at whatever the profiles asked for.

    ONE scan serves every profile. That matters: SerpApi's free tier is 250
    queries a month and we already budget 240, so a scan per profile would blow
    it. Instead we fetch the UNION of what everyone wants and let each profile
    filter its own view in the dashboard:
      • search / include → union      (fetch anything any profile might want)
      • exclude          → intersection (only drop what EVERY profile rejects)
      • salary floor     → the lowest  (the least restrictive wins)
    """
    global CONFIG, INCLUDE, EXCLUDE, KEYWORDS, MIN_SALARY_MAX, USER_CATS
    CONFIG = cfg or {}
    USER_CATS = CONFIG.get("categories") or []
    profiles = CONFIG.get("profiles") or []
    if not isinstance(profiles, list) or not profiles:
        profiles = [{"name": "default", "search": DEFAULT_SEARCH, "include": DEFAULT_INCLUDE,
                     "exclude": DEFAULT_EXCLUDE, "min_salary_max": 9000}]

    def union(field, fallback):
        out = []
        for p in profiles:
            for t in (p.get(field) or []):
                t = (t or "").strip().lower()
                if t and t not in out: out.append(t)
        return out or list(fallback)

    excl_sets = [set((p.get("exclude") or [])) for p in profiles]
    excl_sets = [e for e in excl_sets if e]
    common_excl = sorted(set.intersection(*excl_sets)) if excl_sets else list(DEFAULT_EXCLUDE)

    KEYWORDS = union("search", DEFAULT_SEARCH)
    INCLUDE  = compile_terms(union("include", DEFAULT_INCLUDE), DEFAULT_INCLUDE)
    EXCLUDE  = compile_terms(common_excl, DEFAULT_EXCLUDE)
    floors = [int(p.get("min_salary_max") or 0) for p in profiles]
    MIN_SALARY_MAX = min(floors) if floors else 9000
    print(f"  · {len(profiles)} profile(s): {len(KEYWORDS)} search terms, "
          f"{len(common_excl)} shared exclusions, floor S${MIN_SALARY_MAX:,}")

GOOD_LINK = ("smartrecruiters","myworkdayjobs","eightfold","avature","successfactors","taleo","icims",
             "workday","lever.co","greenhouse","careers.","jobs.","/careers")
AGGREGATORS = ("trabajo","jobrapido","neuvoo","talent.com","learn4good","jooble","adzuna","whatjobs",
               "jobsora","recruit.net","mncjobz","kitjob","simplyhired","jobcloud")

# ------------------------------- HELPERS ------------------------------- #

def preferred(company):
    """Is this one of the houses? Honours the edited list, not just the default,
    so removing a brand in Settings really removes it everywhere."""
    c=fold(company)
    lst=CONFIG.get("house_brands")
    if not (isinstance(lst,list) and lst): lst=PREFERRED_BRANDS
    return any(fold(b) in c for b in lst)

def is_agency(company):
    c=(company or "").lower(); return any(a in c for a in AGENCIES)

def type_of(company):
    """Which brands are houses, and which are agencies — editable from Settings.
    The dashboard is seeded with these exact lists (echoed into jobs.json), so
    what the user edits is the real rule, not a copy of it."""
    c=fold(company)
    agy = CONFIG.get("agency_terms") or []
    hse = CONFIG.get("house_brands") or []
    if agy:
        if any(fold(a) in c for a in agy): return "agency"
    elif is_agency(company): return "agency"
    if hse:
        if any(fold(b) in c for b in hse): return "house"
    elif preferred(company): return "house"
    return "other"

def _cfg_list(key, fallback):
    v = CONFIG.get(key)
    src = v if isinstance(v, list) and v else fallback
    return [fold(x) for x in src]

def sector_of(company, title, desc):
    """Which sector a role belongs to. Two different tests, deliberately:
      • BRANDS match the company name only — 'Chanel' as an employer.
      • SIGNALS match the whole posting — a tech role that merely mentions Chanel
        as a client shouldn't become a beauty role, which is why they're separate.
    Both lists are editable from Settings; these are just the defaults."""
    c=fold(company)
    blob=fold(f"{company} {title} {desc}")
    tb=_cfg_list("travel_brands", HOSPITALITY_BRANDS); ts=_cfg_list("travel_signals", HOSP_SIGNALS)
    lb=_cfg_list("luxury_brands", SECTOR_BRANDS);      ls=_cfg_list("luxury_signals", LUX_SIGNALS)
    if any(b in c for b in tb) or any(w in blob for w in ts):
        return "hospitality"
    if preferred(company) or any(b in c for b in lb): return "beauty_luxury"
    return "beauty_luxury" if any(w in blob for w in ls) else "general"

FINANCE_SIGNALS = ["bank","banking","insurance","insurer","assurance","reinsur","asset management",
                   "wealth management","private bank","capital markets","securities","brokerage",
                   "dbs","ocbc","uob","standard chartered","stanchart","citibank","citigroup","hsbc",
                   "jpmorgan","j.p. morgan","goldman sachs","morgan stanley","bnp paribas","credit suisse",
                   "deutsche bank","barclays","maybank","cimb","aia","prudential","great eastern",
                   "manulife","ntuc income","income insurance","allianz","axa","chubb","zurich",
                   "swiss re","munich re","marsh","aon","willis towers","tokio marine","msig","etiqa"]

USER_CATS = []
def category_of(company, title, sector, type_, desc=""):
    """User-defined categories win when configured; house/agency stay ours."""
    if type_=="house":  return "house"
    if type_=="agency": return "recruiter"
    if USER_CATS:
        # Two scopes, and they must match the dashboard exactly or a role lands in
        # one category on the server and another in the browser:
        #   terms   → the EMPLOYER'S NAME only
        #   signals → the whole posting: employer, title and description
        who  = company or ""
        blob = f"{company} {title} {desc}"
        for c in USER_CATS:
            if c.get("locked"): continue
            # Houses and Recruiters are settled by type_of() on the employer's
            # name above. Letting them also match on description would turn any
            # company that says "luxury maison" into a house.
            if c.get("id") in ("house", "agency", "recruiter"): continue
            if TermSet(c.get("terms")   or []).search(who):  return c["id"]
            if TermSet(c.get("signals") or []).search(blob): return c["id"]
        if sector=="hospitality":   return "travel"
        if sector=="beauty_luxury": return "beauty_luxury"
        return "other"
    return _category_builtin(company, title, sector, type_, desc)

def _category_builtin(company, title, sector, type_, desc=""):
    """Primary bucket for the UI tabs — each role lands in exactly one.

    Company identity is checked BEFORE content signals: a 'TikTok Shop — Beauty'
    role is a tech-company role that happens to mention beauty, not a beauty
    house. Previously the beauty/luxury sector test ran first and swallowed it,
    so the tech test below was never reached."""
    if type_=="house":            return "house"
    who=(company or "").lower()
    if any(w in who for w in TECH_SIGNALS):    return "tech"
    if any(w in who for w in FINANCE_SIGNALS): return "finance"
    if sector=="hospitality":     return "travel"
    if sector=="beauty_luxury":   return "beauty_luxury"
    if type_=="agency":           return "recruiter"
    blob=f"{company} {title}".lower()
    if any(w in blob for w in TECH_SIGNALS):    return "tech"
    if any(w in blob for w in FINANCE_SIGNALS): return "finance"
    if any(w in blob for w in CONSULT_SIGNALS): return "consulting"
    return "other"

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
    ty=type_of(company); se=sector_of(company,title,description)
    return {"id":str(id_), "title":(title or "").strip(), "company":company, "source":source,
            "type":ty, "sector":se, "category":category_of(company,title,se,ty,description),
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

QUOTA={}
def serpapi_quota(key):
    """Ask SerpApi how many searches are left this month, so the dashboard can warn."""
    try:
        r=requests.get("https://serpapi.com/account.json",params={"api_key":key},timeout=15)
        if r.status_code!=200: return
        d=r.json()
        left=d.get("total_searches_left"); used=d.get("this_month_usage")
        limit=d.get("searches_per_month")
        QUOTA["serpapi"]={"left":left,"used":used,"limit":limit}
        print(f"  · SerpApi quota: {used}/{limit} used, {left} left")
    except Exception as e:
        print(f"  ! SerpApi quota check failed: {e}",file=sys.stderr)

def connector_google_jobs():
    key=os.getenv("SERPAPI_KEY")
    if not key: print("  · Google Jobs skipped (no SERPAPI_KEY)"); return []
    serpapi_quota(key)
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
    """Records per tenant. It used to `continue` past every failure without a
    record(), so a broken tenant showed as a mute grey "0 fetched" that could
    never say why."""
    out=[]; hdr={"User-Agent":UA,"Accept":"application/json"}
    for label,cfg in EIGHTFOLD_TENANTS.items():
        fetched=0; kept=0; why=""
        try:
            r=requests.get(f"https://{cfg['host']}/api/apply/v2/jobs",
                params={"domain":cfg["domain"],"location":"Singapore","start":0,"num":100},
                headers=hdr,timeout=TIMEOUT)
        except requests.RequestException as e:
            why=type(e).__name__
            print(f"  ! Eightfold {label}: {e}",file=sys.stderr)
            record(f"Eightfold · {label}",0,0,error=True,detail=why); continue
        if r.status_code!=200:
            why=f"HTTP {r.status_code}"
            print(f"  ! Eightfold {label}: HTTP {r.status_code}",file=sys.stderr)
            record(f"Eightfold · {label}",0,0,error=True,detail=why); continue
        try:
            data=r.json()
        except Exception:
            record(f"Eightfold · {label}",0,0,error=True,detail="200 but not JSON"); continue
        positions=(data.get("positions") or data.get("data") or [])
        fetched=len(positions)
        for p in positions:
            loc=p.get("location") or (", ".join(p.get("locations",[])) if isinstance(p.get("locations"),list) else "")
            if "singapore" not in (loc or "").lower():
                continue
            pid=p.get("id") or p.get("position_id") or p.get("displayJobId","")
            url=p.get("canonicalPositionUrl") or f"https://{cfg['host']}/careers?pid={pid}"
            links={"primary":url,"careers":url,"linkedin":"","mcf":""}
            out.append(job(f"ef-{pid}",p.get("name") or p.get("title"),label,"Eightfold",links,
                           "",clean_text(p.get("job_description",""))))
            kept+=1
        record(f"Eightfold · {label}",fetched,kept,
               error=False, detail="" if fetched else "200 but no positions in the response")
    return out

def connector_successfactors():
    out=[]; hdr={"User-Agent":BROWSER_UA}
    # Job links look like  href="[optional /region]/job/<slug>/<id>/"  — slug carries the city.
    # The optional prefix matters: Sephora uses /Singapore/job/…, so anchoring at /job/ misses it.
    job_re=re.compile(r'href="([^"]*?/job/([^"/]+?)/(\d+)/)"[^>]*>\s*([^<]{3,160}?)\s*</a>', re.I)
    for label,cfg in SF_SITES.items():
        host=cfg["host"]; company=cfg.get("company")
        seen=set(); fetched=0; kept=0; errored=False; why=""
        try:
            for page in range(SF_MAX_PAGES):
                params={"q":"Singapore","sortColumn":"referencedate","sortDirection":"desc","startrow":page*50}
                if company: params["company"]=company
                try:
                    r=requests.get(f"https://{host}/search/",params=params,headers=hdr,timeout=TIMEOUT)
                except requests.RequestException as e:
                    why=type(e).__name__
                    print(f"  ! SF {label}: {e}",file=sys.stderr); errored=True; break
                if r.status_code!=200:
                    why=f"HTTP {r.status_code}"
                    print(f"  ! SF {label}: HTTP {r.status_code}",file=sys.stderr); errored=True; break
                matches=job_re.findall(r.text)
                if not matches:
                    if page==0 and not fetched:
                        # 200 but nothing job-shaped. Almost always the wrong URL
                        # shape: the hosted portals (career5.successfactors.eu with
                        # ?company=X) are the legacy Jobs2Web portal at
                        # /career?company=X, not the Career Site Builder /search/
                        # that the brand-domain sites use.
                        why="200 but no job links — wrong URL shape?"
                        print(f"  ! SF {label}: {why}",file=sys.stderr); errored=True
                    break                                       # past the last page
                new=False
                for href,slug,jid,title in matches:
                    if jid in seen:
                        continue
                    seen.add(jid); fetched+=1; new=True
                    if "singapore" not in slug.lower():
                        continue
                    url=f"https://{host}{href}"
                    if company: url += ("&" if "?" in url else "?")+f"company={company}"
                    links={"primary":url,"careers":url,"linkedin":"","mcf":""}
                    out.append(job(f"sf-{jid}",clean_text(title),label,"SuccessFactors",links,"",""))
                    kept+=1
                time.sleep(0.3)
                if not new:
                    break
        except Exception as e:
            why=f"crashed: {type(e).__name__}"
            print(f"  ! SF {label} crashed: {e}",file=sys.stderr); errored=True
        record(f"SuccessFactors · {label}", fetched, kept, error=errored, detail=why)
    return out

def connector_greenhouse():
    out=[]; hdr={"User-Agent":BROWSER_UA,"Accept":"application/json"}
    for label,token in GREENHOUSE_BOARDS.items():
        fetched=0; kept=0; errored=False
        try:
            r=requests.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
                           params={"content":"true"}, headers=hdr, timeout=TIMEOUT)
            if r.status_code!=200:
                print(f"  ! GH {label}: HTTP {r.status_code}",file=sys.stderr); errored=True
            else:
                for j in r.json().get("jobs",[]):
                    fetched+=1
                    loc=((j.get("location") or {}).get("name") or "")
                    if "singapore" not in loc.lower():
                        continue
                    url=j.get("absolute_url","")
                    links={"primary":url,"careers":url,"linkedin":"","mcf":""}
                    posted=(j.get("updated_at") or "")[:10]
                    out.append(job(f"gh-{j.get('id')}",j.get("title",""),label,"Greenhouse",links,
                                   posted,clean_text(j.get("content",""))))
                    kept+=1
        except Exception as e:
            print(f"  ! GH {label}: {e}",file=sys.stderr); errored=True
        record(f"Greenhouse · {label}", fetched, kept, error=errored)
    return out

def connector_oracle():
    out=[]; hdr={"User-Agent":BROWSER_UA,"Accept":"application/json"}
    for label,cfg in ORACLE_SITES.items():
        host=cfg["host"]; site=cfg["site"]
        fetched=0; kept=0; errored=False
        try:
            offset=0; limit=200
            for _ in range(3):                                  # up to 600 newest matches
                finder=f"findReqs;siteNumber={site},facetsList=NONE,limit={limit},offset={offset},keyword=Singapore"
                try:
                    r=requests.get(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
                                   params={"onlyData":"true","expand":"requisitionList.secondaryLocations","finder":finder},
                                   headers=hdr,timeout=TIMEOUT)
                except requests.RequestException as e:
                    print(f"  ! ORC {label}: {e}",file=sys.stderr); errored=True; break
                if r.status_code!=200:
                    print(f"  ! ORC {label}: HTTP {r.status_code}",file=sys.stderr); errored=True; break
                try:
                    data=r.json()
                except ValueError:
                    print(f"  ! ORC {label}: non-JSON",file=sys.stderr); errored=True; break
                reqs=[]
                for it in data.get("items",[]):
                    reqs += it.get("requisitionList",[]) or []
                if not reqs:
                    break
                for req in reqs:
                    fetched+=1
                    loc=str(req.get("PrimaryLocation") or "")
                    secs=" ".join(str(x.get("Name","")) for x in (req.get("secondaryLocations") or []))
                    if "singapore" not in (loc+" "+secs).lower():
                        continue
                    rid=req.get("Id") or req.get("RequisitionId") or ""
                    url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}"
                    links={"primary":url,"careers":url,"linkedin":"","mcf":""}
                    posted=str(req.get("PostedDate") or "")[:10]
                    out.append(job(f"orc-{rid}",req.get("Title",""),label,"Oracle",links,posted,""))
                    kept+=1
                if len(reqs)<limit:
                    break
                offset+=limit; time.sleep(0.3)
        except Exception as e:
            print(f"  ! ORC {label} crashed: {e}",file=sys.stderr); errored=True
        record(f"Oracle · {label}", fetched, kept, error=errored)
    return out

CONNECTORS=[connector_mycareersfuture, connector_smartrecruiters, connector_successfactors,
            connector_workday, connector_greenhouse, connector_oracle, connector_eightfold, connector_google_jobs]

# ------------------------------- MAIN ------------------------------- #

def load_previous():
    if OUT.exists():
        try: return {j["id"]:j for j in json.loads(OUT.read_text()).get("jobs",[])}
        except (json.JSONDecodeError,KeyError): return {}
    return {}

def run(dry_run=False):
    apply_config(load_config())          # editable search terms, before anything is fetched
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
            if conn.__name__ not in ("connector_workday","connector_successfactors","connector_greenhouse",
                                     "connector_oracle","connector_eightfold"):   # these record per-site
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
    write_history(jobs, now)
    for fn, label in ((lambda: fetch_national(now), "national census"), (fetch_market, "official stats")):
        try: fn()
        except Exception as e: print(f"  ! {label} skipped: {e}", file=sys.stderr)
    OUT.write_text(json.dumps({"generated_at":now,"sources":STATS,"quota":QUOTA,
                               "config":{"search":KEYWORDS,"min_salary_max":MIN_SALARY_MAX,
                                         "profiles":[p.get("name") for p in (CONFIG.get("profiles") or [])],
                                         # The live house/agency rules, so Settings can show and edit the real thing.
                                         # Every matching rule, published so Settings can show and edit the real thing.
                                         "house_brands":CONFIG.get("house_brands") or sorted(PREFERRED_BRANDS),
                                         "agency_terms":CONFIG.get("agency_terms") or sorted(AGENCIES),
                                         "luxury_brands":CONFIG.get("luxury_brands") or sorted(SECTOR_BRANDS),
                                         "luxury_signals":CONFIG.get("luxury_signals") or sorted(LUX_SIGNALS),
                                         "travel_brands":CONFIG.get("travel_brands") or sorted(HOSPITALITY_BRANDS),
                                         "travel_signals":CONFIG.get("travel_signals") or sorted(HOSP_SIGNALS)},
                               "jobs":jobs},indent=2,ensure_ascii=False))
    print(f"Wrote {OUT} ({len(jobs)} roles).")

MARKET = Path("data/market.json")
# Singapore publishes official job-vacancy statistics going back to 1998 — the
# only reliable way to see seasonality before our own history is old enough.
# Quarterly, whole-economy, by industry. Fetched once a day, best-effort: if the
# endpoint moves or is down, we keep whatever we already had and carry on.
MOM_DATASETS = {
    "vacancies_by_industry_annual": "d_01966fdb498fa7fa2863635e25539693",
    "vacancy_rate_quarterly":       "d_60ba5027f80aef9a07d747067a948bfc",
}
def mcf_total(body):
    """How many SG jobs match this filter right now? MyCareersFuture is the
    government board (most SG vacancies must be posted there), free and
    unmetered — the closest thing to a national census of live postings."""
    try:
        r = requests.post("https://api.mycareersfuture.gov.sg/v2/search",
                          params={"limit": 1, "page": 0},
                          json=body,
                          headers={"User-Agent": UA, "Content-Type": "application/json",
                                   "Accept": "application/json"}, timeout=TIMEOUT)
        if r.status_code != 200:
            print(f"  ! MCF census HTTP {r.status_code}", file=sys.stderr); return None
        d = r.json()
        for k in ("total", "totalCount", "count"):
            if isinstance(d.get(k), int): return d[k]
        return None
    except Exception as e:
        print(f"  ! MCF census: {e}", file=sys.stderr); return None

def fetch_national(now):
    """One dated row of the WHOLE Singapore market: total live postings, and the
    same split by salary band. Our own jobs.json only ever sees roles matching
    our keywords, so it can never answer 'is Singapore hiring?'. This can."""
    rows = []
    if NATIONAL.exists():
        try:
            rows = json.loads(NATIONAL.read_text())
            if not isinstance(rows, list): rows = []
        except Exception: rows = []
    today = now[:10]
    total = mcf_total({"search": "", "sortBy": ["new_posting_date"]})
    if total is None:
        print("  ! national census skipped (MCF unavailable)", file=sys.stderr); return
    bands = {}
    for label, lo in [("6k+", 6000), ("9k+", 9000), ("12k+", 12000), ("16k+", 16000)]:
        n = mcf_total({"search": "", "salary": {"minimum": lo}, "sortBy": ["new_posting_date"]})
        if n is not None: bands[label] = n
        time.sleep(0.3)
    row = {"date": today, "total": total, "by_salary_floor": bands}
    rows = [r for r in rows if r.get("date") != today] + [row]
    rows.sort(key=lambda r: r.get("date", ""))
    rows = rows[-1460:]
    NATIONAL.parent.mkdir(parents=True, exist_ok=True)
    NATIONAL.write_text(json.dumps(rows, indent=1, ensure_ascii=False))
    print(f"  \u00b7 national census: {total:,} live SG postings, {len(rows)} day(s) recorded")

NATIONAL = Path("data/national.json")

def fetch_market():
    out = {}
    if MARKET.exists():
        try: out = json.loads(MARKET.read_text()) or {}
        except Exception: out = {}
    today = datetime.now(timezone.utc).date().isoformat()
    if out.get("fetched_on") == today:
        print("  · market data already fetched today"); return
    got_any = False
    for name, ds in MOM_DATASETS.items():
        try:
            r = requests.get("https://data.gov.sg/api/action/datastore_search",
                             params={"resource_id": ds, "limit": 5000}, timeout=30)
            if r.status_code != 200:
                print(f"  ! market {name}: HTTP {r.status_code}", file=sys.stderr); continue
            d = r.json()
            recs = (d.get("result") or {}).get("records") or []
            if recs:
                out[name] = recs; got_any = True
                print(f"  · market {name}: {len(recs)} rows")
        except Exception as e:
            print(f"  ! market {name}: {e}", file=sys.stderr)
    if got_any:
        out["fetched_on"] = today
        out["source"] = "Ministry of Manpower / SingStat via data.gov.sg"
        MARKET.parent.mkdir(parents=True, exist_ok=True)
        MARKET.write_text(json.dumps(out, ensure_ascii=False))
    else:
        print("  ! no market data fetched (keeping any previous file)", file=sys.stderr)

# Fixed bins so a chart drawn in 2027 is comparable with one drawn today.
SALARY_BINS = [(0, 6000), (6000, 9000), (9000, 12000), (12000, 16000), (16000, 10**9)]
SALARY_BINS_LABELS = ["<6k", "6\u20139k", "9\u201312k", "12\u201316k", "16k+", "not stated"]
def salary_bin(v):
    if not isinstance(v, (int, float)) or v <= 0: return "not stated"
    for i, (lo, hi) in enumerate(SALARY_BINS):
        if lo <= v < hi: return SALARY_BINS_LABELS[i]
    return "16k+"

HIST = Path("data/history.json")
def write_history(jobs, now):
    """Append one dated row per day so the market can be read over time.

    A time series can only be built forward — jobs.json is overwritten every run,
    so without this the past is unrecoverable. One row per day (last run wins),
    kept for two years; a few KB a year.
    """
    try:
        rows = json.loads(HIST.read_text()) if HIST.exists() else []
        if not isinstance(rows, list): rows = []
    except Exception:
        rows = []
    today = now[:10]
    by_cat = {}
    for j in jobs:
        c = j.get("category") or "other"
        by_cat[c] = by_cat.get(c, 0) + 1
    # Salary bins (monthly SGD, using the published UPPER band). Recorded now so
    # that in a year we can say how pay bands moved — impossible to backfill.
    by_sal = {b: 0 for b in SALARY_BINS_LABELS}
    for j in jobs:
        by_sal[salary_bin(j.get("salary_max"))] += 1
    posted_week = sum(1 for j in jobs if (j.get("posted_date") or "") >= (datetime.now(timezone.utc)-timedelta(days=7)).date().isoformat())
    row = {"date": today, "total": len(jobs), "by_category": by_cat, "by_salary": by_sal,
           "new_this_week": posted_week,
           "first_seen_today": sum(1 for j in jobs if (j.get("first_seen") or "")[:10] == today)}
    rows = [r for r in rows if r.get("date") != today] + [row]
    rows.sort(key=lambda r: r.get("date", ""))
    rows = rows[-730:]
    HIST.parent.mkdir(parents=True, exist_ok=True)
    HIST.write_text(json.dumps(rows, indent=1, ensure_ascii=False))
    print(f"  · history: {len(rows)} day(s) recorded")

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--dry-run",action="store_true")
    run(dry_run=ap.parse_args().dry_run)

if __name__=="__main__":
    main()
