#!/usr/bin/env python3
"""Телеграм-бот важных новостей (бесплатный, без нейросетей).

Идея: важная новость — та, о которой одновременно пишут многие независимые редакции.
Бот собирает заголовки из RSS-лент, группирует заголовки об одном событии и публикует
событие, если его подхватило не меньше MIN_SOURCES разных источников. Если таких нет —
ничего не пишет.

Если задан OLLAMA_URL, локальная нейросеть (Ollama) строго проверяет каждый сюжет:
касается ли он каждого жителя России, требует ли его внимания и является ли
свершившимся фактом. Публикуется только то, что прошло все три проверки, — коротким
пересказом с пояснением, что это значит для человека.
Без нейросети публикуется заголовок и описание из ленты.

Запускайте периодически (cron / планировщик задач) или с флагом --loop (так работает
docker-контейнер). Состояние хранится в state.json.
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
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR))
STATE_FILE = DATA_DIR / "state.json"
FEEDS_FILE = Path(os.environ.get("FEEDS_FILE", BASE_DIR / "feeds.txt"))
ENV_FILE = BASE_DIR / ".env"

POSTED_MEMORY = 48 * 3600         # сколько помнить опубликованные/отклонённые сюжеты
MAX_ASSESS = 10                   # сколько сюжетов максимум оценивать нейросетью за запуск
REASSESS_GROWTH = 3               # отклонённый сюжет оценить заново, если источников стало на 3+ больше
NO_LLM_MIN_SOURCES = 5            # порог источников, когда нейросеть не настроена или сбоит
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
        state.setdefault("rejected", [])
        return state
    return {"items": {}, "posted": [], "rejected": []}


def save_state(state, window):
    now = time.time()
    state["items"] = {k: v for k, v in state["items"].items() if now - v["published"] < window}
    for key in ("posted", "rejected"):
        state[key] = [p for p in state[key] if now - p["time"] < POSTED_MEMORY]
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


def find_same(cluster, records, idf):
    """Ищет среди записей (опубликованных/отклонённых) тот же сюжет."""
    links = {m["link"] for m in cluster["members"]}
    for p in records:
        if links & set(p["links"]):
            return p
        for t in p["titles"]:
            t = set(t)
            if any(similarity(m["tokens"], t, idf) >= SIMILARITY for m in cluster["members"]):
                return p
    return None


def remember(cluster, **extra):
    return {
        "time": time.time(),
        "links": [m["link"] for m in cluster["members"]],
        "titles": [sorted(m["tokens"]) for m in cluster["members"]],
        "sources": len(cluster["sources"]),
        **extra,
    }


def unique_by_source(cluster):
    """По одной новости от каждого источника, «главная» — первой."""
    main, result, seen = cluster["main"], [], set()
    for m in sorted(cluster["members"], key=lambda m: m is not main):
        if m["source"] not in seen:
            seen.add(m["source"])
            result.append(m)
    return result


CLASSIFY_PROMPT = """Ты — редактор телеграм-канала для жителей России. В канал попадают только новости,
которые лично касаются почти каждого человека в России и требуют его внимания.
Ниже сообщения разных изданий об одном событии. Заполни поля по порядку.

effect_on_person — одно предложение: как это событие может отразиться на обычном человеке
в России — на его деньгах, ценах, налогах, работе, связи и интернете, поездках,
безопасности, здоровье, правах. Думай о последствиях, а не только о том, кого событие
называет прямо: например, повышение налога для бизнеса ведёт к росту цен для всех.
Если никак не отразится — напиши «никак».

is_fact — true, если в сообщениях сказано, что это уже произошло: принято, подписано,
объявлено, введено, заблокировано, выросло, упало. false, если это только предложение,
законопроект на рассмотрении, прогноз, слух или обсуждение.

affects_everyone — true, если последствия коснутся почти любого взрослого жителя России
или его семьи, по всей стране. false, если только одного города или региона, отдельной
профессии или компании, или это событие в другой стране без последствий для россиян.

requires_attention — true, если человеку лучше об этом знать: ему может понадобиться
что-то сделать, подготовиться или изменить планы. false для заявлений, мнений,
переговоров, встреч, спорта, культуры, шоу-бизнеса и происшествий без последствий для страны.

Примеры:

Сообщения: «Пенсионный возраст повышен до 67 лет: закон подписан»
{"effect_on_person": "Всем работающим придётся выходить на пенсию позже", "is_fact": true, "affects_everyone": true, "requires_attention": true}

Сообщения: «Правительство утвердило рост тарифов ЖКХ на 15% по всей России с 1 марта»
{"effect_on_person": "Коммунальные платежи вырастут у каждой семьи", "is_fact": true, "affects_everyone": true, "requires_attention": true}

Сообщения: «YouTube полностью заблокирован в России»
{"effect_on_person": "Миллионы людей потеряют доступ к привычному сервису", "is_fact": true, "affects_everyone": true, "requires_attention": true}

Сообщения: «Минфин предложил повысить подоходный налог»
{"effect_on_person": "Налог может вырасти, но пока это только предложение", "is_fact": false, "affects_everyone": true, "requires_attention": false}

Сообщения: «В Казани открылась новая станция метро»
{"effect_on_person": "Касается только жителей Казани", "is_fact": true, "affects_everyone": false, "requires_attention": false}

Сообщения: «Президент провёл встречу с губернатором Тульской области»
{"effect_on_person": "никак", "is_fact": true, "affects_everyone": false, "requires_attention": false}"""

REWRITE_PROMPT = """Ты — редактор новостного телеграм-канала для жителей России. Ниже сообщения
разных изданий об одном событии. Перескажи новость по-русски, только фактами из сообщений,
без эмоций и кликбейта:
- headline: короткий нейтральный заголовок (до 100 символов);
- text: 1–2 предложения: что произошло;
- impact: одно предложение: что это значит для обычного человека и на что обратить внимание."""

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "effect_on_person": {"type": "string"},
        "is_fact": {"type": "boolean"},
        "affects_everyone": {"type": "boolean"},
        "requires_attention": {"type": "boolean"},
    },
    "required": ["effect_on_person", "is_fact", "affects_everyone", "requires_attention"],
}

REWRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "text": {"type": "string"},
        "impact": {"type": "string"},
    },
    "required": ["headline", "text", "impact"],
}

CHECKS = {"is_fact": "свершившийся факт", "affects_everyone": "касается всех",
          "requires_attention": "требует внимания"}


def verdict(a):
    """Публикуем, только если нейросеть ответила «да» на все три вопроса."""
    return all(a[k] for k in CHECKS)


def describe(a):
    return ", ".join(("+" if a[k] else "-") + name for k, name in CHECKS.items())


def ollama_request(path, body=None, timeout=60):
    """Запрос к Ollama: POST с JSON, если передан body, иначе GET."""
    url = os.environ["OLLAMA_URL"].rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def ensure_model():
    """Ждёт запуска Ollama и скачивает модель, только если её ещё нет на диске (~2 ГБ)."""
    if not os.environ.get("OLLAMA_URL"):
        return
    model = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
    wanted = model if ":" in model else model + ":latest"
    for attempt in range(30):
        try:
            names = {m["name"] for m in ollama_request("/api/tags").get("models", [])}
            if wanted not in names:
                print(f"Скачиваю модель {model} (~2 ГБ, один раз, займёт несколько минут)...")
                ollama_request("/api/pull", {"model": model, "stream": False}, timeout=3600)
            print(f"Модель {model} готова")
            return
        except Exception as e:
            print(f"[warn] Ollama недоступна ({e}), повтор через 10 с", file=sys.stderr)
            time.sleep(10)


def material(cluster):
    """Текст сюжета для нейросети: до 5 изданий, описания обрезаны — меньше читать модели."""
    lines = []
    for m in unique_by_source(cluster)[:5]:
        line = f"- {m['source_name']}: {m['title'].rstrip('.')}."
        summary = m["summary"]
        if summary and not summary.startswith(m["title"]):
            if len(summary) > 150:
                summary = summary[:150].rsplit(" ", 1)[0] + "…"
            line += f" {summary}"
        lines.append(line)
    return "\n".join(lines)


def ask_model(prompt, schema, cluster):
    result = ollama_request("/api/chat", {
        "model": os.environ.get("OLLAMA_MODEL", "qwen2.5:3b"),
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": material(cluster)},
        ],
        "format": schema,
        "stream": False,
        "options": {"temperature": 0.2},
    }, timeout=600)
    return json.loads(result["message"]["content"])


def classify(cluster):
    """Шаг 1: короткий ответ «как это отразится на человеке» и три да/нет. None — при сбое."""
    try:
        data = ask_model(CLASSIFY_PROMPT, CLASSIFY_SCHEMA, cluster)
        for k in CHECKS:
            data[k] = data[k] is True
        data["effect_on_person"] = str(data.get("effect_on_person", "")).strip()[:300]
        return data
    except Exception as e:
        print(f"[warn] нейросеть не сработала: {e}", file=sys.stderr)
        return None


def rewrite(cluster):
    """Шаг 2 (только для прошедших отбор): заголовок, пересказ, «что это значит»."""
    try:
        data = ask_model(REWRITE_PROMPT, REWRITE_SCHEMA, cluster)
        result = {
            "headline": data["headline"].strip()[:200],
            "text": data["text"].strip()[:1500],
            "impact": data.get("impact", "").strip()[:500],
        }
        return result if result["headline"] and result["text"] else None
    except Exception as e:
        print(f"[warn] нейросеть не смогла пересказать, публикую заголовок из ленты: {e}",
              file=sys.stderr)
        return None


def assess(cluster):
    """Решение о публикации и, если публикуем, пересказ. Пересказ пишется только для
    прошедших отбор: генерация текста — самая медленная часть работы модели."""
    a = classify(cluster)
    if a is not None and verdict(a):
        a.update(rewrite(cluster) or {})
    return a


def format_post(cluster, assessment=None):
    main = cluster["main"]
    if assessment and assessment.get("headline"):
        headline, body = assessment["headline"], assessment["text"]
        if assessment["impact"]:
            body += f"\n\nЧто это значит: {assessment['impact']}"
    else:
        headline = main["title"]
        body = main["summary"] if not main["summary"].startswith(main["title"]) else ""
    text = f"<b>{html.escape(headline)}</b>"
    if body:
        text += f"\n\n{html.escape(body)}"
    sources = unique_by_source(cluster)
    links = [f'<a href="{html.escape(m["link"])}">{html.escape(m["source_name"])}</a>'
             for m in sources[:6]]
    text += f"\n\nИсточников: {len(sources)} — " + " · ".join(links)
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


def pick_stories(clusters, state, idf, dry_run):
    """Отбирает сюжеты для публикации. Возвращает [(сюжет, ответ нейросети)]."""
    use_llm = bool(os.environ.get("OLLAMA_URL"))
    min_sources = int(os.environ.get("MIN_SOURCES", "3" if use_llm else str(NO_LLM_MIN_SOURCES)))

    candidates = [c for c in clusters if len(c["sources"]) >= min_sources]
    print(f"Кандидатов ({min_sources}+ источников): {len(candidates)}")
    picked, assessed = [], 0
    for c in candidates:
        n = len(c["sources"])
        if find_same(c, state["posted"], idf):
            continue
        prev = find_same(c, state["rejected"], idf)
        if prev and n < prev["sources"] + REASSESS_GROWTH:
            continue  # уже признан неважным, и с тех пор почти никто не подхватил
        if not use_llm:
            picked.append((c, None))
            continue
        if assessed >= MAX_ASSESS:
            break
        assessed += 1
        a = assess(c)
        if a is None:
            continue  # нейросеть сбоит — ничего не публикуем, попробуем в следующий раз
        ok = verdict(a)
        print(f"  [{n} ист.] {'ПУБЛИКУЮ' if ok else 'пропускаю'}: {c['main']['title']}\n"
              f"      {describe(a)} | для человека: {a['effect_on_person']}")
        if ok:
            picked.append((c, a))
        elif not dry_run:
            state["rejected"].append(remember(c))
    return picked


# Набор выдуманных сюжетов для проверки модели: (сообщения изданий, надо ли публиковать)
TEST_CASES = [
    (["Объявлена частичная мобилизация", "Путин подписал указ о частичной мобилизации",
      "В России начинается мобилизация резервистов"], True),
    (["Курс доллара превысил 150 рублей впервые в истории", "Рубль обвалился на 30% за день",
      "ЦБ ввёл экстренные меры из-за обвала рубля"], True),
    (["Госдума приняла закон о повышении НДС до 22% с 1 января",
      "НДС вырастет до 22%: закон принят в третьем чтении", "Путин подписал закон о повышении НДС"], True),
    (["Роскомнадзор полностью заблокировал WhatsApp в России", "WhatsApp перестал работать у россиян",
      "РКН подтвердил блокировку WhatsApp"], True),
    (["В Госдуму внесён законопроект о запрете VPN", "Депутаты предложили запретить VPN",
      "Законопроект о VPN могут рассмотреть осенью"], False),
    (["Землетрясение магнитудой 6,1 произошло у берегов Чили", "В Чили зафиксировали землетрясение",
      "Подземные толчки у побережья Чили, жертв нет"], False),
    (["Лавров провёл переговоры с министром иностранных дел Бразилии",
      "Глава МИД России встретился с бразильским коллегой", "Лавров и Виейра обсудили сотрудничество"], False),
    (["Известная певица вышла замуж в третий раз", "Звезда эстрады сыграла свадьбу на Мальдивах",
      "Певица показала фото со свадьбы"], False),
    (["Сборная России по хоккею обыграла Беларусь", "Хоккеисты России победили Беларусь 4:2",
      "Россия выиграла у Беларуси в хоккей"], False),
    (["Песков: Кремль не комментирует слухи об отставке министра", "В Кремле отказались комментировать слухи",
      "Песков ответил на вопрос об отставке"], False),
    (["В Новосибирске на две недели отключат горячую воду", "Жителям Новосибирска отключат горячую воду",
      "График отключения горячей воды в Новосибирске"], False),
    (["Аналитики прогнозируют рост цен на бензин", "Эксперты ожидают подорожания топлива осенью",
      "Цены на бензин могут вырасти на 10%"], False),
]


def test_model():
    """Прогоняет тестовые сюжеты через нейросеть и показывает, правильно ли она отбирает."""
    if not os.environ.get("OLLAMA_URL"):
        sys.exit("Не задан OLLAMA_URL — нейросеть не настроена")
    ensure_model()
    model = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
    # загружаем модель в память заранее, чтобы время загрузки не портило замер скорости
    started = time.time()
    try:
        ollama_request("/api/generate", {"model": model, "prompt": "", "stream": False}, timeout=600)
    except Exception as e:
        print(f"[warn] не удалось загрузить модель: {e}", file=sys.stderr)
    print(f"Загрузка модели в память: {time.time() - started:.1f} с\n")
    ok, total_time = 0, 0.0
    for titles, expected in TEST_CASES:
        members = [{"source": f"s{i}", "source_name": f"Издание {i + 1}", "title": t, "summary": "",
                    "link": f"https://example.com/{i}"} for i, t in enumerate(titles)]
        cluster = {"members": members, "main": members[0], "sources": {m["source"] for m in members}}
        started = time.time()
        a = assess(cluster)
        total_time += time.time() - started
        if a is None:
            print(f"ОШИБКА  {titles[0]}\n")
            continue
        got = verdict(a)
        ok += got == expected
        print(f"{'OK  ' if got == expected else 'МИМО'}  {'опубликовал бы' if got else 'пропустил бы'}"
              f" (надо {'публиковать' if expected else 'пропустить'}):  {titles[0]}")
        print(f"      {describe(a)} | для человека: {a['effect_on_person']}")
        if got and a.get("headline"):
            print(f"      пост: {a['headline']} — {a['text']} Что это значит: {a['impact']}")
        print()
    n = len(TEST_CASES)
    print(f"Итог: {ok}/{n} решений верны, в среднем {total_time / n:.1f} с на сюжет")
    if ok < n * 0.8:
        print("Модель отбирает неточно. Попробуйте OLLAMA_MODEL=qwen2.5:7b (нужно ~8 ГБ памяти).")


def run_once(dry_run):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHANNEL_ID")
    max_posts = int(os.environ.get("MAX_POSTS", "3"))
    window = float(os.environ.get("WINDOW_HOURS", "6")) * 3600

    state = load_state()
    # копим новости между запусками: ленты отдают лишь последние N записей
    state["items"].update(fetch_items(read_feeds(), window))
    now = time.time()
    items = {k: v for k, v in state["items"].items() if now - v["published"] < window}
    clusters, idf = build_clusters(items)
    print(f"Новостей за окно: {len(items)}, сюжетов: {len(clusters)}")

    if dry_run:
        print("\nКрупнейшие сюжеты:")
        for c in clusters[:10]:
            mark = "уже было" if find_same(c, state["posted"], idf) else ""
            print(f"  {len(c['sources']):2d} ист. | {c['main']['title']}  {mark}")
        print()

    picked = pick_stories(clusters, state, idf, dry_run)

    if dry_run:
        for c, a in picked[:max_posts]:
            print("\n--- пост ---\n" + format_post(c, a))
        return

    posted = 0
    for c, a in picked[:max_posts]:
        send_telegram(token, chat_id, format_post(c, a))
        posted += 1
        state["posted"].append(remember(c))
        save_state(state, window)  # сохраняем сразу, чтобы при сбое не запостить повторно
        time.sleep(1)
    print(f"Опубликовано: {posted}")
    save_state(state, window)


def main():
    parser = argparse.ArgumentParser(description="Постит важные новости в телеграм-канал")
    parser.add_argument("--dry-run", action="store_true",
                        help="ничего не отправлять, показать самые крупные сюжеты")
    parser.add_argument("--check-feeds", action="store_true",
                        help="проверить, какие ленты из feeds.txt работают")
    parser.add_argument("--test-model", action="store_true",
                        help="проверить, как нейросеть оценивает важность на тестовых сюжетах")
    parser.add_argument("--loop", action="store_true",
                        help="работать постоянно, запускаясь каждые INTERVAL_MINUTES минут")
    args = parser.parse_args()

    if args.check_feeds:
        check_feeds()
        return

    load_env()
    if args.test_model:
        test_model()
        return
    if not args.dry_run and not (os.environ.get("TELEGRAM_BOT_TOKEN")
                                 and os.environ.get("TELEGRAM_CHANNEL_ID")):
        sys.exit("Задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHANNEL_ID в .env")

    if not args.loop:
        run_once(args.dry_run)
        return

    interval = float(os.environ.get("INTERVAL_MINUTES", "30")) * 60
    ensure_model()
    while True:
        print(time.strftime("%Y-%m-%d %H:%M:%S"), "— запуск")
        try:
            run_once(args.dry_run)
        except Exception as e:
            print(f"[error] {e}", file=sys.stderr)
        sys.stdout.flush()
        time.sleep(interval)


if __name__ == "__main__":
    main()
