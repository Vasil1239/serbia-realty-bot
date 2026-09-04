#!/usr/bin/env python3
"""
Tested stdlib-only fetchers for Serbian real-estate listing sources
(probe date: 2026-09-01, sandbox IP; re-test from GitHub Actions runner).

Every fetch_<source>(kind, deal) returns list[dict] with keys:
  id, url, title, price, m2, rooms, place, created, photo, source
  kind  = "apartments" | "houses"
  deal  = "sale" | "rent"
  price = int EUR (or None), m2 = float (or None), rooms = str/float (or None)
  created = ISO-8601 string when the source exposes it, else None

Politeness: PAUSE seconds between HTTP requests (module-level throttle).
Run:  python3 sources_snippets.py         -> prints counts per source
      python3 sources_snippets.py -v      -> also prints first item of each
"""
import json
import re
import sys
import threading
import time
import html as htmllib
import urllib.request
import urllib.parse
from html.parser import HTMLParser

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
PAUSE = 1.0
_last_req = {}                 # host -> время последнего запроса
_throttle_lock = threading.Lock()


def _throttle(host=""):
    """Не чаще одного запроса в PAUSE секунд на каждый хост.
    Разные сайты можно опрашивать параллельно (потокобезопасно)."""
    while True:
        with _throttle_lock:
            now = time.time()
            wait = PAUSE - (now - _last_req.get(host, 0.0))
            if wait <= 0:
                _last_req[host] = now
                return
        time.sleep(wait)


def http_get(url, headers=None, data=None, timeout=30):
    """GET (or POST when data given). Returns decoded text."""
    _throttle(urllib.parse.urlparse(url).netloc)
    h = {"User-Agent": UA, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
         "Accept-Language": "sr,ru;q=0.8,en;q=0.7"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return raw.decode("utf-8", "ignore")


def http_json(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    hdr = {"Content-Type": "application/json"} if data else None
    return json.loads(http_get(url, headers=hdr, data=data))


def strip_tags(s):
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", s or "")).replace("\xa0", " ")


def squash(s):
    return re.sub(r"\s+", " ", s or "").strip()


def to_int(s):
    """Parse '185000.00', '134.100', '410 000', '41,000.00', '550.0€' -> int."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    m = re.search(r"\d[\d\s.,]*", str(s))
    if not m:
        return None
    t = re.sub(r"\s", "", m.group(0)).rstrip(".,")
    if "," in t and "." in t:
        dec = "," if t.rfind(",") > t.rfind(".") else "."
        t = t.replace("." if dec == "," else ",", "")
        t = t.split(dec)[0]
    elif "," in t or "." in t:
        sep = "," if "," in t else "."
        head, tail = t.rsplit(sep, 1)
        if len(tail) == 3:          # thousands separator (134.100 / 1,000)
            t = t.replace(sep, "")
        else:                       # decimal part (185000.00 / 550.0)
            t = head.replace(sep, "")
    return int(t) if t.isdigit() else None


def to_float(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"\d+(?:[.,]\d+)?", str(s))
    return float(m.group(0).replace(",", ".")) if m else None


def row(source, id, url, title=None, price=None, m2=None, rooms=None, place=None,
        created=None, photo=None):
    return {"id": f"{source}:{id}", "url": url, "title": squash(title) if title else None,
            "price": price, "m2": m2, "rooms": rooms, "place": squash(place) if place else None,
            "created": created, "photo": photo, "source": source}


# ---------------------------------------------------------------------------
# 1. cityexpert.rs  — public JSON API (POST https://cityexpert.rs/api/Search)
#    WORKS. Sort "datedsc" verified. ptId 1 = apartments, 2 = houses.
#    rentOrSale "s" | "r". cityId omitted => all cities (1 Beograd, 2 Novi Sad).
# ---------------------------------------------------------------------------
CE_CITY = {1: "beograd", 2: "novi-sad", 3: "nis"}


def fetch_cityexpert(kind="apartments", deal="sale", page=1, per_page=30):
    body = {"ptId": [1 if kind == "apartments" else 2],
            "rentOrSale": "s" if deal == "sale" else "r",
            "currentPage": page, "resultsPerPage": per_page, "sort": "datedsc"}
    d = http_json("https://cityexpert.rs/api/Search", body)
    out = []
    for it in d.get("result", []):
        pid = it["propId"]
        prefix = "prodaja-nekretnina" if deal == "sale" else "izdavanje-nekretnina"
        city = CE_CITY.get(it.get("cityId"), "beograd")
        url = f"https://cityexpert.rs/{prefix}/{city}/{pid}"
        photo = None
        if it.get("coverPhoto"):
            bucket = (pid // 1000) * 1000
            photo = f"https://img.cityexpert.rs/properties/720x/{bucket}/{pid}/slike/{it['coverPhoto']}"
        title = f"{'Stan' if kind == 'apartments' else 'Kuća'} {it.get('structure') or ''} {it.get('street') or ''}".strip()
        out.append(row("cityexpert", pid, url, title, to_int(it.get("price")),
                       to_float(it.get("size")), it.get("structure"),
                       ", ".join([x for x in [it.get("municipality"), city.replace('-', ' ').title()] if x]),
                       it.get("firstPublished"), photo))
    return out


# ---------------------------------------------------------------------------
# 2. oglasi.rs  — server-rendered HTML with schema.org microdata.
#    WORKS. Sort: ?s=d (prvo najnoviji); ?i=40 items per page; &p=N page.
#    <time datetime> gives exact publish/renew time.
# ---------------------------------------------------------------------------
OG_CAT = {("apartments", "sale"): "prodaja-stanova", ("apartments", "rent"): "izdavanje-stanova",
          ("houses", "sale"): "prodaja-kuca", ("houses", "rent"): "izdavanje-kuca"}


def fetch_oglasi_rs(kind="apartments", deal="sale", page=1):
    url = f"https://www.oglasi.rs/nekretnine/{OG_CAT[(kind, deal)]}?s=d&i=40&p={page}"
    s = http_get(url)
    out = []
    for art in re.findall(r"<article itemprop=\"itemListElement\".*?</article>", s, re.S):
        m = re.search(r'href="(/oglas/([^/"]+)/[^"]*)"', art)
        if not m:
            continue
        href, oid = m.group(1), m.group(2)
        title = re.search(r'<h2 itemprop="name">(.*?)</h2>', art, re.S)
        price = re.search(r'itemprop="price" content="([^"]+)"', art)
        cur = re.search(r'itemprop="priceCurrency" content="([^"]+)"', art)
        img = re.search(r'<img src="([^"]+)"[^>]*itemprop="image"', art)
        tm = re.search(r'<time datetime="([^"]+)"', art)
        cats = re.findall(r'itemprop="category" href="[^"]*">([^<]*)</a>', art)
        m2 = re.search(r"Kvadratura:\s*<strong>([\d.,]+)\s*m2", art)
        rooms = re.search(r"Sobnost:.*?<strong>([^<]+)</strong>", art, re.S)
        price_v = to_int(price.group(1)) if price else None
        if cur and cur.group(1) != "EUR":
            price_v = None  # RSD prices are rare; skip conversion
        out.append(row("oglasi_rs", oid, "https://www.oglasi.rs" + href,
                       strip_tags(title.group(1)) if title else None, price_v,
                       to_float(m2.group(1)) if m2 else None,
                       squash(rooms.group(1)) if rooms else None,
                       ", ".join(cats[2:]) if len(cats) > 2 else None,
                       tm.group(1) if tm else None, img.group(1) if img else None))
    return out


# ---------------------------------------------------------------------------
# 3. kupujemprodajem.com — public listing pages embed full JSON in
#    <script id="__NEXT_DATA__">.  WORKS.  /api/ is robots-disallowed and NOT used.
#    order=posted desc. NOTE: page=1 is 30 paid "top search" ads; genuinely
#    newest ads start on page=2 (sorted by postedRaw desc). postedRaw is the
#    renew time; isRenewed tells you if it is a bump rather than a new ad.
# ---------------------------------------------------------------------------
KP_CAT = {("apartments", "sale"): ("nekretnine-prodaja/stanovi", 2821, 2822),
          ("houses", "sale"): ("nekretnine-prodaja/kuce", 2821, 2823),
          ("apartments", "rent"): ("nekretnine-izdavanje/stanovi", 2850, 2851),
          ("houses", "rent"): ("nekretnine-izdavanje/kuce", 2850, 2853)}


def fetch_kupujemprodajem(kind="apartments", deal="sale", page=2, skip_promoted=True):
    path, cat, grp = KP_CAT[(kind, deal)]
    url = (f"https://www.kupujemprodajem.com/{path}/pretraga?categoryId={cat}"
           f"&groupId={grp}&order=posted%20desc&page={page}")
    s = http_get(url)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', s, re.S)
    if not m:
        return []
    se = json.loads(m.group(1))["props"]["initialReduxState"]["search"]
    out = []
    for aid in se.get("adsIds", []):
        a = se["byId"][str(aid)]
        if skip_promoted and a.get("isTopSearch"):
            continue
        m2 = rooms = None
        for sec in a.get("adAttributes") or []:
            for at in sec.get("attributes", []):
                v = (at.get("values") or [None])[0]
                if at.get("code") == "realEstateArea":
                    m2 = to_float(v)
                elif "NumberOfRooms" in (at.get("code") or ""):
                    rooms = v
        price = a.get("priceNumber") if (a.get("currencyAcronym") or "eur") == "eur" else None
        created = a.get("postedRaw")
        if created:
            created = created.replace(" ", "T") + "+02:00"  # site time = Europe/Belgrade
        out.append(row("kupujemprodajem", aid, "https://www.kupujemprodajem.com" + a["adUrl"],
                       a.get("name"), to_int(price), m2, rooms, a.get("location"),
                       created, a.get("image") or None))
    return out


# ---------------------------------------------------------------------------
# 4. nadjidom.com — server-rendered HTML.  WORKS.
#    Sort: &sort=date&sort_type=1 (newest first). Date is day precision dd/mm/yy.
# ---------------------------------------------------------------------------
def fetch_nadjidom(kind="apartments", deal="sale", page=1):
    k = "stanovi" if kind == "apartments" else "kuce"
    d = "prodaja" if deal == "sale" else "izdavanje"
    url = f"https://www.nadjidom.com/sr/nekretnine/{d}/{k}&sort=date&sort_type=1"
    if page > 1:
        url += f"&page={page}"
    s = http_get(url)
    out, seen = [], set()
    # each listing appears as an <h2><a href=...details/ID/...> block
    for blk in re.split(r'<div\s+class="estate-loop', s)[1:]:
        m = re.search(r'href="(https://www\.nadjidom\.com/sr/details/(\d+)/[^"]*)"', blk)
        if not m or m.group(2) in seen:
            continue
        # the item is split into two consecutive chunks (image + body); merge with next
        seen.add(m.group(2))
        title = re.search(r"<h2[^>]*>\s*<a[^>]*>(.*?)</a>", blk, re.S)
        if not title:
            continue
        img = re.search(r'<img class="list-image" src="([^"]+)"', blk) or re.search(r'src="(https://cdn\.nadjidom\.com/images/photos/[^"]+)"', blk)
        txt = squash(strip_tags(blk))
        price = re.search(r"(\d[\d.]*)\s*€", txt)
        m2 = re.search(r"(\d+(?:[.,]\d+)?)\s*m\s*2", txt)
        rooms = re.search(r"\b(garsonjera|jedno\w*soban|dvo\w*soban|tro\w*soban|četvo\w*soban|peto\w*soban|višesoban)\b", txt, re.I)
        place = re.search(r'fi-map-pin"?[^>]*>\s*</i>\s*([^<]+)<', blk) or re.search(r"<p[^>]*>\s*<i[^>]*></i>\s*([^<]+)</p>", blk)
        date = re.search(r"\b(\d\d)/(\d\d)/(\d\d)\b", txt)
        created = f"20{date.group(3)}-{date.group(2)}-{date.group(1)}" if date else None
        out.append(row("nadjidom", m.group(2), m.group(1), strip_tags(title.group(1)),
                       to_int(price.group(1)) if price else None,
                       to_float(m2.group(1)) if m2 else None,
                       rooms.group(1).lower() if rooms else None,
                       squash(place.group(1)) if place else None, created,
                       img.group(1) if img else None))
    return out


# ---------------------------------------------------------------------------
# 5. imovina.net — server-rendered HTML. PARTIAL: no date in list, no sort
#    param; list is "Najnoviji" but ~2-3 pages of paid "top" ads come first.
#    Strategy: fetch pages 1..N, dedupe by numeric id, treat id as recency.
#    t39 = stanovi, t41 = kuće, c1 = prodaja, c2 = izdavanje.
# ---------------------------------------------------------------------------
def fetch_imovina(kind="apartments", deal="sale", page=1):
    t = "39" if kind == "apartments" else "41"
    kslug = "stanovi" if kind == "apartments" else "kuce"
    c = "1" if deal == "sale" else "2"
    dslug = "prodaja" if deal == "sale" else "izdavanje"
    url = f"https://imovina.net/nekretnine/{kslug}/{dslug}/{page}/t{t}-c{c}-p{page}.html"
    s = http_get(url)
    out, seen = [], set()
    for blk in re.findall(r'<div class="listapretraga.*?<div style="clear:both;">', s, re.S):
        m = re.search(r'href="(https://imovina\.net/nekretnina/[^/]+/[^/]+/(\d+)/)"\s+title="([^"]*)"', blk)
        if not m or m.group(2) in seen:
            continue
        seen.add(m.group(2))
        img = re.search(r"background:url\('([^']+)'\)", blk)
        price = re.search(r'class="oglascena">([^<]*)<', blk)
        loc = re.search(r'class="listlokacija">([^<]*)<', blk)
        m2 = re.search(r'class="liststruktura">.*?<br>\s*([\d.,]+)\s*m2', blk, re.S)
        rooms = re.search(r'class="listakvadratura hidden-xs">.*?<br>\s*([^<]+)<', blk, re.S)
        top = 'class="topstyle"' in blk
        out.append(row("imovina", m.group(2), m.group(1), m.group(3),
                       to_int(price.group(1)) if price and "euro" in price.group(1).lower() or (price and "€" in price.group(1)) else None,
                       to_float(m2.group(1)) if m2 else None,
                       squash(rooms.group(1)).lower() if rooms else None,
                       loc.group(1) if loc else None, None,
                       ("https://imovina.net" + img.group(1)) if img else None))
        out[-1]["promoted"] = top
    return out


# ---------------------------------------------------------------------------
# 6. realitica.com — old-school server HTML. PARTIAL (small Serbian inventory,
#    no date in list, featured ads mixed in). Sort: sort=date_desc.
#    for=Prodaja | DuziNajam ; type=Apartment | House ; pState=Srbija.
# ---------------------------------------------------------------------------
def fetch_realitica(kind="apartments", deal="sale", page=0):
    q = {"cur_page": page, "for": "Prodaja" if deal == "sale" else "DuziNajam",
         "pState": "Srbija", "type": "Apartment" if kind == "apartments" else "Home",
         "lng": "hr", "sort": "date_desc"}
    url = "https://www.realitica.com/?" + urllib.parse.urlencode(q)
    s = http_get(url)
    out, seen = [], set()
    for blk in re.findall(r'<div class="thumb_div".*?Detaljno</a>', s, re.S):
        m = re.search(r'href="(https://www\.realitica\.com/hr/listing/(\d+))"', blk)
        if not m or m.group(2) in seen:
            continue
        seen.add(m.group(2))
        img = re.search(r'<img src="([^"]+)"', blk)
        body = re.search(r"</div>\s*<div>(.*?)<a href", blk, re.S)
        txt = squash(strip_tags(body.group(1))) if body else ""
        price = re.search(r"€\s*([\d.]+)", txt)
        m2 = re.search(r"(\d+)\s*m\s*2", txt)
        rooms = re.search(r"(\d+(?:[.,]\d)?)\s*sob", txt)
        place = None
        parts = [p.strip() for p in re.split(r"<br\s*/?>", body.group(1))] if body else []
        if len(parts) >= 3:
            place = squash(strip_tags(parts[2]))
        desc = re.search(r'style="text-decoration: none;color:black;">(.*?)</a>', blk, re.S)
        photo = img.group(1) if img else None
        if photo and photo.startswith("/"):
            photo = "https://www.realitica.com" + photo
        out.append(row("realitica", m.group(2), m.group(1),
                       squash(strip_tags(desc.group(1)))[:120] if desc else txt[:80],
                       to_int(price.group(1)) if price else None,
                       to_float(m2.group(1)) if m2 else None,
                       rooms.group(1) if rooms else None, place, None, photo))
    return out


# ---------------------------------------------------------------------------
# 7. nekretnine365.com — regional (HR/BA/RS) portal, HTML with data-listing-*
#    attributes.  WORKS but low Serbian volume (12 per page).
#    Sort: ?sort_by=date&sort_type=desc ; filter country:Srbija.
# ---------------------------------------------------------------------------
def fetch_nekretnine365(kind="apartments", deal="sale", page=1):
    cat = "prodaja-najam-stanova" if kind == "apartments" else "prodaja-najam-kuca"
    sr = "Prodaja" if deal == "sale" else "Najam"
    url = f"https://www.nekretnine365.com/nekretnine/{cat}/sale-rent:{sr}/country:Srbija/"
    if page > 1:
        url += f"page:{page}/"
    url += "?sort_by=date&sort_type=desc"
    s = http_get(url)
    out = []
    for art in re.findall(r'<article class="item.*?</article>', s, re.S):
        m = re.search(r'data-listing-id="(\d+)"\s+data-listing-url="([^"]+)"\s+data-listing-title="([^"]*)"'
                      r'\s+data-listing-fields="([^"]*)"\s+data-listing-picture="([^"]*)"', art)
        if not m:
            continue
        fields = [f.strip() for f in htmllib.unescape(m.group(4)).split(",")]
        # fields: id, "41,000.00 €" (may be split by the comma!), Prodaja, Stan, Srbija, Region, Grad
        price = re.search(r'price-tag">\s*<span>([^<]*)<', art)
        m2 = re.search(r'class="square_feet">([\d.,]+)', art)
        rooms = re.search(r'class="badrooms">([^<]*)<', art)
        place = ", ".join(fields[-2:]) if len(fields) >= 2 else None
        p = to_int(price.group(1)) if price else None
        rv = rooms.group(1).strip() if rooms else None
        if rv and rv.isdigit() and len(rv) == 2:
            rv = f"{rv[0]}.{rv[1]}"      # site encodes 1.5 as "15", 4.0 as "40"
        out.append(row("nekretnine365", m.group(1), m.group(2), htmllib.unescape(m.group(3)),
                       p, to_float(m2.group(1)) if m2 else None,
                       rv, place, None, m.group(5) or None))
    return out


# ---------------------------------------------------------------------------
# 8. Telegram public channel previews  https://t.me/s/<channel>
#    WORKS (no JS needed; 20 latest posts; ?before=<msg_id> for older).
#    Many channels are aggregator bots re-posting halooglasi.com / nekretnine.rs
#    (both directly BLOCKED for us) so this is an indirect route to them.
# ---------------------------------------------------------------------------
TG_CHANNELS = {
    # channel: (kind, deal, language/notes)
    "rent_bg": ("apartments", "rent", "ru; bot re-posting oglasi.rs Belgrade rentals"),
    "rent_ns": ("apartments", "rent", "ru; bot re-posting oglasi.rs Novi Sad rentals"),
    "belgrade_apartmens": ("apartments", "rent", "sr/ru; bot re-posting nekretnine.rs+halooglasi Belgrade"),
    "novisad_apartmens": ("apartments", "rent", "sr/ru; bot re-posting nekretnine.rs+halooglasi Novi Sad"),
    "BelgradeRental": ("apartments", "rent", "en; bot re-posting halooglasi (rich structured text)"),
    "apartments_in_belgrade": ("apartments", "rent", "sr/en; Rentava bot re-posting halooglasi"),
    "flattorentbelgrade": ("apartments", "rent", "ru; agent channel, hashtags, albums"),
    "FlatsInBelgrade": ("apartments", "rent", "ru; agent channel"),
    "beograd_stan": ("apartments", "rent", "ru; agency channel"),
    "novisad_stan": ("apartments", "rent", "ru; agency channel"),
    "kvartiraSerbia": ("apartments", "both", "ru; agency, rent+sale, structured"),
    "belgraderent": ("apartments", "rent", "ru; agency"),
}


def fetch_telegram(channel, before=None):
    url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
    s = http_get(url)
    out = []
    msgs = re.findall(r'<div class="tgme_widget_message_wrap.*?(?=<div class="tgme_widget_message_wrap|</section>)', s, re.S)
    for m in msgs:
        post = re.search(r'data-post="([^"/]+)/(\d+)"', m)
        if not post:
            continue
        mid = post.group(2)
        t = re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>', m, re.S)
        txt = ""
        if t:
            txt = htmllib.unescape(re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>", "\n", t.group(1)))).strip()
        date = re.search(r'class="tgme_widget_message_date"[^>]*>\s*<time datetime="([^"]+)"', m)
        photos = [p for p in re.findall(r"background-image:url\('([^']+)'\)", m) if "telesco.pe" in p or "cdn" in p]
        links = [htmllib.unescape(l) for l in re.findall(r'href="(https?://[^"]+)"', m) if "t.me/" not in l and "telegram.org" not in l]
        price = re.search(r"(?:€|EUR|евро|eur|e)\s*([\d][\d.\s]{1,7})|([\d][\d.\s]{1,7})\s*(?:€|EUR|евро|eur|e\b)", txt, re.I)
        pv = to_int(price.group(1) or price.group(2)) if price else None
        m2 = re.search(r"(\d{2,3}(?:[.,]\d)?)\s*(?:m2|m²|м2|м²|кв\.?\s*м)", txt, re.I)
        rooms = re.search(r"(\d(?:[.,]\d)?)\s*(?:комнат|комн|соб|rooms?|bedroom|bd\b|-?к\b)", txt, re.I) or \
            re.search(r"\b(garsonjera|студия|jedno\w*soban|dvo\w*soban|tro\w*soban|četvo\w*soban)\b", txt, re.I)
        out.append(row("tg_" + channel, mid, f"https://t.me/{channel}/{mid}", txt.split("\n")[0][:120] if txt else None,
                       pv, to_float(m2.group(1)) if m2 else None,
                       rooms.group(1) if rooms else None, None,
                       date.group(1) if date else None, photos[0] if photos else None))
        out[-1]["text"] = txt
        out[-1]["ext_links"] = links
        out[-1]["photos"] = photos
    return out


# ---------------------------------------------------------------------------
FETCHERS = {
    "cityexpert": fetch_cityexpert,
    "oglasi_rs": fetch_oglasi_rs,
    "kupujemprodajem": fetch_kupujemprodajem,
    "nadjidom": fetch_nadjidom,
    "imovina": fetch_imovina,
    "realitica": fetch_realitica,
    "nekretnine365": fetch_nekretnine365,
}
