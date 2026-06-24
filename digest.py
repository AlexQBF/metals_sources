#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот-дайджест по рынку драгметаллов (золото и серебро).

Что делает:
  1. Читает список RSS-лент из feeds.json
  2. Обходит ленты, собирает материалы за последние 24 часа
  3. Отдаёт собранное ИИ (GPT/Grok через OpenAI-совместимый API):
     - отбирает релевантное по золоту и серебру
     - оценивает важность
     - склеивает дубли (одно событие из разных источников = один пункт)
     - пишет краткий дайджест: топ важных + 1-2 строки по каждому
  4. Отправляет дайджест в Telegram-канал/группу

Без ключа ИИ (AI_API_KEY не задан) работает в РЕЖИМЕ ЗАГЛУШКИ:
собирает материалы и присылает сырой список — чтобы проверить, что механика работает.

Все секреты берутся из переменных окружения (в GitHub Actions — из Secrets):
  TELEGRAM_BOT_TOKEN  — токен бота из @BotFather
  TELEGRAM_CHAT_ID    — id канала/группы, куда слать (напр. -1001234567890)
  AI_API_KEY          — ключ нейросети (можно оставить пустым на старте)
  AI_BASE_URL         — адрес API: OpenAI https://api.openai.com/v1
                        Grok (xAI) https://api.x.ai/v1
  AI_MODEL            — модель, напр. gpt-4o-mini или grok-2-latest
"""

import os
import json
import time
import html
from datetime import datetime, timezone, timedelta

import requests
import feedparser

# ---------- Настройки ----------
HOURS_WINDOW = 24                  # за сколько часов собираем материалы
MAX_ITEMS_TO_AI = 80               # сколько максимум материалов отдаём ИИ (защита от перерасхода)
REQUEST_TIMEOUT = 20               # таймаут запроса к ленте, сек
FEEDS_FILE = "feeds.json"

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

AI_KEY = os.environ.get("AI_API_KEY", "").strip()
AI_BASE = os.environ.get("AI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
AI_MODEL = os.environ.get("AI_MODEL", "gpt-4o-mini").strip()


# ---------- 1. Чтение списка лент ----------
def load_feeds():
    with open(FEEDS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    feeds = []
    for section in ("ru", "intl"):
        for item in data.get(section, []):
            if item.get("enabled", True) and item.get("url"):
                feeds.append({"name": item.get("name", item["url"]), "url": item["url"]})
    return feeds


# ---------- 2. Сбор материалов за последние 24 часа ----------
def entry_time(entry):
    """Достаём дату публикации записи, если есть."""
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def collect_items(feeds):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_WINDOW)
    items = []
    seen_links = set()

    for feed in feeds:
        try:
            resp = requests.get(
                feed["url"],
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0 (DigestBot)"},
            )
            parsed = feedparser.parse(resp.content)
        except Exception as e:
            print(f"[!] Не удалось прочитать {feed['name']}: {e}")
            continue

        for entry in parsed.entries:
            link = entry.get("link", "")
            if link in seen_links:
                continue
            ts = entry_time(entry)
            # если дата есть и старше окна — пропускаем; если даты нет — берём (на всякий случай)
            if ts and ts < cutoff:
                continue
            seen_links.add(link)
            title = html.unescape(entry.get("title", "").strip())
            summary = html.unescape(entry.get("summary", "").strip())
            # обрезаем слишком длинные описания
            if len(summary) > 600:
                summary = summary[:600] + "…"
            items.append({
                "source": feed["name"],
                "title": title,
                "summary": summary,
                "link": link,
            })

    print(f"[i] Собрано материалов за {HOURS_WINDOW} ч: {len(items)}")
    return items[:MAX_ITEMS_TO_AI]


# ---------- 3. Обработка через ИИ ----------
AI_SYSTEM_PROMPT = (
    "Ты — аналитик рынка драгоценных металлов. Тебе дают список материалов "
    "(новости, отчёты, статьи) за последние сутки из разных источников. Твоя задача:\n"
    "1. Оставить только релевантное по ЗОЛОТУ и СЕРЕБРУ (рынок, цены, спрос/предложение, "
    "прогнозы, решения регуляторов, отчёты по этим металлам). Остальное (сталь, уголь, "
    "нефть, прочие темы) — отбросить.\n"
    "2. Склеить дубли: если одно и то же событие освещено в нескольких источниках — "
    "оставить ОДИН пункт.\n"
    "3. Оценить важность и отобрать только действительно значимое (крупные движения цен, "
    "решения ЦБ/регуляторов, важные отчёты и прогнозы). Проходные заметки убрать.\n"
    "4. Отсортировать от самого важного к менее важному.\n\n"
    "Формат ответа (на русском, без вступлений и заключений):\n"
    "Для каждого пункта:\n"
    "• <b>Заголовок своими словами</b>\n"
    "  Одна-две строки: что произошло и почему важно. (Источник)\n\n"
    "Если значимых материалов по золоту/серебру нет — напиши одну строку: "
    "«Существенных новостей по золоту и серебру за сутки не найдено.»"
)


def make_digest_with_ai(items):
    # формируем компактный список для модели
    lines = []
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. [{it['source']}] {it['title']}\n   {it['summary']}\n   {it['link']}")
    user_content = "Материалы за последние сутки:\n\n" + "\n\n".join(lines)

    payload = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content": AI_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.3,
    }
    headers = {
        "Authorization": f"Bearer {AI_KEY}",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        f"{AI_BASE}/chat/completions",
        headers=headers,
        json=payload,
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


# ---------- Режим заглушки (без ключа ИИ) ----------
def make_digest_stub(items):
    if not items:
        return "За последние сутки материалов не собрано."
    lines = ["<b>⚠️ Тестовый режим (ИИ не подключён)</b>",
             "Сырой список собранных материалов без фильтрации:\n"]
    for it in items[:30]:
        title = html.escape(it["title"])
        src = html.escape(it["source"])
        link = html.escape(it["link"])
        lines.append(f"• <a href=\"{link}\">{title}</a> ({src})")
    return "\n".join(lines)


# ---------- 4. Отправка в Telegram ----------
def send_to_telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        print("[!] Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — отправка пропущена.")
        print("---- Дайджест ----")
        print(text)
        return

    # Telegram ограничивает сообщение ~4096 символами — режем на части
    chunks = []
    while text:
        chunks.append(text[:3800])
        text = text[3800:]

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
    today = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=3)))  # МСК
    header = f"📊 <b>Дайджест по золоту и серебру</b>\n{today.strftime('%d.%m.%Y')}\n\n"

    feeds = load_feeds()
    print(f"[i] Лент к обходу: {len(feeds)}")
    items = collect_items(feeds)

    if AI_KEY:
        try:
            body = make_digest_with_ai(items)
        except Exception as e:
            print(f"[!] Ошибка ИИ: {e} — отправляю заглушку.")
            body = make_digest_stub(items)
    else:
        body = make_digest_stub(items)

    send_to_telegram(header + body)
    print("[i] Готово.")


if __name__ == "__main__":
    main()
