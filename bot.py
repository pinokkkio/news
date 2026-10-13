#!/usr/bin/env python3
"""Телеграм-бот: раз за запуск собирает свежие новости из RSS, просит Claude
выбрать действительно важные и публикует их в канал. Если важного нет — молчит.

Запускайте периодически (cron / планировщик задач). Состояние хранится в state.json.
"""

import argparse
import calendar
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import anthropic
import feedparser

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"
FEEDS_FILE = BASE_DIR / "feeds.txt"
ENV_FILE = BASE_DIR / ".env"

SEEN_TTL = 7 * 24 * 3600          # сколько помнить уже просмотренные новости
POSTED_MEMORY = 48 * 3600         # сколько помнить опубликованные заголовки (против повторов)
MAX_AGE = 24 * 3600               # новости старше этого не рассматриваем
MAX_CANDIDATES = 300              # ограничение на число заголовков за один запуск
USER_AGENT = "Mozilla/5.0 (news-telegram-bot)"


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
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"seen": {}, "posted": []}


def save_state(state):
    now = time.time()
    state["seen"] = {k: t for k, t in state["seen"].items() if now - t < SEEN_TTL}
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


def fetch_candidates(feeds, seen):
    now = time.time()
    items = []
    for url in feeds:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                parsed = feedparser.parse(resp.read())
        except Exception as e:
            print(f"[warn] не удалось загрузить {url}: {e}", file=sys.stderr)
            continue
        source = parsed.feed.get("title", urllib.parse.urlparse(url).netloc)
        for entry in parsed.entries:
            link = entry.get("link")
            title = (entry.get("title") or "").strip()
            if not link or not title:
                continue
            key = hashlib.sha1(link.encode()).hexdigest()
            if key in seen:
                continue
            ts = entry.get("published_parsed") or entry.get("updated_parsed")
            published = calendar.timegm(ts) if ts else now
            if now - published > MAX_AGE:
                continue
            summary = html.unescape(re.sub(r"<[^>]+>", " ", entry.get("summary", "")))
            items.append({
                "key": key,
                "source": source,
                "title": title,
                "summary": " ".join(summary.split())[:300],
                "link": link,
                "published": published,
            })
    items.sort(key=lambda x: x["published"], reverse=True)
    return items[:MAX_CANDIDATES]


SYSTEM_PROMPT = """Ты — строгий выпускающий редактор новостного телеграм-канала.
Тебе дают список свежих заголовков из разных источников. Твоя задача — отобрать
ТОЛЬКО по-настоящему важные новости: события, которые завтра будут на первых полосах
крупнейших изданий и реально влияют на жизнь многих людей (крупные политические решения,
войны и перемирия, катастрофы с жертвами, резкие движения рынков, важнейшие научные
прорывы, смерть или отставка ключевых мировых фигур и т.п.).

Правила:
- Планка высокая. Обычные, местные, развлекательные, спортивные (кроме исторических),
  рекламные и «мнения» — пропускай. В большинстве запусков подходящих новостей 0–2.
- Если ничего действительно важного нет — верни пустой список. Это нормальный результат.
- Одну и ту же историю из разных источников объединяй в один пост.
- Не повторяй истории из списка «уже опубликовано», если нет существенно нового развития.
- Для каждого поста: короткий заголовок и 1–3 предложения сути на языке: {language}.
  Пиши нейтрально, только факты из присланных материалов, ничего не выдумывай.
- В ids укажи номера исходных новостей, на которых основан пост.
- importance: оценка от 1 до 10, где 10 — событие исторического масштаба."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "posts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "headline": {"type": "string"},
                    "summary": {"type": "string"},
                    "importance": {"type": "integer"},
                    "ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["headline", "summary", "importance", "ids"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["posts"],
    "additionalProperties": False,
}


def select_important(candidates, posted, language, model):
    lines = [
        f"[{i}] ({c['source']}) {c['title']} — {c['summary']}"
        for i, c in enumerate(candidates)
    ]
    already = "\n".join(f"- {p['headline']}" for p in posted) or "(пусто)"
    user_msg = (
        f"Уже опубликовано за последние 48 часов:\n{already}\n\n"
        f"Свежие новости:\n" + "\n".join(lines)
    )

    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM_PROMPT.format(language=language),
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
        messages=[{"role": "user", "content": user_msg}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("модель отказалась обрабатывать запрос")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("ответ модели обрезан (max_tokens)")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)["posts"]


def format_post(post, candidates):
    links = []
    for i in post["ids"]:
        if 0 <= i < len(candidates):
            c = candidates[i]
            links.append(f'<a href="{html.escape(c["link"])}">{html.escape(c["source"])}</a>')
    text = f"<b>{html.escape(post['headline'])}</b>\n\n{html.escape(post['summary'])}"
    if links:
        text += "\n\n" + " · ".join(dict.fromkeys(links))
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


def main():
    parser = argparse.ArgumentParser(description="Постит важные новости в телеграм-канал")
    parser.add_argument("--dry-run", action="store_true",
                        help="ничего не отправлять и не сохранять, только показать посты")
    args = parser.parse_args()

    load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHANNEL_ID")
    min_importance = int(os.environ.get("MIN_IMPORTANCE", "7"))
    language = os.environ.get("NEWS_LANGUAGE", "русский")
    model = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
    if not args.dry_run and (not token or not chat_id):
        sys.exit("Задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHANNEL_ID в .env")

    state = load_state()
    candidates = fetch_candidates(read_feeds(), state["seen"])
    print(f"Новых заголовков: {len(candidates)}")
    if not candidates:
        return

    posts = select_important(candidates, state["posted"], language, model)
    posts = [p for p in posts if p["importance"] >= min_importance]
    print(f"Важных новостей: {len(posts)}")

    for post in posts:
        text = format_post(post, candidates)
        if args.dry_run:
            print(f"--- importance {post['importance']} ---\n{text}\n")
            continue
        send_telegram(token, chat_id, text)
        state["posted"].append({"headline": post["headline"], "time": time.time()})
        save_state(state)  # сохраняем сразу, чтобы при сбое не запостить повторно
        time.sleep(1)

    if args.dry_run:
        return
    now = time.time()
    for c in candidates:
        state["seen"][c["key"]] = now
    save_state(state)


if __name__ == "__main__":
    main()
