"""Готовые картинки из интернета: Memepedia + русскоязычный Pikabu через Meme API.

Никаких аккаунтов, подписей пользователя и ручной копилки для поиска не нужно.
Запросы идут только на фиксированные публичные источники; изображения получает Telegram.
"""
from __future__ import annotations

import asyncio
from html.parser import HTMLParser
import json
import random
import re
import time
import urllib.parse
import urllib.request

MEDIA_HOSTS = {"memepedia.ru", "www.memepedia.ru", "memepedia.world", "www.memepedia.world",
               "i.redd.it", "preview.redd.it"}
PAGE_HOSTS = {"memepedia.ru", "www.memepedia.ru", "memepedia.world", "www.memepedia.world", "meme-api.com"}


def safe_image(url):
    if not isinstance(url, str) or len(url) > 2048:
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname in MEDIA_HOSTS
                and not parsed.username and not parsed.password and parsed.port in (None, 443)
                and bool(re.search(r"\.(?:jpg|jpeg|png|webp|gif)$", parsed.path, re.I)))
    except ValueError:
        return False


def wire_url(url):
    """Telegram и urllib требуют ASCII-URL, в том числе для русских имён файлов."""
    return urllib.parse.quote(url, safe=":/?=&%+-._~")


class SourceRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname not in PAGE_HOSTS or parsed.username or parsed.port not in (None, 443):
            raise ValueError("Unsupported source redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def read_source(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in PAGE_HOSTS or parsed.username or parsed.port not in (None, 443):
        raise ValueError("Unsupported source")
    request = urllib.request.Request(wire_url(url), headers={"User-Agent": "KoreshBot/2.0 (meme lookup)"})
    with urllib.request.build_opener(SourceRedirect()).open(request, timeout=10) as response:
        data = response.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("Source response too large")
    return data.decode("utf-8", errors="replace")


class MemePage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.images = [], []
        self.heading = 0
        self.anchor = None
        self.words = []
        self.skip_templates = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("h1", "h2", "h3"):
            self.heading += 1
            self.words = []
        if tag == "a" and self.heading:
            self.anchor = attrs.get("href", "")
        if tag == "img" and not self.skip_templates:
            classes = attrs.get("class", "")
            url = attrs.get("data-src") or attrs.get("src", "")
            label = attrs.get("alt") or attrs.get("title", "")
            width = int(attrs.get("width", "0")) if attrs.get("width", "0").isdigit() else 0
            height = int(attrs.get("height", "0")) if attrs.get("height", "0").isdigit() else 0
            gallery = "wp-image-" in classes or (not classes and width >= 300 and height >= 200 and "/wp-content/uploads/" in url)
            # Галерея готовых мемов, без логотипов, обложек статьи и связанных новостей.
            if (gallery and "wp-post-image" not in classes
                    and safe_image(url) and not re.search(r"шаблон|template|исходник", label, re.I)):
                self.images.append({"url": wire_url(url), "title": label[:200]})

    def handle_data(self, text):
        if self.heading:
            self.words.append(text)

    def handle_endtag(self, tag):
        if tag in ("h1", "h2", "h3") and self.heading:
            title = " ".join(" ".join(self.words).split())
            if self.anchor:
                parsed = urllib.parse.urlsplit(self.anchor)
                if parsed.hostname in PAGE_HOSTS and parsed.hostname != "meme-api.com":
                    self.links.append((self.anchor, title))
            self.skip_templates = bool(re.search(r"шаблон|template|оригинал", title, re.I))
            self.heading = max(0, self.heading - 1)
            self.anchor = None


def search_terms(query):
    stop = {"кореш", "пожалуйста", "скинь", "покажи", "найди", "давай", "мем", "мемы", "какой", "про", "тему", "для", "когда", "готовый", "интернет", "картинку"}
    words = [word for word in re.findall(r"[а-яёa-z]{3,}", query.casefold().replace("ё", "е")) if word not in stop and not word.startswith("котор")]
    terms = []
    for word in words:
        if word.startswith(("кот", "кошк", "кошеч")):
            terms.append("кот")
            continue
        for ending in ("ами", "ями", "ого", "ыми", "ими", "ая", "яя", "ое", "ее", "ые", "ие", "ой", "ый", "ий", "ов", "ев", "ом", "ам", "ах", "ях", "а", "я", "ы", "и", "у", "ю", "е", "о", "ь"):
            if word.endswith(ending) and len(word) - len(ending) >= 3:
                word = word[:-len(ending)]
                break
        terms.append(word[:5])
    return terms[:8]


def relevance(terms, title):
    words = [word for word in re.findall(r"[а-яёa-z]{3,}", title.casefold().replace("ё", "е")) if not word.startswith("котор")]
    words = ["кот" if word.startswith(("кошк", "кошеч")) else word for word in words]
    return sum(any(word.startswith(term) for word in words) for term in terms)


class InternetMemes:
    def __init__(self):
        self.cache = {}

    async def candidates(self, query=""):
        key = query.casefold().strip()[:100]
        cached = self.cache.get(key)
        if cached and time.time() - cached[0] < 600:
            return list(cached[1])
        # Обе ветки независимы; недоступность одного сайта не блокирует второй.
        results = await asyncio.gather(
            asyncio.to_thread(self._memepedia, key) if key else asyncio.sleep(0, result=[]),
            asyncio.to_thread(self._pikabu), return_exceptions=True)
        items = []
        terms = search_terms(key)
        for result in results:
            if isinstance(result, list):
                for item in result:
                    score = relevance(terms, item["title"])
                    item = dict(item, score=score + item.get("score", 0))
                    items.append(item)
        unique = {item["url"]: item for item in items if safe_image(item["url"])}
        items = list(unique.values())
        # Тематический поиск не подменяем случайной картинкой, если совпадений нет.
        if terms:
            items = [item for item in items if item["score"] > 0]
        self.cache[key] = (time.time(), items)
        if len(self.cache) > 50:
            self.cache.pop(next(iter(self.cache)))
        return list(items)

    def _memepedia(self, query):
        page = MemePage()
        # Короткий поисковый запрос даёт больше готовых мемов, чем полное описание сцены.
        raw_words = re.findall(r"[а-яёa-z]{3,}", query.casefold())
        raw_words = [word for word in raw_words if word not in {"мем", "мемы", "скинь", "про", "готовый"}]
        terms = search_terms(query)
        if "кот" in terms:
            search = "коты мем"
        else:
            search = " ".join(raw_words[:2]) + " мем"
        page.feed(read_source("https://memepedia.ru/?s=" + urllib.parse.quote(search)))
        ranked = sorted(page.links, key=lambda x: relevance(terms, x[1]), reverse=True)
        items = []
        deadline = time.monotonic() + 18
        for url, title in ranked[:5]:
            score = relevance(terms, title)
            if not score or time.monotonic() > deadline:
                continue
            try:
                article = MemePage()
                article.feed(read_source(url))
                for item in article.images:
                    items.append(dict(item, title=title + (": " + item["title"] if item["title"] else ""),
                                      source=url, score=score + int("только мемы" in title.casefold() or "мемы про" in title.casefold())))
            except (OSError, ValueError):
                continue
            if len(items) >= 20:
                break
        return items

    def _pikabu(self):
        data = json.loads(read_source("https://meme-api.com/gimme/Pikabu/30"))
        return [{"url": item["url"], "title": item.get("title", "Мем"), "source": item.get("postLink", "https://www.reddit.com/r/Pikabu/")}
                for item in data.get("memes", []) if not item.get("nsfw") and not item.get("spoiler") and safe_image(item.get("url"))]

    async def find(self, query="", excluded=()):
        items = [item for item in await self.candidates(query) if item["url"] not in set(excluded)]
        if not items:
            return []
        random.shuffle(items)
        return sorted(items, key=lambda item: item["score"], reverse=True)[:5]
