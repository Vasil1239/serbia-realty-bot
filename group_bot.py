#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот новых объявлений недвижимости Сербии.

Порядок публикации для каждого объявления:
1. В ORIGINAL_CHAT_ID уходит исходное доступное содержание из источника.
2. Только при успешной отправке оригинала в GROUP_CHAT_ID уходит форматированная версия.
3. При ошибке ORIGINAL_CHAT_ID основной канал это объявление не получает.

Нужные GitHub Secrets / Variables:
  BOT_TOKEN         токен Telegram-бота
  GROUP_CHAT_ID     ID основного чата с форматированными публикациями
Необязательно:
  ORIGINAL_CHAT_ID  ID чата оригиналов, по умолчанию -1004325987530
  FORCE_RUN=1       ручной тест вне окна 09:00–22:00 по Белграду
"""

import html
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import urllib.error
import urllib.parse
import urllib.request

import sources as S

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GROUP_CHAT_ID = os.environ.get("GROUP_CHAT_ID", "")
ORIGINAL_CHAT_ID = os.environ.get("ORIGINAL_CHAT_ID", "-1004325987530")
FORCE_RUN = os.environ.get("FORCE_RUN", "") == "1"

LOCAL_TZ = ZoneInfo("Europe/Belgrade")
POST_WINDOW_LOCAL = (9, 22)
LOOKBACK_HOURS = 16
MAX_POSTS_PER_KIND = None
MIN_POST_INTERVAL = 3.05
COLLECT_WORKERS = 8
PRIORITY_KEYWORDS = ("beograd", "belgrade", "белград", "novi beograd")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "group_posted.json")

CATEGORIES = [
    ("apartments", "sale", "Продажа квартиры", "#продажа #квартира"),
    ("houses", "sale", "Продажа дома", "#продажа #дом"),
    ("apartments", "rent", "Аренда квартиры", "#аренда #квартира"),
    ("houses", "rent", "Аренда дома", "#аренда #дом"),
]

DATED_SITES = {
    "4zida": lambda kind, deal: fetch_4zida(kind, deal),
    "kupujemprodajem": lambda kind, deal: S.fetch_kupujemprodajem(kind, deal, page=2),
    "oglasi_rs": lambda kind, deal: S.fetch_oglasi_rs(kind, deal),
    "cityexpert": lambda kind, deal: S.fetch_cityexpert(kind, deal),
    "nadjidom": lambda kind, deal: S.fetch_nadjidom(kind, deal),
}
WATERMARK_SITES = {
    "imovina": lambda kind, deal: S.fetch_imovina(kind, deal),
    "nekretnine365": lambda kind, deal: S.fetch_nekretnine365(kind, deal),
}
TG_CHANNELS = [
    ("belgrade_apartmens", "apartments", "rent"),
    ("novisad_apartmens", "apartments", "rent"),
    ("BelgradeRental", "apartments", "rent"),
    ("rent_bg", "apartments", "rent"),
    ("rent_ns", "apartments", "rent"),
    ("flattorentbelgrade", "apartments", "rent"),
    ("FlatsInBelgrade", "apartments", "rent"),
    ("beograd_stan", "apartments", "rent"),
    ("novisad_stan", "apartments", "rent"),
    ("kvartiraSerbia", "apartments", "both"),
    ("flattobuybelgrade", "apartments", "sale"),
]

SOURCE_NAMES = {
    "4zida": "4zida.rs",
    "kupujemprodajem": "KupujemProdajem",
    "oglasi_rs": "oglasi.rs",
    "cityexpert": "CityExpert",
    "nadjidom": "nadjidom.com",
    "imovina": "imovina.net",
    "nekretnine365": "nekretnine365.com",
}

ROOMS_RU = {
    "0.5": "студия", "1": "1-комн.", "1.0": "1-комн.", "1.5": "1.5-комн.",
    "2": "2-комн.", "2.0": "2-комн.", "2.5": "2.5-комн.", "3": "3-комн.",
    "3.0": "3-комн.", "3.5": "3.5-комн.", "4": "4-комн.", "4.0": "4-комн.",
    "4.5": "4.5-комн.", "5": "5+ комн.", "5.0": "5+ комн.",
    "garsonjera": "студия", "jednosoban": "1-комн.", "jednoiposoban": "1.5-комн.",
    "dvosoban": "2-комн.", "dvoiposoban": "2.5-комн.", "trosoban": "3-комн.",
    "troiposoban": "3.5-комн.", "cetvorosoban": "4-комн.", "četvorosoban": "4-комн.",
    "petosoban": "5-комн.", "višesoban": "5+ комн.",
}

CITY_TAGS = [
    ("novi beograd", "#белград"), ("beograd", "#белград"), ("belgrade", "#белград"),
    ("белград", "#белград"), ("novi sad", "#новисад"), ("нови сад", "#новисад"),
    ("niš", "#ниш"), ("nis", "#ниш"), ("subotica", "#суботица"),
    ("kragujevac", "#крагуевац"), ("zlatibor", "#златибор"), ("pančevo", "#панчево"),
    ("pancevo", "#панчево"), ("zemun", "#земун"), ("šabac", "#шабац"),
    ("sombor", "#сомбор"), ("čačak", "#чачак"), ("kraljevo", "#кралево"),
    ("vrnjačka", "#врнячкабаня"), ("smederevo", "#смедерево"), ("zrenjanin", "#зренянин"),
]


def log(message):
    print(message, flush=True)


def fetch_4zida(kind, deal, pages=2):
    result = []
    for page in range(1, pages + 1):
        url = f"https://api.4zida.rs/v6/search/{kind}?for={deal}&page={page}&sort=createdAtDesc"
        data = json.loads(S.http_get(url))
        ads = data.get("ads", []) if isinstance(data, dict) else []
        for ad in ads:
            image = (ad.get("image") or {}).get("search") or {}
            photo = image.get("380x0_fill_0_jpeg") or image.get("380x0_fill_0_webp")
            places = [name for name in (ad.get("placeNames") or [])
                      if name.lower() not in ("gradske lokacije", "okolne lokacije")]
            floor = ad.get("redactedFloor")
            total_floors = ad.get("redactedTotalFloors")
            row = S.row(
                "4zida", ad.get("id"), "https://www.4zida.rs" + (ad.get("urlPath") or ""),
                ad.get("title"), S.to_int(ad.get("price")), S.to_float(ad.get("m2")),
                ad.get("roomCount"), ", ".join(places[:3]), ad.get("createdAt"), photo,
            )
            row["address"] = ad.get("address")
            row["desc"] = ad.get("description100")
            row["extra"] = f"этаж {floor}/{total_floors}" if floor is not None and total_floors else None
            if kind == "houses" and (ad.get("lotSize") or ad.get("lotArea")):
                row["extra"] = f"участок {ad.get('lotSize') or ad.get('lotArea')} ар"
            result.append(row)
        if not ads:
            break
    return result


def tg_call(method, payload):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        headers={"User-Agent": S.UA},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            return json.loads(error.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "description": str(error)}
    except Exception as error:
        return {"ok": False, "description": str(error)}


_last_send = 0.0


def paced_telegram_call(method, payload, label):
    global _last_send
    for attempt in range(2):
        wait = MIN_POST_INTERVAL - (time.time() - _last_send)
        if wait > 0:
            time.sleep(wait)
        _last_send = time.time()
        response = tg_call(method, payload)
        if response.get("error_code") == 429 and attempt == 0:
            retry = int((response.get("parameters") or {}).get("retry_after", 30)) + 1
            log(f"  ! {label}: Telegram просит подождать {retry} с")
            time.sleep(retry)
            continue
        return response
    return response


def original_text(r):
    """Неформатированное содержание, которое доступно сборщику из источника."""
    if r["source"].startswith("tg_"):
        text = (r.get("text") or "").strip()
        links = []
        for link in (r.get("ext_links") or []):
            if link and link.strip() and link.strip() not in links:
                links.append(link.strip())
        source_url = (r.get("url") or "").strip()
        if source_url and source_url not in links:
            links.append(source_url)
        for link in links:
            if link not in text:
                text = f"{text}\n{link}" if text else link
        return text or "Объявление без исходного текста"

    parts = []
    if r.get("title"):
        parts.append(str(r["title"]).strip())
    if r.get("desc"):
        parts.append(str(r["desc"]).strip())
    if r.get("price") is not None:
        parts.append(f"Цена: {fmt_money(r['price'])} €")
    if r.get("m2") is not None:
        parts.append(f"Площадь: {fmt_money(r['m2'])} м²")
    if r.get("rooms") is not None:
        parts.append(f"Комнат: {r['rooms']}")
    if r.get("place"):
        parts.append(f"Место: {r['place']}")
    if r.get("address"):
        parts.append(f"Адрес: {r['address']}")
    if r.get("url"):
        parts.append(r["url"])
    return "\n\n".join(parts)[:4000] or "Объявление без исходного текста"


def send_original(r):
    """Сначала отправляет исходный вариант. False = основной пост запрещён."""
    text = original_text(r)
    if r.get("photo"):
        response = paced_telegram_call(
            "sendPhoto",
            {"chat_id": ORIGINAL_CHAT_ID, "photo": r["photo"], "caption": text[:1024]},
            "оригинал",
        )
        if response.get("ok"):
            return True
        log(f"  ! оригинал: фото не отправлено: {response.get('description', response)}")

    response = paced_telegram_call(
        "sendMessage",
        {"chat_id": ORIGINAL_CHAT_ID, "text": text[:4000], "disable_web_page_preview": False},
        "оригинал",
    )
    if response.get("ok"):
        return True

    log(f"  ! оригинал НЕ отправлен в {ORIGINAL_CHAT_ID}: {response.get('description', response)}")
    return False


def send_formatted(text, photo):
    if photo:
        response = paced_telegram_call(
            "sendPhoto",
            {"chat_id": GROUP_CHAT_ID, "photo": photo, "caption": text[:1024], "parse_mode": "HTML"},
            "форматированный пост",
        )
        if response.get("ok"):
            return response
        log(f"  ! фото форматированного поста не отправлено: {response.get('description', response)}")

    return paced_telegram_call(
        "sendMessage",
        {"chat_id": GROUP_CHAT_ID, "text": text[:4000], "parse_mode": "HTML"},
        "форматированный пост",
    )


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as file:
            state = json.load(file)
    else:
        state = {}
    state.setdefault("posted", {})
    state.setdefault("watermark", {})
    return state


def save_state(state):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    state["posted"]
