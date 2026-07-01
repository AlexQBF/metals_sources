#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Проверка наличия RSS у списка сайтов. Запускать на GitHub Actions (там открытый интернет).
Результат сохраняется в rss_report.md (выгружается артефактом).
Ничего не трогает в основном боте — отдельный диагностический скрипт.
"""
import requests, re
from concurrent.futures import ThreadPoolExecutor

SITES = [
 # Российские
 "zolteh.ru","nedradv.ru","dprom.online","goldminingunion.ru","metaltorg.ru",
 "interfax.ru","kommersant.ru","vedomosti.ru","rbc.ru","moex.com",
 "bcs-express.ru","finam.ru","tbank.ru","profinance.ru","cbr.ru",
 "minfin.gov.ru","gokhran.ru","rosnedra.gov.ru","rosstat.gov.ru","customs.gov.ru",
 "zolotoy-zapas.ru","businesstat.ru","delprof.ru","roif-expert.ru","marketing.rbc.ru",
 # Зарубежные
 "gold.org","silverinstitute.org","lbma.org.uk","metalsfocus.com","cpmgroup.com",
 "heraeus.com","kitco.com","bullionvault.com","mining.com","investing.com",
 "tradingeconomics.com","reuters.com","bloomberg.com","cmegroup.com","lme.com",
 "en.sge.com.cn","worldbank.org","imf.org","usgs.gov","statista.com",
 "spglobal.com","fastmarkets.com","argusmedia.com","woodmac.com","crugroup.com",
]

# типовые адреса лент (включая известные нестандартные)
PATHS = ["/rss","/feed","/rss.xml","/feed.xml","/atom.xml","/feeds","/news/rss",
         "/en/rss","/rss.asp","/RSS/news.xml","/rss/news","/news/feed","/blog/feed"]
HDR = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 RSScheck"}

def looks_like_rss(txt):
    t = txt[:3000].lower()
    return ("<rss" in t) or ("<feed" in t and "xmlns" in t) or ("<?xml" in t and ("<channel" in t or "<entry" in t))

def check(site):
    base = "https://" + site
    for p in PATHS:
        try:
            r = requests.get(base+p, timeout=10, headers=HDR, allow_redirects=True)
            if r.status_code==200 and looks_like_rss(r.text):
                return (site, "✅ ЕСТЬ RSS", base+p)
        except Exception:
            pass
    # подсказка <link rel=alternate type=application/rss+xml> в HTML главной
    try:
        r = requests.get(base, timeout=10, headers=HDR, allow_redirects=True)
        if r.status_code==200:
            m = re.search(r'<link[^>]+application/(?:rss|atom)\+xml[^>]*>', r.text, re.I)
            if m:
                href = re.search(r'href=["\']([^"\']+)', m.group(0), re.I)
                if href:
                    u = href.group(1)
                    return (site, "✅ ЕСТЬ RSS (link)", u if u.startswith("http") else base+u)
            return (site, "❌ нет RSS", f"страница открылась (HTTP 200), ленты не найдено")
        return (site, "❌ нет RSS", f"HTTP {r.status_code}")
    except Exception as e:
        return (site, "⚠️ недоступен", type(e).__name__)

def main():
    results=[]
    with ThreadPoolExecutor(max_workers=12) as ex:
        for res in ex.map(check, SITES):
            results.append(res)
    have = [r for r in results if r[1].startswith("✅")]
    lines = ["# Проверка RSS по сайтам", "",
             f"Есть RSS: {len(have)} из {len(SITES)}", "", "| Сайт | Статус | Адрес ленты / примечание |", "|---|---|---|"]
    for site,status,note in results:
        lines.append(f"| {site} | {status} | {note} |")
    with open("rss_report.md","w",encoding="utf-8") as f:
        f.write("\n".join(lines))
    # дублируем в консоль
    print(f"{'САЙТ':<28}{'СТАТУС':<22}АДРЕС/ПРИМЕЧАНИЕ")
    print("-"*95)
    for site,status,note in results:
        print(f"{site:<28}{status:<22}{note}")

if __name__ == "__main__":
    main()
