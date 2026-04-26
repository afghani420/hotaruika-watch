#!/usr/bin/env python3
"""
ホタルイカ身投げ・掬い情報 収集スクリプト
- Serper APIで検索
- Anthropic APIで要約・フィルタリング
- data/results.json に新着順で蓄積
"""

import os
import json
import hashlib
import logging
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
import anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BRAVE_API_KEY = os.environ["BRAVE_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")

JST = timezone(timedelta(hours=9))
DATA_FILE = Path(__file__).parent.parent / "data" / "results.json"
GEOCACHE_FILE = Path(__file__).parent.parent / "data" / "geocache.json"
MAX_ITEMS = 200

SEARCH_QUERIES = [
    "ホタルイカ 身投げ 新潟 漁港 OR 海岸 OR 浜",
    "ホタルイカ 掬い 富山 漁港 OR 海岸 OR 浜",
    "ほたるいか 身投げ 2025",
    # X（旧Twitter）限定検索
    "site:x.com ホタルイカ 身投げ 新潟 OR 富山",
    "site:x.com ほたるいか 掬い 2025",
    # Instagram
    "site:instagram.com ホタルイカ 身投げ OR 掬い",
    # ブログ
    "site:note.com ほたるいか 身投げ OR 掬い",
    "site:ameblo.jp ホタルイカ 身投げ OR 掬い 新潟 OR 富山",
    "site:hatenablog.com ほたるいか 身投げ OR 掬い",
    # YouTube
    "site:youtube.com ほたるいか 身投げ OR 掬い 2025",
]

EXCLUDE_KEYWORDS = ["水揚げ", "漁獲量", "競り", "競売", "プロ漁師", "kg", "トン", "卸売"]


def extract_youtube_id(url: str) -> str | None:
    """YouTube URLから動画IDを抽出する"""
    import re
    patterns = [
        r"youtube\.com/watch\?.*v=([A-Za-z0-9_-]{11})",
        r"youtu\.be/([A-Za-z0-9_-]{11})",
        r"youtube\.com/shorts/([A-Za-z0-9_-]{11})",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def fetch_youtube_published_at(video_id: str) -> str | None:
    """YouTube Data API v3 で公開日時を取得（YYYY-MM-DD形式で返す）"""
    if not YOUTUBE_API_KEY:
        return None
    try:
        resp = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"id": video_id, "part": "snippet", "key": YOUTUBE_API_KEY},
            timeout=10,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if not items:
            return None
        published = items[0]["snippet"]["publishedAt"]  # e.g. "2026-04-10T03:22:00Z"
        return published[:10]  # YYYY-MM-DD
    except Exception as e:
        logger.warning(f"YouTube API エラー [{video_id}]: {e}")
        return None


def load_geocache() -> dict:
    if GEOCACHE_FILE.exists():
        try:
            return json.loads(GEOCACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_geocache(cache: dict):
    GEOCACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def geocode_location(location: str, cache: dict) -> tuple[float | None, float | None]:
    """Nominatim APIで場所名を緯度経度に変換（キャッシュ付き）"""
    if not location:
        return None, None
    if location in cache:
        entry = cache[location]
        return entry.get("lat"), entry.get("lng")
    try:
        import time
        time.sleep(1.1)  # Nominatim利用規約: 1秒に1リクエスト
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": f"{location} 日本", "format": "json", "limit": 1, "countrycodes": "jp"},
            headers={"User-Agent": "hotaruika-watch/1.0 (github.com/afghani420/hotaruika-watch)"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data:
            lat = float(data[0]["lat"])
            lng = float(data[0]["lon"])
            cache[location] = {"lat": lat, "lng": lng}
            return lat, lng
        cache[location] = {"lat": None, "lng": None}
    except Exception as e:
        logger.warning(f"ジオコーディング失敗 [{location}]: {e}")
        cache[location] = {"lat": None, "lng": None}
    return None, None


def search_brave(query: str) -> list[dict]:
    """Brave Search APIで検索してweb resultsを返す"""
    try:
        resp = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"X-Subscription-Token": BRAVE_API_KEY, "Accept": "application/json"},
            params={"q": query, "count": 20, "country": "jp", "search_lang": "ja"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("web", {}).get("results", [])
    except Exception as e:
        logger.error(f"Brave検索エラー [{query}]: {e}")
        return []


def fetch_ogp_image(url: str) -> str | None:
    """URLからOGP画像URLを取得する"""
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        from html.parser import HTMLParser

        class OGPParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.image = None

            def handle_starttag(self, tag, attrs):
                if tag == "meta":
                    attrs_dict = dict(attrs)
                    if attrs_dict.get("property") == "og:image":
                        self.image = attrs_dict.get("content")

        parser = OGPParser()
        parser.feed(resp.text[:10000])
        return parser.image
    except Exception:
        return None


def process_with_claude(title: str, snippet: str, url: str, serper_date: str = "") -> dict | None:
    """Anthropic APIで記事を処理。除外すべきなら None を返す"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    today = datetime.now(JST).strftime("%Y-%m-%d")
    prompt = f"""以下の記事情報を分析してください。

タイトル: {title}
スニペット: {snippet}
URL: {url}
検索結果の日付表示: {serper_date or "不明"}
本日の日付: {today}

判断基準:
- 「プロ漁師の水揚げ・漁獲量・競り・kg・トン」に関する記事は除外（is_relevant: false）
- 個人・観光客がホタルイカを掬ったり見たりした体験情報のみ残す（is_relevant: true）
- ホタルイカの身投げや掬い体験に関係ない記事も除外

以下のJSON形式のみで返してください（他のテキスト不要）:
{{
  "is_relevant": true または false,
  "summary": "100字以内の要約（is_relevant=falseの場合は空文字）",
  "location": "場所名（〇〇漁港・〇〇海岸・〇〇浜など、不明なら空文字）",
  "published_at": "記事の公開日（YYYY-MM-DD形式。タイトル・スニペット・日付表示から推定。年だけ分かる場合はYYYY-01-01。不明ならnull）"
}}"""

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text.strip()
        # JSONブロックを抽出
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        result = json.loads(text)
        return result
    except Exception as e:
        logger.error(f"Claude処理エラー [{url}]: {e}")
        return None


def make_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def load_existing() -> list[dict]:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_results(items: list[dict]):
    DATA_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main():
    logger.info("=== ホタルイカ情報収集開始 ===")
    existing = load_existing()
    existing_urls = {item["url"] for item in existing}
    geocache = load_geocache()

    new_items = []

    for query in SEARCH_QUERIES:
        logger.info(f"検索中: {query}")
        results = search_brave(query)
        logger.info(f"  {len(results)}件ヒット")

        for r in results:
            url = r.get("url", "")
            title = r.get("title", "")
            snippet = r.get("description", "")

            if not url or url in existing_urls:
                continue

            # 明らかな除外キーワードチェック
            combined = title + snippet
            if any(kw in combined for kw in EXCLUDE_KEYWORDS):
                logger.info(f"  スキップ（除外KW）: {title[:40]}")
                continue

            logger.info(f"  処理中: {title[:40]}")
            serper_date = r.get("page_age", "")
            analysis = process_with_claude(title, snippet, url, serper_date)

            if not analysis or not analysis.get("is_relevant"):
                logger.info(f"  除外（AI判定）: {title[:40]}")
                continue

            thumbnail = fetch_ogp_image(url)

            # YouTube URLなら API から正確な公開日を取得
            published_at = analysis.get("published_at")
            yt_id = extract_youtube_id(url)
            if yt_id:
                yt_date = fetch_youtube_published_at(yt_id)
                if yt_date:
                    published_at = yt_date
                    logger.info(f"  YouTube公開日取得: {yt_date}")

            location = analysis.get("location", "")
            lat, lng = geocode_location(location, geocache)

            item = {
                "id": make_id(url),
                "title": title,
                "url": url,
                "summary": analysis.get("summary", ""),
                "location": location,
                "lat": lat,
                "lng": lng,
                "published_at": published_at,
                "thumbnail": thumbnail,
                "fetched_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
            }
            new_items.append(item)
            existing_urls.add(url)
            logger.info(f"  追加: {title[:40]} / {location} ({lat},{lng})")

    save_geocache(geocache)

    if new_items:
        combined = new_items + existing
        combined = combined[:MAX_ITEMS]
        # published_at がある記事を優先して新しい順にソート
        def sort_key(item):
            pub = item.get("published_at")
            if pub:
                return (0, pub)
            return (1, item.get("fetched_at", ""))
        combined.sort(key=sort_key, reverse=True)
        save_results(combined)
        logger.info(f"=== {len(new_items)}件追加、合計{len(combined)}件 ===")
    else:
        logger.info("=== 新着なし ===")


if __name__ == "__main__":
    main()
