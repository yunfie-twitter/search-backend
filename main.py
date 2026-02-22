import asyncio
import os
import urllib.parse
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from typing import Literal, Optional, Dict, Any
import re

try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

from fastapi import FastAPI, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
import httpx
import redis.asyncio as redis
import orjson

# ==========================================
# **グローバル設定**
# ==========================================
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
SEARXNG_URL = "https://search.p2pear.asia/search"
MISSKEY_API_URL = "https://pjsekai.world/api/notes/search"
WIKIPEDIA_SUMMARY_API = "https://ja.wikipedia.org/api/rest_v1/page/summary/{title}"

# User-Agent
USER_AGENT = "Wholphin Search/1.0 (https://github.com/yunfie-twitter/search-backend; contact@wholphin.net) httpx/0.27.0"

# Timeout設定(短縮)
API_TIMEOUT = httpx.Timeout(3.0, connect=2.0)
SEARXNG_TIMEOUT = httpx.Timeout(5.0, connect=2.0)
PROXY_TIMEOUT = httpx.Timeout(1.0, connect=0.5)

# キャッシュTTL
CACHE_EXPIRE_SHORT = 3600  # 1h (検索結果)
CACHE_EXPIRE_LONG = 86400  # 24h (Wikipedia Panel)
CACHE_EXPIRE_PROXY = 604800  # 7d (画像プロキシ検証)

# タイプ別最大ページ数
MAX_PAGES = {
    "web": 10,
    "news": 5,
    "image": 3,
    "video": 3,
    "panel": 1,
    "suggest": 1,
}

# グローバルHTTPXクライアント (HTTP/2有効, コネクションプール共有)
global_client: Optional[httpx.AsyncClient] = None

# Redisクライアント
rd: Optional[redis.Redis] = None

# ==========================================
# **ライフサイクル管理**
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global global_client, rd
    
    # 起動時: httpx.AsyncClientとRedis初期化
    limits = httpx.Limits(
        max_keepalive_connections=50,
        max_connections=200,
        keepalive_expiry=30.0
    )
    global_client = httpx.AsyncClient(
        limits=limits,
        timeout=SEARXNG_TIMEOUT,
        http2=True,  # HTTP/2有効化
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT}  # デフォルトUser-Agent設定
    )
    
    rd = redis.from_url(
        REDIS_URL,
        decode_responses=True,
        max_connections=50,  # プールサイズ明示
        socket_keepalive=True
    )
    
    yield
    
    # 終了時: クリーンアップ
    await global_client.aclose()
    await rd.close()

app = FastAPI(
    title="Wholphin Search API - Performance Optimized",
    lifespan=lifespan
)

# Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)  # gzip圧縮有効化 (1KB以上)

# ==========================================
# **ユーティリティ関数**
# ==========================================
def extract_wiki_title(url: str) -> Optional[str]:
    """Wikipediaタイトル抽出 (文字列検索のみ)"""
    if not url or "wikipedia.org" not in url:
        return None
    
    match = re.search(r'/wiki/([^/?#]+)', url)
    if not match:
        return None
    
    title = urllib.parse.unquote(match.group(1))
    return title.replace('_', ' ')

def get_favicon(url: str) -> Optional[str]:
    """Favicon URL生成 (遅延評価)"""
    if not url:
        return None
    return f"https://t1.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url={urllib.parse.quote(url)}&size=128"

# ==========================================
# **Wikipedia Panel取得**
# ==========================================
async def fetch_wikipedia_panel(title: str) -> Optional[Dict]:
    """Wikipedia REST APIで詳細情報取得 (曖昧さ回避除外, 24hキャッシュ)"""
    cache_key = f"wiki:panel:{title}"
    
    print(f"[DEBUG] Fetching Wikipedia panel for: {title}")
    
    # Redisキャッシュ確認 (getのみ使用)
    try:
        cached = await rd.get(cache_key)
        if cached:
            print(f"[DEBUG] Wikipedia panel cache HIT: {title}")
            return orjson.loads(cached)
    except Exception as e:
        print(f"[DEBUG] Cache error: {e}")
        pass
    
    api_url = WIKIPEDIA_SUMMARY_API.format(title=urllib.parse.quote(title, safe=''))
    print(f"[DEBUG] Wikipedia API URL: {api_url}")
    
    try:
        resp = await global_client.get(api_url, timeout=API_TIMEOUT)
        print(f"[DEBUG] Wikipedia API status: {resp.status_code}")
        
        if resp.status_code == 200:
            data = resp.json()
            print(f"[DEBUG] Wikipedia data type: {data.get('type')}")
            
            # 曖昧さ回避判定
            if data.get("type") == "disambiguation":
                print(f"[DEBUG] Rejected: disambiguation page (type)")
                return None
            
            extract = data.get("extract", "")
            if "曖昧さ回避" in extract or "disambiguation" in extract.lower():
                print(f"[DEBUG] Rejected: disambiguation in extract")
                return None
            
            panel = {
                "title": data.get("title", ""),
                "description": data.get("description", ""),
                "extract": extract,
                "thumbnail": {
                    "url": data.get("thumbnail", {}).get("url", ""),
                    "width": data.get("thumbnail", {}).get("width"),
                    "height": data.get("thumbnail", {}).get("height")
                },
                "api_url": api_url
            }
            
            print(f"[DEBUG] Wikipedia panel created successfully: {data.get('title')}")
            
            # 24h長期キャッシュ
            try:
                await rd.setex(cache_key, CACHE_EXPIRE_LONG, orjson.dumps(panel))
            except Exception as e:
                print(f"[DEBUG] Cache save error: {e}")
                pass
            
            return panel
    except Exception as e:
        print(f"[DEBUG] Wikipedia API error: {e}")
        pass
    
    return None

# ==========================================
# **プロキシURL検証 (Redisキャッシュ付き)**
# ==========================================
async def get_valid_proxy_url(original_url: str) -> str:
    """プロキシURL検証結果キャッシュ (7d)"""
    if not original_url:
        return ""
    
    cache_key = f"proxy:check:{original_url}"
    
    # キャッシュ確認
    try:
        cached = await rd.get(cache_key)
        if cached:
            return cached
    except Exception:
        pass
    
    proxy_base = "https://proxy.wholphin.net/image.webp?url="
    proxy_url = f"{proxy_base}{urllib.parse.quote(original_url)}"
    
    # HEADリクエストで検証
    try:
        resp = await global_client.head(proxy_url, timeout=PROXY_TIMEOUT)
        result_url = original_url if (resp.status_code >= 400) else proxy_url
    except Exception:
        result_url = original_url
    
    # 結果キャッシュ
    try:
        await rd.setex(cache_key, CACHE_EXPIRE_PROXY, result_url)
    except Exception:
        pass
    
    return result_url

# ==========================================
# **SearXNG単一ページ検索**
# ==========================================
async def fetch_searxng_single_page(
    q: str, 
    page: int, 
    category: str, 
    result_parser, 
    extra_processing=None, 
    safesearch: int = 0,
    lang: str = "ja"
):
    """SearXNG単一ページ検索 (HTTP/2, 多言語対応)"""
    params = {
        "q": q,
        "categories": category,
        "format": "json",
        "pageno": page,
        "safesearch": safesearch,
        "language": lang
    }
    
    print(f"[DEBUG] SearXNG Request: q={q}, category={category}, pageno={page}")
    
    try:
        resp = await global_client.get(SEARXNG_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        print(f"[DEBUG] SearXNG Response: page={page}, results_count={len(results)}")
        
        parsed_items = result_parser(results, page)
        print(f"[DEBUG] Parsed items: page={page}, parsed_count={len(parsed_items)}")
        
        # extra_processingがあり、複数件ある場合のみ並列化
        if extra_processing and len(parsed_items) > 1:
            parsed_items = await asyncio.gather(
                *[extra_processing(item) for item in parsed_items]
            )
        elif extra_processing and len(parsed_items) == 1:
            parsed_items[0] = await extra_processing(parsed_items[0])
        
        return parsed_items
            
    except Exception as e:
        print(f"SearXNG {category} Error Page {page}: {e}")
        return []

# ==========================================
# **Google Suggest**
# ==========================================
async def fetch_suggest(q: str):
    """Google Suggest取得"""
    url = f"https://www.google.com/complete/search?hl=ja&output=toolbar&q={urllib.parse.quote(q)}"
    try:
        resp = await global_client.get(url, timeout=API_TIMEOUT)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        return [{"title": cs.find('suggestion').get('data')}
                for cs in root.findall('CompleteSuggestion')
                if cs.find('suggestion') is not None]
    except Exception:
        return []

# ==========================================
# **Web/News/Images/Video**
# ==========================================
async def fetch_web_searxng(q: str, page: int, safesearch: int = 0, lang: str = "ja"):
    """Web検索"""
    def parser(results, p):
        items = []
        for i in results:
            if not (i.get("title") and i.get("url")):
                continue
            
            items.append({
                "title": i.get("title"),
                "summary": i.get("content", ""),
                "url": i.get("url"),
                "favicon": get_favicon(i.get("url")),
                "page": p
            })
        return items
    
    return await fetch_searxng_single_page(q, page, "general", parser, safesearch=safesearch, lang=lang)

async def fetch_news_searxng(q: str, page: int, safesearch: int = 0, lang: str = "ja"):
    """ニュース検索"""
    def parser(results, p):
        return [{
            "title": i.get("title"),
            "summary": i.get("content"),
            "url": i.get("url"),
            "favicon": get_favicon(i.get("url")),
            "publishedDate": i.get("publishedDate"),
            "page": p
        } for i in results if i.get("title") and i.get("url")]
    
    return await fetch_searxng_single_page(q, page, "news", parser, safesearch=safesearch, lang=lang)

async def fetch_images_searxng(q: str, page: int, safesearch: int = 0, lang: str = "ja"):
    """画像検索 (プロキシキャッシュ付き)"""
    def parser(results, p):
        return [{
            "title": i.get("title", "No Title"),
            "url": i.get("url"),
            "raw_thumb": i.get("thumbnail_src") or i.get("img_src"),
            "domain": i.get("source"),
            "page": p
        } for i in results if i.get("url") and (i.get("thumbnail_src") or i.get("img_src"))]
    
    async def processor(item):
        raw = item.pop("raw_thumb", None)
        item["thumbnail"] = await get_valid_proxy_url(raw) if raw else ""
        return item
    
    return await fetch_searxng_single_page(q, page, "images", parser, processor, safesearch, lang)

async def fetch_video_searxng(q: str, page: int, safesearch: int = 0, lang: str = "ja"):
    """動画検索 (プロキシキャッシュ付き)"""
    def parser(results, p):
        return [{
            "title": i.get("title"),
            "url": i.get("url"),
            "raw_thumb": i.get("thumbnail"),
            "duration": i.get("duration"),
            "publishedDate": i.get("publishedDate"),
            "page": p
        } for i in results if i.get("title") and i.get("url")]
    
    async def processor(item):
        raw = item.pop("raw_thumb", None)
        item["thumbnail"] = await get_valid_proxy_url(raw) if raw else ""
        return item
    
    return await fetch_searxng_single_page(q, page, "videos", parser, processor, safesearch, lang)

# ==========================================
# **Wikipedia Panel検索 (並列化)**
# ==========================================
async def fetch_panel_searxng(q: str, safesearch: int = 0, lang: str = "ja"):
    """Wikipedia Panel検索 (候補並列取得)"""
    params = {
        "q": q,
        "categories": "general",
        "format": "json",
        "pageno": 1,
        "safesearch": safesearch,
        "language": lang
    }
    
    print(f"[DEBUG] Panel search for query: {q}")
    
    try:
        resp = await global_client.get(SEARXNG_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        
        print(f"[DEBUG] SearXNG returned {len(results)} results for panel search")
        
        # Wikipedia記事を抽出
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
                print(f"[DEBUG] Found Wikipedia candidate: {wiki_title} ({item.get('url')})")
        
        print(f"[DEBUG] Total Wikipedia candidates: {len(wiki_candidates)}")
        
        if not wiki_candidates:
            print(f"[DEBUG] No Wikipedia candidates found")
            return {"query": q, "type": "panel", "wiki_found": False}
        
        # 並列でWikipedia Panel取得 (最初の有効なものを採用)
        tasks = [fetch_wikipedia_panel(c["wiki_title"]) for c in wiki_candidates[:3]]  # 上位3件まで
        panels = await asyncio.gather(*tasks)
        
        for candidate, panel in zip(wiki_candidates[:3], panels):
            if panel:  # 曖昧さ回避でなければ採用
                candidate["wikipedia_panel"] = panel
                candidate["wiki_priority"] = True
                print(f"[DEBUG] Panel found successfully: {candidate['wiki_title']}")
                return {
                    "query": q,
                    "type": "panel",
                    "wiki_found": True,
                    "wiki_title": candidate["wiki_title"],
                    "featured_panel": candidate
                }
        
        print(f"[DEBUG] All Wikipedia candidates were disambiguation pages")
        return {"query": q, "type": "panel", "wiki_found": False}
        
    except Exception as e:
        print(f"[ERROR] Panel search error: {e}")
        return {"query": q, "type": "panel", "wiki_found": False}

# ==========================================
# **検索実行・キャッシュ**
# ==========================================
async def exec_search_and_cache(q: str, page: int, type: str, safesearch: int = 0, lang: str = "ja"):
    # タイプ別最大ページ数を適用
    max_page = MAX_PAGES.get(type, 5)
    page = min(page, max_page)
    
    cache_key = f"search:{type}:{q}:p{page}:s{safesearch}:l{lang}"
    print(f"[DEBUG] Cache key: {cache_key}")
    
    # Redisキャッシュ確認 (getのみ)
    try:
        cached = await rd.get(cache_key)
        if cached:
            print(f"[DEBUG] Cache HIT for page {page}")
            return orjson.loads(cached)
        else:
            print(f"[DEBUG] Cache MISS for page {page}")
    except Exception as e:
        print(f"[DEBUG] Cache error: {e}")
        pass

    raw_results = []
    try:
        if type == "image":
            raw_results = await fetch_images_searxng(q, page, safesearch, lang)
        elif type == "video":
            raw_results = await fetch_video_searxng(q, page, safesearch, lang)
        elif type == "news":
            raw_results = await fetch_news_searxng(q, page, safesearch, lang)
        elif type == "web":
            raw_results = await fetch_web_searxng(q, page, safesearch, lang)
        elif type == "panel":
            raw_results = await fetch_panel_searxng(q, safesearch, lang)
        elif type == "suggest":
            raw_results = await fetch_suggest(q)
    except Exception as e:
        print(f"Error in exec_search_and_cache ({type}): {e}")
        return None

    # 結果整形
    if isinstance(raw_results, dict):  # panelはdict
        resp = raw_results
    else:
        # 重複除外 (最適化)
        if type != "suggest":
            seen = set()
            unique_results = [item for item in raw_results 
                            if (url := item.get("url")) and url not in seen and not seen.add(url)]
            resp = {
                "query": q,
                "type": type,
                "page": page,
                "source": "live",
                "count": len(unique_results),
                "results": unique_results
            }
        else:
            resp = {"query": q, "type": type, "results": raw_results}

    # orjsonでキャッシュ
    try:
        ttl = CACHE_EXPIRE_LONG if type == "panel" else CACHE_EXPIRE_SHORT
        await rd.setex(cache_key, ttl, orjson.dumps(resp))
        print(f"[DEBUG] Cached results for page {page}")
    except Exception as e:
        print(f"[DEBUG] Cache save error: {e}")
        pass

    return resp

# ==========================================
# **エンドポイント**
# ==========================================
@app.get("/search")
async def search_endpoint(
    background_tasks: BackgroundTasks,
    q: str = Query(..., description="検索ワード"),
    page: int = Query(1, ge=1, le=10, description="ページ番号 (タイプ別に上限あり)"),
    type: Literal["web", "image", "suggest", "video", "news", "panel"] = Query("web"),
    safesearch: int = Query(0, ge=0, le=2, description="セーフサーチ: 0=無効, 1=中程度, 2=厳格"),
    lang: str = Query("ja", pattern="^(ja|en)$", description="言語: ja=日本語, en=英語")
):
    print(f"[DEBUG] Endpoint called: q={q}, page={page}, type={type}")
    result = await exec_search_and_cache(q, page, type, safesearch, lang)
    
    if result is None:
        return {"query": q, "type": type, "page": page, "count": 0, "results": [], "error": "Search failed"}

    # バックグラウンドプリフェッチ (web検索時のみ, 1ページ目のみ)
    if type == "web" and page == 1:
        background_tasks.add_task(exec_search_and_cache, q, 1, "image", safesearch, lang)
        background_tasks.add_task(exec_search_and_cache, q, 1, "video", safesearch, lang)
        background_tasks.add_task(exec_search_and_cache, q, 1, "news", safesearch, lang)
        background_tasks.add_task(exec_search_and_cache, q, 1, "panel", safesearch, lang)

    return result

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        loop="uvloop",  # uvloop有効化
        log_level="info"
    )
