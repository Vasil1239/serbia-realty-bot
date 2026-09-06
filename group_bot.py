#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот «Поиск квартир 1».

Обычный режим:
  - публикует только новые объявления;
  - сначала отправляет исходное доступное содержание в ORIGINAL_CHAT_ID;
  - затем отправляет форматированную версию в GROUP_CHAT_ID.

Контролируемый тест:
  FORCE_RUN=1
  - берёт ровно ОДНО самое свежее найденное объявление;
  - игнорирует дедупликацию только для этого одного теста;
  - сначала шлёт оригинал в ORIGINAL_CHAT_ID;
  - при успешной доставке шлёт форматированный вариант в GROUP_CHAT_ID;
  - НЕ записывает тестовое объявление в group_posted.json.

Переменные окружения:
  BOT_TOKEN         — токен Telegram-бота
  GROUP_CHAT_ID     — основной чат форматированных объявлений
  ORIGINAL_CHAT_ID  — чат исходных объявлений, по умолчанию -1004325987530
  FORCE_RUN=1       — один контролируемый тест; не использовать для обычного запуска
"""

import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import sources as S

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GROUP_CHAT_ID = os.environ.get("GROUP_CHAT_ID", "")
ORIGINAL_CHAT_ID = os.environ.get("ORIGINAL_CHAT_ID", "-1004325987530")
FORCE_RUN = os.environ.get("FORCE_RUN", "") == "1"

LOCAL_TZ = ZoneInfo("Europe/Belgrade")
POST_WINDOW_LOCAL = (9, 22)
LOOKBACK_HOURS = 48 if FORCE_RUN else 16
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


def paced_call(method, payload, label):
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


def original_text(row):
    """Не применяет format_post: сохраняет доступный исходный текст и все ссылки."""
    if row["source"].startswith("tg_"):
        text = (row.get("text") or "").strip()
        links = []
        for link in (row.get("ext_links") or []):
            clean = str(link).strip()
            if clean and clean not in links:
                links.append(clean)
        source_url = str(row.get("url") or "").strip()
        if source_url and source_url not in links:
            links.append(source_url)
        for link in links:
            if link not in text:
                text = f"{text}\n{link}" if text else link
        return text[:4000] or "Объявление без исходного текста"

    parts = []
    if row.get("title"):
        parts.append(str(row["title"]).strip())
    if row.get("desc"):
        parts.append(str(row["desc"]).strip())
    if row.get("price") is not None:
        parts.append(f"Цена: {fmt_money(row['price'])} €")
    if row.get("m2") is not None:
        parts.append(f"Площадь: {fmt_money(row['m2'])} м²")
    if row.get("rooms") is not None:
        parts.append(f"Комнат: {row['rooms']}")
    if row.get("place"):
        parts.append(f"Место: {row['place']}")
    if row.get("address"):
        parts.append(f"Адрес: {row['address']}")
    if row.get("url"):
        parts.append(str(row["url"]))
    return "\n\n".join(parts)[:4000] or "Объявление без исходного текста"


def send_original(row):
    """Возвращает True только когда ORIGINAL_CHAT_ID подтвердил приём сообщения."""
    text = original_text(row)
    if row.get("photo"):
        response = paced_call(
            "sendPhoto",
            {"chat_id": ORIGINAL_CHAT_ID, "photo": row["photo"], "caption": text[:1024]},
            "оригинал",
        )
        if response.get("ok"):
            log(f"  ✓ оригинал доставлен в {ORIGINAL_CHAT_ID} (фото)")
            return True
        log(f"  ! оригинал: фото не отправлено: {response.get('description', response)}")

    response = paced_call(
        "sendMessage",
        {"chat_id": ORIGINAL_CHAT_ID, "text": text[:4000], "disable_web_page_preview": False},
        "оригинал",
    )
    if response.get("ok"):
        log(f"  ✓ оригинал доставлен в {ORIGINAL_CHAT_ID} (текст)")
        return True

    log(f"  ! оригинал НЕ доставлен в {ORIGINAL_CHAT_ID}: {response.get('description', response)}")
    return False


def send_formatted(text, photo):
    if photo:
        response = paced_call(
            "sendPhoto",
            {"chat_id": GROUP_CHAT_ID, "photo": photo, "caption": text[:1024], "parse_mode": "HTML"},
            "форматированный пост",
        )
        if response.get("ok"):
            return response
        log(f"  ! фото форматированного поста не отправлено: {response.get('description', response)}")
    return paced_call(
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
    state["posted"] = {key: value for key, value in state["posted"].items() if value > cutoff}
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=1)


def parse_dt(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo else result.replace(tzinfo=LOCAL_TZ)


def is_fresh(row, since):
    created = parse_dt(row.get("created"))
    if created is None:
        return False
    if len(str(row.get("created") or "")) <= 10:
        return created.date() >= since.astimezone(LOCAL_TZ).date()
    return created >= since


def kind_matches(row, kind):
    url = str(row.get("url") or "").lower()
    if row["source"] != "nadjidom":
        return True
    return not (("-stan." in url and kind == "houses") or ("-kuca." in url and kind == "apartments"))


def fingerprint(row):
    place = (row.get("place") or row.get("address") or "").lower().split(",")[0].strip()
    m2 = row.get("m2")
    return f"{row.get('price')}|{int(m2) if m2 else '?'}|{place[:20]}"


def esc(value):
    return html.escape(str(value), quote=True) if value else ""


def fmt_money(value):
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def rooms_ru(value):
    if value is None:
        return None
    key = str(value).strip().lower().replace(",", ".")
    if key in ROOMS_RU:
        return ROOMS_RU[key]
    for source, translated in ROOMS_RU.items():
        if source in key:
            return translated
    return f"{key}-комн." if re.fullmatch(r"\d+(\.\d+)?", key) else None


def city_tag(row):
    content = " ".join(str(row.get(key) or "") for key in ("place", "address", "title", "url", "text")).lower()
    for key, tag in CITY_TAGS:
        if key in content:
            return tag
    return "#сербия"


def is_priority(row):
    content = " ".join(str(row.get(key) or "") for key in ("place", "address", "url", "title")).lower()
    return any(keyword in content for keyword in PRIORITY_KEYWORDS)


def source_label(row):
    source = row["source"]
    return f"Telegram @{source[3:]}" if source.startswith("tg_") else SOURCE_NAMES.get(source, source)


def format_post(row, kind, deal, title, hashtags):
    lines = [f"<b>{title}</b> — {esc(row.get('place') or 'Сербия')}"]
    details = []
    if row.get("m2"):
        details.append(f"📐 {fmt_money(row['m2'])} м²")
    translated_rooms = rooms_ru(row.get("rooms"))
    if translated_rooms:
        details.append(translated_rooms)
    if row.get("extra"):
        details.append(str(row["extra"]))
    if details:
        lines.append(" · ".join(details))

    unit = "€" if deal == "sale" else "€/мес"
    if row.get("price"):
        per_m2 = f" ({fmt_money(row['price'] / row['m2'])} €/м²)" if deal == "sale" and row.get("m2") else ""
        lines.append(f"💶 <b>{fmt_money(row['price'])} {unit}</b>{per_m2}")

    address = row.get("address")
    if address and str(address).lower() not in str(row.get("place") or "").lower():
        lines.append(f"📍 {esc(address)}, {esc(row.get('place') or '')}".rstrip(", "))

    description = row.get("desc") or (row.get("title") if row["source"] != "4zida" else None)
    if description:
        lines.append(esc(re.sub(r"\s+", " ", str(description)).strip()[:160]))

    if row["source"].startswith("tg_"):
        links = [html.unescape(str(link)) for link in row.get("ext_links", []) if "maps" not in str(link) and "google" not in str(link)]
        if links:
            host = urllib.parse.urlparse(links[0]).netloc.replace("www.", "")
            lines.append(f'🔗 <a href="{esc(links[0])}">Источник: {esc(host)}</a> · <a href="{esc(row["url"])}">пост в {esc(source_label(row))}</a>')
        else:
            lines.append(f'🔗 <a href="{esc(row["url"])}">Источник: {esc(source_label(row))}</a>')
    else:
        lines.append(f'🔗 <a href="{esc(row["url"])}">Источник: {esc(source_label(row))}</a>')

    tag = city_tag(row)
    lines.append(f"{hashtags} {tag}" + (" #сербия" if tag != "#сербия" else ""))
    return "\n".join(lines)


def tg_category(channel_kind, channel_deal, row):
    text = str(row.get("text") or "").lower()
    if channel_deal == "both":
        deal = "sale" if re.search(r"прода|prodaj|sale|купить", text) else "rent"
    else:
        deal = channel_deal
    first_line = text.split("\n")[0][:80]
    kind = "houses" if re.search(r"\bдом\b|kuć|kuca|\bhouse\b|вилл", first_line) else channel_kind
    return kind, deal


def fetch_site(name, function):
    result = []
    for kind, deal, _, _ in CATEGORIES:
        try:
            result.append((kind, deal, function(kind, deal), None))
        except Exception as error:
            result.append((kind, deal, [], f"{type(error).__name__}: {error}"))
    return name, result


def fetch_channel(channel):
    try:
        return channel, S.fetch_telegram(channel), None
    except Exception as error:
        return channel, [], f"{type(error).__name__}: {error}"


def collect(since, state):
    bucket = {(kind, deal): [] for kind, deal, _, _ in CATEGORIES}
    stats = {}
    with ThreadPoolExecutor(max_workers=COLLECT_WORKERS) as executor:
        dated_jobs = [executor.submit(fetch_site, name, function) for name, function in DATED_SITES.items()]
        watermark_jobs = [executor.submit(fetch_site, name, function) for name, function in WATERMARK_SITES.items()]
        telegram_jobs = [executor.submit(fetch_channel, channel) for channel, _, _ in TG_CHANNELS]
        dated_results = [job.result() for job in dated_jobs]
        watermark_results = [job.result() for job in watermark_jobs]
        telegram_results = [job.result() for job in telegram_jobs]

    for name, category_results in dated_results:
        for kind, deal, rows, error in category_results:
            if error:
                log(f"  ! {name} {kind}/{deal}: {error}")
            items = [row for row in rows if row.get("price") and is_fresh(row, since) and kind_matches(row, kind)]
            stats[name] = stats.get(name, 0) + len(items)
            bucket[(kind, deal)].extend(items)

    for name, category_results in watermark_results:
        for kind, deal, rows, error in category_results:
            if error:
                log(f"  ! {name} {kind}/{deal}: {error}")
                continue
            watermark_key = f"{name}:{kind}:{deal}"
            items = [row for row in rows if row.get("price") and not row.get("promoted")]
            ids = [int(re.sub(r"\D", "", row["id"].split(":", 1)[1]) or 0) for row in items]
            if not ids:
                continue
            watermark = state["watermark"].get(watermark_key)
            if FORCE_RUN:
                new_items = items
            else:
                new_items = [row for row, item_id in zip(items, ids) if watermark is not None and item_id > watermark] if watermark is not None else []
                state["watermark"][watermark_key] = max(ids + [watermark or 0])
            stats[name] = stats.get(name, 0) + len(new_items)
            bucket[(kind, deal)].extend(new_items)

    channel_info = {channel: (kind, deal) for channel, kind, deal in TG_CHANNELS}
    for channel, posts, error in telegram_results:
        if error:
            log(f"  ! @{channel}: {error}")
            continue
        channel_kind, channel_deal = channel_info[channel]
        count = 0
        for row in posts:
            text = str(row.get("text") or "")
            if not row.get("price") or not is_fresh(row, since) or len(text) < 30:
                continue
            if text.startswith("📊") or "owner listings today" in text:
                continue
            kind, deal = tg_category(channel_kind, channel_deal, row)
            row["place"] = row.get("place") or channel_place(channel)
            row["desc"] = re.sub(r"https?://\S+", "", text)
            bucket[(kind, deal)].append(row)
            count += 1
        stats[f"@{channel}"] = count

    log("  найдено по источникам: " + ", ".join(f"{name}={count}" for name, count in stats.items()))
    return bucket


def channel_place(channel):
    name = channel.lower()
    if "novisad" in name or name == "rent_ns":
        return "Novi Sad"
    if "belgrade" in name or "beograd" in name or name == "rent_bg":
        return "Beograd"
    return "Сербия"


def normal_pick(items, state, limit):
    seen_fingerprints = set()
    fresh = []
    for row in items:
        fp = fingerprint(row)
        if row["id"] in state["posted"] or fp in state["posted"] or fp in seen_fingerprints:
            continue
        seen_fingerprints.add(fp)
        fresh.append(row)
    fresh.sort(key=sort_key)

    per_source = {}
    for row in fresh:
        per_source.setdefault(row["source"], []).append(row)
    source_order = sorted(per_source, key=lambda source: (source.startswith("tg_"), source != "4zida"))

    result = []
    while (limit is None or len(result) < limit) and any(per_source.values()):
        for source in source_order:
            if per_source[source] and (limit is None or len(result) < limit):
                result.append(per_source[source].pop(0))
    return result


def sort_key(row):
    created = parse_dt(row.get("created")) or datetime.min.replace(tzinfo=timezone.utc)
    return (not is_priority(row), row.get("photo") is None, -created.timestamp())


def test_pick(bucket):
    """Выбирает только одно самое свежее объявление для FORCE_RUN=1."""
    candidates = []
    for kind, deal, title, hashtags in CATEGORIES:
        for row in bucket[(kind, deal)]:
            candidates.append((row, kind, deal, title, hashtags))
    if not candidates:
        return None
    candidates.sort(key=lambda item: sort_key(item[0]))
    return candidates[0]


def posting_allowed():
    hour = datetime.now(LOCAL_TZ).hour
    return POST_WINDOW_LOCAL[0] <= hour < POST_WINDOW_LOCAL[1]


def publish_one(row, kind, deal, title, hashtags, state, remember):
    log(f"  → источник: {source_label(row)} | {row.get('url')}")
    if not send_original(row):
        log("  ! основной чат: ПРОПУСК, потому что оригинал не доставлен")
        return False

    response = send_formatted(format_post(row, kind, deal, title, hashtags), row.get("photo"))
    if not response.get("ok"):
        log(f"  ! форматированный пост не отправлен: {response.get('description', response)}")
        return False

    if remember:
        now = datetime.now(timezone.utc).isoformat()
        state["posted"][row["id"]] = now
        state["posted"][fingerprint(row)] = now
    log("  ✓ маршрут завершён: оригинал → форматированный пост")
    return True


def main():
    if not BOT_TOKEN or not GROUP_CHAT_ID:
        log("Не заданы BOT_TOKEN / GROUP_CHAT_ID — публикация остановлена.")
        return
    if not ORIGINAL_CHAT_ID:
        log("Не задан ORIGINAL_CHAT_ID — публикация остановлена.")
        return

    now_local = datetime.now(LOCAL_TZ)
    log(f"Местное время (Белград): {now_local:%Y-%m-%d %H:%M}")
    log(f"Оригиналы: {ORIGINAL_CHAT_ID}; форматированные посты: {GROUP_CHAT_ID}")
    if FORCE_RUN:
        log("РЕЖИМ ТЕСТА: будет опубликовано ровно одно объявление без записи в состояние.")
    elif not posting_allowed():
        log("Вне окна публикаций (09:00–22:00) — пропуск запуска.")
        return

    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    state = load_state()
    started = time.time()
    log("Сбор объявлений (параллельно)...")
    bucket = collect(since, state)
    log(f"  сбор занял {time.time() - started:.0f} с")

    if FORCE_RUN:
        selected = test_pick(bucket)
        if not selected:
            log("ТЕСТ НЕ ВЫПОЛНЕН: за последние 48 часов не найдено ни одного подходящего объявления.")
            return
        row, kind, deal, title, hashtags = selected
        log("ТЕСТ: выбрано одно объявление; дедупликация и запись в state отключены только для него.")
        ok = publish_one(row, kind, deal, title, hashtags, state, remember=False)
        if ok:
            log("ТЕСТ УСПЕШЕН: проверьте оба чата. Обычный режим не изменён.")
        else:
            log("ТЕСТ НЕУСПЕШЕН: смотрите строку с ошибкой Telegram выше.")
        return

    save_state(state)
    posted_count = 0
    original_failures = 0
    for kind, deal, title, hashtags in CATEGORIES:
        selected = normal_pick(bucket[(kind, deal)], state, MAX_POSTS_PER_KIND)
        log(f"Категория: {title} — свежих {len(bucket[(kind, deal)])}, к публикации {len(selected)}")
        for row in selected:
            if not posting_allowed():
                log("Вышли за разрешённое время — остаток обработается следующим запуском.")
                save_state(state)
                log(f"Опубликовано: {posted_count}; ошибки оригинала: {original_failures}")
                return
            if publish_one(row, kind, deal, title, hashtags, state, remember=True):
                posted_count += 1
                if posted_count % 20 == 0:
                    save_state(state)
            else:
                original_failures += 1

    save_state(state)
    log(f"Готово. В основной чат: {posted_count}; ошибки оригинала: {original_failures}; всего {time.time() - started:.0f} с")


if __name__ == "__main__":
    main()
    
