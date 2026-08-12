"""基于 GitHub Releases 的 Windows 单文件更新器。"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
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


def _version_tuple(value: str) -> tuple[int, ...]:
    value = value.strip().lower().lstrip("v")
    parts = []
    for item in value.split("."):
        digits = "".join(char for char in item if char.isdigit())
        parts.append(int(digits or 0))
    return tuple((parts + [0, 0, 0])[:3])


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
    """依次返回用户更新通道和构建时注入的备用更新通道。"""
    candidates: list[tuple[str, bool]] = [(proxy.strip(), False)]
    builtin = BUILTIN_UPDATE_PROXY.strip()
    if builtin and builtin != proxy.strip():
        candidates.append((builtin, True))
    return candidates


def _run_with_proxy_fallback(operation, proxy: str = "", fallback_notice=None):
    last_error: Exception | None = None
    candidates = _proxy_candidates(proxy)
    for index, (candidate, is_builtin) in enumerate(candidates):
        if is_builtin and fallback_notice:
            fallback_notice()
        try:
            return operation(candidate, is_builtin)
        except Exception as exc:
            last_error = exc
            if index + 1 >= len(candidates):
                raise
    raise RuntimeError(f"更新请求失败：{last_error}")


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
    """下载并校验新版 EXE，返回临时文件路径。"""
    target = Path(tempfile.gettempdir()) / f"{APP_NAME}-{release['version']}.exe.download"
    def request(candidate: str, _is_builtin: bool):
        target.unlink(missing_ok=True)
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
                with target.open("wb") as stream:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            stream.write(chunk)
                            digest.update(chunk)
                            downloaded += len(chunk)
                            if progress:
                                progress(downloaded, total)

        if digest.hexdigest().lower() != expected:
            target.unlink(missing_ok=True)
            raise RuntimeError("新版 EXE 的 SHA256 校验失败，已取消更新")
        return target

    return _run_with_proxy_fallback(request, proxy, fallback_notice)


def can_self_update() -> bool:
    return bool(getattr(sys, "frozen", False)) and sys.platform == "win32"


def install_and_restart(downloaded_exe: Path) -> None:
    """退出当前程序后替换 EXE 并自动重启。"""
    if not can_self_update():
        raise RuntimeError("源码运行模式不能自动替换文件，请下载 Release 版本")

    current_exe = Path(sys.executable).resolve()
    script_path = Path(tempfile.gettempdir()) / f"{APP_NAME}-update-{os.getpid()}.ps1"

    def ps_quote(value: str) -> str:
        return value.replace("'", "''")

    script = f"""$ErrorActionPreference = 'Stop'
$Source = '{ps_quote(str(downloaded_exe.resolve()))}'
$Target = '{ps_quote(str(current_exe))}'
$Working = '{ps_quote(str(current_exe.parent))}'
$WaitPid = {os.getpid()}
for ($i = 0; $i -lt 120; $i++) {{
    $Running = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {{
        try {{ $_.Path -eq $Target }} catch {{ $false }}
    }})
    if ($Running.Count -eq 0) {{ break }}
    Start-Sleep -Seconds 1
}}
for ($i = 0; $i -lt 60; $i++) {{
    try {{
        Copy-Item -LiteralPath $Source -Destination ($Target + '.new') -Force
        Move-Item -LiteralPath ($Target + '.new') -Destination $Target -Force
        # 给杀毒软件和文件系统留出完成扫描、刷新文件句柄的时间。
        Start-Sleep -Seconds 5
        Start-Process -FilePath $Target -WorkingDirectory $Working
        Remove-Item -LiteralPath $Source -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
        exit 0
    }} catch {{
        Start-Sleep -Seconds 1
    }}
}}
exit 1
"""
    script_path.write_text(script, encoding="utf-8-sig")
    subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
        ],
        creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )
