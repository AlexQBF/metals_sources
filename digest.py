#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ГИБРИДНЫЙ бот-дайджест по золоту и серебру: Telegram-каналы + RSS-сайты в ОДНОМ дайджесте.
Без котировок (добавим позже для боевого).

Поток:
  1. Собирает посты из ТГ-каналов (channels.json, через t.me/s/) И записи с RSS-сайтов (feeds_sites.json)
  2. Складывает всё в общий пул, отсеивает уже отправленное (sent_hybrid.json)
  3. Gemini: фильтр золото/серебро, СКЛЕЙКА ДУБЛЕЙ МЕЖДУ ВСЕМИ ИСТОЧНИКАМИ, важность,
     дайджест 6-8 пунктов, ссылка вшита в глагол заголовка (ведёт на пост ТГ или статью сайта)
  4. Шлёт в Telegram-канал, сохраняет журналы и архив

Секреты (GitHub Actions Secrets):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  — токен и id ТЕСТОВОГО канала
  AI_API_KEY, AI_BASE_URL, AI_MODEL                 — Gemini
"""

import os
import re
import json
import time
import html
from datetime import datetime, timezone, timedelta

import requests
import feedparser
from bs4 import BeautifulSoup

CHANNELS_FILE = "channels.json"
FEEDS_FILE = "feeds_sites.json"
SENT_FILE = "sent.json"
RECENT_DIGESTS_FILE = "recent_digests.json"
DIGESTS_DIR = "digests"
MAX_ITEMS_TO_AI = 200
RECENT_DIGESTS_KEEP = 5
REQUEST_TIMEOUT = 25

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

AI_KEY = os.environ.get("AI_API_KEY", "").strip()
AI_BASE = os.environ.get("AI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai").strip().rstrip("/")
AI_MODEL = os.environ.get("AI_MODEL", "gemini-2.5-flash").strip()

METALS_API_KEY = os.environ.get("METALS_API_KEY", "").strip()  # ключ metals.dev для котировок

MSK = timezone(timedelta(hours=3))


def get_hours_window():
    return 72 if datetime.now(MSK).weekday() == 0 else 36


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------- Источник 1: Telegram-каналы ----------
def load_channels():
    data = load_json(CHANNELS_FILE, {"channels": []})
    out = []
    for ch in data.get("channels", []):
        if ch.get("enabled", True) and ch.get("username"):
            out.append({"name": ch.get("name", ch["username"]), "username": ch["username"].lstrip("@")})
    return out


def parse_post_time(wrap):
    t = wrap.select_one("time[datetime]")
    if t and t.get("datetime"):
        try:
            return datetime.fromisoformat(t["datetime"].replace("Z", "+00:00"))
        except Exception:
            return None
    return None


def collect_telegram(channels, cutoff, sent_ids):
    items = []
    for ch in channels:
        try:
            resp = requests.get(f"https://t.me/s/{ch['username']}", timeout=REQUEST_TIMEOUT,
                                headers={"User-Agent": "Mozilla/5.0 (DigestBot/1.0)"})
            if resp.status_code != 200:
                print(f"[!] TG @{ch['username']}: HTTP {resp.status_code}")
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            print(f"[!] TG {ch['name']}: ошибка — {e}")
            continue

        fresh = 0
        for wrap in soup.select(".tgme_widget_message_wrap"):
            msg = wrap.select_one(".tgme_widget_message")
            if not msg:
                continue
            post_id = msg.get("data-post", "")
            if not post_id or post_id in sent_ids:
                continue
            ts = parse_post_time(wrap)
            if ts and ts < cutoff:
                continue
            tb = wrap.select_one(".tgme_widget_message_text")
            if not tb:
                continue
            for br in tb.find_all("br"):
                br.replace_with("\n")
            text = tb.get_text().strip()
            if not text:
                continue
            if len(text) > 800:
                text = text[:800] + "…"
            items.append({
                "id": post_id,
                "source": ch["name"],
                "type": "Telegram-канал",
                "title": "",
                "text": text,
                "link": "https://t.me/" + post_id,
            })
            fresh += 1
        print(f"[i] TG {ch['name']}: новых {fresh}")
        time.sleep(0.3)
    return items


# ---------- Источник 2: RSS-сайты ----------
def load_feeds():
    data = load_json(FEEDS_FILE, {"feeds": []})
    out = []
    for it in data.get("feeds", []):
        if it.get("enabled", True) and it.get("url"):
            out.append({"name": it.get("name", it["url"]), "url": it["url"]})
    return out


def entry_time(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def collect_rss(feeds, cutoff, sent_ids):
    items = []
    for feed in feeds:
        try:
            resp = requests.get(feed["url"], timeout=REQUEST_TIMEOUT,
                                headers={"User-Agent": "Mozilla/5.0 (DigestBot/1.0)"})
            parsed = feedparser.parse(resp.content)
        except Exception as e:
            print(f"[!] RSS {feed['name']}: ошибка — {e}")
            continue
        got = len(parsed.entries)
        if getattr(parsed, "bozo", 0) and got == 0:
            print(f"[!] RSS {feed['name']}: не похоже на RSS — 0")
            continue
        fresh = 0
        for entry in parsed.entries:
            eid = entry.get("link") or entry.get("id") or f"{feed['name']}:{entry.get('title','')}"
            if eid in sent_ids:
                continue
            ts = entry_time(entry)
            if ts and ts < cutoff:
                continue
            title = html.unescape(entry.get("title", "").strip())
            summary = html.unescape(re.sub(r"<[^>]+>", "", entry.get("summary", ""))).strip()
            if len(summary) > 700:
                summary = summary[:700] + "…"
            items.append({
                "id": eid,
                "source": feed["name"],
                "type": "сайт",
                "title": title,
                "text": summary,
                "link": entry.get("link", ""),
            })
            fresh += 1
        print(f"[i] RSS {feed['name']}: получено {got}, новых {fresh}")
        time.sleep(0.3)
    return items


def collect_all():
    hours = get_hours_window()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    sent = load_json(SENT_FILE, {"ids": []})
    sent_ids = set(sent.get("ids", []))

    channels = load_channels()
    feeds = load_feeds()
    print(f"[i] Источники: {len(channels)} ТГ-каналов + {len(feeds)} сайтов. Окно {hours}ч. В памяти: {len(sent_ids)}")

    tg_items = collect_telegram(channels, cutoff, sent_ids)
    rss_items = collect_rss(feeds, cutoff, sent_ids)
    all_items = tg_items + rss_items
    print(f"[i] Итого новых: {len(all_items)} (ТГ {len(tg_items)} + сайты {len(rss_items)})")
    return all_items[:MAX_ITEMS_TO_AI], sent_ids


# ---------- Обработка через Gemini ----------
AI_SYSTEM = (
    "Ты — редактор ежедневного дайджеста «AU и AG. Главное за день» по рынку ЗОЛОТА и СЕРЕБРА. "
    "Тебе дают материалы из двух типов источников — Telegram-каналов и отраслевых сайтов — за последние "
    "~1,5 суток, и список тем прошлых дайджестов.\n\n"
    "ЖЁСТКИЙ ФИЛЬТР ТЕМЫ: оставляй ТОЛЬКО золото и серебро (цены, спрос/предложение, добыча, запасы, "
    "прогнозы, отчёты, решения регуляторов/ЦБ, сделки, проекты). СТРОГО ВЫБРАСЫВАЙ платину, палладий, МПГ, "
    "медь, никель, сталь, уголь, нефть, алмазы и прочее. Если материал в основном про них — выбрасывай целиком.\n\n"
    "ТОЛЬКО НОВОСТИ И СОБЫТИЯ: что-то произошло/изменилось/заявлено/куплено/выросло/упало. "
    "Образовательные и технические материалы без новостного повода — отсекай.\n\n"
    "СКЛЕЙКА ДУБЛЕЙ (ВАЖНО): одно и то же событие часто приходит из разных источников (ТГ и сайты) и с РАЗНЫМИ "
    "формулировками, деталями и даже цифрами. Считай новости ДУБЛЯМИ, если они об ОДНОМ И ТОМ ЖЕ событии — "
    "та же компания и та же суть (например «Полюс объявил дивиденды за I квартал», даже если в одном источнике "
    "39,5 млрд, а в другом 28 млрд, или разные формулировки). ОБЪЕДИНЯЙ такие в ОДИН пункт (бери самые точные/полные "
    "цифры), НИКОГДА не давай два пункта про одно событие.\n"
    "АНТИ-ПОВТОР ПО ДНЯМ: если тема уже была в прошлых дайджестах (список ниже) — НЕ повторяй её, даже если сейчас "
    "она пришла в другой формулировке или с другими цифрами, КРОМЕ случая, когда есть реально НОВОЕ развитие события.\n\n"
    "ОЦЕНКА ВАЖНОСТИ: оцени значимость каждой новости для рынка золота/серебра и отрасли "
    "(масштаб, влияние на цены, размер компании/сделки, значимость для РФ и мира). "
    "Сначала самое важное, затем менее важное — строго по убыванию значимости. Проходное убирай.\n\n"
    "Пунктов: от 6 до 8. Если действительно значимого меньше 6 — дай меньше, не добивай мусором.\n\n"
    "ФОРМАТ каждого пункта:\n"
    "🔸 <b>Заголовок</b>\n"
    "1-2 ёмких предложения по сути: что произошло и почему важно.\n\n"
    "ТРЕБОВАНИЯ К ЗАГОЛОВКУ:\n"
    "- Заголовок ПОНЯТНЫЙ и информативный: из него сразу ясно, ЧТО произошло "
    "(например «Цены на золото упали ниже $4000 на фоне укрепления доллара»). Не обрубай до 2-3 слов, "
    "но и не длиннее одной строки.\n"
    "- ССЫЛКА вшивается ВНУТРЬ заголовка в одно самое подходящее слово: предпочтительно глагол "
    "(«упали», «купила», «выплатит»), а если глагола нет — в главное ключевое слово. "
    "Оформи как <a href=\"URL\">слово</a> внутри заголовка. Весь заголовок обёрнут в <b>...</b>.\n"
    "- URL бери ТОЛЬКО из поля «Ссылка» соответствующего материала — НИЧЕГО НЕ ВЫДУМЫВАЙ. "
    "Если новость собрана из нескольких источников — ставь ссылку одного, самого содержательного "
    "(при равном приоритете предпочитай ссылку сайта посту Telegram). Если ссылки нет — заголовок без ссылки.\n\n"
    "Пример: 🔸 <b>«Полюс» <a href=\"URL\">выплатит</a> дивиденды за I квартал более 39,5 млрд рублей</b>\n"
    "Крупнейший российский золотодобытчик направит на дивиденды 39,528 млрд рублей.\n\n"
    "Между пунктами пустая строка. Только HTML-теги <b> и <a>. Без Markdown, без вступления/заключения.\n"
    "Если значимого нет — верни ровно: НЕТ_НОВОСТЕЙ"
)


def make_digest_ai(items, recent_topics):
    blocks = []
    for i, p in enumerate(items, 1):
        head = f"[{i}] Тип: {p['type']} | Источник: {p['source']}\nСсылка: {p['link']}"
        if p["title"]:
            head += f"\nЗаголовок: {p['title']}"
        head += f"\nТекст: {p['text']}"
        blocks.append(head)
    user = "МАТЕРИАЛЫ:\n\n" + "\n\n".join(blocks)
    if recent_topics:
        user += "\n\nТЕМЫ ПРОШЛЫХ ДАЙДЖЕСТОВ (не повторять):\n" + "\n".join(f"- {t}" for t in recent_topics)

    payload = {"model": AI_MODEL, "messages": [
        {"role": "system", "content": AI_SYSTEM}, {"role": "user", "content": user}], "temperature": 0.4}
    backoff = [30, 60, 90]
    last_err = None
    total = len(backoff) + 1
    for attempt in range(1, total + 1):
        try:
            resp = requests.post(f"{AI_BASE}/chat/completions",
                                 headers={"Authorization": f"Bearer {AI_KEY}", "Content-Type": "application/json"},
                                 json=payload, timeout=120)
            if resp.status_code >= 500:
                last_err = f"HTTP {resp.status_code}"
                if attempt <= len(backoff):
                    print(f"[i] Gemini {resp.status_code}, попытка {attempt}/{total}, жду {backoff[attempt-1]}с…")
                    time.sleep(backoff[attempt-1])
                continue
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except requests.exceptions.RequestException as e:
            last_err = str(e)
            if attempt <= len(backoff):
                print(f"[i] Сбой Gemini ({attempt}/{total}): {e}, жду {backoff[attempt-1]}с…")
                time.sleep(backoff[attempt-1])
    raise RuntimeError(f"Gemini недоступен после {total} попыток: {last_err}")



# ---------- Котировки золота и серебра (metals.dev, latest) ----------
def fetch_prices():
    if not METALS_API_KEY:
        return None
    try:
        r = requests.get("https://api.metals.dev/v1/latest",
                         params={"api_key": METALS_API_KEY, "currency": "USD", "unit": "toz"},
                         timeout=REQUEST_TIMEOUT)
        if not r.ok:
            print(f"[!] metals.dev: HTTP {r.status_code} {r.text[:200]}")
            return None
        data = r.json()
        metals = data.get("metals", {}) or {}
        gold = metals.get("gold")
        silver = metals.get("silver")
        if gold is None and silver is None:
            print(f"[!] metals.dev: нет цен. Ответ: {str(data)[:200]}")
            return None
        ts = (data.get("timestamps", {}) or {}).get("metal", "")
        try:
            d = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(MSK).strftime("%d.%m.%Y")
        except Exception:
            d = datetime.now(MSK).strftime("%d.%m.%Y")
        def fmt(v):
            return f"{v:,.0f}".replace(",", " ") if v and v >= 100 else (f"{v:,.2f}" if v else "—")
        lines = ["\n———", f"<i>Котировки на {d}</i>"]
        if gold is not None:
            lines.append(f"<i>Au: {fmt(gold)} $/унц.</i>")
        if silver is not None:
            lines.append(f"<i>Ag: {fmt(silver)} $/унц.</i>")
        return "\n".join(lines)
    except Exception as e:
        print(f"[!] metals.dev: ошибка — {e}")
        return None


def clean_html_for_telegram(text):
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
    allowed = {"b", "i", "u", "s", "a", "code", "pre"}
    def strip_tag(m):
        tag = m.group(1).lower().lstrip("/")
        return m.group(0) if tag in allowed else ""
    return re.sub(r"</?\s*([a-zA-Z0-9]+)[^>]*>", strip_tag, text)


def send_to_telegram(text):
    text = clean_html_for_telegram(text)
    if not TG_TOKEN or not TG_CHAT:
        print("[!] Нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — вывод в лог:")
        print(text)
        return
    LIMIT = 3800
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > LIMIT:
            if cur: chunks.append(cur); cur = ""
            chunks.append(line[:LIMIT]); line = line[LIMIT:]
        if len(cur) + len(line) + 1 > LIMIT:
            chunks.append(cur); cur = line
        else:
            cur = cur + "\n" + line if cur else line
    if cur: chunks.append(cur)
    for chunk in chunks:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": TG_CHAT, "text": chunk, "parse_mode": "HTML",
                                "disable_web_page_preview": "true"}, timeout=30)
        if not r.ok:
            print(f"[!] Ошибка отправки: {r.status_code} {r.text}")
        time.sleep(1)


def main():
    now_msk = datetime.now(MSK)
    months = ["января","февраля","марта","апреля","мая","июня",
              "июля","августа","сентября","октября","ноября","декабря"]
    date_ru = f"{now_msk.day} {months[now_msk.month-1]} {now_msk.year}"
    header = f"<b>🪙 Главное за день · {date_ru}</b>\n\n"

    items, sent_ids = collect_all()
    recent = load_json(RECENT_DIGESTS_FILE, {"topics": []})

    ai_failed = False
    if AI_KEY:
        try:
            body = make_digest_ai(items, recent.get("topics", []))
        except Exception as e:
            print(f"[!] Ошибка Gemini: {e}")
            ai_failed = True
            body = ("⚠️ Дайджест временно недоступен: сервис ИИ перегружен. "
                    "Следующая попытка — в очередном запуске.")
    else:
        body = "⚠️ Не задан AI_API_KEY."

    if body.strip() == "НЕТ_НОВОСТЕЙ":
        body = "За период существенных новостей по золоту и серебру не найдено."

    # котировки в конец (только если дайджест сформирован)
    if not ai_failed:
        prices = fetch_prices()
        if prices:
            body = body + "\n" + prices

    send_to_telegram(header + body)

    new_ids = [] if ai_failed else [p["id"] for p in items if p["id"]]
    sent_ids.update(new_ids)
    save_json(SENT_FILE, {"ids": list(sent_ids)[-4000:]})

    if AI_KEY and not ai_failed and body and "не найдено" not in body:
        os.makedirs(DIGESTS_DIR, exist_ok=True)
        with open(os.path.join(DIGESTS_DIR, now_msk.strftime("%Y-%m-%d") + ".md"), "w", encoding="utf-8") as f:
            f.write(f"# Дайджест {date_ru}\n\n" + body)
        heads = re.findall(r"<b>(.*?)</b>", body)
        heads = [re.sub(r"<[^>]+>", "", h) for h in heads]  # чистим вложенные <a> из тем
        topics = (recent.get("topics", []) + heads)[-RECENT_DIGESTS_KEEP * 6:]
        save_json(RECENT_DIGESTS_FILE, {"topics": topics})

    print("[i] Готово.")


if __name__ == "__main__":
    main()
