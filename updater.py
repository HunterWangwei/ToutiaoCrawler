"""基于 GitHub Releases 的 Windows 单文件更新器。"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import requests

from toutiao_crawler import normalize_socks5_proxy
from update_proxy_config import BUILTIN_UPDATE_PROXY
from version import (
    APP_NAME,
    APP_VERSION,
    CHECKSUM_ASSET_NAME,
    GITHUB_API_LATEST,
    UPDATE_ASSET_NAME,
)


def _version_tuple(value: str) -> tuple[int, int, int, int, int]:
    """生成可比较版本键：alpha < beta < test < rc < 正式版。"""
    normalized = value.strip().lower().lstrip("v")
    match = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-_.]?([a-z]+)(\d*)?)?", normalized)
    if not match:
        return 0, 0, 0, 0, 0
    major, minor, patch = (int(item or 0) for item in match.group(1, 2, 3))
    label = match.group(4) or ""
    prerelease_number = int(match.group(5) or 0)
    aliases = {"a": "alpha", "b": "beta", "preview": "test", "pre": "test"}
    label = aliases.get(label, label)
    stage = {"alpha": 0, "beta": 1, "test": 2, "rc": 3}.get(label, 4 if not label else 0)
    return major, minor, patch, stage, prerelease_number


def _session(proxy: str = "") -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    normalized = normalize_socks5_proxy(proxy)
    if normalized:
        session.proxies.update({"http": normalized, "https": normalized})
    session.headers.update({
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    return session


def _proxy_candidates(proxy: str = "") -> list[tuple[str, bool]]:
    """代理优先：用户代理、内置代理、最后才尝试直连。"""
    user_proxy = proxy.strip()
    builtin = BUILTIN_UPDATE_PROXY.strip()
    candidates: list[tuple[str, bool]] = []
    if user_proxy:
        candidates.append((user_proxy, False))
    if builtin and builtin != user_proxy:
        candidates.append((builtin, True))
    candidates.append(("", False))
    return candidates


def _run_with_proxy_fallback(operation, proxy: str = "", fallback_notice=None):
    errors: list[str] = []
    candidates = _proxy_candidates(proxy)
    for candidate, is_builtin in candidates:
        if is_builtin and fallback_notice:
            fallback_notice()
        channel = "用户代理" if candidate and not is_builtin else "内置代理" if is_builtin else "直连"
        for attempt in range(1, 4):
            try:
                return operation(candidate, is_builtin)
            except Exception as exc:
                errors.append(f"{channel}第{attempt}次：{type(exc).__name__}: {exc}")
                if attempt < 3:
                    time.sleep(attempt)
    raise RuntimeError("所有更新通道均失败：\n" + "\n".join(errors))


def check_latest_release(proxy: str = "", fallback_notice=None) -> dict | None:
    """有新版时返回发布信息，否则返回 None。"""
    def request(candidate: str, is_builtin: bool):
        with _session(candidate) as session:
            response = session.get(GITHUB_API_LATEST, timeout=(10, 20))
            response.raise_for_status()
            return response.json(), is_builtin

    release, used_builtin = _run_with_proxy_fallback(request, proxy, fallback_notice)

    tag = str(release.get("tag_name") or "").strip()
    if not tag or _version_tuple(tag) <= _version_tuple(APP_VERSION):
        return None

    assets = {asset.get("name"): asset for asset in release.get("assets") or []}
    executable = assets.get(UPDATE_ASSET_NAME)
    checksum = assets.get(CHECKSUM_ASSET_NAME)
    if not executable or not checksum:
        raise RuntimeError("GitHub 新版本缺少 EXE 或 SHA256 校验文件")

    return {
        "version": tag.lstrip("vV"),
        "notes": release.get("body") or "",
        "page_url": release.get("html_url") or "",
        "exe_url": executable.get("browser_download_url"),
        "checksum_url": checksum.get("browser_download_url"),
        "used_builtin_proxy": used_builtin,
    }


def download_release(
    release: dict,
    proxy: str = "",
    progress: Callable[[int, int], None] | None = None,
    fallback_notice=None,
) -> Path:
    """下载到当前程序目录，校验后返回带版本号的新版 EXE 路径。"""
    if not can_self_update():
        raise RuntimeError("源码运行模式不能自动下载到程序目录，请下载 Release 版本")
    version = str(release["version"]).strip().lstrip("vV")
    install_dir = Path(sys.executable).resolve().parent
    final_target = install_dir / f"{APP_NAME}-{version}.exe"
    partial_target = install_dir / f"{APP_NAME}-{version}.exe.part"

    def request(candidate: str, _is_builtin: bool):
        partial_target.unlink(missing_ok=True)
        with _session(candidate) as session:
            checksum_response = session.get(release["checksum_url"], timeout=(10, 20))
            checksum_response.raise_for_status()
            expected = checksum_response.text.strip().split()[0].lower()
            if len(expected) != 64:
                raise RuntimeError("GitHub SHA256 校验文件格式无效")

            with session.get(release["exe_url"], stream=True, timeout=(10, 60)) as response:
                response.raise_for_status()
                total = int(response.headers.get("Content-Length") or 0)
                downloaded = 0
                if progress:
                    progress(downloaded, total)
                digest = hashlib.sha256()
                with partial_target.open("wb") as stream:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            stream.write(chunk)
                            digest.update(chunk)
                            downloaded += len(chunk)
                            if progress:
                                progress(downloaded, total)

        if digest.hexdigest().lower() != expected:
            partial_target.unlink(missing_ok=True)
            raise RuntimeError("新版 EXE 的 SHA256 校验失败，已取消更新")
        os.replace(partial_target, final_target)
        return final_target

    return _run_with_proxy_fallback(request, proxy, fallback_notice)


def can_self_update() -> bool:
    return bool(getattr(sys, "frozen", False)) and sys.platform == "win32"


def install_and_restart(downloaded_exe: Path) -> None:
    """启动并排保存的新版 EXE；调用方随后关闭旧版。"""
    if not can_self_update():
        raise RuntimeError("源码运行模式不能自动启动更新，请下载 Release 版本")
    downloaded_exe = downloaded_exe.resolve()
    if not downloaded_exe.is_file():
        raise FileNotFoundError(f"新版文件不存在：{downloaded_exe}")
    subprocess.Popen(
        [str(downloaded_exe)],
        cwd=str(downloaded_exe.parent),
        creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )
