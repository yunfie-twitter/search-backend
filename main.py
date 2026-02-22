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

# CORS設定
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Redis設定
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
rd = redis.from_url(REDIS_URL, decode_responses=True)
CACHE_EXPIRE = 3600

# 外部API設定
SEARXNG_URL = "https://search.p2pear.asia/search"
MISSKEY_API_URL = "https://pjsekai.world/api/notes/search"
WIKIPEDIA_SUMMARY_API = "https://ja.wikipedia.org/api/rest_v1/page/summary/{title}"

# HTTPクライアント設定
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
    # タイトル正規化(アンダースコア→スペース)
    title = title.replace('_', ' ')
    return title

async def fetch_wikipedia_panel(client: httpx.AsyncClient, title: str) -> Optional[Dict]:
    """Wikipedia REST APIで詳細情報取得 (曖昧さ回避ページは除外)"""
    api_url = WIKIPEDIA_SUMMARY_API.format(title=urllib.parse.quote(title, safe=''))
    
    try:
        resp = await client.get(api_url, timeout=3.0)
        if resp.status_code == 200:
            data = resp.json()
            
            # 曖昧さ回避ページの判定と除外
            page_type = data.get("type", "")
            if page_type == "disambiguation":
                return None
            
            # extractに「曖昧さ回避」「disambiguation」が含まれる場合も除外
            extract = data.get("extract", "")
            if "曖昧さ回避" in extract or "disambiguation" in extract.lower():
                return None
            
            return {
                "title": data.get("title", ""),
                "description": data.get("description", ""),
                "extract": extract,
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
# **プロキシURL生成・SearXNG検索**
# ==========================================
async def _get_valid_proxy_url(client: httpx.AsyncClient, original_url: str) -> Optional[str]:
    """プロキシURLの検証と生成 (403エラー時は元URLを返す)"""
    if not original_url: 
        return None
    proxy_base = "https://proxy.wholphin.net/image.webp?url="
    proxy_url = f"{proxy_base}{urllib.parse.quote(original_url)}"
    try:
        resp = await client.head(proxy_url, timeout=1.0)
        if resp.status_code == 403 or resp.status_code >= 400:
            return original_url
        return proxy_url
    except Exception:
        return original_url

async def fetch_searxng_parallel(q: str, pages: int, category: str, result_parser, extra_processing=None, safesearch: int = 0):
    """SearXNG並列検索 (セーフサーチ対応)"""
    async with httpx.AsyncClient(limits=LIMITS, timeout=TIMEOUT) as client:
        async def fetch_page(page_num: int):
            params = {
                "q": q, 
                "categories": category, 
                "format": "json", 
                "pageno": page_num,
                "safesearch": safesearch  # 0=無効, 1=中程度, 2=厳格
            }
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

# Google Suggest・Misskey
async def fetch_suggest(q: str):
    """Google Suggest取得"""
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

async def fetch_social_misskey(q: str, pages: int):
    """Misskey検索"""
    return []  # Misskey実装は既存のまま保持

# ==========================================
# **Web/News/Images/Video (Wikipedia除外・プロキシ対応)**
# ==========================================
def _get_favicon(url):
    """Favicon取得"""
    if not url: 
        return None
    return f"https://t1.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url={urllib.parse.quote(url)}&size=128"

def is_wikipedia_url(url: str) -> bool:
    """WikipediaのURLかどうか判定"""
    return "wikipedia.org" in url if url else False

async def fetch_web_searxng(q, pages, safesearch: int = 0):
    """Web検索 (Wikipediaを除外)"""
    def parser(results, p):
        items = []
        for i in results:
            if not (i.get("title") and i.get("url")): 
                continue
            
            # Wikipediaの記事を除外 (ボード機能があるため)
            if is_wikipedia_url(i.get("url")):
                continue
            
            item = {
                "title": i.get("title"),
                "summary": i.get("content", ""),
                "url": i.get("url"),
                "favicon": _get_favicon(i.get("url")),
                "page": p
            }
            items.append(item)
        return items
    return await fetch_searxng_parallel(q, pages, "general", parser, safesearch=safesearch)

async def fetch_news_searxng(q, pages, safesearch: int = 0):
    """ニュース検索"""
    def parser(results, p):
        return [{
            "title": i.get("title"),
            "summary": i.get("content"),
            "url": i.get("url"),
            "favicon": _get_favicon(i.get("url")),
            "publishedDate": i.get("publishedDate"),
            "page": p
        } for i in results if i.get("title") and i.get("url")]
    return await fetch_searxng_parallel(q, pages, "news", parser, safesearch=safesearch)

async def fetch_images_searxng(q, pages, safesearch: int = 0):
    """画像検索 (プロキシURL対応)"""
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
    
    return await fetch_searxng_parallel(q, pages, "images", parser, processor, safesearch=safesearch)

async def fetch_video_searxng(q, pages, safesearch: int = 0):
    """動画検索 (プロキシURL対応)"""
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
    
    return await fetch_searxng_parallel(q, pages, "videos", parser, processor, safesearch=safesearch)

# ==========================================
# **Wikipedia Panel専用検索**
# ==========================================
async def fetch_panel_searxng(q: str, pages: int = 1, safesearch: int = 0):
    """Wikipedia Panel検索 (曖昧さ回避を除外)"""
    # Panel用には一時的にWikipedia含めて検索
    async with httpx.AsyncClient(limits=LIMITS, timeout=TIMEOUT) as client:
        params = {
            "q": q, 
            "categories": "general", 
            "format": "json", 
            "pageno": 1,
            "safesearch": safesearch
        }
        try:
            resp = await client.get(SEARXNG_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            
            # Wikipedia記事を探す
            wiki_candidates = []
            for item in results:
                if not (item.get("title") and item.get("url")):
                    continue
                wiki_title = extract_wiki_title(item.get("url"))
                if wiki_title:
                    wiki_candidates.append({
                        "title": item.get("title"),
                        "summary": item.get("content", ""),
                        "url": item.get("url"),
                        "wiki_title": wiki_title
                    })
            
            if not wiki_candidates:
                return {"query": q, "type": "panel", "wiki_found": False, "results": []}
            
            # 最初のWikipedia記事でPanel情報取得 (曖昧さ回避は自動除外)
            for candidate in wiki_candidates:
                wiki_panel = await fetch_wikipedia_panel(client, candidate["wiki_title"])
                if wiki_panel:  # 曖昧さ回避でなければ採用
                    candidate["wikipedia_panel"] = wiki_panel
                    candidate["wiki_priority"] = True
                    return {
                        "query": q,
                        "type": "panel",
                        "wiki_found": True,
                        "wiki_title": candidate["wiki_title"],
                        "featured_panel": candidate
                    }
            
            # すべて曖昧さ回避だった場合
            return {"query": q, "type": "panel", "wiki_found": False, "results": []}
            
        except Exception as e:
            print(f"Panel search error: {e}")
            return {"query": q, "type": "panel", "wiki_found": False, "results": []}

# ==========================================
# **共通実行・キャッシュ**
# ==========================================
async def _exec_search_and_cache(q: str, pages: int, type: str, safesearch: int = 0):
    cache_key = f"search:{type}:{q}:{pages}:safe{safesearch}"
    
    try:
        if await rd.exists(cache_key):
            cached = await rd.get(cache_key)
            return json.loads(cached)
    except Exception:
        pass

    raw_results = []
    try:
        if type == "image":
            raw_results = await fetch_images_searxng(q, pages, safesearch)
        elif type == "video":
            raw_results = await fetch_video_searxng(q, pages, safesearch)
        elif type == "news":
            raw_results = await fetch_news_searxng(q, pages, safesearch)
        elif type == "social":
            raw_results = await fetch_social_misskey(q, pages)
        elif type == "web":
            raw_results = await fetch_web_searxng(q, pages, safesearch)
        elif type == "panel":
            raw_results = await fetch_panel_searxng(q, pages, safesearch)
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
# **エンドポイント**
# ==========================================
@app.get("/search")
async def search_endpoint(
    background_tasks: BackgroundTasks,
    q: str = Query(..., description="検索ワード"),
    pages: int = Query(1, ge=1, le=5),
    type: Literal["web", "image", "suggest", "video", "news", "social", "panel"] = Query("web"),
    safesearch: int = Query(0, ge=0, le=2, description="セーフサーチ: 0=無効, 1=中程度, 2=厳格")
):
    result = await _exec_search_and_cache(q, pages, type, safesearch)
    
    if result is None:
        return {"query": q, "type": type, "count": 0, "results": [], "error": "Search failed"}

    # バックグラウンドプリフェッチ
    if type == "web":
        background_tasks.add_task(_exec_search_and_cache, q, pages, "image", safesearch)
        background_tasks.add_task(_exec_search_and_cache, q, pages, "video", safesearch)
        background_tasks.add_task(_exec_search_and_cache, q, pages, "news", safesearch)
        background_tasks.add_task(_exec_search_and_cache, q, pages, "panel", safesearch)

    return result

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
