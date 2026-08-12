#!/usr/bin/env python3
"""今日头条采集桌面 UI。"""

from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError as exc:
    raise SystemExit("缺少拖拽组件，请先运行：pip install -r requirements.txt") from exc

from toutiao_crawler import ToutiaoCrawler, save_article
from updater import can_self_update, check_latest_release, download_release, install_and_restart
from version import APP_VERSION, GITHUB_RELEASES_URL
from profile_ui import ProfileTab


URL_RE = re.compile(r"https?://(?:www\.|m\.)?toutiao\.com/(?:article|w)/\d+/?[^\s]*", re.I)
DEFAULT_BLOCKED_WORDS = "政治|中央|证券|央行"


class CrawlerUI:
    def __init__(self) -> None:
        self.root = TkinterDnD.Tk()
        self.root.title(f"今日头条内容采集 v{APP_VERSION}")
        self.root.geometry("1050x720")
        self.root.minsize(860, 600)
        self.events: queue.Queue[tuple] = queue.Queue()
        self.running = False
        self.cancel_event = threading.Event()
        self.rows: dict[str, str] = {}
        self.active_status: dict[str, tuple[str, float]] = {}
        self.config_save_job: str | None = None
        self.config_path = self._find_config_path()
        self.config = self._load_config()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_events)
        self.root.after(1800, lambda: self._check_update(manual=False))

    def _build(self) -> None:
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True)
        micro_tab = ttk.Frame(self.notebook)
        profile_tab = ttk.Frame(self.notebook)
        self.notebook.add(micro_tab, text="微头条链接采集")
        self.notebook.add(profile_tab, text="个人主页采集")

        settings = ttk.LabelFrame(micro_tab, text="微头条链接采集设置", padding=10)
        settings.pack(fill="x", padx=12, pady=(12, 6))

        ttk.Label(settings, text="保存目录：").grid(row=0, column=0, sticky="w")
        self.output_var = tk.StringVar(value=str((Path.cwd() / "output").resolve()))
        ttk.Entry(settings, textvariable=self.output_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(settings, text="选择", command=self._choose_output).grid(row=0, column=2)

        ttk.Label(settings, text="S5 代理：").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.proxy_var = tk.StringVar()
        ttk.Entry(settings, textvariable=self.proxy_var).grid(row=1, column=1, sticky="ew", padx=6, pady=(8, 0))
        ttk.Label(settings, text="如 127.0.0.1:1080 或 host:port:user:pass").grid(
            row=1, column=2, sticky="w", pady=(8, 0)
        )

        ttk.Label(settings, text="高赞评论：").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.comment_count = tk.IntVar(value=10)
        ttk.Spinbox(settings, from_=5, to=10, textvariable=self.comment_count, width=7).grid(
            row=2, column=1, sticky="w", padx=6, pady=(8, 0)
        )
        ttk.Label(settings, text="并发线程：").grid(row=2, column=1, sticky="w", padx=(100, 0), pady=(8, 0))
        self.thread_count = tk.IntVar(value=3)
        ttk.Spinbox(settings, from_=1, to=5, textvariable=self.thread_count, width=7).grid(
            row=2, column=1, sticky="w", padx=(170, 0), pady=(8, 0)
        )
        ttk.Label(settings, text="建议 3，最高 5").grid(row=2, column=2, sticky="w", pady=(8, 0))

        ttk.Label(settings, text="违禁词：").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.blocked_words_var = tk.StringVar(
            value=str(self.config.get("blocked_words") or DEFAULT_BLOCKED_WORDS)
        )
        ttk.Entry(settings, textvariable=self.blocked_words_var).grid(
            row=3, column=1, columnspan=2, sticky="ew", padx=6, pady=(8, 0)
        )
        self.blocked_words_var.trace_add("write", self._schedule_config_save)
        settings.columnconfigure(1, weight=1)

        drop = ttk.LabelFrame(micro_tab, text="链接导入", padding=10)
        drop.pack(fill="x", padx=12, pady=6)
        self.drop_label = ttk.Label(
            drop,
            text="把包含头条链接的 TXT 文件拖到这里\n也可以点击右侧按钮导入",
            anchor="center",
            justify="center",
            relief="groove",
            padding=16,
        )
        self.drop_label.pack(side="left", fill="x", expand=True)
        self.drop_label.drop_target_register(DND_FILES)
        self.drop_label.dnd_bind("<<Drop>>", self._on_drop)
        ttk.Button(drop, text="导入 TXT", command=self._choose_txt).pack(side="left", padx=(10, 0))
        ttk.Button(drop, text="清空列表", command=self._clear).pack(side="left", padx=(8, 0))

        table_frame = ttk.Frame(micro_tab)
        table_frame.pack(fill="both", expand=True, padx=12, pady=6)
        self.table = ttk.Treeview(table_frame, columns=("url", "status"), show="headings")
        self.table.heading("url", text="链接")
        self.table.heading("status", text="采集状态")
        self.table.column("url", width=700, minwidth=350)
        self.table.column("status", width=180, minwidth=130, anchor="center")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        actions = ttk.Frame(micro_tab, padding=(12, 6, 12, 12))
        actions.pack(fill="x")
        self.summary_var = tk.StringVar(value="等待导入链接")
        ttk.Label(actions, textvariable=self.summary_var).pack(side="left")
        self.update_progress_var = tk.DoubleVar(value=0)
        self.update_progress = ttk.Progressbar(
            actions, variable=self.update_progress_var, maximum=100, length=160, mode="determinate"
        )
        self.update_progress.pack(side="left", padx=(12, 4))
        self.update_progress_text = tk.StringVar(value="")
        ttk.Label(actions, textvariable=self.update_progress_text, width=8).pack(side="left")
        ttk.Label(actions, text=f"v{APP_VERSION}").pack(side="left", padx=(12, 0))
        self.update_button = ttk.Button(actions, text="检查更新", command=lambda: self._check_update(True))
        self.update_button.pack(side="right", padx=(8, 0))
        self.stop_button = ttk.Button(actions, text="停止", command=self._stop, state="disabled")
        self.stop_button.pack(side="right")
        self.start_button = ttk.Button(actions, text="开始采集", command=self._start)
        self.start_button.pack(side="right", padx=(0, 8))

        self.profile_tab = ProfileTab(
            profile_tab,
            self.root,
            self.config,
            self._schedule_config_save,
        )

    @staticmethod
    def _config_candidates() -> list[Path]:
        if getattr(sys, "frozen", False):
            primary = Path(sys.executable).resolve().parent / "config.json"
        else:
            primary = Path(__file__).resolve().parent / "config.json"
        appdata = Path(os.environ.get("APPDATA") or Path.home()) / "ToutiaoCrawler" / "config.json"
        return [primary, appdata]

    def _find_config_path(self) -> Path:
        candidates = self._config_candidates()
        for path in candidates:
            if path.is_file():
                return path
        return candidates[0]

    def _load_config(self) -> dict:
        for path in self._config_candidates():
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    self.config_path = path
                    return data
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
        return {}

    def _schedule_config_save(self, *_args) -> None:
        if self.config_save_job:
            self.root.after_cancel(self.config_save_job)
        self.config_save_job = self.root.after(500, self._save_config)

    def _save_config(self) -> None:
        self.config_save_job = None
        data = {
            "config_version": 1,
            "blocked_words": self.blocked_words_var.get(),
        }
        if hasattr(self, "profile_tab"):
            data.update(self.profile_tab.config_values())
        candidates = [self.config_path] + [
            path for path in self._config_candidates() if path != self.config_path
        ]
        for path in candidates:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".json.tmp")
                temporary.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                temporary.replace(path)
                self.config_path = path
                self.config = data
                return
            except OSError:
                continue

    def _on_close(self) -> None:
        self.cancel_event.set()
        if hasattr(self, "profile_tab"):
            self.profile_tab.stop_event.set()
        if self.config_save_job:
            self.root.after_cancel(self.config_save_job)
            self.config_save_job = None
        self._save_config()
        self.root.destroy()

    def _choose_output(self) -> None:
        path = filedialog.askdirectory(initialdir=self.output_var.get() or str(Path.cwd()))
        if path:
            self.output_var.set(path)

    def _choose_txt(self) -> None:
        paths = filedialog.askopenfilenames(filetypes=[("TXT 文件", "*.txt"), ("所有文件", "*.*")])
        self._import_files(list(paths))

    def _on_drop(self, event) -> None:
        self._import_files(list(self.root.tk.splitlist(event.data)))

    def _import_files(self, paths: list[str]) -> None:
        added = 0
        errors = []
        for raw_path in paths:
            path = Path(raw_path)
            if not path.is_file() or path.suffix.lower() != ".txt":
                errors.append(path.name or raw_path)
                continue
            try:
                text = path.read_text(encoding="utf-8-sig")
            except UnicodeDecodeError:
                text = path.read_text(encoding="gb18030")
            for url in URL_RE.findall(text):
                url = url.rstrip(".,;，。；、)）]】")
                if url not in self.rows:
                    item = self.table.insert("", "end", values=(url, "等待采集"))
                    self.rows[url] = item
                    added += 1
        self.summary_var.set(f"已导入 {len(self.rows)} 条链接，本次新增 {added} 条")
        if errors:
            messagebox.showwarning("部分文件未导入", "只支持 TXT 文件：\n" + "\n".join(errors[:8]))

    def _clear(self) -> None:
        if self.running:
            return
        for item in self.table.get_children():
            self.table.delete(item)
        self.rows.clear()
        self.summary_var.set("等待导入链接")

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
        if self.running or not self.rows:
            if not self.rows:
                messagebox.showinfo("没有链接", "请先拖入或导入包含头条链接的 TXT 文件。")
            return
        try:
            count = int(self.comment_count.get())
            if not 5 <= count <= 10:
                raise ValueError
            workers = int(self.thread_count.get())
            if not 1 <= workers <= 5:
                raise ValueError("并发线程必须设置为 1–5")
            # 提前验证代理格式和依赖是否正常。
            ToutiaoCrawler(proxy=self.proxy_var.get())
        except Exception as exc:
            messagebox.showerror("设置错误", str(exc))
            return
        self.running = True
        self.cancel_event.clear()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        urls = list(self.rows)
        blocked_words = [
            word.strip()
            for word in re.split(r"[|｜]", self.blocked_words_var.get())
            if word.strip()
        ]
        for url in urls:
            self._set_status(url, "等待采集")
        threading.Thread(
            target=self._worker,
            args=(urls, count, workers, blocked_words),
            daemon=True,
        ).start()

    def _worker(self, urls: list[str], count: int, workers: int, blocked_words: list[str]) -> None:
        proxy = self.proxy_var.get()
        root = Path(self.output_var.get()).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        log_path = root / "采集日志.txt"
        log_lock = threading.Lock()

        def log(url: str, message: str) -> None:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with log_lock:
                with log_path.open("a", encoding="utf-8-sig") as stream:
                    stream.write(f"[{stamp}] {url} | {message}\n")

        def process(url: str) -> str:
            if self.cancel_event.is_set():
                self.events.put(("status", url, "已停止"))
                return "stopped"
            crawler = ToutiaoCrawler(proxy=proxy)
            try:
                log(url, "开始采集")
                self.events.put(("status", url, "采集正文"))
                article = crawler.article(url)
                log(url, f"正文成功，长度 {len(article['content'])}，图片 {len(article['images'])} 张")
                article_text = f"{article.get('title', '')}\n{article.get('content', '')}"
                matched_words = [word for word in blocked_words if word in article_text]
                if matched_words:
                    matched = "、".join(matched_words)
                    log(url, f"含违禁词，已过滤：{matched}")
                    self.events.put(("status", url, f"含违禁词：{matched}"))
                    return "filtered"
                self.events.put(("status", url, "采集评论"))
                comments = crawler.comments(article["id"], article["detail_url"], count, pages=3)
                log(url, f"评论成功，筛选 {len(comments)} 条")
                self.events.put(("status", url, "下载图片"))
                save_article(article, comments, root, crawler)
                log(url, "采集完成")
                self.events.put(("status", url, "完成"))
                return "completed"
            except Exception as exc:
                message = str(exc).replace("\n", " ")[:80]
                log(url, f"失败：{type(exc).__name__}: {exc}")
                self.events.put(("status", url, f"失败：{message}"))
                return "failed"
            finally:
                time.sleep(crawler.delay)

        completed = 0
        filtered = 0
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="toutiao") as executor:
            futures = {executor.submit(process, url): url for url in urls}
            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result == "completed":
                        completed += 1
                    elif result == "filtered":
                        filtered += 1
                except Exception as exc:
                    url = futures[future]
                    log(url, f"线程异常：{type(exc).__name__}: {exc}")
                    self.events.put(("status", url, f"失败：线程异常 {exc}"))
        self.events.put(("done", completed, filtered, len(urls)))

    def _stop(self) -> None:
        self.cancel_event.set()
        self.summary_var.set("正在停止，将在当前请求结束后停止……")

    def _check_update(self, manual: bool) -> None:
        if self.running or (hasattr(self, "profile_tab") and self.profile_tab.running):
            if manual:
                messagebox.showinfo("检查更新", "请在采集任务结束后检查更新。")
            return
        self.update_button.configure(state="disabled")
        if manual:
            self.summary_var.set("正在检查 GitHub 更新……")

        def worker() -> None:
            try:
                release = check_latest_release(
                    self.proxy_var.get(),
                    lambda: self.events.put(("update_fallback", "正在尝试内置备用更新代理……")),
                )
                self.events.put(("update_check", manual, release, None))
            except Exception as exc:
                self.events.put(("update_check", manual, None, str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _download_update(self, release: dict) -> None:
        self.update_button.configure(state="disabled")
        self.summary_var.set(f"正在下载 v{release['version']}……")
        self.update_progress.stop()
        self.update_progress.configure(mode="determinate")
        self.update_progress_var.set(0)
        self.update_progress_text.set("0%")

        def worker() -> None:
            try:
                def progress(downloaded: int, total: int) -> None:
                    self.events.put(("update_progress", downloaded, total))

                path = download_release(
                    release,
                    self.proxy_var.get(),
                    progress,
                    lambda: self.events.put(("update_fallback", "直连失败，正在使用内置备用代理下载……")),
                )
                self.events.put(("update_download", release, path, None))
            except Exception as exc:
                self.events.put(("update_download", release, None, str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "status":
                    self._set_status(event[1], event[2])
                elif event[0] == "done":
                    self.running = False
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.summary_var.set(
                        f"采集结束：完成 {event[1]} 条，违禁词过滤 {event[2]} 条，"
                        f"总计 {event[3]} 条"
                    )
                elif event[0] == "update_check":
                    _, manual, release, error = event
                    self.update_button.configure(state="normal")
                    if error:
                        if manual:
                            messagebox.showerror("检查更新失败", error)
                            self.summary_var.set("检查更新失败")
                    elif release is None:
                        if manual:
                            messagebox.showinfo("检查更新", f"当前 v{APP_VERSION} 已是最新版本。")
                            self.summary_var.set("当前已是最新版本")
                    else:
                        notes = str(release.get("notes") or "").strip()
                        preview = notes[:500] + ("……" if len(notes) > 500 else "")
                        prompt = f"发现新版本 v{release['version']}\n\n{preview}\n\n是否立即更新？"
                        if messagebox.askyesno("发现新版本", prompt):
                            if can_self_update():
                                self._download_update(release)
                            else:
                                webbrowser.open(release.get("page_url") or GITHUB_RELEASES_URL)
                        elif manual:
                            self.summary_var.set("已取消更新")
                elif event[0] == "update_download":
                    _, release, path, error = event
                    self.update_button.configure(state="normal")
                    if error:
                        self.update_progress.stop()
                        self.update_progress.configure(mode="determinate")
                        self.update_progress_var.set(0)
                        self.update_progress_text.set("")
                        messagebox.showerror("更新失败", error)
                        self.summary_var.set("更新失败")
                    else:
                        try:
                            install_and_restart(path)
                        except Exception as exc:
                            self.update_progress.stop()
                            self.update_progress.configure(mode="determinate")
                            self.update_progress_var.set(0)
                            self.update_progress_text.set("")
                            messagebox.showerror("安装更新失败", str(exc))
                            self.summary_var.set("安装更新失败")
                        else:
                            self.update_progress.stop()
                            self.update_progress.configure(mode="determinate")
                            self.update_progress_var.set(100)
                            self.update_progress_text.set("100%")
                            self.summary_var.set(f"新版已启动：{path.name}；正在关闭旧版……")
                            self.root.after(1200, self.root.destroy)
                elif event[0] == "update_progress":
                    downloaded, total = event[1], event[2]
                    if total > 0:
                        percent = min(100, downloaded * 100 / total)
                        self.update_progress.configure(mode="determinate")
                        self.update_progress_var.set(percent)
                        self.update_progress_text.set(f"{percent:.0f}%")
                        self.summary_var.set(
                            f"正在下载更新：{downloaded / 1024 / 1024:.1f}/"
                            f"{total / 1024 / 1024:.1f} MB"
                        )
                    else:
                        if str(self.update_progress.cget("mode")) != "indeterminate":
                            self.update_progress.configure(mode="indeterminate")
                            self.update_progress.start(12)
                        self.update_progress_text.set(f"{downloaded / 1024 / 1024:.1f} MB")
                elif event[0] == "update_fallback":
                    self.summary_var.set(event[1])
        except queue.Empty:
            pass
        for url, (stage, started) in list(self.active_status.items()):
            item = self.rows.get(url)
            if item:
                elapsed = int(time.monotonic() - started)
                self.table.set(item, "status", f"{stage}（{elapsed}秒）")
        self.root.after(100, self._poll_events)

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    CrawlerUI().run()
