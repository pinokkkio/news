#!/usr/bin/env python3
"""Телеграм-бот важных новостей (бесплатный, без нейросетей).

Идея: важная новость — та, о которой одновременно пишут многие независимые редакции.
Бот собирает заголовки из RSS-лент, группирует заголовки об одном событии и публикует
событие, если его подхватило не меньше MIN_SOURCES разных источников. Если таких нет —
ничего не пишет.

Запускайте периодически (cron / планировщик задач). Состояние хранится в state.json.
"""

import argparse
import calendar
import hashlib
import html
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import feedparser

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"
FEEDS_FILE = BASE_DIR / "feeds.txt"
ENV_FILE = BASE_DIR / ".env"

POSTED_MEMORY = 48 * 3600         # сколько помнить опубликованные события (против повторов)
SIMILARITY = 0.5                  # порог похожести двух заголовков (0..1)
USER_AGENT = "Mozilla/5.0 (news-telegram-bot)"

STOPWORDS = set("""
и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее
мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был
него до вас нибудь опять уж вам ведь там потом себя ничего ей может они тут где есть надо ней
для мы тебя их чем была сам чтоб без будто чего раз тоже себе под будет ж тогда кто этот того
потому этого какой совсем ним здесь этом один почти мой тем чтобы нее сейчас были куда зачем
всех никогда можно при наконец два об другой хоть после над больше тот через эти нас про всего
них какая много разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя
такой им более всегда конечно всю между это также который которые которая заявил заявила
заявили рассказал сообщил сообщили назвал назвали стало стал стала году года против
the a an of to in on for and or is are was were be by with at from as that this it its
after over into says said will has have new
""".split())


def load_env():
    """Минимальный загрузчик .env (KEY=VALUE), не перетирает уже заданные переменные."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_state():
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        state.setdefault("items", {})
        state.setdefault("posted", [])
        return state
    return {"items": {}, "posted": []}


def save_state(state, window):
    now = time.time()
    state["items"] = {k: v for k, v in state["items"].items() if now - v["published"] < window}
    state["posted"] = [p for p in state["posted"] if now - p["time"] < POSTED_MEMORY]
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)


def read_feeds():
    return [
        line.strip()
        for line in FEEDS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def fetch_feed(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return feedparser.parse(resp.read())


def fetch_items(feeds, window):
    """Скачивает ленты и возвращает свежие новости {ключ: новость}."""
    now = time.time()
    items = {}
    for url in feeds:
        try:
            parsed = fetch_feed(url)
        except Exception as e:
            print(f"[warn] не удалось загрузить {url}: {e}", file=sys.stderr)
            continue
        # источник = домен ленты, чтобы разные ленты одной редакции считались одним источником
        source = urllib.parse.urlparse(url).netloc.removeprefix("www.")
        source_name = parsed.feed.get("title", source)
        for entry in parsed.entries:
            link = entry.get("link")
            title = html.unescape((entry.get("title") or "").strip())
            if not link or not title:
                continue
            ts = entry.get("published_parsed") or entry.get("updated_parsed")
            published = calendar.timegm(ts) if ts else now
            if now - published > window:
                continue
            summary = html.unescape(re.sub(r"<[^>]+>", " ", entry.get("summary", "")))
            items[hashlib.sha1(link.encode()).hexdigest()] = {
                "source": source,
                "source_name": source_name,
                "title": title,
                "summary": " ".join(summary.split())[:400],
                "link": link,
                "published": min(published, now),
            }
    return items


def tokens(text):
    """Ключевые слова заголовка: без стоп-слов, обрезанные до 5 букв (грубый стемминг,
    чтобы «Украина», «Украины», «украинский» совпадали)."""
    words = re.findall(r"[a-zа-я0-9]+", text.lower().replace("ё", "е"))
    return {w[:5] for w in words if len(w) >= 3 and w not in STOPWORDS}


def similarity(a, b, idf):
    """Доля общих слов, взвешенная по редкости слова: совпадение редких слов
    («землетрясение», фамилия) значит больше, чем частых («Россия»)."""
    shared = a & b
    if len(shared) < 2:
        return 0.0
    weight = lambda ws: sum(idf.get(w, idf["__max__"]) for w in ws)
    return weight(shared) / min(weight(a), weight(b))


def build_clusters(items):
    """Группирует новости об одном событии. Возвращает список кластеров,
    отсортированный по числу разных источников."""
    entries = sorted((dict(v) for v in items.values()), key=lambda x: x["published"])
    for e in entries:
        e["tokens"] = tokens(e["title"])

    # idf: чем реже слово встречается в заголовках, тем оно весомее
    df = {}
    for e in entries:
        for w in e["tokens"]:
            df[w] = df.get(w, 0) + 1
    n = max(len(entries), 1)
    idf = {w: math.log(1 + n / c) for w, c in df.items()}
    idf["__max__"] = math.log(1 + n)

    clusters = []
    for e in entries:
        best, best_sim = None, SIMILARITY
        for c in clusters:
            sim = max(similarity(e["tokens"], m["tokens"], idf) for m in c["members"])
            if sim >= best_sim:
                best, best_sim = c, sim
        if best is None:
            clusters.append({"members": [e]})
        else:
            best["members"].append(e)

    for c in clusters:
        c["sources"] = {m["source"] for m in c["members"]}
        # заголовок поста — самый «типичный»: больше всего похож на остальные в группе
        c["main"] = max(
            c["members"],
            key=lambda m: sum(similarity(m["tokens"], o["tokens"], idf) for o in c["members"]),
        )
    clusters.sort(key=lambda c: len(c["sources"]), reverse=True)
    return clusters, idf


def already_posted(cluster, posted, idf):
    links = {m["link"] for m in cluster["members"]}
    for p in posted:
        if links & set(p["links"]):
            return True
        for t in p["titles"]:
            t = set(t)
            if any(similarity(m["tokens"], t, idf) >= SIMILARITY for m in cluster["members"]):
                return True
    return False


def format_post(cluster):
    main = cluster["main"]
    text = f"<b>{html.escape(main['title'])}</b>"
    if main["summary"] and not main["summary"].startswith(main["title"]):
        text += f"\n\n{html.escape(main['summary'])}"
    links, seen = [], set()
    for m in sorted(cluster["members"], key=lambda m: m is not main):
        if m["source"] in seen:
            continue
        seen.add(m["source"])
        links.append(f'<a href="{html.escape(m["link"])}">{html.escape(m["source_name"])}</a>')
    text += f"\n\nИсточников: {len(seen)} — " + " · ".join(links[:6])
    return text


def send_telegram(token, chat_id, text):
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    with urllib.request.urlopen(url, data=data, timeout=20) as resp:
        result = json.loads(resp.read())
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")


def check_feeds():
    for url in read_feeds():
        try:
            parsed = fetch_feed(url)
            n = len(parsed.entries)
            print(f"{'OK  ' if n else 'ПУСТО'} {n:4d}  {url}")
        except Exception as e:
            print(f"ОШИБКА      {url}  ({e})")


def main():
    parser = argparse.ArgumentParser(description="Постит важные новости в телеграм-канал")
    parser.add_argument("--dry-run", action="store_true",
                        help="ничего не отправлять, показать самые крупные сюжеты")
    parser.add_argument("--check-feeds", action="store_true",
                        help="проверить, какие ленты из feeds.txt работают")
    args = parser.parse_args()

    if args.check_feeds:
        check_feeds()
        return

    load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHANNEL_ID")
    min_sources = int(os.environ.get("MIN_SOURCES", "5"))
    max_posts = int(os.environ.get("MAX_POSTS", "3"))
    window = float(os.environ.get("WINDOW_HOURS", "6")) * 3600
    if not args.dry_run and (not token or not chat_id):
        sys.exit("Задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHANNEL_ID в .env")

    state = load_state()
    # копим новости между запусками: ленты отдают лишь последние N записей
    state["items"].update(fetch_items(read_feeds(), window))
    now = time.time()
    items = {k: v for k, v in state["items"].items() if now - v["published"] < window}
    clusters, idf = build_clusters(items)
    print(f"Новостей за окно: {len(items)}, сюжетов: {len(clusters)}")

    if args.dry_run:
        print(f"\nКрупнейшие сюжеты (публикуются при {min_sources}+ источниках):")
        for c in clusters[:10]:
            mark = "уже было" if already_posted(c, state["posted"], idf) else ""
            print(f"  {len(c['sources']):2d} ист. | {c['main']['title']}  {mark}")
        return

    posted = 0
    for c in clusters:
        if len(c["sources"]) < min_sources or posted >= max_posts:
            break
        if already_posted(c, state["posted"], idf):
            continue
        send_telegram(token, chat_id, format_post(c))
        posted += 1
        state["posted"].append({
            "time": time.time(),
            "links": [m["link"] for m in c["members"]],
            "titles": [sorted(m["tokens"]) for m in c["members"]],
        })
        save_state(state, window)  # сохраняем сразу, чтобы при сбое не запостить повторно
        time.sleep(1)
    print(f"Опубликовано: {posted}")
    save_state(state, window)


if __name__ == "__main__":
    main()
