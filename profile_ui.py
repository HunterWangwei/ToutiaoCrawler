"""“个人主页采集”独立标签页。"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from toutiao_profile_crawler import ProfileCrawler, safe_name, save_post


DEFAULT_PROFILE_BLOCKED_WORDS = "政治|中央|证券|央行"


class ProfileTab:
    def __init__(self, parent, root, config: dict, schedule_config_save) -> None:
        self.parent = parent
        self.root = root
        self.config = config
        self.schedule_config_save = schedule_config_save
        self.events: queue.Queue[tuple] = queue.Queue()
        self.running = False
        self.stop_event = threading.Event()
        self.rows: dict[str, str] = {}
        self.active_status: dict[str, tuple[str, float]] = {}
        self._build()
        self.root.after(120, self._poll_events)

    def _build(self) -> None:
        settings = ttk.LabelFrame(self.parent, text="个人主页采集设置", padding=10)
        settings.pack(fill="x", padx=12, pady=(12, 6))

        ttk.Label(settings, text="主页链接：").grid(row=0, column=0, sticky="w")
        self.profile_url = self._string_var("profile_url", "")
        ttk.Entry(settings, textvariable=self.profile_url).grid(
            row=0, column=1, columnspan=3, sticky="ew", padx=6
        )

        ttk.Label(settings, text="保存目录：").grid(row=1, column=0, sticky="w", pady=(8, 0))
        default_output = str((Path.cwd() / "profile_output").resolve())
        self.output_var = self._string_var("profile_output", default_output)
        ttk.Entry(settings, textvariable=self.output_var).grid(
            row=1, column=1, columnspan=2, sticky="ew", padx=6, pady=(8, 0)
        )
        ttk.Button(settings, text="选择", command=self._choose_output).grid(
            row=1, column=3, pady=(8, 0)
        )

        ttk.Label(settings, text="S5 代理：").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.proxy_var = self._string_var("", "")
        ttk.Entry(settings, textvariable=self.proxy_var).grid(
            row=2, column=1, sticky="ew", padx=6, pady=(8, 0)
        )
        ttk.Label(settings, text="Cookie 文件：").grid(row=2, column=2, sticky="e", pady=(8, 0))
        self.cookie_file_var = self._string_var("", "")
        cookie_box = ttk.Frame(settings)
        cookie_box.grid(row=2, column=3, sticky="ew", pady=(8, 0))
        ttk.Entry(cookie_box, textvariable=self.cookie_file_var, width=22).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(cookie_box, text="选择", command=self._choose_cookie).pack(side="left", padx=(5, 0))

        ttk.Label(settings, text="主页页数：").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.profile_pages = self._int_var("profile_pages", 0)
        ttk.Spinbox(settings, from_=0, to=999, textvariable=self.profile_pages, width=7).grid(
            row=3, column=1, sticky="w", padx=6, pady=(8, 0)
        )
        ttk.Label(settings, text="0 表示抓到末页").grid(
            row=3, column=1, sticky="w", padx=(75, 0), pady=(8, 0)
        )

        ttk.Label(settings, text="高赞评论：").grid(row=3, column=2, sticky="e", pady=(8, 0))
        self.comment_count = self._int_var("profile_comments", 10)
        ttk.Spinbox(settings, from_=5, to=10, textvariable=self.comment_count, width=7).grid(
            row=3, column=3, sticky="w", padx=6, pady=(8, 0)
        )

        ttk.Label(settings, text="并发线程：").grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.thread_count = self._int_var("profile_threads", 3)
        ttk.Spinbox(settings, from_=1, to=5, textvariable=self.thread_count, width=7).grid(
            row=4, column=1, sticky="w", padx=6, pady=(8, 0)
        )
        ttk.Label(settings, text="当前采集作者：").grid(row=4, column=2, sticky="e", pady=(8, 0))
        self.author_var = self._string_var("", "尚未识别")
        ttk.Label(settings, textvariable=self.author_var).grid(
            row=4, column=3, sticky="w", padx=6, pady=(8, 0)
        )

        ttk.Label(settings, text="违禁词：").grid(row=5, column=0, sticky="w", pady=(8, 0))
        self.blocked_words_var = self._string_var(
            "profile_blocked_words", DEFAULT_PROFILE_BLOCKED_WORDS
        )
        ttk.Entry(settings, textvariable=self.blocked_words_var).grid(
            row=5, column=1, columnspan=3, sticky="ew", padx=6, pady=(8, 0)
        )
        settings.columnconfigure(1, weight=1)
        settings.columnconfigure(3, weight=1)

        table_frame = ttk.Frame(self.parent)
        table_frame.pack(fill="both", expand=True, padx=12, pady=6)
        self.table = ttk.Treeview(table_frame, columns=("url", "status"), show="headings")
        self.table.heading("url", text="主页作品链接")
        self.table.heading("status", text="采集状态")
        self.table.column("url", width=700, minwidth=350)
        self.table.column("status", width=200, minwidth=140, anchor="center")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        actions = ttk.Frame(self.parent, padding=(12, 6, 12, 12))
        actions.pack(fill="x")
        self.summary_var = self._string_var("", "等待输入个人主页链接")
        ttk.Label(actions, textvariable=self.summary_var).pack(side="left")
        ttk.Button(actions, text="清空列表", command=self._clear).pack(side="right", padx=(8, 0))
        self.stop_button = ttk.Button(actions, text="停止", command=self._stop, state="disabled")
        self.stop_button.pack(side="right", padx=(8, 0))
        self.start_button = ttk.Button(actions, text="开始采集主页", command=self._start)
        self.start_button.pack(side="right")

    def _string_var(self, key: str, default: str):
        import tkinter as tk

        value = self.config.get(key, default) if key else default
        var = tk.StringVar(value=str(value))
        if key:
            var.trace_add("write", self.schedule_config_save)
        return var

    def _int_var(self, key: str, default: int):
        import tkinter as tk

        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        var = tk.IntVar(value=value)
        var.trace_add("write", self.schedule_config_save)
        return var

    def config_values(self) -> dict:
        def integer(var, default: int) -> int:
            try:
                return int(var.get())
            except (ValueError, TypeError):
                return default

        return {
            "profile_url": self.profile_url.get(),
            "profile_output": self.output_var.get(),
            "profile_pages": integer(self.profile_pages, 0),
            "profile_comments": integer(self.comment_count, 10),
            "profile_threads": integer(self.thread_count, 3),
            "profile_blocked_words": self.blocked_words_var.get(),
        }

    def _choose_output(self) -> None:
        path = filedialog.askdirectory(initialdir=self.output_var.get() or str(Path.cwd()))
        if path:
            self.output_var.set(path)

    def _choose_cookie(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("TXT 文件", "*.txt"), ("所有文件", "*.*")])
        if path:
            self.cookie_file_var.set(path)

    def _clear(self) -> None:
        if self.running:
            return
        for item in self.table.get_children():
            self.table.delete(item)
        self.rows.clear()
        self.author_var.set("正在识别……")
        self.summary_var.set("等待输入个人主页链接")

    def _set_status(self, url: str, status: str) -> None:
        item = self.rows.get(url)
        if item:
            self.table.set(item, "status", status)
            self.table.see(item)
        if status in {"采集正文", "采集评论", "下载图片"}:
            self.active_status[url] = (status, time.monotonic())
        else:
            self.active_status.pop(url, None)

    def _start(self) -> None:
        if self.running:
            return
        profile_url = self.profile_url.get().strip()
        try:
            ProfileCrawler.profile_token(profile_url)
            pages = int(self.profile_pages.get())
            comments = int(self.comment_count.get())
            workers = int(self.thread_count.get())
            if pages < 0:
                raise ValueError("主页页数不能小于 0")
            if not 5 <= comments <= 10:
                raise ValueError("评论数量必须设置为 5–10")
            if not 1 <= workers <= 5:
                raise ValueError("并发线程必须设置为 1–5")
            cookie_file = self.cookie_file_var.get().strip()
            cookie = Path(cookie_file).read_text(encoding="utf-8-sig").strip() if cookie_file else ""
            ProfileCrawler(cookie=cookie, proxy=self.proxy_var.get())
        except Exception as exc:
            messagebox.showerror("个人主页设置错误", str(exc))
            return

        for item in self.table.get_children():
            self.table.delete(item)
        self.rows.clear()
        blocked = [
            word.strip()
            for word in re.split(r"[|｜]", self.blocked_words_var.get())
            if word.strip()
        ]
        proxy = self.proxy_var.get().strip()
        output_path = self.output_var.get().strip()
        self.running = True
        self.stop_event.clear()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.summary_var.set("正在读取个人主页作品列表……")
        threading.Thread(
            target=self._worker,
            args=(profile_url, pages, comments, workers, cookie, blocked, proxy, output_path),
            daemon=True,
        ).start()

    def _worker(
        self,
        profile_url: str,
        pages: int,
        comments: int,
        workers: int,
        cookie: str,
        blocked_words: list[str],
        proxy: str,
        output_path: str,
    ) -> None:
        base_root = Path(output_path).expanduser().resolve()
        base_root.mkdir(parents=True, exist_ok=True)
        log_lock = threading.Lock()
        log_path: Path | None = None

        def log(url: str, message: str) -> None:
            if log_path is None:
                return
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with log_lock:
                with log_path.open("a", encoding="utf-8-sig") as stream:
                    stream.write(f"[{stamp}] {url} | {message}\n")

        try:
            listing_crawler = ProfileCrawler(cookie=cookie, proxy=proxy)
            urls = listing_crawler.list_post_urls(profile_url, max_pages=pages)
        except Exception as exc:
            self.events.put(("listing_error", str(exc)))
            return

        # 先读取一篇作品确定主页作者，再创建“保存目录/作者名/”结构。
        preloaded_posts: dict[str, dict] = {}
        author = ""
        for candidate_url in urls[:5]:
            try:
                candidate_crawler = ProfileCrawler(cookie=cookie, proxy=proxy)
                candidate_post = candidate_crawler.post(candidate_url)
                preloaded_posts[candidate_url] = candidate_post
                author = str(candidate_post.get("author") or "").strip()
                if author:
                    break
            except Exception:
                continue
        if not author:
            token = ProfileCrawler.profile_token(profile_url)
            author = f"未知作者_{safe_name(token, 12)}"
        root = base_root / safe_name(author, 50)
        root.mkdir(parents=True, exist_ok=True)
        log_path = root / "个人主页采集日志.txt"
        self.events.put(("author", author, str(root)))
        self.events.put(("urls", urls))
        results: list[dict] = []

        def process(url: str) -> tuple[str, dict | None]:
            if self.stop_event.is_set():
                self.events.put(("status", url, "已停止"))
                return "stopped", None
            crawler = ProfileCrawler(cookie=cookie, proxy=proxy)
            try:
                log(url, "开始采集")
                self.events.put(("status", url, "采集正文"))
                post = preloaded_posts.get(url) or crawler.post(url)
                matched = [word for word in blocked_words if word in post.get("content", "")]
                if matched:
                    log(url, "含违禁词，已过滤：" + "、".join(matched))
                    self.events.put(("status", url, "含违禁词：" + "、".join(matched)))
                    return "filtered", None
                self.events.put(("status", url, "采集评论"))
                post_comments = crawler.comments(post["id"], post["detail_url"], comments, 3)
                self.events.put(("status", url, "下载图片"))
                save_post(root, crawler, post, post_comments)
                post["comments"] = post_comments
                log(url, f"完成：图片 {len(post.get('local_images') or [])} 张，评论 {len(post_comments)} 条")
                self.events.put(("status", url, "完成"))
                return "completed", post
            except Exception as exc:
                log(url, f"失败：{type(exc).__name__}: {exc}")
                self.events.put(("status", url, "失败：" + str(exc).replace("\n", " ")[:80]))
                return "failed", None

        completed = filtered = failed = 0
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="profile") as executor:
            futures = {executor.submit(process, url): url for url in urls}
            for future in as_completed(futures):
                try:
                    status, post = future.result()
                except Exception as exc:
                    status, post = "failed", None
                    url = futures[future]
                    log(url, f"线程异常：{type(exc).__name__}: {exc}")
                    self.events.put(("status", url, f"失败：线程异常 {exc}"))
                if status == "completed":
                    completed += 1
                    if post:
                        results.append(post)
                elif status == "filtered":
                    filtered += 1
                elif status == "failed":
                    failed += 1

        (root / "posts.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8-sig"
        )
        self.events.put(("done", completed, filtered, failed, len(urls), str(root)))

    def _stop(self) -> None:
        self.stop_event.set()
        self.summary_var.set("正在停止，运行中的请求结束后停止……")

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "urls":
                    urls = event[1]
                    for url in urls:
                        self.rows[url] = self.table.insert("", "end", values=(url, "等待采集"))
                    self.summary_var.set(f"主页发现 {len(urls)} 条作品，开始采集……")
                elif event[0] == "author":
                    self.author_var.set(event[1])
                    self.summary_var.set(f"当前采集作者：{event[1]}；保存到 {event[2]}")
                elif event[0] == "status":
                    self._set_status(event[1], event[2])
                elif event[0] == "listing_error":
                    self.running = False
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.summary_var.set("读取个人主页失败")
                    self.author_var.set("识别失败")
                    messagebox.showerror("个人主页读取失败", event[1])
                elif event[0] == "done":
                    self.running = False
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.summary_var.set(
                        f"采集结束：完成 {event[1]}，违禁词 {event[2]}，失败 {event[3]}，"
                        f"总计 {event[4]}；作者目录 {event[5]}"
                    )
        except queue.Empty:
            pass

        for url, (stage, started) in list(self.active_status.items()):
            item = self.rows.get(url)
            if item:
                self.table.set(item, "status", f"{stage}（{int(time.monotonic() - started)}秒）")
        self.root.after(120, self._poll_events)
