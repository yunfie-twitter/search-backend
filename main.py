import asyncio
import json
import os
import random
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Literal, List, Optional, Dict, Any
import re

from fastapi import FastAPI, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
import httpx
from bs4 import BeautifulSoup
import redis.asyncio as redis
from playwright.sync_api import sync_playwright

app = FastAPI(title="Wholphin Search API - Wikipedia Panel対応")

# CORS設定（変更なし）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Redis設定（変更なし）
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
rd = redis.from_url(REDIS_URL, decode_responses=True)
CACHE_EXPIRE = 3600

# 外部API設定
SEARXNG_URL = "https://search.p2pear.asia/search"
MISSKEY_API_URL = "https://pjsekai.world/api/notes/search"
WIKIPEDIA_SUMMARY_API = "https://ja.wikipedia.org/api/rest_v1/page/summary/{title}"

# HTTPクライアント設定（変更なし）
LIMITS = httpx.Limits(max_keepalive_connections=20, max_connections=100)
TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# ==========================================
# **Wikipediaタイトル抽出・正規化**
# ==========================================
def extract_wiki_title(url: str) -> Optional[str]:
    """Wikipedia記事のタイトルをURLから抽出・正規化"""
    if "wikipedia.org" not in url:
        return None
    
    # URLからタイトル抽出
    match = re.search(r'/wiki/([^/?#]+)', url)
    if not match:
        return None
    
    title = urllib.parse.unquote(match.group(1))
    # タイトル正規化（アンダースコア→スペース）
    title = title.replace('_', ' ')
    return title

async def fetch_wikipedia_panel(client: httpx.AsyncClient, title: str) -> Optional[Dict]:
    """Wikipedia REST APIで詳細情報取得"""
    api_url = WIKIPEDIA_SUMMARY_API.format(title=urllib.parse.quote(title, safe=''))
    
    try:
        resp = await client.get(api_url, timeout=3.0)
        if resp.status_code == 200:
            data = resp.json()
            return {
                "title": data.get("title", ""),
                "description": data.get("description", ""),
                "extract": data.get("extract", ""),
                "thumbnail": {
                    "url": data.get("thumbnail", {}).get("url", ""),
                    "width": data.get("thumbnail", {}).get("width"),
                    "height": data.get("thumbnail", {}).get("height")
                },
                "birth_date": data.get("birth_date", ""),
                "death_date": data.get("death_date", ""),
                "infobox": data.get("infobox", {}),
                "api_url": api_url
            }
    except Exception:
        pass
    return None

# ==========================================
# **プロキシURL生成・SearXNG検索（変更なし）**
# ==========================================
async def _get_valid_proxy_url(client: httpx.AsyncClient, original_url: str) -> Optional[str]:
    if not original_url: return None
    proxy_base = "https://proxy.wholphin.net/image.webp?url="
    proxy_url = f"{proxy_base}{urllib.parse.quote(original_url)}"
    try:
        resp = await client.head(proxy_url, timeout=1.0)
        if resp.status_code == 403 or resp.status_code >= 400:
            return original_url
        return proxy_url
    except Exception:
        return original_url

async def fetch_searxng_parallel(q: str, pages: int, category: str, result_parser, extra_processing=None):
    async with httpx.AsyncClient(limits=LIMITS, timeout=TIMEOUT) as client:
        async def fetch_page(page_num: int):
            params = {"q": q, "categories": category, "format": "json", "pageno": page_num}
            try:
                resp = await client.get(SEARXNG_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
                results = data.get("results", [])
                parsed_items = result_parser(results, page_num)
                
                if extra_processing and parsed_items:
                    parsed_items = await asyncio.gather(
                        *[extra_processing(client, item) for item in parsed_items]
                    )
                return parsed_items
            except Exception as e:
                print(f"SearXNG {category} Error Page {page_num}: {e}")
                return []
        tasks = [fetch_page(i + 1) for i in range(pages)]
        pages_results = await asyncio.gather(*tasks)
    return [item for page_result in pages_results for item in page_result]

# Google Suggest・Misskey（変更なし、省略）
async def fetch_suggest(q: str):  # 既存実装保持
    url = f"https://www.google.com/complete/search?hl=ja&output=toolbar&q={urllib.parse.quote(q)}"
    async with httpx.AsyncClient(timeout=3.0) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            return [{"title": cs.find('suggestion').get('data')} 
                    for cs in root.findall('CompleteSuggestion') 
                    if cs.find('suggestion') is not None]
        except Exception:
            return []

async def fetch_social_misskey(q: str, pages: int):  # 既存実装保持
    # ... 既存のMisskey実装（変更なし）
    return []

# ==========================================
# **Web/News/Images/Video（Wikipedia拡張）**
# ==========================================
def _get_favicon(url):  # 変更なし
    if not url: return None
    return f"https://t1.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url={urllib.parse.quote(url)}&size=128"

async def fetch_web_searxng(q, pages):
    def parser(results, p):
        items = []
        for i in results:
            if not (i.get("title") and i.get("url")): continue
            item = {
                "title": i.get("title"),
                "summary": i.get("content", ""),
                "url": i.get("url"),
                "favicon": _get_favicon(i.get("url")),
                "wiki_title": extract_wiki_title(i.get("url")),  # 👈 新規追加
                "page": p
            }
            items.append(item)
        return items
    return await fetch_searxng_parallel(q, pages, "general", parser)

async def fetch_news_searxng(q, pages):  # 変更なし
    def parser(results, p):
        return [{
            "title": i.get("title"),
            "summary": i.get("content"),
            "url": i.get("url"),
            "favicon": _get_favicon(i.get("url")),
            "publishedDate": i.get("publishedDate"),
            "page": p
        } for i in results if i.get("title") and i.get("url")]
    return await fetch_searxng_parallel(q, pages, "news", parser)

async def fetch_images_searxng(q, pages):  # 変更なし
    def parser(results, p):
        return [{
            "title": i.get("title", "No Title"),
            "url": i.get("url"),
            "raw_thumb": i.get("thumbnail_src") or i.get("img_src"),
            "domain": i.get("source"),
            "page": p
        } for i in results if i.get("url") and (i.get("thumbnail_src") or i.get("img_src"))]
    async def processor(client, item):
        raw = item.pop("raw_thumb", None)
        item["thumbnail"] = await _get_valid_proxy_url(client, raw)
        return item
    return await fetch_searxng_parallel(q, pages, "images", parser, processor)

async def fetch_video_searxng(q, pages):  # 変更なし
    def parser(results, p):
        return [{
            "title": i.get("title"),
            "url": i.get("url"),
            "raw_thumb": i.get("thumbnail"),
            "duration": i.get("duration"),
            "publishedDate": i.get("publishedDate"),
            "page": p
        } for i in results if i.get("title") and i.get("url")]
    async def processor(client, item):
        raw = item.pop("raw_thumb", None)
        item["thumbnail"] = await _get_valid_proxy_url(client, raw)
        return item
    return await fetch_searxng_parallel(q, pages, "videos", parser, processor)

# ==========================================
# **Wikipedia Panel専用検索（新規）**
# ==========================================
async def fetch_panel_searxng(q: str, pages: int = 1):
    """Web検索→Wikipedia Panel情報取得"""
    raw_results = await fetch_web_searxng(q, pages)
    
    async with httpx.AsyncClient(limits=LIMITS, timeout=TIMEOUT) as client:
        # Wikipedia記事がある結果のみフィルタ
        wiki_candidates = [r for r in raw_results if r.get("wiki_title")]
        
        if not wiki_candidates:
            return {"query": q, "type": "panel", "wiki_found": False, "results": raw_results[:3]}
        
        # 1つ目のWikipedia記事でPanel情報取得（高速化）
        first_wiki = wiki_candidates[0]
        wiki_title = first_wiki["wiki_title"]
        
        wiki_panel = await fetch_wikipedia_panel(client, wiki_title)
        
        # 結果にPanel情報追加
        first_wiki["wikipedia_panel"] = wiki_panel
        first_wiki["wiki_priority"] = True
        
        result = {
            "query": q,
            "type": "panel",
            "wiki_found": bool(wiki_panel),
            "wiki_title": wiki_title,
            "results": raw_results,
            "featured_panel": first_wiki if wiki_panel else None
        }
        return result

# ==========================================
# **共通実行・キャッシュ（panel追加）**
# ==========================================
async def _exec_search_and_cache(q: str, pages: int, type: str):
    cache_key = f"search:{type}:{q}:{pages}"
    
    try:
        if await rd.exists(cache_key):
            cached = await rd.get(cache_key)
            return json.loads(cached)
    except Exception:
        pass

    raw_results = []
    try:
        if type == "image":
            raw_results = await fetch_images_searxng(q, pages)
        elif type == "video":
            raw_results = await fetch_video_searxng(q, pages)
        elif type == "news":
            raw_results = await fetch_news_searxng(q, pages)
        elif type == "social":
            raw_results = await fetch_social_misskey(q, pages)
        elif type == "web":
            raw_results = await fetch_web_searxng(q, pages)
        elif type == "panel":  # 👈 新規追加
            raw_results = await fetch_panel_searxng(q, pages)
        elif type == "suggest":
            raw_results = await fetch_suggest(q)
    except Exception as e:
        print(f"Error in _exec_search_and_cache ({type}): {e}")
        return None

    if isinstance(raw_results, dict):  # panelはdictを返す
        resp = raw_results
    else:
        unique_results = []
        if type != "suggest":
            seen = set()
            for item in raw_results:
                key = item.get("url")
                if key and key not in seen:
                    seen.add(key)
                    unique_results.append(item)
            resp = {
                "query": q, "type": type, "source": "live",
                "count": len(unique_results), "results": unique_results, "pagination": []
            }
        else:
            resp = {"query": q, "type": type, "results": raw_results}

    await rd.setex(cache_key, CACHE_EXPIRE, json.dumps(resp, ensure_ascii=False))
    return resp

# ==========================================
# **エンドポイント（panel追加）**
# ==========================================
@app.get("/search")
async def search_endpoint(
    background_tasks: BackgroundTasks,
    q: str = Query(..., description="検索ワード"),
    pages: int = Query(1, ge=1, le=5),
    type: Literal["web", "image", "suggest", "video", "news", "social", "panel"] = Query("web")  # 👈 panel追加
):
    result = await _exec_search_and_cache(q, pages, type)
    
    if result is None:
        return {"query": q, "type": type, "count": 0, "results": [], "error": "Search failed"}

    # バックグラウンドプリフェッチ
    if type == "web":
        background_tasks.add_task(_exec_search_and_cache, q, pages, "image")
        background_tasks.add_task(_exec_search_and_cache, q, pages, "video")
        background_tasks.add_task(_exec_search_and_cache, q, pages, "news")
        background_tasks.add_task(_exec_search_and_cache, q, pages, "panel")  # 👈 panelも追加

    return result

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
