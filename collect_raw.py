#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
СБОРЩИК СЫРЫХ ПОСТОВ (диагностика полноты сбора).

ВАЖНО: это отдельный, ИЗОЛИРОВАННЫЙ скрипт для проверки.
Он НЕ трогает рабочий бот:
  - НЕ шлёт в Telegram
  - НЕ обращается к Gemini
  - НЕ читает и НЕ меняет sent.json / recent_digests.json
  - НЕ коммитит в репозиторий
  - запускается только вручную

Что делает: берёт каналы из channels.json, обходит t.me/s/<username>,
собирает ВСЕ посты (без фильтрации) начиная с даты START_DATE и складывает
в один файл raw_posts.md. Файл выгружается как артефакт (скачиваешь и
скармливаешь своей ИИ для проверки).

Ограничение Telegram: t.me/s/ отдаёт только последние ~16-20 постов на канал.
Более ранние посты быстрых каналов недоступны без авторизации.
"""

import json
import time
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

# С какой даты собирать (включительно), в формате ГГГГ-ММ-ДД
START_DATE = "2026-06-24"

CHANNELS_FILE = "channels.json"
OUTPUT_FILE = "raw_posts.md"
REQUEST_TIMEOUT = 20


def load_channels():
    with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for ch in data.get("channels", []):
        if ch.get("enabled", True) and ch.get("username"):
            out.append({"name": ch.get("name", ch["username"]),
                        "username": ch["username"].lstrip("@")})
    return out


def parse_post_time(wrap):
    t = wrap.select_one("time[datetime]")
    if t and t.get("datetime"):
        try:
            return datetime.fromisoformat(t["datetime"].replace("Z", "+00:00"))
        except Exception:
            return None
    return None


def fetch_channel_posts(username, cutoff):
    url = f"https://t.me/s/{username}"
    resp = requests.get(url, timeout=REQUEST_TIMEOUT,
                        headers={"User-Agent": "Mozilla/5.0 (compatible; RawCollector/1.0)"})
    if resp.status_code != 200:
        print(f"[!] @{username}: HTTP {resp.status_code}")
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    posts = []
    for wrap in soup.select(".tgme_widget_message_wrap"):
        msg = wrap.select_one(".tgme_widget_message")
        if not msg:
            continue
        post_id = msg.get("data-post", "")
        ts = parse_post_time(wrap)
        # берём только посты не раньше cutoff (если дата есть)
        if ts and ts < cutoff:
            continue
        tb = wrap.select_one(".tgme_widget_message_text")
        if tb:
            for br in tb.find_all("br"):
                br.replace_with("\n")
            text = tb.get_text().strip()
        else:
            text = "(пост без текста — фото/видео/файл)"
        posts.append({
            "id": post_id,
            "url": "https://t.me/" + post_id if post_id else "",
            "text": text,
            "ts": ts.isoformat() if ts else "(дата неизвестна)",
        })
    return posts


def main():
    # cutoff = начало START_DATE по Москве, переводим в UTC
    msk = timezone(timedelta(hours=3))
    start_local = datetime.strptime(START_DATE, "%Y-%m-%d").replace(tzinfo=msk)
    cutoff = start_local.astimezone(timezone.utc)

    channels = load_channels()
    print(f"[i] Каналов: {len(channels)}, собираем посты с {START_DATE} (МСК)")

    total = 0
    lines = [f"# Сырые посты с {START_DATE}",
             f"Собрано: {datetime.now(msk).strftime('%d.%m.%Y %H:%M МСК')}",
             f"Каналов обойдено: {len(channels)}",
             "",
             "ВНИМАНИЕ: t.me/s/ отдаёт только последние ~20 постов на канал, "
             "поэтому ранние посты быстрых каналов могут отсутствовать.",
             "", "---", ""]

    for ch in channels:
        try:
            posts = fetch_channel_posts(ch["username"], cutoff)
        except Exception as e:
            print(f"[!] {ch['name']} (@{ch['username']}): ошибка — {e}")
            lines.append(f"## {ch['name']} (@{ch['username']})\n_ошибка чтения: {e}_\n")
            continue

        print(f"[i] {ch['name']}: собрано {len(posts)}")
        lines.append(f"## {ch['name']} (@{ch['username']}) — постов: {len(posts)}\n")
        for p in posts:
            total += 1
            lines.append(f"**[{p['ts']}]** {p['url']}")
            lines.append(p["text"])
            lines.append("")
        lines.append("---\n")
        time.sleep(0.4)

    lines.insert(4, f"**Всего собрано постов: {total}**")
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"[i] Готово. Всего постов: {total}. Файл: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
