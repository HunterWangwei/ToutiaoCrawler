#!/usr/bin/env python3
"""今日头条微头条协议抓取：正文、图片和高赞评论（仅标准库）。"""

from __future__ import annotations

import argparse
from datetime import datetime
import html
import json
import queue
import re
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlencode, urljoin, urlparse

try:
    import requests
except ImportError:  # 给出比模块导入错误更清楚的提示
    requests = None


DEFAULT_URLS = [
    "https://www.toutiao.com/article/1872961296302087",
    "https://www.toutiao.com/article/1872974616713225",
    "https://www.toutiao.com/article/1872989616110604",
    "https://www.toutiao.com/article/1872984783030282",
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)


def safe_name(value: str, limit: int = 70) -> str:
    value = re.sub(r'[\\/:*?"<>|\r\n]+', "_", value).strip(" ._")
    return (value[:limit].rstrip() or "untitled")


def strip_html(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(value).strip()


# 头条把不少表情保存为 [捂脸]、[飞吻]、[招财进宝] 等文本标记。
BRACKET_EMOJI_RE = re.compile(r"\[[^\[\]\r\n]{1,16}\]")

# 常见 Emoji、手势、图形符号、旗帜、肤色修饰符、变体选择符和组合连接符。
UNICODE_EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\u2600-\u26FF"
    "\u2700-\u27BF"
    "\uFE0E-\uFE0F"
    "\u200D"
    "]"
)


def clean_comment_text(text: str) -> str:
    """删除下游无法识别的表情，保留评论中的正常文字。"""
    text = (text or "").strip()
    if not text:
        return ""
    # @豆包 多为自动问答召唤评论，按用户要求整条排除。
    if re.search(r"@\s*豆包", text, flags=re.I):
        return ""
    text = BRACKET_EMOJI_RE.sub("", text)
    text = UNICODE_EMOJI_RE.sub("", text)
    # 清除 Emoji 键帽组合符，并整理删除表情后留下的多余空白。
    text = text.replace("\u20e3", "")
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    # 纯表情或清洗后只剩标点的评论不采集。
    if not any(char.isalnum() for char in text):
        return ""
    return text


def is_usable_comment(text: str) -> bool:
    """兼容旧调用：判断评论清洗后是否还有有效文字。"""
    return bool(clean_comment_text(text))


class ToutiaoCrawler:
    def __init__(self, timeout: int = 25, delay: float = 0.8, proxy: str = "", retries: int = 2):
        if requests is None:
            raise RuntimeError("缺少 requests，请先运行：pip install -r requirements.txt")
        self.timeout = timeout
        self.retries = max(1, retries)
        self.delay = delay
        self.session = requests.Session()
        # 不读取对方电脑上的 HTTP_PROXY/HTTPS_PROXY 等环境变量，避免被失效的
        # 系统代理劫持。需要代理时只使用界面中明确填写的 SOCKS5。
        self.session.trust_env = False
        self.proxy = normalize_socks5_proxy(proxy)
        if self.proxy:
            self.session.proxies.update({"http": self.proxy, "https": self.proxy})

    def get(
        self, url: str, *, referer: str | None = None, accept: str = "*/*", mobile: bool = False
    ) -> bytes:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36"
                if mobile else UA
            ),
            "Accept": accept,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            "Connection": "keep-alive",
        }
        if referer:
            headers["Referer"] = referer
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            result: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

            def request_once() -> None:
                try:
                    # 每次尝试使用独立 Session；即便上一条底层 DNS 调用卡死，
                    # 后续重试也不会与它共享连接池。
                    with requests.Session() as request_session:
                        request_session.trust_env = False
                        if self.proxy:
                            request_session.proxies.update({"http": self.proxy, "https": self.proxy})
                        response = request_session.get(
                            url,
                            headers=headers,
                            timeout=(min(10, self.timeout), self.timeout),
                        )
                        response.raise_for_status()
                        result.put((True, response.content))
                except Exception as exc:
                    result.put((False, exc))

            # requests 的超时在少数 Windows DNS/代理故障下可能不能及时返回，
            # 再加一层硬超时，保证 UI 不会永久停在“采集正文”。
            threading.Thread(target=request_once, daemon=True).start()
            try:
                ok, value = result.get(timeout=self.timeout + 12)
            except queue.Empty:
                last_error = TimeoutError(f"请求硬超时（{self.timeout + 12} 秒）")
            else:
                if ok:
                    return value  # type: ignore[return-value]
                last_error = value  # type: ignore[assignment]

            if attempt < self.retries:
                time.sleep(1.2)

        raise RuntimeError(f"网络请求失败，已重试 {self.retries} 次：{last_error}")

    @staticmethod
    def item_id(url: str) -> str:
        match = re.search(r"/(?:article|w)/(\d+)", url)
        if not match:
            raise ValueError(f"无法从 URL 提取作品 ID: {url}")
        return match.group(1)

    def article(self, source_url: str) -> dict:
        item_id = self.item_id(source_url)
        # /article/ 对微头条可能只返回跳转壳；/w/ 会返回带 RENDER_DATA 的 SSR 页面。
        # PC 站对无浏览器环境常返回 JS 风控页；移动站同样是 HTTP 协议请求，
        # 且稳定返回包含 RENDER_DATA 的服务端渲染 HTML。
        detail_url = f"https://m.toutiao.com/w/{item_id}/"
        raw = self.get(detail_url, referer="https://m.toutiao.com/", mobile=True).decode(
            "utf-8", "replace"
        )
        match = re.search(
            r'<script[^>]+id=["\']RENDER_DATA["\'][^>]*>(.*?)</script>', raw, re.S | re.I
        )
        if not match:
            raise RuntimeError(f"作品 {item_id} 未找到 RENDER_DATA，可能触发了风控或页面结构已变")
        payload = json.loads(unquote(html.unescape(match.group(1))))
        if payload.get("articleInfo", {}).get("thread", {}).get("threadBase"):
            base = payload["articleInfo"]["thread"]["threadBase"]
            user = (base.get("user") or {}).get("info") or {}
            action = base.get("action") or {}
            image_nodes = base.get("largeImageList") or base.get("originImageList") or []
            images = [n.get("url") for n in image_nodes if n.get("url")]
            created = base.get("createTime")
            publish_time = datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M:%S") if created else ""
            data = {
                "title": base.get("title"), "content": base.get("content"),
                "richContent": base.get("richContent"), "source": user.get("name"),
                "publishTime": publish_time, "ugcImages": images,
                "likeData": {"count": action.get("diggCount", 0)},
            }
        else:
            data = payload.get("data", payload)
            images = data.get("ugcImages") or []
        if not images:
            images = re.findall(r'<img[^>]+src=["\']([^"\']+)', data.get("content", ""), re.I)
        images = [urljoin("https:", u) if u.startswith("//") else u for u in images]
        return {
            "id": item_id,
            "source_url": source_url,
            "detail_url": detail_url,
            "title": strip_html(data.get("title") or data.get("content", ""))[:180],
            "author": data.get("source") or (data.get("mediaInfo") or {}).get("name") or "",
            "publish_time": data.get("publishTime") or "",
            "content": strip_html(data.get("richContent") or data.get("content") or ""),
            "images": list(dict.fromkeys(images)),
            "like_count": (data.get("likeData") or {}).get("count", 0),
        }

    def comments(self, item_id: str, referer: str, top_n: int, pages: int = 3) -> list[dict]:
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
            obj = json.loads(self.get(url, referer=referer, accept="application/json").decode("utf-8"))
            batch = obj.get("data") or []
            if not batch:
                break
            for wrapper in batch:
                c = wrapper.get("comment", wrapper)
                comment_text = clean_comment_text(c.get("text") or "")
                if not comment_text:
                    continue
                cid = str(c.get("id_str") or c.get("id") or "")
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                user = c.get("user") or {}
                rows.append({
                    "id": cid,
                    "user": user.get("name") or c.get("user_name") or "匿名用户",
                    "text": comment_text,
                    "digg_count": int(c.get("digg_count") or 0),
                    "reply_count": int(c.get("reply_count") or 0),
                    "create_time": c.get("create_time") or 0,
                })
            if not obj.get("has_more"):
                break
            time.sleep(self.delay)
        # 点赞数相同时优先保留内容更丰富、文字更长的评论；
        # 点赞数和字数都相同时，再以回复数作为最后排序条件。
        rows.sort(
            key=lambda x: (
                x["digg_count"],
                len(re.sub(r"\s+", "", x["text"])),
                x["reply_count"],
            ),
            reverse=True,
        )
        return rows[:top_n]

    def download_images(self, article: dict, folder: Path) -> list[str]:
        saved = []
        folder.mkdir(parents=True, exist_ok=True)
        for idx, url in enumerate(article["images"], 1):
            try:
                data = self.get(url, referer=article["detail_url"], accept="image/*")
                path_ext = Path(urlparse(url).path).suffix.lower()
                ext = path_ext if path_ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"} else ".jpg"
                path = folder / f"{idx:02d}{ext}"
                path.write_bytes(data)
                saved.append(str(path))
            except Exception as exc:
                print(f"  [图片失败] {url}: {exc}")
        return saved


def normalize_socks5_proxy(proxy: str) -> str:
    """支持 socks5://user:pass@host:port、host:port、host:port:user:pass。"""
    proxy = proxy.strip()
    if not proxy:
        return ""
    if "://" in proxy:
        return proxy
    parts = proxy.split(":")
    if len(parts) == 2:
        return f"socks5h://{parts[0]}:{parts[1]}"
    if len(parts) == 4:
        host, port, username, password = parts
        return f"socks5h://{username}:{password}@{host}:{port}"
    raise ValueError("S5 代理格式应为 host:port、host:port:user:pass 或 socks5://user:pass@host:port")


def save_article(article: dict, comments: list[dict], root: Path, crawler: ToutiaoCrawler) -> tuple[Path, Path]:
    # TXT 文件名和图片子文件夹名使用完全相同的名称，作品 ID 可避免重名。
    name = f'{article["id"]}_{safe_name(article["title"], 40)}'
    content_dir = root / "内容"
    image_dir = root / "图片" / name
    content_dir.mkdir(parents=True, exist_ok=True)

    article["local_images"] = crawler.download_images(article, image_dir)

    # 文件中只写正文和评论，不写标题、作者、时间、链接、点赞等元数据。
    lines = [article["content"].strip(), "", "评论：", ""]
    for index, comment in enumerate(comments, 1):
        lines.append(f'{index}. {comment["text"].strip()}')
        lines.append("")

    txt_path = content_dir / f"{name}.txt"
    txt_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8-sig")
    return txt_path, image_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取今日头条微头条正文、图片和高赞评论")
    parser.add_argument("urls", nargs="*", default=DEFAULT_URLS, help="头条 article/w 链接")
    parser.add_argument("-o", "--output", default="output", help="输出目录")
    parser.add_argument("-n", "--top-comments", type=int, default=10, choices=range(5, 11), metavar="5-10")
    parser.add_argument("--comment-pages", type=int, default=3, help="抓取多少页评论后再按点赞排序")
    parser.add_argument("--proxy", default="", help="SOCKS5 代理，例如 127.0.0.1:1080")
    args = parser.parse_args()
    crawler = ToutiaoCrawler(proxy=args.proxy)
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    failures = 0
    for url in args.urls:
        try:
            print(f"[抓取] {url}")
            article = crawler.article(url)
            comments = crawler.comments(article["id"], article["detail_url"], args.top_comments, args.comment_pages)
            txt_path, image_dir = save_article(article, comments, root, crawler)
            print(
                f"  [完成] 图片 {len(article['local_images'])} 张，高赞评论 {len(comments)} 条"
                f" -> {txt_path}；{image_dir}"
            )
        except Exception as exc:
            failures += 1
            print(f"  [失败] {type(exc).__name__}: {exc}")
        time.sleep(crawler.delay)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
