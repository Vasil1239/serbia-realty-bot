#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот «Новые объявления недвижимости Сербии» для Telegram-ГРУППЫ.

Источники (см. sources.py):
  сайты      — 4zida.rs, KupujemProdajem, oglasi.rs, CityExpert, nadjidom.com,
               imovina.net, nekretnine365.com (realitica.com блокирует GitHub — отключён)
  Telegram   — публичные каналы-агрегаторы (в т.ч. перепосты halooglasi.com и
               nekretnine.rs, которые напрямую закрыты защитой от ботов)

Категории: продажа квартир, продажа домов, аренда квартир, аренда домов.
Каждый пост содержит ссылку на первоисточник.

Расписание задаёт cron в .github/workflows/group-bot.yml (5 запусков в день);
скрипт лишь следит за окном публикаций: после 22:00 и до 09:00 постов нет.
Сбор идёт параллельно по источникам, посты — с шагом ~3 с (лимит Telegram 20/мин).

Переменные окружения:
  BOT_TOKEN      — токен бота (@BotFather)
  GROUP_CHAT_ID  — числовой id группы
  FORCE_RUN=1    — запустить вне расписания (для теста)
"""

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import sources as S

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GROUP_CHAT_ID = os.environ.get("GROUP_CHAT_ID", "")
FORCE_RUN = os.environ.get("FORCE_RUN", "") == "1"

LOCAL_TZ = ZoneInfo("Europe/Belgrade")
POST_WINDOW_LOCAL = (9, 22)          # публикуем строго с 09:00 до 22:00; после 22:00 — ни одного поста,
                                     # что не успели — уйдёт с утренним запуском
LOOKBACK_HOURS = 16                  # утренний запуск покрывает вечер и ночь
MAX_POSTS_PER_KIND = None            # None = без лимита, публикуем всё найденное
MIN_POST_INTERVAL = 3.05             # лимит Telegram 20 сообщений/мин в группу = 1 пост в 3 с
                                     # (считается от начала предыдущей отправки, а не после неё)
COLLECT_WORKERS = 8                  # параллельный сбор: по потоку на источник
PRIORITY_KEYWORDS = ("beograd", "belgrade", "белград", "novi beograd")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "group_posted.json")

CATEGORIES = [
    ("apartments", "sale", "Продажа квартиры", "#продажа #квартира"),
    ("houses",     "sale", "Продажа дома",     "#продажа #дом"),
    ("apartments", "rent", "Аренда квартиры",  "#аренда #квартира"),
    ("houses",     "rent", "Аренда дома",      "#аренда #дом"),
]

# сайты с точной датой публикации
DATED_SITES = {
    "4zida":           lambda k, d: fetch_4zida(k, d),
    "kupujemprodajem": lambda k, d: S.fetch_kupujemprodajem(k, d, page=2),
    "oglasi_rs":       lambda k, d: S.fetch_oglasi_rs(k, d),
    "cityexpert":      lambda k, d: S.fetch_cityexpert(k, d),
    "nadjidom":        lambda k, d: S.fetch_nadjidom(k, d),
}
# сайты без даты в выдаче — берём только id новее уже виденного (водяной знак)
WATERMARK_SITES = {
    "imovina":       lambda k, d: S.fetch_imovina(k, d),
    "nekretnine365": lambda k, d: S.fetch_nekretnine365(k, d),
}
# Telegram-каналы: (канал, kind, deal или "both")
TG_CHANNELS = [
    ("belgrade_apartmens", "apartments", "rent"),
    ("novisad_apartmens",  "apartments", "rent"),
    ("BelgradeRental",     "apartments", "rent"),
    ("rent_bg",            "apartments", "rent"),
    ("rent_ns",            "apartments", "rent"),
    ("flattorentbelgrade", "apartments", "rent"),
    ("FlatsInBelgrade",    "apartments", "rent"),
    ("beograd_stan",       "apartments", "rent"),
    ("novisad_stan",       "apartments", "rent"),
    ("kvartiraSerbia",     "apartments", "both"),
    ("flattobuybelgrade",  "apartments", "sale"),
]

SOURCE_NAMES = {
    "4zida": "4zida.rs", "kupujemprodajem": "KupujemProdajem", "oglasi_rs": "oglasi.rs",
    "cityexpert": "CityExpert", "nadjidom": "nadjidom.com", "imovina": "imovina.net",
    "nekretnine365": "nekretnine365.com",
}
# ===============================================

ROOMS_RU = {"0.5": "студия", "1": "1-комн.", "1.0": "1-комн.", "1.5": "1.5-комн.",
            "2": "2-комн.", "2.0": "2-комн.", "2.5": "2.5-комн.", "3": "3-комн.",
            "3.0": "3-комн.", "3.5": "3.5-комн.", "4": "4-комн.", "4.0": "4-комн.",
            "4.5": "4.5-комн.", "5": "5+ комн.", "5.0": "5+ комн.",
            "garsonjera": "студия", "jednosoban": "1-комн.", "jednoiposoban": "1.5-комн.",
            "dvosoban": "2-комн.", "dvoiposoban": "2.5-комн.", "trosoban": "3-комн.",
            "troiposoban": "3.5-комн.", "cetvorosoban": "4-комн.", "četvorosoban": "4-комн.",
            "petosoban": "5-комн.", "višesoban": "5+ комн."}

CITY_TAGS = [("novi beograd", "#белград"), ("beograd", "#белград"), ("belgrade", "#белград"),
             ("белград", "#белград"), ("novi sad", "#новисад"), ("нови сад", "#новисад"),
             ("niš", "#ниш"), ("nis", "#ниш"), ("subotica", "#суботица"),
             ("kragujevac", "#крагуевац"), ("zlatibor", "#златибор"), ("pančevo", "#панчево"),
             ("pancevo", "#панчево"), ("zemun", "#земун"), ("šabac", "#шабац"),
             ("sombor", "#сомбор"), ("čačak", "#чачак"), ("kraljevo", "#кралево"),
             ("vrnjačka", "#врнячкабаня"), ("smederevo", "#смедерево"), ("zrenjanin", "#зренянин")]


def log(msg):
    print(msg, flush=True)


# ---------- 4zida (адаптер к общему формату) ----------
def fetch_4zida(kind, deal, pages=2):
    out = []
    for page in range(1, pages + 1):
        url = f"https://api.4zida.rs/v6/search/{kind}?for={deal}&page={page}&sort=createdAtDesc"
        d = json.loads(S.http_get(url))
        ads = d.get("ads", []) if isinstance(d, dict) else []
        for ad in ads:
            img = (ad.get("image") or {}).get("search") or {}
            photo = img.get("380x0_fill_0_jpeg") or img.get("380x0_fill_0_webp")
            names = [n for n in (ad.get("placeNames") or [])
                     if n.lower() not in ("gradske lokacije", "okolne lokacije")]
            floor = ad.get("redactedFloor")
            total = ad.get("redactedTotalFloors")
            r = S.row("4zida", ad.get("id"), "https://www.4zida.rs" + (ad.get("urlPath") or ""),
                      ad.get("title"), S.to_int(ad.get("price")), S.to_float(ad.get("m2")),
                      ad.get("roomCount"), ", ".join(names[:3]), ad.get("createdAt"), photo)
            r["address"] = ad.get("address")
            r["desc"] = ad.get("description100")
            r["extra"] = (f"этаж {floor}/{total}" if floor is not None and total else None)
            if kind == "houses" and (ad.get("lotSize") or ad.get("lotArea")):
                r["extra"] = f"участок {ad.get('lotSize') or ad.get('lotArea')} ар"
            out.append(r)
        if not ads:
            break
    return out


# ---------- Telegram ----------
def tg_call(method, payload):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": S.UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "description": str(e)}


def send_post(text, photo):
    if photo:
        resp = tg_call("sendPhoto", {"chat_id": GROUP_CHAT_ID, "photo": photo,
                                     "caption": text[:1000], "parse_mode": "HTML"})
        if resp.get("ok") or resp.get("error_code") == 429:
            return resp
        log(f"  ! фото не принято ({resp.get('description')}), шлю текстом")
    return tg_call("sendMessage", {"chat_id": GROUP_CHAT_ID, "text": text[:4000],
                                   "parse_mode": "HTML"})


_last_send = 0.0


def send_paced(text, photo):
    """Отправка с выдержкой MIN_POST_INTERVAL между началами отправок и одним повтором при 429."""
    global _last_send
    for attempt in range(2):
        wait = MIN_POST_INTERVAL - (time.time() - _last_send)
        if wait > 0:
            time.sleep(wait)
        _last_send = time.time()
        resp = send_post(text, photo)
        if resp.get("error_code") == 429 and attempt == 0:
            retry = int((resp.get("parameters") or {}).get("retry_after", 30)) + 1
            log(f"  ! Telegram просит подождать {retry} с")
            time.sleep(retry)
            continue
        return resp
    return resp


# ---------- состояние ----------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    else:
        st = {}
    st.setdefault("posted", {})
    st.setdefault("watermark", {})
    return st


def save_state(state):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    state["posted"] = {k: v for k, v in state["posted"].items() if v > cutoff}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


# ---------- утилиты ----------
def parse_dt(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt


def is_fresh(r, since):
    dt = parse_dt(r.get("created"))
    if dt is None:
        return False
    if len(r.get("created") or "") <= 10:          # только дата (nadjidom) — берём сегодня/вчера
        return dt.date() >= (since.astimezone(LOCAL_TZ).date())
    return dt >= since


def kind_matches(r, kind):
    """nadjidom подмешивает квартиры в раздел домов и наоборот — отсекаем по URL."""
    u = (r.get("url") or "").lower()
    if r["source"] != "nadjidom":
        return True
    return not (("-stan." in u and kind == "houses") or ("-kuca." in u and kind == "apartments"))


def fingerprint(r):
    place = (r.get("place") or r.get("address") or "").lower().split(",")[0].strip()
    m2 = r.get("m2")
    return f"{r.get('price')}|{int(m2) if m2 else '?'}|{place[:20]}"


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")) if s else ""


def fmt_money(v):
    try:
        return f"{float(v):,.0f}".replace(",", " ")
    except (TypeError, ValueError):
        return str(v)


def rooms_ru(v):
    if v is None:
        return None
    key = str(v).strip().lower().replace(",", ".")
    if key in ROOMS_RU:
        return ROOMS_RU[key]
    for k, ru in ROOMS_RU.items():
        if k in key:
            return ru
    if re.fullmatch(r"\d+(\.\d+)?", key):
        return f"{key}-комн."
    return None


def city_tag(r):
    hay = " ".join(str(r.get(k) or "") for k in ("place", "address", "title", "url", "text")).lower()
    for key, tag in CITY_TAGS:
        if key in hay:
            return tag
    return "#сербия"


def is_priority(r):
    hay = " ".join(str(r.get(k) or "") for k in ("place", "address", "url", "title")).lower()
    return any(k in hay for k in PRIORITY_KEYWORDS)


def source_label(r):
    src = r["source"]
    if src.startswith("tg_"):
        return f"Telegram @{src[3:]}"
    return SOURCE_NAMES.get(src, src)


def format_post(r, kind, deal, title, hashtags):
    lines = [f"<b>{title}</b> — {esc(r.get('place') or 'Сербия')}"]
    details = []
    if r.get("m2"):
        details.append(f"📐 {fmt_money(r['m2'])} м²")
    rr = rooms_ru(r.get("rooms"))
    if rr:
        details.append(rr)
    if r.get("extra"):
        details.append(r["extra"])
    if details:
        lines.append(" · ".join(details))
    unit = "€" if deal == "sale" else "€/мес"
    if r.get("price"):
        ppm2 = ""
        if deal == "sale" and r.get("m2"):
            ppm2 = f" ({fmt_money(r['price'] / r['m2'])} €/м²)"
        lines.append(f"💶 <b>{fmt_money(r['price'])} {unit}</b>{ppm2}")
    addr = r.get("address")
    if addr and addr.lower() not in (r.get("place") or "").lower():
        lines.append(f"📍 {esc(addr)}, {esc(r.get('place') or '')}".rstrip(", "))
    desc = r.get("desc") or (r.get("title") if r["source"] != "4zida" else None)
    if desc:
        lines.append(esc(re.sub(r"\s+", " ", desc).strip()[:160]))
    # ссылки на первоисточник
    if r["source"].startswith("tg_"):
        ext = [S.htmllib.unescape(l) for l in r.get("ext_links", []) if "maps" not in l and "google" not in l]
        if ext:
            host = urllib.parse.urlparse(ext[0]).netloc.replace("www.", "")
            lines.append(f'🔗 <a href="{esc(ext[0])}">Источник: {esc(host)}</a> · '
                         f'<a href="{esc(r["url"])}">пост в {esc(source_label(r))}</a>')
        else:
            lines.append(f'🔗 <a href="{esc(r["url"])}">Источник: {esc(source_label(r))}</a>')
    else:
        lines.append(f'🔗 <a href="{esc(r["url"])}">Источник: {esc(source_label(r))}</a>')
    tag = city_tag(r)
    lines.append(f"{hashtags} {tag}" + (" #сербия" if tag != "#сербия" else ""))
    return "\n".join(lines)


# ---------- сбор ----------
def tg_category(ch_kind, ch_deal, r):
    """Определяет категорию поста из Telegram-канала."""
    txt = (r.get("text") or "").lower()
    if ch_deal == "both":
        deal = "sale" if re.search(r"прода|prodaj|sale|купить", txt) else "rent"
    else:
        deal = ch_deal
    head = txt.split("\n")[0][:80]
    kind = "houses" if re.search(r"\bдом\b|kuć|kuca|\bhouse\b|вилл", head) else ch_kind
    return kind, deal


def _fetch_site(name, fn):
    """Один сайт по всем категориям (выполняется в своём потоке; запросы к сайту — последовательно)."""
    out = []
    for kind, deal, _, _ in CATEGORIES:
        try:
            out.append((kind, deal, fn(kind, deal), None))
        except Exception as e:
            out.append((kind, deal, [], f"{type(e).__name__}: {e}"))
    return name, out


def _fetch_channel(ch):
    try:
        return ch, S.fetch_telegram(ch), None
    except Exception as e:
        return ch, [], f"{type(e).__name__}: {e}"


def collect(since, state):
    """Возвращает {(kind, deal): [записи]} со всех источников.
    Сетевая часть идёт параллельно (поток на источник), фильтрация и состояние — в основном потоке."""
    bucket = {(k, d): [] for k, d, _, _ in CATEGORIES}
    stats = {}

    with ThreadPoolExecutor(max_workers=COLLECT_WORKERS) as ex:
        dated_f = [ex.submit(_fetch_site, n, fn) for n, fn in DATED_SITES.items()]
        wm_f = [ex.submit(_fetch_site, n, fn) for n, fn in WATERMARK_SITES.items()]
        tg_f = [ex.submit(_fetch_channel, ch) for ch, _, _ in TG_CHANNELS]
        dated = [f.result() for f in dated_f]
        wms = [f.result() for f in wm_f]
        tgs = [f.result() for f in tg_f]

    for name, per_cat in dated:
        for kind, deal, rows, err in per_cat:
            if err:
                log(f"  ! {name} {kind}/{deal}: {err}")
            items = [r for r in rows if r.get("price") and is_fresh(r, since) and kind_matches(r, kind)]
            stats[name] = stats.get(name, 0) + len(items)
            bucket[(kind, deal)] += items

    for name, per_cat in wms:
        for kind, deal, rows, err in per_cat:
            if err:
                log(f"  ! {name} {kind}/{deal}: {err}")
                continue
            wm_key = f"{name}:{kind}:{deal}"
            items = [r for r in rows if r.get("price") and not r.get("promoted")]
            ids = [int(re.sub(r"\D", "", r["id"].split(":", 1)[1]) or 0) for r in items]
            if not ids:
                continue
            wm = state["watermark"].get(wm_key)
            new_items = []
            if wm is not None:
                new_items = [r for r, i in zip(items, ids) if i > wm]
            state["watermark"][wm_key] = max(ids + [wm or 0])
            stats[name] = stats.get(name, 0) + len(new_items)
            bucket[(kind, deal)] += new_items

    ch_meta = {ch: (k, d) for ch, k, d in TG_CHANNELS}
    for ch, posts, err in tgs:
        if err:
            log(f"  ! @{ch}: {err}")
            continue
        ch_kind, ch_deal = ch_meta[ch]
        n = 0
        for r in posts:
            txt = r.get("text") or ""
            if not r.get("price") or not is_fresh(r, since) or len(txt) < 30:
                continue
            if txt.startswith("📊") or "owner listings today" in txt:   # сводки, не объявления
                continue
            kind, deal = tg_category(ch_kind, ch_deal, r)
            r["place"] = r.get("place") or ch_place(ch)
            r["desc"] = re.sub(r"https?://\S+", "", txt)
            bucket[(kind, deal)].append(r)
            n += 1
        stats[f"@{ch}"] = n

    log("  свежих по источникам: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    return bucket


def ch_place(ch):
    c = ch.lower()
    if "novisad" in c or c == "rent_ns":
        return "Novi Sad"
    if "belgrade" in c or "beograd" in c or c == "rent_bg":
        return "Beograd"
    return "Сербия"


def pick(items, state, limit):
    """Отбор с чередованием источников; Белград и фото — в приоритете."""
    seen_fp = set()
    fresh = []
    for r in items:
        fp = fingerprint(r)
        if r["id"] in state["posted"] or fp in state["posted"] or fp in seen_fp:
            continue
        seen_fp.add(fp)
        fresh.append(r)
    fresh.sort(key=lambda r: (not is_priority(r), r.get("photo") is None,
                              -(parse_dt(r.get("created")) or datetime.min.replace(tzinfo=timezone.utc)).timestamp()))
    by_src = {}
    for r in fresh:
        by_src.setdefault(r["source"], []).append(r)
    order = sorted(by_src, key=lambda s: (s.startswith("tg_"), s != "4zida"))
    chosen = []
    while (limit is None or len(chosen) < limit) and any(by_src.values()):
        for s in order:
            if by_src[s] and (limit is None or len(chosen) < limit):
                chosen.append(by_src[s].pop(0))
    return chosen


def posting_allowed():
    h = datetime.now(LOCAL_TZ).hour
    return POST_WINDOW_LOCAL[0] <= h < POST_WINDOW_LOCAL[1]


def main():
    if not BOT_TOKEN or not GROUP_CHAT_ID:
        log("Не заданы BOT_TOKEN / GROUP_CHAT_ID — публикация пропущена.")
        return
    log(f"Местное время (Белград): {datetime.now(LOCAL_TZ):%Y-%m-%d %H:%M}")
    if not FORCE_RUN and not posting_allowed():
        log("Вне окна публикаций (09:00–22:00) — пропуск запуска.")
        return

    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    state = load_state()
    t0 = time.time()
    log("Сбор объявлений (параллельно)...")
    bucket = collect(since, state)
    log(f"  сбор занял {time.time() - t0:.0f} с")
    save_state(state)
    posted_now = 0

    limit = MAX_POSTS_PER_KIND
    for kind, deal, title, hashtags in CATEGORIES:
        items = bucket[(kind, deal)]
        chosen = pick(items, state, limit)
        log(f"Категория: {title} — свежих {len(items)}, к публикации {len(chosen)}")
        for r in chosen:
            if not FORCE_RUN and not posting_allowed():
                log("Вышли за разрешённое время публикаций — остаток выйдет утром в 10:00.")
                save_state(state)
                log(f"Опубликовано за запуск: {posted_now}")
                return
            text = format_post(r, kind, deal, title, hashtags)
            resp = send_paced(text, r.get("photo"))
            if resp.get("ok"):
                now = datetime.now(timezone.utc).isoformat()
                state["posted"][r["id"]] = now
                state["posted"][fingerprint(r)] = now
                posted_now += 1
                log(f"  ✓ {source_label(r)}: {r['url']}")
                if posted_now % 20 == 0:
                    save_state(state)          # промежуточное сохранение на случай обрыва
            else:
                log(f"  ! Telegram отказал: {resp}")

    save_state(state)
    log(f"Готово. Опубликовано за запуск: {posted_now}, всего {time.time() - t0:.0f} с")


if __name__ == "__main__":
    main()
