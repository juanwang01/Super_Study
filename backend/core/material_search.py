"""网络学习材料搜索与下载：搜索合适资料 → 抓取/下载 → 交给预处理管线转 Markdown。

数据源（无需 API Key，开放可抓）：
- 维基教科书（zh.wikibooks.org）：教科书级条目，最适合当学习材料
- 维基百科（zh.wikipedia.org）：知识条目
- 任意资料链接（网页 / PDF / EPUB / DOCX…）：下载后走 markitdown/OCR 预处理
"""
from __future__ import annotations

import httpx
from urllib.parse import quote

from config import get_http_proxy

TIMEOUT = 25


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
_BROWSER_HEADERS = {
    "User-Agent": _UA,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}


def _client(timeout: float | None = None) -> httpx.Client:
    kwargs: dict = {"timeout": timeout or TIMEOUT, "follow_redirects": True,
                    "headers": dict(_BROWSER_HEADERS)}
    proxy = get_http_proxy()
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.Client(**kwargs)


# ---------------------------------------------------------------------------
# 通用网页搜索（无 API Key，抓取 DuckDuckGo / Bing 的 HTML 结果）
# ---------------------------------------------------------------------------


def _duckduckgo_search(query: str, limit: int = 8) -> list[dict]:
    """DuckDuckGo HTML 端点搜索（无需 Key）。"""
    from bs4 import BeautifulSoup
    url = "https://html.duckduckgo.com/html/"
    with _client() as c:
        r = c.get(url, params={"q": query},
                  headers={"User-Agent": _UA})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for res in soup.select("div.result"):
        a = res.select_one("a.result__a")
        if not a:
            continue
        title = a.get_text(" ", strip=True)
        href = a.get("href", "")
        # DDG 用重定向链接，需解码 uddg 参数
        if "uddg=" in href:
            from urllib.parse import unquote, parse_qs, urlparse
            href = parse_qs(urlparse(href).query).get("uddg", [href])[0]
        snippet_el = res.select_one(".result__snippet")
        snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
        out.append({"title": title, "url": href, "snippet": snippet[:200], "source": "全网搜索"})
        if len(out) >= limit:
            break
    return out


def _bing_search(query: str, limit: int = 8) -> list[dict]:
    """Bing 国内版搜索（cn.bing.com，无需代理）。"""
    from bs4 import BeautifulSoup
    url = "https://cn.bing.com/search"
    with _client() as c:
        r = c.get(url, params={"q": query, "mkt": "zh-CN"},
                  headers={"User-Agent": _UA})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for li in soup.select("li.b_algo"):
        a = li.select_one("h2 a")
        if not a:
            continue
        title = a.get_text(" ", strip=True)
        href = a.get("href", "")
        # 解析跳转链接（ck/a、link?url=）
        if ("/ck/a" in href) or ("/link?" in href) or href.startswith("/"):
            try:
                with _client() as c2:
                    r2 = c2.get(href)
                    href = str(r2.url)
            except Exception:
                pass
        p = li.select_one("p")
        snippet = p.get_text(" ", strip=True) if p else ""
        if href.startswith("http"):
            out.append({"title": title, "url": href, "snippet": snippet[:200], "source": "Bing搜索"})
        if len(out) >= limit:
            break
    return out


def _baidu_search(query: str, limit: int = 8) -> list[dict]:
    """百度搜索（备用国内通道，过滤广告位）。"""
    from bs4 import BeautifulSoup
    url = "https://www.baidu.com/s"
    with _client() as c:
        r = c.get(url, params={"wd": query, "rn": limit},
                  headers={"User-Agent": _UA})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for a in soup.select("h3 a")[:limit]:
        title = a.get_text(" ", strip=True)
        href = a.get("href", "")
        if "/baidu.php" in href or "推广" in title:
            continue  # 跳过广告
        if "/link?" in href:
            try:
                with _client() as c2:
                    r2 = c2.get(href)
                    href = str(r2.url)
            except Exception:
                pass
        snippet_el = a.find_parent("div", class_="c-container") or a.find_parent("div")
        snippet = ""
        for cand in (".c-abstract", ".content-right_8Zs40", "span"):
            el = snippet_el.select_one(cand) if snippet_el else None
            if el and el.get_text(strip=True):
                snippet = el.get_text(" ", strip=True)
                break
        if href.startswith("http"):
            out.append({"title": title, "url": href, "snippet": snippet[:200], "source": "百度搜索"})
        if len(out) >= limit:
            break
    return out


def web_search(query: str, limit: int = 8) -> list[dict]:
    """全网搜索：优先国内可直连（Bing 国内版 → 百度），DuckDuckGo 作为代理补充。"""
    for fn in (_bing_search, _baidu_search):
        try:
            results = fn(query, limit)
            if results:
                return results
        except Exception:
            continue
    try:
        return _duckduckgo_search(query, limit)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 维基搜索
# ---------------------------------------------------------------------------
def _wiki_search(site: str, query: str, limit: int = 6) -> list[dict]:
    """搜索维基站点，返回 [{title, snippet, url}]。维基国内需代理，超时给短预算避免拖慢主流程。"""
    api = f"https://{site}/w/api.php"
    params = {
        "action": "query", "list": "search", "srsearch": query,
        "srlimit": str(limit), "format": "json",
        "utf8": "1",
    }
    with _client(timeout=8) as c:
        r = c.get(api, params=params)
        r.raise_for_status()
        data = r.json()
    out = []
    for it in data.get("query", {}).get("search", []):
        title = it.get("title", "")
        if not title:
            continue
        # 去 HTML 标签
        snippet = it.get("snippet", "").replace('<span class="searchmatch">', "").replace("</span>", "")
        out.append({
            "title": title,
            "snippet": snippet[:200],
            "source": "维基教科书" if site == "zh.wikibooks.org" else "维基百科",
            "url": f"https://{site}/wiki/{quote(title.replace(' ', '_'))}",
        })
    return out


def search_materials(query: str) -> list[dict]:
    """多源搜索学习材料：优先国内可直连（Bing/百度），DuckDuckGo 兜底。"""
    results: list[dict] = []
    seen = set()
    try:
        for item in web_search(query):
            if item["url"] and item["url"] not in seen:
                seen.add(item["url"])
                results.append(item)
    except Exception:
        pass
    return results


# ---------------------------------------------------------------------------
# 维基条目正文抓取 → Markdown
# ---------------------------------------------------------------------------
def fetch_wiki_page(title: str, site: str = "zh.wikibooks.org") -> tuple[str, str]:
    """抓取维基条目正文，转为 Markdown。

    返回 (markdown_text, title)
    """
    from bs4 import BeautifulSoup
    api = f"https://{site}/w/api.php"
    params = {
        "action": "parse", "page": title, "prop": "text",
        "format": "json", "utf8": "1",
    }
    with _client() as c:
        r = c.get(api, params=params)
        r.raise_for_status()
        data = r.json()
    html = data.get("parse", {}).get("text", {}).get("*", "")
    if not html:
        raise ValueError("未获取到条目内容")

    soup = BeautifulSoup(html, "html.parser")
    # 去掉编辑链接、目录、脚注
    for sel in (".mw-editsection", ".toc", ".mw-references-wrap", ".navbox",
                "sup.reference", "table.infobox"):
        for el in soup.select(sel):
            el.decompose()

    lines = []
    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "pre", "blockquote"]):
        if el.find_parent(["h1", "h2", "h3", "h4", "ul", "ol"]):
            continue
        name = el.name
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if name in ("h1", "h2", "h3", "h4"):
            lines.append(f"{'#' * int(name[1])} {text}")
        elif name == "li":
            lines.append(f"- {text}")
        elif name == "pre":
            lines.append(f"```\n{text}\n```")
        elif name == "blockquote":
            lines.append(f"> {text}")
        else:
            lines.append(text)
    md = "\n\n".join(lines)
    return md, data.get("parse", {}).get("title", title)


# ---------------------------------------------------------------------------
# 任意 URL 下载
# ---------------------------------------------------------------------------
def download_url(url: str, save_path: str) -> str:
    """下载 URL 内容到本地文件，返回 Content-Type。

    网页 / PDF / EPUB / DOCX / TXT 等都可。下载后由预处理管线统一转 Markdown。
    """
    with _client() as c:
        r = c.get(url)
        r.raise_for_status()
        content_type = r.headers.get("content-type", "").split(";")[0].strip().lower()
        with open(save_path, "wb") as f:
            f.write(r.content)
    return content_type


def guess_extension(content_type: str, url: str, default: str = ".html") -> str:
    """根据 Content-Type 与 URL 后缀猜扩展名。"""
    ct_map = {
        "application/pdf": ".pdf",
        "application/epub+zip": ".epub",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "text/plain": ".txt",
        "text/markdown": ".md",
        "text/html": ".html",
        "application/octet-stream": ".html",
    }
    if content_type in ct_map:
        return ct_map[content_type]
    # 按 URL 路径后缀
    path = url.split("?")[0].lower()
    for ext in (".pdf", ".epub", ".docx", ".txt", ".md", ".html", ".htm"):
        if path.endswith(ext):
            return ext
    return default
