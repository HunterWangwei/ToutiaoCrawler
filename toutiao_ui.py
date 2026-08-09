#!/usr/bin/env python3
"""今日头条采集桌面 UI。"""

from __future__ import annotations

import queue
import re
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


URL_RE = re.compile(r"https?://(?:www\.|m\.)?toutiao\.com/(?:article|w)/\d+/?[^\s]*", re.I)


class CrawlerUI:
    def __init__(self) -> None:
        self.root = TkinterDnD.Tk()
        self.root.title(f"今日头条内容采集 v{APP_VERSION}")
        self.root.geometry("960x650")
        self.root.minsize(780, 520)
        self.events: queue.Queue[tuple] = queue.Queue()
        self.running = False
        self.cancel_event = threading.Event()
        self.rows: dict[str, str] = {}
        self.active_status: dict[str, tuple[str, float]] = {}
        self._build()
        self.root.after(100, self._poll_events)
        self.root.after(1800, lambda: self._check_update(manual=False))

    def _build(self) -> None:
        settings = ttk.LabelFrame(self.root, text="采集设置", padding=10)
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
        settings.columnconfigure(1, weight=1)

        drop = ttk.LabelFrame(self.root, text="链接导入", padding=10)
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

        table_frame = ttk.Frame(self.root)
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

        actions = ttk.Frame(self.root, padding=(12, 6, 12, 12))
        actions.pack(fill="x")
        self.summary_var = tk.StringVar(value="等待导入链接")
        ttk.Label(actions, textvariable=self.summary_var).pack(side="left")
        ttk.Label(actions, text=f"v{APP_VERSION}").pack(side="left", padx=(12, 0))
        self.update_button = ttk.Button(actions, text="检查更新", command=lambda: self._check_update(True))
        self.update_button.pack(side="right", padx=(8, 0))
        self.stop_button = ttk.Button(actions, text="停止", command=self._stop, state="disabled")
        self.stop_button.pack(side="right")
        self.start_button = ttk.Button(actions, text="开始采集", command=self._start)
        self.start_button.pack(side="right", padx=(0, 8))

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
        for url in urls:
            self._set_status(url, "等待采集")
        threading.Thread(target=self._worker, args=(urls, count, workers), daemon=True).start()

    def _worker(self, urls: list[str], count: int, workers: int) -> None:
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

        def process(url: str) -> bool:
            if self.cancel_event.is_set():
                self.events.put(("status", url, "已停止"))
                return False
            crawler = ToutiaoCrawler(proxy=proxy)
            try:
                log(url, "开始采集")
                self.events.put(("status", url, "采集正文"))
                article = crawler.article(url)
                log(url, f"正文成功，长度 {len(article['content'])}，图片 {len(article['images'])} 张")
                self.events.put(("status", url, "采集评论"))
                comments = crawler.comments(article["id"], article["detail_url"], count, pages=3)
                log(url, f"评论成功，筛选 {len(comments)} 条")
                self.events.put(("status", url, "下载图片"))
                save_article(article, comments, root, crawler)
                log(url, "采集完成")
                self.events.put(("status", url, "完成"))
                return True
            except Exception as exc:
                message = str(exc).replace("\n", " ")[:80]
                log(url, f"失败：{type(exc).__name__}: {exc}")
                self.events.put(("status", url, f"失败：{message}"))
                return False
            finally:
                time.sleep(crawler.delay)

        completed = 0
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="toutiao") as executor:
            futures = {executor.submit(process, url): url for url in urls}
            for future in as_completed(futures):
                try:
                    if future.result():
                        completed += 1
                except Exception as exc:
                    url = futures[future]
                    log(url, f"线程异常：{type(exc).__name__}: {exc}")
                    self.events.put(("status", url, f"失败：线程异常 {exc}"))
        self.events.put(("done", completed, len(urls)))

    def _stop(self) -> None:
        self.cancel_event.set()
        self.summary_var.set("正在停止，将在当前请求结束后停止……")

    def _check_update(self, manual: bool) -> None:
        if self.running:
            if manual:
                messagebox.showinfo("检查更新", "请在采集任务结束后检查更新。")
            return
        self.update_button.configure(state="disabled")
        if manual:
            self.summary_var.set("正在检查 GitHub 更新……")

        def worker() -> None:
            try:
                release = check_latest_release(self.proxy_var.get())
                self.events.put(("update_check", manual, release, None))
            except Exception as exc:
                self.events.put(("update_check", manual, None, str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _download_update(self, release: dict) -> None:
        self.update_button.configure(state="disabled")
        self.summary_var.set(f"正在下载 v{release['version']}……")

        def worker() -> None:
            try:
                path = download_release(release, self.proxy_var.get())
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
                    self.summary_var.set(f"采集结束：完成 {event[1]} / {event[2]} 条")
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
                        messagebox.showerror("更新失败", error)
                        self.summary_var.set("更新失败")
                    else:
                        try:
                            install_and_restart(path)
                        except Exception as exc:
                            messagebox.showerror("安装更新失败", str(exc))
                            self.summary_var.set("安装更新失败")
                        else:
                            self.summary_var.set("正在安装更新并重启……")
                            self.root.after(300, self.root.destroy)
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
