#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот «Мониторинг недвижимости Сербии» для Telegram-канала.

Что делает:
  1. Забирает свежие объявления (продажа + аренда квартир) с портала 4zida.rs
     через его открытый API.
  2. Фильтрует: только Белград (можно изменить), только с ценой,
     только опубликованные за последние LOOKBACK_HOURS часов.
  3. Отсекает дубли (файл состояния posted.json).
  4. Публикует до MAX_POSTS_PER_KIND продаж и столько же аренд в канал через Telegram Bot API
     (без ссылок на источник — вместо них призыв писать в личку CONTACT).

Запуск:  python3 bot.py
Настройка: заполните BOT_TOKEN ниже (получить у @BotFather) и при желании
измените CHANNEL, фильтры и частоту в планировщике (cron / Планировщик задач).
"""

import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬТЕ_ТОКЕН_БОТА")  # от @BotFather
CHANNEL = "@serbia_hays"          # канал, куда публикуем (бот должен быть админом)
LOOKBACK_HOURS = 13               # искать объявления не старше N часов (утренний запуск покрывает ночь)
MAX_POSTS_PER_KIND = 5            # максимум постов на категорию (5 продаж + 5 аренд)
CONTACT = "@fortyna1239"          # личка для связи (вместо ссылки на источник)
PAGES_TO_SCAN = 10                # сколько страниц выдачи сканировать на категорию
PAUSE_BETWEEN_POSTS = 25          # секунд между постами
CITY_KEYWORDS = ()                # () = вся Сербия; можно ограничить, например ("beograd",)
PRIORITY_KEYWORDS = ("beograd",)  # эти локации публикуются в первую очередь
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "posted.json")

CATEGORIES = [
    ("sale", "Продажа", "#продажа"),
    ("rent", "Аренда", "#аренда"),
]
# ===============================================

API = "https://api.4zida.rs/v6/search/apartments"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

ROOMS_RU = {0.5: "студия", 1: "1-комн.", 1.5: "1.5-комн.", 2: "2-комн.",
            2.5: "2.5-комн.", 3: "3-комн.", 3.5: "3.5-комн.", 4: "4-комн.",
            5: "5+ комн."}

CITY_TAGS = {"beograd": "#белград", "novi-sad": "#новисад", "nis": "#ниш",
             "subotica": "#суботица", "kragujevac": "#крагуевац", "zlatibor": "#златибор",
             "pancevo": "#панчево", "zemun": "#земун", "sabac": "#шабац",
             "sombor": "#сомбор", "cacak": "#чачак", "kraljevo": "#кралево",
             "vrnjacka-banja": "#врнячкабаня", "smederevo": "#смедерево"}


def city_tag_from_path(url_path):
    slug = url_path.strip("/").split("/")[1] if url_path.count("/") >= 2 else ""
    for key, tag in CITY_TAGS.items():
        if slug.endswith(key) or key in slug:
            return tag
    return "#сербия"


def http_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def tg_call(method, payload):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=data, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


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


def fetch_fresh_ads(deal, since):
    """Собирает свежие объявления одной категории со страниц выдачи."""
    fresh = {}
    for page in range(1, PAGES_TO_SCAN + 1):
        try:
            data = http_json(f"{API}?for={deal}&page={page}")
        except Exception as e:
            print(f"  ! страница {page} ({deal}): {e}")
            continue
        for ad in data.get("ads", []):
            created = ad.get("createdAt", "")
            url_path = ad.get("urlPath", "") or ""
            price = ad.get("price")
            if not created or not price:
                continue
            if CITY_KEYWORDS and not any(k in url_path for k in CITY_KEYWORDS):
                continue
            try:
                dt = datetime.fromisoformat(created)
            except ValueError:
                continue
            if dt < since:
                continue
            fresh[ad["id"]] = ad
        time.sleep(1)
    return list(fresh.values())


def fingerprint(ad):
    return f"{ad.get('price')}|{ad.get('m2')}|{(ad.get('address') or '').lower()}"


def district_from_path(url_path):
    parts = url_path.strip("/").split("/")
    if len(parts) >= 2:
        d = parts[1].replace("-", " ").title()
        # убираем служебные хвосты вроде «Opstina Beograd»
        for tail in (" Opstina Beograd", " Beograd", " Gradske Lokacije"):
            if d.endswith(tail):
                d = d[: -len(tail)]
        return d.strip() or "Белград"
    return "Белград"


def format_post(ad, deal_ru, hashtag):
    rooms = ROOMS_RU.get(ad.get("roomCount"), f"{ad.get('roomCount', '?')}-комн.")
    m2 = ad.get("m2", "?")
    price = ad.get("price", 0)
    district = district_from_path(ad.get("urlPath", ""))
    address = ad.get("address") or district
    url = "https://www.4zida.rs" + ad.get("urlPath", "")
    floor = ad.get("redactedFloor")
    total_floors = ad.get("redactedTotalFloors")
    desc = re.sub(r"\s+", " ", ad.get("description100") or "").strip()

    address = re.sub(r"\bOpstina\s+", "", address).strip() or district
    ppm2 = ad.get("pricePerM2")
    ppm2_txt = f" ({ppm2:,.0f} €/м²)".replace(",", " ") if ppm2 else ""
    lines = [f"🏠 <b>{deal_ru}: {rooms}, {m2} м² — {address}</b>",
             f"📐 {m2} м² · {rooms}" + (f" · этаж {floor}/{total_floors}" if floor is not None and total_floors else "")]
    if str(ad.get("for")) == "sale":
        lines.append(f"💶 <b>{price:,.0f} €</b>".replace(",", " ") + ppm2_txt)
    else:
        lines.append(f"💶 <b>{price:,.0f} €/мес</b>".replace(",", " ") + ppm2_txt)
    lines.append(f"📍 {address}")
    if desc:
        lines.append(desc[:120])
    lines.append(f"📩 Подробности и просмотр — пишите в личку: {CONTACT}")
    city_tag = city_tag_from_path(ad.get("urlPath", ""))
    tags = f"{hashtag} {city_tag}" + (" #сербия" if city_tag != "#сербия" else "")
    lines.append(tags)
    return "\n".join(lines), url


def ad_photo(ad):
    img = ad.get("image") or {}
    search = img.get("search") or {}
    for key in ("380x0_fill_0_jpeg", "380x0_fill_0_webp"):
        if search.get(key):
            return search[key]
    return None


def main():
    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    state = load_state()
    posted_now = 0

    for deal, deal_ru, hashtag in CATEGORIES:
        posted_in_kind = 0
        print(f"Категория: {deal_ru}")
        ads = fetch_fresh_ads(deal, since)
        # приоритетные города и объявления с фото — первыми
        ads.sort(key=lambda a: (
            not any(k in (a.get("urlPath") or "") for k in PRIORITY_KEYWORDS),
            ad_photo(a) is None,
            a.get("createdAt", ""),
        ))
        print(f"  найдено свежих: {len(ads)}")

        for ad in ads:
            if posted_in_kind >= MAX_POSTS_PER_KIND:
                break
            key = ad["id"]
            fp = fingerprint(ad)
            if key in state["posted"] or fp in state["posted"]:
                continue
            text, url = format_post(ad, deal_ru, hashtag)
            try:
                photo = ad_photo(ad)
                if photo:
                    resp = tg_call("sendPhoto", {"chat_id": CHANNEL, "photo": photo,
                                                 "caption": text, "parse_mode": "HTML"})
                else:
                    resp = tg_call("sendMessage", {"chat_id": CHANNEL, "text": text,
                                                   "parse_mode": "HTML",
                                                   "disable_web_page_preview": "false"})
                if resp.get("ok"):
                    now = datetime.now(timezone.utc).isoformat()
                    state["posted"][key] = now
                    state["posted"][fp] = now
                    posted_now += 1
                    posted_in_kind += 1
                    print(f"  ✓ опубликовано: {url}")
                    save_state(state)
                    time.sleep(PAUSE_BETWEEN_POSTS)
                else:
                    print(f"  ! Telegram отказал: {resp}")
            except Exception as e:
                print(f"  ! ошибка публикации: {e}")

    print(f"Готово. Опубликовано за запуск: {posted_now}")


if __name__ == "__main__":
    main()
