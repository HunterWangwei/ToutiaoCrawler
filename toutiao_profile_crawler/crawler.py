#!/usr/bin/env python3
"""今日头条个人主页微头条协议采集工具。"""

from __future__ import annotations

import argparse
from datetime import datetime
import html
import json
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlencode, urljoin, urlparse

import requests


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)
PROFILE_RE = re.compile(r"https?://(?:www\.)?toutiao\.com/c/user/token/([^/?#]+)", re.I)
BRACKET_EMOJI_RE = re.compile(r"\[[^\[\]\r\n]{1,16}\]")
UNICODE_EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001FAFF\u2600-\u27BF\uFE0E-\uFE0F\u200D"
    "]"
)


def safe_name(value: str, limit: int = 50) -> str:
    value = re.sub(r'[\\/:*?"<>|\r\n]+', "_", value).strip(" ._")
    return value[:limit].rstrip() or "untitled"


def strip_html(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value or "", flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(value).strip()


def clean_comment(text: str) -> str:
    text = (text or "").strip()
    if not text or re.search(r"@\s*豆包", text, flags=re.I):
        return ""
    text = BRACKET_EMOJI_RE.sub("", text)
    text = UNICODE_EMOJI_RE.sub("", text).replace("\u20e3", "")
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    return text if any(char.isalnum() for char in text) else ""


class ProfileCrawler:
    def __init__(
        self,
        *,
        cookie: str = "",
        proxy: str = "",
        timeout: int = 25,
        delay: float = 1.0,
        retries: int = 3,
    ) -> None:
        self.timeout = timeout
        self.delay = delay
        self.retries = retries
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "User-Agent": UA,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
                "Connection": "keep-alive",
            }
        )
        if cookie:
            self.session.headers["Cookie"] = cookie.strip()
        if proxy:
            proxy_url = self._proxy_url(proxy)
            self.session.proxies.update({"http": proxy_url, "https": proxy_url})

    @staticmethod
    def _proxy_url(proxy: str) -> str:
        if "://" in proxy:
            return proxy
        parts = proxy.split(":")
        if len(parts) == 2:
            return f"socks5h://{parts[0]}:{parts[1]}"
        if len(parts) == 4:
            host, port, username, password = parts
            return f"socks5h://{username}:{password}@{host}:{port}"
        raise ValueError("代理格式应为 host:port 或 host:port:user:password")

    def get(
        self, url: str, *, referer: str = "", accept: str = "*/*", mobile: bool = False
    ) -> bytes:
        headers = {"Accept": accept}
        if mobile:
            headers["User-Agent"] = (
                "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36"
            )
        if referer:
            headers["Referer"] = referer
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                response = self.session.get(
                    url,
                    headers=headers,
                    timeout=(min(10, self.timeout), self.timeout),
                )
                response.raise_for_status()
                if not response.content:
                    raise RuntimeError("服务器返回空响应，可能触发头条风控")
                return response.content
            except Exception as exc:
                last_error = exc
                if attempt + 1 < self.retries:
                    time.sleep(1.2 * (attempt + 1))
        raise RuntimeError(f"请求失败：{last_error}")

    @staticmethod
    def profile_token(profile_url: str) -> str:
        match = PROFILE_RE.search(profile_url)
        if not match:
            raise ValueError("不是有效的今日头条个人主页 URL")
        return unquote(match.group(1))

    def list_post_urls(self, profile_url: str, max_pages: int = 0) -> list[str]:
        token = self.profile_token(profile_url)
        referer = f"https://www.toutiao.com/c/user/token/{token}/?tab=wtt"
        found: list[str] = []
        seen: set[str] = set()

        def add(item_id: object) -> None:
            value = str(item_id or "")
            if value.isdigit() and value not in seen:
                seen.add(value)
                found.append(f"https://www.toutiao.com/w/{value}/")

        def add_from_row(row: dict) -> None:
            add(row.get("group_id") or row.get("item_id") or row.get("thread_id"))
            base_cell = row.get("base_cell") or {}
            log_pb = base_cell.get("log_pb") or {}
            add(
                log_pb.get("group_id_str")
                or log_pb.get("post_gid")
                or log_pb.get("logpb_group_id")
            )
            raw_data = (row.get("stream_cell") or {}).get("raw_data")
            if raw_data:
                try:
                    raw = json.loads(raw_data)
                except (TypeError, json.JSONDecodeError):
                    raw = {}
                add(
                    raw.get("group_id")
                    or raw.get("item_id")
                    or raw.get("thread_id")
                    or raw.get("id")
                )

        try:
            homepage = self.get(referer, referer="https://www.toutiao.com/").decode(
                "utf-8", "replace"
            )
            for item_id in re.findall(r'href=["\']/w/(\d+)/?', homepage, re.I):
                add(item_id)
        except Exception:
            pass

        cursor: int | str = 0
        page = 0
        use_compat_api = False
        while max_pages <= 0 or page < max_pages:
            if use_compat_api:
                params = {
                    "category": "pc_profile_ugc",
                    "utm_source": "toutiao",
                    "visit_user_token": token,
                    "max_behot_time": cursor,
                }
                endpoint = "https://www.toutiao.com/api/pc/feed/"
            else:
                params = {
                    "category": "pc_profile_ugc",
                    "token": token,
                    "max_behot_time": cursor,
                    "aid": 24,
                    "app_name": "toutiao_web",
                }
                endpoint = "https://www.toutiao.com/api/pc/list/user/feed"
            url = endpoint + "?" + urlencode(params)
            try:
                payload = json.loads(
                    self.get(url, referer=referer, accept="application/json, text/plain, */*").decode(
                        "utf-8", "replace"
                    )
                )
            except Exception:
                if not use_compat_api:
                    use_compat_api = True
                    cursor = 0
                    page = 0
                    continue
                if found:
                    break
                raise

            if payload.get("message") != "success":
                raise RuntimeError(f"主页接口返回异常：{payload.get('message') or payload}")
            for row in payload.get("data") or []:
                add_from_row(row)

            page += 1
            next_cursor = (payload.get("next") or {}).get("max_behot_time")
            if not payload.get("has_more") or next_cursor in (None, cursor):
                break
            cursor = next_cursor
            time.sleep(self.delay)

        if not found:
            raise RuntimeError("主页中没有找到可采集的微头条")
        return found

    def post(self, source_url: str) -> dict:
        match = re.search(r"/(?:article|w)/(\d+)", source_url)
        if not match:
            raise ValueError(f"无法提取作品 ID：{source_url}")
        item_id = match.group(1)
        detail_url = f"https://m.toutiao.com/w/{item_id}/"
        raw = self.get(detail_url, referer="https://m.toutiao.com/", mobile=True).decode(
            "utf-8", "replace"
        )
        match = re.search(
            r'<script[^>]+id=["\']RENDER_DATA["\'][^>]*>(.*?)</script>', raw, re.S | re.I
        )
        if not match:
            raise RuntimeError(f"作品 {item_id} 没有找到 RENDER_DATA")
        payload = json.loads(unquote(html.unescape(match.group(1))))
        base = payload.get("articleInfo", {}).get("thread", {}).get("threadBase")
        if not base:
            data = payload.get("data", payload)
            content = data.get("richContent") or data.get("content") or ""
            images = data.get("ugcImages") or []
            author = data.get("source") or ""
            created = data.get("publishTime") or ""
        else:
            content = base.get("richContent") or base.get("content") or ""
            nodes = base.get("largeImageList") or base.get("originImageList") or []
            images = [node.get("url") for node in nodes if node.get("url")]
            author = ((base.get("user") or {}).get("info") or {}).get("name") or ""
            timestamp = base.get("createTime")
            created = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S") if timestamp else ""
        if not images:
            images = re.findall(r'<img[^>]+src=["\']([^"\']+)', content, re.I)
        images = [urljoin("https:", url) if url.startswith("//") else url for url in images]
        return {
            "id": item_id,
            "url": source_url,
            "detail_url": detail_url,
            "author": author,
            "publish_time": created,
            "content": strip_html(content),
            "images": list(dict.fromkeys(images)),
        }

    def comments(self, item_id: str, referer: str, top_n: int, pages: int) -> list[dict]:
        rows: list[dict] = []
        seen: set[str] = set()
        for page in range(pages):
            params = {
                "aid": 24,
                "app_name": "toutiao_web",
                "group_id": item_id,
                "item_id": item_id,
                "offset": page * 20,
                "count": 20,
            }
            url = "https://www.toutiao.com/article/v2/tab_comments/?" + urlencode(params)
            payload = json.loads(
                self.get(url, referer=referer, accept="application/json").decode("utf-8", "replace")
            )
            batch = payload.get("data") or []
            if not batch:
                break
            for wrapper in batch:
                row = wrapper.get("comment", wrapper)
                text = clean_comment(row.get("text") or "")
                comment_id = str(row.get("id_str") or row.get("id") or "")
                if not text or not comment_id or comment_id in seen:
                    continue
                seen.add(comment_id)
                user = row.get("user") or {}
                rows.append(
                    {
                        "id": comment_id,
                        "user": user.get("name") or row.get("user_name") or "匿名用户",
                        "text": text,
                        "digg_count": int(row.get("digg_count") or 0),
                        "reply_count": int(row.get("reply_count") or 0),
                    }
                )
            if not payload.get("has_more"):
                break
            time.sleep(self.delay)
        rows.sort(
            key=lambda row: (row["digg_count"], len(row["text"]), row["reply_count"]),
            reverse=True,
        )
        return rows[:top_n]

    def download_images(self, post: dict, folder: Path) -> list[str]:
        folder.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        for index, url in enumerate(post["images"], 1):
            try:
                data = self.get(url, referer=post["detail_url"], accept="image/*")
                ext = Path(urlparse(url).path).suffix.lower()
                if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
                    ext = ".jpg"
                path = folder / f"{index:02d}{ext}"
                path.write_bytes(data)
                saved.append(str(path))
            except Exception as exc:
                print(f"  [图片失败] {url}: {exc}")
        return saved


def save_post(root: Path, crawler: ProfileCrawler, post: dict, comments: list[dict]) -> None:
    name = f"{post['id']}_{safe_name(post['content'])}"
    content_dir = root / "内容"
    image_dir = root / "图片" / name
    content_dir.mkdir(parents=True, exist_ok=True)
    post["local_images"] = crawler.download_images(post, image_dir)
    lines = [post["content"], "", "评论：", ""]
    for index, comment in enumerate(comments, 1):
        lines.append(f"{index}. {comment['text']}（赞 {comment['digg_count']}）")
        lines.append("")
    (content_dir / f"{name}.txt").write_text(
        "\n".join(lines).rstrip() + "\n", encoding="utf-8-sig"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="采集今日头条个人主页的微头条、图片和高赞评论")
    parser.add_argument("profile_url", help="今日头条个人主页 URL")
    parser.add_argument("-o", "--output", default="output", help="输出目录")
    parser.add_argument("-n", "--top-comments", type=int, choices=range(5, 11), default=10)
    parser.add_argument("--comment-pages", type=int, default=3)
    parser.add_argument("--profile-pages", type=int, default=0, help="0 表示抓到末页")
    parser.add_argument("--cookie-file", default="")
    parser.add_argument("--proxy", default="")
    parser.add_argument("--delay", type=float, default=1.0)
    args = parser.parse_args()

    cookie = Path(args.cookie_file).read_text(encoding="utf-8-sig").strip() if args.cookie_file else ""
    crawler = ProfileCrawler(cookie=cookie, proxy=args.proxy, delay=max(0.2, args.delay))
    root = Path(args.output).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    urls = crawler.list_post_urls(args.profile_url, max_pages=args.profile_pages)
    print(f"[主页] 共发现 {len(urls)} 条微头条")
    results: list[dict] = []
    failures = 0
    for index, url in enumerate(urls, 1):
        try:
            print(f"[{index}/{len(urls)}] {url}")
            post = crawler.post(url)
            comments = crawler.comments(
                post["id"], post["detail_url"], args.top_comments, args.comment_pages
            )
            post["comments"] = comments
            save_post(root, crawler, post, comments)
            results.append(post)
            print(f"  完成：图片 {len(post['local_images'])} 张，评论 {len(comments)} 条")
        except Exception as exc:
            failures += 1
            print(f"  失败：{type(exc).__name__}: {exc}")
        time.sleep(crawler.delay)

    (root / "posts.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8-sig"
    )
    print(f"采集结束：成功 {len(results)} 条，失败 {failures} 条，输出目录 {root}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
