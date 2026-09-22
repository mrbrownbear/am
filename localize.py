#!/usr/bin/env python3
import hashlib
import html
import os
import re
import shutil
import sys
import time
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

ROOT = "https://www.alanmenken.com/"
ROOT_HOST = urlparse(ROOT).netloc
OUT = Path(".")
STAGE = Path(".localized-build")

ASSET_HOSTS = {
    ROOT_HOST,
    "images.ctfassets.net",
    "videos.ctfassets.net",
    "fast.fonts.net",
    "vjs.zencdn.net",
}

BLOCKED_HOST_PARTS = (
    "google-analytics",
    "googletagmanager",
    "doubleclick",
    "bugsnag",
    "cloudflareinsights",
    "browser-update",
)

BLOCKED_PATH_PARTS = (
    "/cdn-cgi/",
    "/mtiFontTrackingCode.js",
)

ASSET_EXTS = {
    ".css", ".js", ".mjs", ".json", ".map",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp4", ".webm", ".ogg", ".mp3", ".wav", ".vtt",
    ".xml", ".webmanifest",
}

TEXT_EXTS = {".html", ".css", ".js", ".mjs", ".json", ".svg", ".xml", ".webmanifest", ".vtt"}

EXPLICIT_ASSETS = [
    "https://fast.fonts.net/lt/1.css?apiType=css&c=2b6274cc-0aac-4194-bcb4-c9ff85aa3733&fontids=812386,812395",
    "https://vjs.zencdn.net/vttjs/0.14.1/vtt.min.js",
]

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
})
adapter = requests.adapters.HTTPAdapter(max_retries=3)
session.mount("https://", adapter)
session.mount("http://", adapter)

asset_map = {}
page_map = {}
text_sources = {}
visited_pages = set()
visited_assets = set()
failed = []

URL_RE = re.compile(r"""(?P<url>https?://[^\s"'<>\\)]+|//[A-Za-z0-9._-]+/[^\s"'<>\\)]*)""", re.I)
ROOT_REL_RE = re.compile(
    r"""(?P<q>["'(=:\s])(?P<url>/[^"'()<>\s]+?\.(?:css|js|mjs|json|map|png|jpe?g|gif|webp|avif|svg|ico|woff2?|ttf|otf|eot|mp4|webm|ogg|mp3|wav|vtt|xml|webmanifest)(?:\?[^"'()<>\s]*)?)""",
    re.I,
)
CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)(?P<url>[^)'"]+)\1\s*\)""", re.I)
IMPORT_RE = re.compile(r"""(?:src|href|poster|data-src|data-video|content)\s*=\s*["'](?P<url>[^"']+)["']""", re.I)


def canonical(url, base=ROOT):
    url = html.unescape(str(url).strip())
    if not url or url.startswith(("data:", "blob:", "about:", "mailto:", "tel:", "javascript:", "#")):
        return None
    if url.startswith("//"):
        url = "https:" + url
    absolute = urljoin(base, url)
    absolute, _ = urldefrag(absolute)
    return absolute


def blocked(url):
    p = urlparse(url)
    host = p.netloc.lower()
    full = url.lower()
    if any(x in host for x in BLOCKED_HOST_PARTS):
        return True
    if any(x.lower() in p.path.lower() for x in BLOCKED_PATH_PARTS):
        return True
    if "sessions.bugsnag.com" in full:
        return True
    return False


def suffix_for(url):
    path = urlparse(url).path.lower()
    return Path(path).suffix.lower()


def is_asset(url):
    if not url or blocked(url):
        return False
    p = urlparse(url)
    if p.netloc not in ASSET_HOSTS:
        return False
    ext = suffix_for(url)
    if ext in ASSET_EXTS:
        return True
    if p.netloc in {"images.ctfassets.net", "videos.ctfassets.net", "fast.fonts.net", "vjs.zencdn.net"}:
        return True
    return False


def is_internal_page(url):
    if not url or blocked(url):
        return False
    p = urlparse(url)
    if p.netloc != ROOT_HOST:
        return False
    if p.query:
        return False
    if p.path.startswith("/cdn-cgi/"):
        return False
    ext = suffix_for(url)
    return not ext or p.path.endswith("/")


def add_query_hash(path, query):
    if not query:
        return path
    p = Path(path)
    digest = hashlib.sha1(query.encode("utf-8")).hexdigest()[:10]
    if p.suffix:
        return str(p.with_name(p.stem + "__q_" + digest + p.suffix))
    return str(p) + "__q_" + digest


def local_asset_path(url):
    p = urlparse(url)
    raw_path = p.path.lstrip("/") or "index"
    raw_path = re.sub(r"[^A-Za-z0-9._/()@+-]", "_", raw_path)
    raw_path = add_query_hash(raw_path, p.query)
    if p.netloc == ROOT_HOST:
        return raw_path
    return f"assets/external/{p.netloc}/{raw_path}"


def local_page_path(url):
    p = urlparse(url)
    path = p.path.strip("/")
    if not path:
        return "index.html"
    return f"{path}/index.html"


def fetch(url, binary=True):
    last = None
    for attempt in range(4):
        try:
            r = session.get(url, timeout=(20, 120), allow_redirects=True)
            if r.status_code >= 400:
                raise requests.HTTPError(f"{r.status_code} {r.reason}")
            return r
        except Exception as e:
            last = e
            if attempt < 3:
                time.sleep(1.5 * (attempt + 1))
    raise last


def extract_candidates(text, base_url):
    found = set()
    for rx in (URL_RE, ROOT_REL_RE, CSS_URL_RE, IMPORT_RE):
        for m in rx.finditer(text):
            raw = m.group("url")
            url = canonical(raw, base_url)
            if url:
                found.add(url)
    return found


def clean_html(text, base_url):
    soup = BeautifulSoup(text, "html.parser")

    for tag in list(soup.find_all(["script", "link", "iframe"])):
        url = tag.get("src") or tag.get("href") or ""
        absolute = canonical(url, base_url) if url else None
        body = tag.string or ""
        marker = (url + " " + body).lower()
        if absolute and blocked(absolute):
            tag.decompose()
            continue
        if any(k in marker for k in ("bugsnag", "gtag(", "google-analytics", "cloudflareinsights", "browser-update")):
            tag.decompose()

    for tag in list(soup.find_all("link")):
        rel = " ".join(tag.get("rel") or []).lower()
        href = tag.get("href") or ""
        if rel in {"preconnect", "dns-prefetch"} and href.startswith(("http://", "https://", "//")):
            tag.decompose()

    head = soup.head
    if head:
        csp = soup.find("meta", attrs={"http-equiv": re.compile("^Content-Security-Policy$", re.I)})
        policy = (
            "default-src 'self' data: blob:; "
            "base-uri 'self'; "
            "img-src 'self' data: blob:; "
            "media-src 'self' data: blob:; "
            "font-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
            "connect-src 'self'; "
            "worker-src 'self' blob:; "
            "frame-src 'self'; "
            "object-src 'none';"
        )
        if csp:
            csp["content"] = policy
        else:
            meta = soup.new_tag("meta")
            meta["http-equiv"] = "Content-Security-Policy"
            meta["content"] = policy
            head.insert(0, meta)

        stub = soup.new_tag("script")
        stub.string = (
            "window.gtag=window.gtag||function(){};"
            "window.dataLayer=window.dataLayer||[];"
            "window.bugsnagClient=window.bugsnagClient||{notify:function(){}};"
        )
        head.insert(1, stub)

    return str(soup)


def rewrite_text(text, base_url):
    def map_raw(raw):
        resolved = canonical(raw, base_url)
        if not resolved:
            return raw
        local = asset_map.get(resolved)
        if local:
            return "/" + local
        if resolved.startswith(ROOT):
            root_variant = resolved.replace(ROOT, "https://alanmenken.com/", 1)
            local = asset_map.get(root_variant)
            if local:
                return "/" + local
        return raw

    def sub_url(m):
        raw = m.group("url")
        return m.group(0).replace(raw, map_raw(raw))

    text = URL_RE.sub(sub_url, text)
    text = ROOT_REL_RE.sub(sub_url, text)
    text = CSS_URL_RE.sub(sub_url, text)
    text = IMPORT_RE.sub(sub_url, text)

    text = text.replace("https://www.alanmenken.com/", "/")
    text = text.replace("https://alanmenken.com/", "/")

    return text


def write_bytes(path, data):
    target = STAGE / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def crawl():
    page_queue = deque([(ROOT, 0)])
    asset_queue = deque(EXPLICIT_ASSETS)
    max_page_depth = 3
    max_pages = 180

    while page_queue and len(visited_pages) < max_pages:
        url, depth = page_queue.popleft()
        url = canonical(url)
        if not url or url in visited_pages or depth > max_page_depth or not is_internal_page(url):
            continue
        visited_pages.add(url)
        try:
            r = fetch(url)
            if "text/html" not in r.headers.get("content-type", "").lower() and "<html" not in r.text[:500].lower():
                continue
            page_path = local_page_path(url)
            page_map[url] = page_path
            cleaned = clean_html(r.text, url)
            text_sources[page_path] = (cleaned, url)

            soup = BeautifulSoup(cleaned, "html.parser")
            for a in soup.find_all("a", href=True):
                linked = canonical(a["href"], url)
                if linked and is_internal_page(linked) and linked not in visited_pages:
                    page_queue.append((linked, depth + 1))

            for candidate in extract_candidates(cleaned, url):
                if is_asset(candidate):
                    asset_queue.append(candidate)
                elif is_internal_page(candidate) and candidate not in visited_pages:
                    # Only actual page links are followed aggressively. This catches route URLs embedded in markup.
                    if urlparse(candidate).path in {"/", "/awards", "/biography", "/faq", "/work"} or urlparse(candidate).path.startswith("/work/"):
                        page_queue.append((candidate, depth + 1))
        except Exception as e:
            failed.append((url, str(e)))

    while asset_queue:
        url = canonical(asset_queue.popleft())
        if not url or url in visited_assets or not is_asset(url):
            continue
        visited_assets.add(url)
        try:
            r = fetch(url)
            final_url = canonical(r.url) or url
            path = local_asset_path(url)
            asset_map[url] = path
            asset_map[final_url] = path
            content_type = r.headers.get("content-type", "").lower()
            ext = suffix_for(url)

            if ext in TEXT_EXTS or any(x in content_type for x in ("text/", "javascript", "json", "xml", "svg")):
                try:
                    text = r.content.decode(r.encoding or "utf-8", errors="replace")
                except Exception:
                    text = r.content.decode("utf-8", errors="replace")
                text_sources[path] = (text, url)
                for candidate in extract_candidates(text, url):
                    if is_asset(candidate) and candidate not in visited_assets:
                        asset_queue.append(candidate)
            else:
                write_bytes(path, r.content)
        except Exception as e:
            failed.append((url, str(e)))

    # A second pass finds assets exposed only after the first round of text resources was fetched.
    added = True
    while added:
        added = False
        pending = []
        for path, (text, base) in list(text_sources.items()):
            for candidate in extract_candidates(text, base):
                if is_asset(candidate) and candidate not in visited_assets:
                    pending.append(candidate)
        if pending:
            added = True
            for u in pending:
                asset_queue.append(u)
            while asset_queue:
                url = canonical(asset_queue.popleft())
                if not url or url in visited_assets or not is_asset(url):
                    continue
                visited_assets.add(url)
                try:
                    r = fetch(url)
                    path = local_asset_path(url)
                    asset_map[url] = path
                    final_url = canonical(r.url)
                    if final_url:
                        asset_map[final_url] = path
                    content_type = r.headers.get("content-type", "").lower()
                    ext = suffix_for(url)
                    if ext in TEXT_EXTS or any(x in content_type for x in ("text/", "javascript", "json", "xml", "svg")):
                        text = r.content.decode(r.encoding or "utf-8", errors="replace")
                        text_sources[path] = (text, url)
                    else:
                        write_bytes(path, r.content)
                except Exception as e:
                    failed.append((url, str(e)))

    for path, (text, base) in text_sources.items():
        if path.endswith(".html"):
            text = clean_html(text, base)
        text = rewrite_text(text, base)
        write_bytes(path, text.encode("utf-8"))


def add_font_link():
    font_url = EXPLICIT_ASSETS[0]
    local = asset_map.get(font_url)
    if not local:
        return
    for page in STAGE.rglob("index.html"):
        s = BeautifulSoup(page.read_text("utf-8"), "html.parser")
        if s.head and not s.find("link", attrs={"data-local-fonts": "menken"}):
            tag = s.new_tag("link", rel="stylesheet", href="/" + local)
            tag["data-local-fonts"] = "menken"
            s.head.append(tag)
            page.write_text(str(s), "utf-8")


def write_support_files():
    vercel = """{
  "trailingSlash": true,
  "headers": [
    {
      "source": "/(.*)",
      "headers": [
        {
          "key": "Cache-Control",
          "value": "public, max-age=3600, s-maxage=86400"
        }
      ]
    }
  ]
}
"""
    (STAGE / "vercel.json").write_text(vercel, "utf-8")

    robots = """User-agent: *
Allow: /
"""
    (STAGE / "robots.txt").write_text(robots, "utf-8")

    report = [
        "# Localisation report",
        "",
        f"Pages captured: {len(page_map)}",
        f"Assets captured: {len(set(asset_map.values()))}",
        f"Failed fetches: {len(failed)}",
        "",
        "Runtime policy: local assets only. CSP blocks remote fetch/XHR/media/image/script/font requests.",
    ]
    if failed:
        report += ["", "## Failed fetches"]
        report += [f"* {u}: {e}" for u, e in failed[:100]]
    (STAGE / "LOCALISATION_REPORT.md").write_text("\n".join(report) + "\n", "utf-8")


def install_stage():
    keep = {".git", ".github", "localize.py", "README.md"}
    for item in list(OUT.iterdir()):
        if item.name in keep or item.name == STAGE.name:
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

    for item in STAGE.iterdir():
        target = OUT / item.name
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        shutil.move(str(item), str(target))
    STAGE.rmdir()


def main():
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)
    crawl()
    add_font_link()
    write_support_files()

    index = STAGE / "index.html"
    if not index.exists() or index.stat().st_size < 10000:
        print("ERROR: index.html was not captured correctly", file=sys.stderr)
        sys.exit(2)

    total = sum(p.stat().st_size for p in STAGE.rglob("*") if p.is_file())
    print(f"Captured {len(page_map)} pages and {len(set(asset_map.values()))} assets")
    print(f"Build size: {total / 1024 / 1024:.1f} MiB")
    if failed:
        print(f"Warnings: {len(failed)} fetches failed. See LOCALISATION_REPORT.md")
    install_stage()


if __name__ == "__main__":
    main()
