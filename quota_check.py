#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Автономный контроль бесплатных минут GitHub Actions.

Считает, сколько минут за текущий календарный месяц потратили все workflow
репозитория (по длительности запусков), и при приближении к лимиту бесплатного
плана присылает предупреждение в Telegram-группу. Каждое предупреждение —
один раз в месяц на порог. Кредиты Perplexity не тратит: работает внутри
самого GitHub Actions.

Окружение: GITHUB_TOKEN, GITHUB_REPOSITORY (даёт Actions), BOT_TOKEN, GROUP_CHAT_ID.
"""
import json
import math
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

FREE_MINUTES = 3000                       # GitHub Pro (4 $/мес), делится со всеми приватными репозиториями
THRESHOLDS = (2200, 2700)                 # при каких значениях предупреждать
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "group_posted.json")

TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GROUP_CHAT_ID = os.environ.get("GROUP_CHAT_ID", "")


def gh(url):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}",
                                               "Accept": "application/vnd.github+json",
                                               "User-Agent": "quota-check"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def used_minutes_this_month():
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rng = f"{start:%Y-%m-%d}..{now:%Y-%m-%d}"
    total, page = 0, 1
    while True:
        d = gh(f"https://api.github.com/repos/{REPO}/actions/runs?per_page=100&page={page}"
               f"&created={urllib.parse.quote(rng)}")
        runs = d.get("workflow_runs", [])
        for r in runs:
            if r.get("status") != "completed" or not r.get("run_started_at"):
                continue
            t0 = datetime.fromisoformat(r["run_started_at"].replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(r["updated_at"].replace("Z", "+00:00"))
            total += max(1, math.ceil((t1 - t0).total_seconds() / 60))   # GitHub округляет вверх
        if len(runs) < 100:
            break
        page += 1
    return total


def tg_send(text):
    data = urllib.parse.urlencode({"chat_id": GROUP_CHAT_ID, "text": text,
                                   "disable_web_page_preview": "1"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def main():
    if not (TOKEN and REPO and BOT_TOKEN and GROUP_CHAT_ID):
        print("quota_check: нет переменных окружения, пропуск")
        return
    try:
        if not gh(f"https://api.github.com/repos/{REPO}").get("private", True):
            print("quota_check: репозиторий публичный — минуты Actions не ограничены, проверка не нужна")
            return
    except Exception as e:
        print(f"quota_check: не удалось узнать видимость репозитория ({e}), считаем как приватный")
    used = used_minutes_this_month()
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    print(f"quota_check: за {month} использовано ~{used} мин из {FREE_MINUTES}")

    with open(STATE_FILE, encoding="utf-8") as f:
        state = json.load(f)
    warned = state.setdefault("quota_warned", {}).setdefault(month, [])
    for th in THRESHOLDS:
        if used >= th and th not in warned:
            left = max(0, FREE_MINUTES - used)
            text = (f"⚠️ Служебное: минуты GitHub Actions (план Pro, 3000/мес) заканчиваются.\n"
                    f"Использовано ~{used} из {FREE_MINUTES} мин за {month}, осталось ~{left} мин "
                    f"(примерно {left // 150} дней работы ботов).\n\n"
                    f"Чтобы боты не остановились до конца месяца, выберите один из вариантов:\n"
                    f"1) GitHub → Settings → Billing → Budgets: добавить карту и бюджет на Actions "
                    f"(сверх лимита $0.008/мин, ~$15–20/мес);\n"
                    f"2) временно сократить частоту запусков;\n"
                    f"3) сделать репозиторий публичным — минуты не ограничены, бесплатно.")
            tg_send(text)
            warned.append(th)
            print(f"quota_check: отправлено предупреждение по порогу {th}")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:                # контроль квоты не должен ломать публикацию
        print(f"quota_check: ошибка {type(e).__name__}: {e}")
