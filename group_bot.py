#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот «Новые объявления недвижимости Сербии» для Telegram-ГРУППЫ.

Что делает:
  1. Забирает самые свежие объявления с портала 4zida.rs (открытый API,
     сортировка по дате публикации) по четырём категориям:
     продажа квартир, продажа домов, аренда квартир, аренда домов.
  2. Берёт только объявления не старше LOOKBACK_HOURS часов и с ценой.
  3. Отсекает дубли (файл состояния group_posted.json).
  4. Публикует до MAX_POSTS_PER_KIND объявлений на категорию в группу
     со ссылкой на первоисточник.
  5. Работает только в «дневные» часы по Белграду (RUN_HOURS_LOCAL);
     ночью (22:00–10:00) ничего не публикует.

Переменные окружения:
  BOT_TOKEN      — токен бота (@BotFather)
  GROUP_CHAT_ID  — числовой id группы (например -1001234567890)
  FORCE_RUN=1    — запустить вне расписания (для теста)
"""

import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GROUP_CHAT_ID = os.environ.get("GROUP_CHAT_ID", "")
FORCE_RUN = os.environ.get("FORCE_RUN", "") == "1"

LOCAL_TZ = ZoneInfo("Europe/Belgrade")
RUN_HOURS_LOCAL = {10, 13, 16, 19, 22}   # раз в 3 часа, ночью тишина
LOOKBACK_HOURS = 13                      # утренний запуск покрывает ночь
MAX_POSTS_PER_KIND = 5                   # постов на категорию за запуск (4 категории)
PAGES_TO_SCAN = 3                        # страниц по 20 объявлений (сортировка по новизне)
PAUSE_BETWEEN_POSTS = 4                  # секунд между постами (лимит групп ~20/мин)
PRIORITY_KEYWORDS = ("beograd",)         # эти локации публикуются первыми
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "group_posted.json")

# (тип, сделка, заголовок, хэштеги)
CATEGORIES = [
    ("apartments", "sale", "Продажа квартиры", "#продажа #квартира"),
    ("houses",     "sale", "Продажа дома",     "#продажа #дом"),
    ("apartments", "rent", "Аренда квартиры",  "#аренда #квартира"),
    ("houses",     "rent", "Аренда дома",      "#аренда #дом"),
]
# ===============================================

API = "https://api.4zida.rs/v6/search/{kind}?for={deal}&page={page}&sort=createdAtDesc"
SITE = "https://www.4zida.rs"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

ROOMS_RU = {"0.5": "студия", "1": "1-комн.", "1.5": "1.5-комн.", "2": "2-комн.",
            "2.5": "2.5-комн.", "3": "3-комн.", "3.5": "3.5-комн.", "4": "4-комн.",
            "4.5": "4.5-комн.", "5": "5+ комн."}

CITY_TAGS = {"beograd": "#белград", "novi-sad": "#новисад", "nis": "#ниш",
             "subotica": "#суботица", "kragujevac": "#крагуевац", "zlatibor": "#златибор",
             "pancevo": "#панчево", "zemun": "#земун", "sabac": "#шабац",
             "sombor": "#сомбор", "cacak": "#чачак", "kraljevo": "#кралево",
             "vrnjacka-banja": "#врнячкабаня", "smederevo": "#смедерево",
             "zrenjanin": "#зренянин", "valjevo": "#валево", "loznica": "#лозница"}


def log(msg):
    print(msg, flush=True)


def http_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def tg_call(method, payload):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=data, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "description": str(e)}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"posted": {}}


def save_state(state):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    state["posted"] = {k: v for k, v in state["posted"].items() if v > cutoff}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def fetch_fresh_ads(kind, deal, since):
    """Собирает свежие объявления одной категории (выдача отсортирована по новизне)."""
    fresh = {}
    for page in range(1, PAGES_TO_SCAN + 1):
        try:
            data = http_json(API.format(kind=kind, deal=deal, page=page))
        except Exception as e:
            log(f"  ! страница {page} ({kind}/{deal}): {e}")
            continue
        ads = data.get("ads", []) if isinstance(data, dict) else []
        older_seen = 0
        for ad in ads:
            created = ad.get("createdAt", "")
            if not created or not ad.get("price"):
                continue
            try:
                dt = datetime.fromisoformat(created)
            except ValueError:
                continue
            if dt < since:
                older_seen += 1
                continue
            fresh[ad["id"]] = ad
        # выдача по новизне: если почти вся страница старая — дальше смысла нет
        if not ads or older_seen >= len(ads) - 2:
            break
        time.sleep(1)
    return list(fresh.values())


def fingerprint(ad):
    return f"{ad.get('price')}|{ad.get('m2')}|{(ad.get('address') or '').lower()}"


def place_str(ad):
    names = ad.get("placeNames") or []
    if isinstance(names, str):
        names = [names]
    names = [n for n in names if n and n.lower() not in ("gradske lokacije", "okolne lokacije")]
    return ", ".join(names[:3])


def city_tag(ad):
    path = (ad.get("urlPath") or "").lower()
    for key, tag in CITY_TAGS.items():
        if key in path:
            return tag
    return "#сербия"


def fmt_money(v):
    try:
        return f"{float(v):,.0f}".replace(",", " ")
    except (TypeError, ValueError):
        return str(v)


def format_post(ad, kind, deal, title, hashtags):
    m2 = ad.get("m2", "?")
    price = ad.get("price", 0)
    url = SITE + (ad.get("urlPath") or "")
    place = place_str(ad)
    address = (ad.get("address") or ad.get("title") or "").strip()
    desc = re.sub(r"\s+", " ", ad.get("description100") or "").strip()

    head = f"<b>{title}</b> — {place or 'Сербия'}"
    lines = [head]

    details = [f"📐 {m2} м²"]
    rooms = ad.get("roomCount")
    if rooms is not None:
        details.append(ROOMS_RU.get(str(rooms), f"{rooms}-комн."))
    if kind == "houses":
        lot = ad.get("lotSize") or ad.get("lotArea")
        if lot:
            details.append(f"участок {lot} ар")
    else:
        floor, total = ad.get("redactedFloor"), ad.get("redactedTotalFloors")
        if floor is not None and total:
            details.append(f"этаж {floor}/{total}")
    lines.append(" · ".join(details))

    ppm2 = ad.get("pricePerM2")
    ppm2_txt = f" ({fmt_money(ppm2)} €/м²)" if ppm2 and deal == "sale" else ""
    unit = "€" if deal == "sale" else "€/мес"
    lines.append(f"💶 <b>{fmt_money(price)} {unit}</b>{ppm2_txt}")

    if address:
        names = [n.lower() for n in (ad.get("placeNames") or [])]
        if address.lower() in names:
            lines.append(f"📍 {place}")
        else:
            lines.append(f"📍 {address}" + (f", {place}" if place else ""))
    if desc:
        lines.append(desc[:150])
    lines.append(f'🔗 <a href="{url}">Источник: 4zida.rs</a>')
    tag = city_tag(ad)
    lines.append(f"{hashtags} {tag}" + (" #сербия" if tag != "#сербия" else ""))
    return "\n".join(lines), url


def ad_photo(ad):
    img = ad.get("image") or {}
    search = img.get("search") or {}
    for key in ("380x0_fill_0_jpeg", "380x0_fill_0_webp"):
        if search.get(key):
            return search[key]
    return None


def within_schedule():
    now_local = datetime.now(LOCAL_TZ)
    log(f"Местное время (Белград): {now_local:%Y-%m-%d %H:%M}")
    return now_local.hour in RUN_HOURS_LOCAL


def main():
    if not BOT_TOKEN or not GROUP_CHAT_ID:
        log("Не заданы BOT_TOKEN / GROUP_CHAT_ID — публикация пропущена.")
        return
    if not FORCE_RUN and not within_schedule():
        log("Вне расписания публикаций — пропуск запуска.")
        return

    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    state = load_state()
    posted_now = 0

    for kind, deal, title, hashtags in CATEGORIES:
        posted_in_kind = 0
        log(f"Категория: {title}")
        ads = fetch_fresh_ads(kind, deal, since)
        ads.sort(key=lambda a: (
            not any(k in (a.get("urlPath") or "") for k in PRIORITY_KEYWORDS),
            ad_photo(a) is None,
        ))
        # среди равных — самые новые первыми
        log(f"  найдено свежих: {len(ads)}")

        for ad in ads:
            if posted_in_kind >= MAX_POSTS_PER_KIND:
                break
            key, fp = ad["id"], fingerprint(ad)
            if key in state["posted"] or fp in state["posted"]:
                continue
            text, url = format_post(ad, kind, deal, title, hashtags)
            photo = ad_photo(ad)
            if photo:
                resp = tg_call("sendPhoto", {"chat_id": GROUP_CHAT_ID, "photo": photo,
                                             "caption": text, "parse_mode": "HTML"})
                if not resp.get("ok"):
                    log(f"  ! фото не отправилось ({resp.get('description')}), шлю текстом")
                    resp = tg_call("sendMessage", {"chat_id": GROUP_CHAT_ID, "text": text,
                                                   "parse_mode": "HTML"})
            else:
                resp = tg_call("sendMessage", {"chat_id": GROUP_CHAT_ID, "text": text,
                                               "parse_mode": "HTML"})
            if resp.get("ok"):
                now = datetime.now(timezone.utc).isoformat()
                state["posted"][key] = now
                state["posted"][fp] = now
                posted_now += 1
                posted_in_kind += 1
                log(f"  ✓ опубликовано: {url}")
                save_state(state)
                time.sleep(PAUSE_BETWEEN_POSTS)
            else:
                log(f"  ! Telegram отказал: {resp}")
                if resp.get("error_code") == 429:
                    time.sleep(int(resp.get("parameters", {}).get("retry_after", 30)) + 1)

    log(f"Готово. Опубликовано за запуск: {posted_now}")


if __name__ == "__main__":
    main()
