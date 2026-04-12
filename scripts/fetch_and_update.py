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

SERPER_API_KEY = os.environ["SERPER_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

JST = timezone(timedelta(hours=9))
DATA_FILE = Path(__file__).parent.parent / "data" / "results.json"
MAX_ITEMS = 200

SEARCH_QUERIES = [
    "ホタルイカ 身投げ 新潟 漁港 OR 海岸 OR 浜",
    "ホタルイカ 掬い 富山 漁港 OR 海岸 OR 浜",
    "ほたるいか 身投げ 2025",
]

EXCLUDE_KEYWORDS = ["水揚げ", "漁獲量", "競り", "競売", "プロ漁師", "kg", "トン", "卸売"]


def search_serper(query: str) -> list[dict]:
    """Serper APIで検索してorganic resultsを返す"""
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": query, "gl": "jp", "hl": "ja", "num": 10},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("organic", [])
    except Exception as e:
        logger.error(f"Serper検索エラー [{query}]: {e}")
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


def process_with_claude(title: str, snippet: str, url: str) -> dict | None:
    """Anthropic APIで記事を処理。除外すべきなら None を返す"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""以下の記事情報を分析してください。

タイトル: {title}
スニペット: {snippet}
URL: {url}

判断基準:
- 「プロ漁師の水揚げ・漁獲量・競り・kg・トン」に関する記事は除外（is_relevant: false）
- 個人・観光客がホタルイカを掬ったり見たりした体験情報のみ残す（is_relevant: true）
- ホタルイカの身投げや掬い体験に関係ない記事も除外

以下のJSON形式のみで返してください（他のテキスト不要）:
{{
  "is_relevant": true または false,
  "summary": "100字以内の要約（is_relevant=falseの場合は空文字）",
  "location": "場所名（〇〇漁港・〇〇海岸・〇〇浜など、不明なら空文字）"
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

    new_items = []

    for query in SEARCH_QUERIES:
        logger.info(f"検索中: {query}")
        results = search_serper(query)
        logger.info(f"  {len(results)}件ヒット")

        for r in results:
            url = r.get("link", "")
            title = r.get("title", "")
            snippet = r.get("snippet", "")

            if not url or url in existing_urls:
                continue

            # 明らかな除外キーワードチェック
            combined = title + snippet
            if any(kw in combined for kw in EXCLUDE_KEYWORDS):
                logger.info(f"  スキップ（除外KW）: {title[:40]}")
                continue

            logger.info(f"  処理中: {title[:40]}")
            analysis = process_with_claude(title, snippet, url)

            if not analysis or not analysis.get("is_relevant"):
                logger.info(f"  除外（AI判定）: {title[:40]}")
                continue

            thumbnail = fetch_ogp_image(url)

            item = {
                "id": make_id(url),
                "title": title,
                "url": url,
                "summary": analysis.get("summary", ""),
                "location": analysis.get("location", ""),
                "thumbnail": thumbnail,
                "fetched_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
            }
            new_items.append(item)
            existing_urls.add(url)
            logger.info(f"  追加: {title[:40]} / {item['location']}")

    if new_items:
        combined = new_items + existing
        combined = combined[:MAX_ITEMS]
        save_results(combined)
        logger.info(f"=== {len(new_items)}件追加、合計{len(combined)}件 ===")
    else:
        logger.info("=== 新着なし ===")


if __name__ == "__main__":
    main()
