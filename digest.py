#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот-дайджест по золоту и серебру из Telegram-каналов, с обработкой через Gemini.

Поток:
  1. Читает список каналов из channels.json
  2. Для каждого канала берёт посты за последние 24 часа (t.me/s/<username>)
  3. Отсеивает посты, которые уже отправлялись (журнал sent.json)
  4. Отдаёт новые посты в Gemini: отбор по золоту/серебру, склейка дублей,
     оценка важности, связный дайджест абзацами (с учётом тем прошлых дайджестов)
  5. Шлёт дайджест в Telegram-канал
  6. Сохраняет дайджест в архив digests/ и обновляет журналы

Без AI_API_KEY работает в тестовом режиме (сырой список постов).

Секреты (GitHub Actions Secrets):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  AI_API_KEY   — ключ Gemini
  AI_BASE_URL  — https://generativelanguage.googleapis.com/v1beta/openai
  AI_MODEL     — gemini-2.5-flash
"""

import os
import re
import json
import time
import html
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

# ---------- Настройки ----------
CHANNELS_FILE = "channels.json"
SENT_FILE = "sent.json"                 # журнал отправленных постов (память)
RECENT_DIGESTS_FILE = "recent_digests.json"  # краткие темы прошлых дайджестов
DIGESTS_DIR = "digests"                 # архив дайджестов по датам
HOURS_WINDOW = 24
MAX_POSTS_TO_AI = 120
RECENT_DIGESTS_KEEP = 5                 # сколько прошлых дайджестов помнить (скользящее окно)
REQUEST_TIMEOUT = 20

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

AI_KEY = os.environ.get("AI_API_KEY", "").strip()
AI_BASE = os.environ.get("AI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai").strip().rstrip("/")
AI_MODEL = os.environ.get("AI_MODEL", "gemini-2.5-flash").strip()

MSK = timezone(timedelta(hours=3))


# ---------- Загрузка/сохранение журналов ----------
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_channels():
    data = load_json(CHANNELS_FILE, {"channels": []})
    out = []
    for ch in data.get("channels", []):
        if ch.get("enabled", True) and ch.get("username"):
            out.append({"name": ch.get("name", ch["username"]),
                        "username": ch["username"].lstrip("@")})
    return out


# ---------- Сбор постов за сутки ----------
def parse_post_time(msg_div):
    t = msg_div.select_one("time[datetime]")
    if t and t.get("datetime"):
        try:
            return datetime.fromisoformat(t["datetime"].replace("Z", "+00:00"))
        except Exception:
            return None
    return None


def fetch_channel_posts(username, cutoff):
    url = f"https://t.me/s/{username}"
    resp = requests.get(url, timeout=REQUEST_TIMEOUT,
                        headers={"User-Agent": "Mozilla/5.0 (compatible; DigestBot/1.0)"})
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
        if ts and ts < cutoff:
            continue
        tb = wrap.select_one(".tgme_widget_message_text")
        if tb:
            for br in tb.find_all("br"):
                br.replace_with("\n")
            text = tb.get_text().strip()
        else:
            text = ""
        if not text:
            continue
        posts.append({
            "id": post_id,
            "url": "https://t.me/" + post_id if post_id else "",
            "text": text,
            "ts": ts.isoformat() if ts else "",
        })
    return posts


def collect_all(channels, sent_ids):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_WINDOW)
    all_posts = []
    for ch in channels:
        try:
            posts = fetch_channel_posts(ch["username"], cutoff)
        except Exception as e:
            print(f"[!] {ch['name']} (@{ch['username']}): ошибка — {e}")
            continue
        fresh = [p for p in posts if p["id"] and p["id"] not in sent_ids]
        print(f"[i] {ch['name']}: за сутки {len(posts)}, новых {len(fresh)}")
        for p in fresh:
            p["source"] = ch["name"]
            all_posts.append(p)
        time.sleep(0.4)
    print(f"[i] Итого новых постов за сутки: {len(all_posts)}")
    return all_posts[:MAX_POSTS_TO_AI]


# ---------- Обработка через Gemini ----------
AI_SYSTEM = (
    "Ты — редактор ежедневного дайджеста по рынку драгоценных металлов (золото и серебро). "
    "Тебе дают посты из отраслевых Telegram-каналов за сутки и список тем, которые уже "
    "освещались в прошлых дайджестах. Сделай следующее:\n"
    "1. Оставь только то, что относится к ЗОЛОТУ и СЕРЕБРУ (цены, спрос/предложение, "
    "добыча, прогнозы, отчёты, решения регуляторов по этим металлам). Остальное "
    "(сталь, уголь, нефть, медь, общие темы, реклама) — отбрось.\n"
    "2. Склей дубли: одно событие из разных каналов = один пункт.\n"
    "3. Не повторяй то, что уже было в прошлых дайджестах (список тем дан ниже), "
    "кроме случаев, когда есть существенно новое развитие.\n"
    "4. Оцени важность, отбери значимое, отсортируй от важного к менее важному.\n"
    "5. Напиши связный дайджест на русском: каждый пункт — заголовок жирным (<b>...</b>) "
    "и абзац в 1–3 предложения, что произошло и почему важно. Между пунктами пустая строка.\n"
    "Не используй Markdown, только HTML-теги <b>. Без вступлений и заключений.\n"
    "Если значимого по золоту/серебру нет — верни ровно: НЕТ_НОВОСТЕЙ"
)


def make_digest_ai(posts, recent_topics):
    blocks = []
    for i, p in enumerate(posts, 1):
        txt = p["text"]
        if len(txt) > 800:
            txt = txt[:800] + "…"
        blocks.append(f"[{i}] Канал: {p['source']}\nСсылка: {p['url']}\nТекст: {txt}")
    user = "ПОСТЫ ЗА СУТКИ:\n\n" + "\n\n".join(blocks)
    if recent_topics:
        user += "\n\nТЕМЫ ПРОШЛЫХ ДАЙДЖЕСТОВ (не повторять):\n" + "\n".join(f"- {t}" for t in recent_topics)

    payload = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content": AI_SYSTEM},
            {"role": "user", "content": user},
        ],
        "temperature": 0.4,
    }
    resp = requests.post(
        f"{AI_BASE}/chat/completions",
        headers={"Authorization": f"Bearer {AI_KEY}", "Content-Type": "application/json"},
        json=payload, timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def make_stub(posts):
    if not posts:
        return "За последние сутки новых постов не собрано."
    lines = ["<b>⚠️ Тестовый режим (ИИ не подключён)</b>",
             "Новые посты за сутки:\n"]
    for p in posts[:40]:
        t = p["text"].strip().replace("\n", " ")
        if len(t) > 200:
            t = t[:200] + "…"
        lines.append(f"• <b>{html.escape(p['source'])}</b>: {html.escape(t)}")
    return "\n".join(lines)


# ---------- Отправка в Telegram ----------
def send_to_telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        print("[!] Нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — вывод в лог:")
        print(text)
        return
    LIMIT = 3800
    chunks, current = [], ""
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
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": TG_CHAT, "text": chunk, "parse_mode": "HTML",
                                "disable_web_page_preview": "true"}, timeout=30)
        if not r.ok:
            print(f"[!] Ошибка отправки: {r.status_code} {r.text}")
        time.sleep(1)


# ---------- Точка входа ----------
def main():
    now_msk = datetime.now(MSK)
    header = f"📊 <b>Дайджест по золоту и серебру</b>\n{now_msk.strftime('%d.%m.%Y')}\n\n"

    channels = load_channels()
    sent = load_json(SENT_FILE, {"ids": []})
    sent_ids = set(sent.get("ids", []))
    recent = load_json(RECENT_DIGESTS_FILE, {"topics": []})

    print(f"[i] Каналов: {len(channels)}, в памяти отправленных: {len(sent_ids)}")
    posts = collect_all(channels, sent_ids)

    if AI_KEY:
        try:
            body = make_digest_ai(posts, recent.get("topics", []))
        except Exception as e:
            print(f"[!] Ошибка Gemini: {e} — отправляю заглушку.")
            body = make_stub(posts)
    else:
        body = make_stub(posts)

    if body.strip() == "НЕТ_НОВОСТЕЙ":
        body = "За последние сутки существенных новостей по золоту и серебру не найдено."

    send_to_telegram(header + body)

    # --- обновляем журналы ---
    new_ids = [p["id"] for p in posts if p["id"]]
    sent_ids.update(new_ids)
    # храним последние ~2000 id, чтобы файл не рос бесконечно
    save_json(SENT_FILE, {"ids": list(sent_ids)[-2000:]})

    # архив дайджеста
    if AI_KEY and body and "не найдено" not in body and "не собрано" not in body:
        os.makedirs(DIGESTS_DIR, exist_ok=True)
        fname = os.path.join(DIGESTS_DIR, now_msk.strftime("%Y-%m-%d") + ".md")
        with open(fname, "w", encoding="utf-8") as f:
            f.write(f"# Дайджест {now_msk.strftime('%d.%m.%Y')}\n\n" + body)
        # запоминаем тему дайджеста (первые строки) для анти-повторов
        topics = recent.get("topics", [])
        # берём заголовки <b>...</b> из тела
        heads = re.findall(r"<b>(.*?)</b>", body)
        topics = (topics + heads)[-RECENT_DIGESTS_KEEP * 5:]
        save_json(RECENT_DIGESTS_FILE, {"topics": topics})

    print("[i] Готово.")


if __name__ == "__main__":
    main()
