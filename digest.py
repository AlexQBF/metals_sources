#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот-дайджест по Telegram-каналам (тестовый режим — без ИИ).

Что делает:
  1. Читает список каналов из channels.json
  2. Для каждого канала заходит на публичную веб-версию https://t.me/s/<username>
  3. Берёт ПОСЛЕДНИЙ пост канала
  4. Собирает всё списком и отправляет в твой Telegram-канал

ИИ пока не подключён. Когда будет API-ключ — добавим слой обработки
(фильтр золото/серебро, важность, склейка дублей, краткая выжимка).

Секреты берутся из переменных окружения (в GitHub Actions — Secrets):
  TELEGRAM_BOT_TOKEN  — токен бота из @BotFather
  TELEGRAM_CHAT_ID    — id канала/группы, куда слать дайджест
"""

import os
import re
import time
import html
import json

import requests
from bs4 import BeautifulSoup

CHANNELS_FILE = "channels.json"
REQUEST_TIMEOUT = 20
MAX_POST_CHARS = 500   # обрезаем длинные посты в тестовом режиме

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()


# ---------- 1. Список каналов ----------
def load_channels():
    with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for ch in data.get("channels", []):
        if ch.get("enabled", True) and ch.get("username"):
            out.append({
                "name": ch.get("name", ch["username"]),
                "username": ch["username"].lstrip("@"),
            })
    return out


# ---------- 2. Чтение последнего поста канала ----------
def fetch_last_post(username):
    """
    Возвращает (text, url) последнего поста публичного канала
    через веб-версию t.me/s/<username>. None, если не удалось.
    """
    url = f"https://t.me/s/{username}"
    resp = requests.get(
        url,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "Mozilla/5.0 (compatible; DigestBot/1.0)"},
    )
    if resp.status_code != 200:
        print(f"[!] {username}: HTTP {resp.status_code}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    # каждый пост — блок с классом tgme_widget_message_wrap; берём последний
    wraps = soup.select(".tgme_widget_message_wrap")
    if not wraps:
        print(f"[!] {username}: постов не найдено (канал приватный или пуст)")
        return None

    last = wraps[-1]

    # текст поста
    text_block = last.select_one(".tgme_widget_message_text")
    if text_block:
        # переносы строк <br> -> \n
        for br in text_block.find_all("br"):
            br.replace_with("\n")
        text = text_block.get_text().strip()
    else:
        text = "(пост без текста — фото/видео/файл)"

    # ссылка на конкретный пост
    msg_div = last.select_one(".tgme_widget_message")
    post_url = ""
    if msg_div and msg_div.get("data-post"):
        post_url = "https://t.me/" + msg_div["data-post"]

    return {"text": text, "url": post_url}


def collect_posts(channels):
    results = []
    for ch in channels:
        try:
            post = fetch_last_post(ch["username"])
        except Exception as e:
            print(f"[!] {ch['name']} (@{ch['username']}): ошибка — {e}")
            post = None

        if post:
            print(f"[i] {ch['name']}: пост получен")
            results.append({
                "name": ch["name"],
                "username": ch["username"],
                "text": post["text"],
                "url": post["url"],
            })
        else:
            results.append({
                "name": ch["name"],
                "username": ch["username"],
                "text": None,
                "url": "",
            })
        time.sleep(0.5)  # вежливая пауза между каналами
    return results


# ---------- 3. Формирование дайджеста (тестовый режим) ----------
def build_digest(posts):
    lines = ["📥 <b>Последние посты из каналов</b> (тестовый режим, без ИИ)\n"]
    for p in posts:
        title = html.escape(p["name"])
        if p["text"] is None:
            lines.append(f"<b>{title}</b>\n— не удалось прочитать\n")
            continue
        text = p["text"].strip()
        if len(text) > MAX_POST_CHARS:
            text = text[:MAX_POST_CHARS].rstrip() + "…"
        text = html.escape(text)
        link = f"\n<a href=\"{html.escape(p['url'])}\">Открыть пост</a>" if p["url"] else ""
        lines.append(f"<b>{title}</b>\n{text}{link}\n")
    return "\n".join(lines)


# ---------- 4. Отправка в Telegram ----------
def send_to_telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        print("[!] Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — вывод в лог.")
        print("---- Дайджест ----")
        print(text)
        return

    LIMIT = 3800
    chunks = []
    current = ""
    for line in text.split("\n"):
        while len(line) > LIMIT:
            if current:
                chunks.append(current); current = ""
            chunks.append(line[:LIMIT]); line = line[LIMIT:]
        if len(current) + len(line) + 1 > LIMIT:
            chunks.append(current); current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        chunks.append(current)

    for chunk in chunks:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={
                "chat_id": TG_CHAT,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
            timeout=30,
        )
        if not resp.ok:
            print(f"[!] Ошибка отправки в Telegram: {resp.status_code} {resp.text}")
        time.sleep(1)


# ---------- Точка входа ----------
def main():
    channels = load_channels()
    print(f"[i] Каналов к обходу: {len(channels)}")
    posts = collect_posts(channels)
    ok = sum(1 for p in posts if p["text"] is not None)
    print(f"[i] Успешно прочитано: {ok} из {len(channels)}")
    digest = build_digest(posts)
    send_to_telegram(digest)
    print("[i] Готово.")


if __name__ == "__main__":
    main()
