"""
模擬器重啟（主要支援 MuMu Player 12 的 MuMuManager.exe，備援 adb reboot）。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time

from settings import _load, get_mumu_config, reload as reload_settings
from utils import log, flow_log

# MuMu 12 常見安裝路徑
MUMU_MANAGER_CANDIDATES = [
    r"C:\Program Files\Netease\MuMuPlayer-12.0\shell\MuMuManager.exe",
    r"C:\Program Files\Netease\MuMu Player 12\shell\MuMuManager.exe",
    r"C:\Program Files\Netease\MuMuPlayer\nx_main\MuMuManager.exe",
    r"D:\Program Files\Netease\MuMuPlayer-12.0\shell\MuMuManager.exe",
]


def find_mumu_manager_path() -> str | None:
    """尋找 MuMuManager.exe（設定路徑優先，其次常見安裝目錄）。"""
    cfg = get_mumu_config()
    custom = str(cfg.get("manager_path") or "").strip().strip('"')
    if custom and os.path.isfile(custom):
        return custom
    for path in MUMU_MANAGER_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def _parse_adb_endpoint(device_id: str) -> tuple[str, int] | None:
    text = str(device_id or "").strip()
    if ":" not in text:
        return None
    host, port_s = text.rsplit(":", 1)
    try:
        return host.strip() or "127.0.0.1", int(port_s.strip())
    except ValueError:
        return None


def _run_manager(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    exe = find_mumu_manager_path()
    if not exe:
        raise FileNotFoundError("找不到 MuMuManager.exe，請在 settings.json 的 mumu.manager_path 填入完整路徑")
    return subprocess.run(
        [exe, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=os.path.dirname(exe),
    )


def _extract_json_blob(text: str):
    """從 MuMuManager 輸出擷取 JSON（可能混雜 log）。"""
    text = (text or "").strip()
    if not text:
        return None
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start >= 0 and end > start:
            chunk = text[start : end + 1]
            try:
                return json.loads(chunk)
            except json.JSONDecodeError:
                pass
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _normalize_info_list(data) -> list[dict]:
    """將 info -v all 回傳整理成 list[dict]。"""
    if data is None:
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        if all(isinstance(v, dict) for v in data.values()):
            return list(data.values())
        if "index" in data or "adb_port" in data:
            return [data]
    return []


def fetch_mumu_instances() -> list[dict]:
    """查詢所有 MuMu 模擬器資訊（需已安裝 MuMuManager）。"""
    r = _run_manager(["info", "-v", "all"], timeout=60)
    data = _extract_json_blob(r.stdout) or _extract_json_blob(r.stderr)
    items = _normalize_info_list(data)
    if not items and (r.stdout or r.stderr):
        log(f"[模擬器] MuMuManager info 輸出: {(r.stdout or r.stderr)[:300]}")
    return items


def resolve_mumu_vm_index(device_id: str, emulator_key: str | None = None) -> str | None:
    """
    依 settings 或 adb 埠解析 MuMu 模擬器索引（--vmindex）。
    emulator_key: settings.emulators 的 key，如 "1"、"2"。
    """
    reload_settings()
    settings = _load()
    emulators = settings.get("emulators") or {}

    if emulator_key and emulator_key in emulators:
        idx = emulators[emulator_key].get("mumu_vm_index")
        if idx is not None and str(idx).strip() != "":
            return str(idx).strip()

    endpoint = _parse_adb_endpoint(device_id)
    if not endpoint:
        return None
    _, port = endpoint

    try:
        for row in fetch_mumu_instances():
            adb_port = row.get("adb_port")
            if adb_port is None:
                continue
            if int(adb_port) == int(port):
                return str(row.get("index", "")).strip() or None
    except FileNotFoundError:
        return None
    except Exception as exc:
        log(f"[模擬器] 解析 vmindex 失敗: {exc}")

    # 常見 MuMu12 對應：16384→0、16416→1（僅在無法查 info 時當猜測）
    if port in (16384, 16385):
        return "0"
    if port in (16416, 16417):
        return "1"
    return None


def _wait_adb_device(device_id: str, timeout_sec: float) -> bool:
    """等待 adb 設備重新上線且 boot completed。"""
    deadline = time.time() + float(timeout_sec)
    host_port = str(device_id or "").strip()
    while time.time() < deadline:
        if ":" in host_port:
            subprocess.run(["adb", "connect", host_port], capture_output=True, text=True, timeout=15)
        r = subprocess.run(
            ["adb", "-s", host_port, "get-state"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if (r.stdout or "").strip() == "device":
            br = subprocess.run(
                ["adb", "-s", host_port, "shell", "getprop", "sys.boot_completed"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if (br.stdout or "").strip() == "1":
                log(f"[模擬器] ADB 已就緒: {host_port}")
                return True
        time.sleep(3)
    log(f"[模擬器] 等待 ADB 逾時: {host_port}")
    return False


def restart_emulator_mumu(device_id: str, emulator_key: str | None = None) -> bool:
    """透過 MuMuManager 重啟整台模擬器（非僅遊戲）。"""
    vmindex = resolve_mumu_vm_index(device_id, emulator_key)
    if not vmindex:
        log("[模擬器] 無法解析 MuMu vmindex，請在 settings.emulators.N.mumu_vm_index 手動設定")
        return False

    cfg = get_mumu_config()
    wait_sec = float(cfg.get("restart_wait_sec", 90))
    boot_timeout = float(cfg.get("adb_boot_timeout_sec", 120))

    log(f"[模擬器] MuMuManager 重啟模擬器 vmindex={vmindex}（裝置 {device_id}）")
    flow_log("模擬器", "重啟", f"MuMu restart -v {vmindex}", status="...")

    try:
        r = _run_manager(["control", "-v", vmindex, "restart"], timeout=180)
    except FileNotFoundError as exc:
        log(f"[模擬器] {exc}")
        return False

    out = f"{r.stdout or ''}\n{r.stderr or ''}".strip()
    if out:
        log(f"[模擬器] MuMuManager: {out[:400]}")

    if r.returncode != 0:
        flow_log("模擬器", "重啟", f"MuMuManager 失敗 code={r.returncode}", status="FAIL")
        return False

    log(f"[模擬器] 等待模擬器重啟完成（約 {wait_sec:.0f}s）...")
    time.sleep(wait_sec)

    if _wait_adb_device(device_id, boot_timeout):
        flow_log("模擬器", "重啟", "模擬器已重啟且 ADB 就緒", status="OK")
        return True

    flow_log("模擬器", "重啟", "模擬器重啟後 ADB 未就緒", status="FAIL")
    return False


def restart_emulator_adb_reboot(device_id: str) -> bool:
    """備援：僅重啟模擬器內 Android（adb reboot），非關閉 MuMu 視窗。"""
    host_port = str(device_id or "").strip()
    if not host_port:
        return False
    log(f"[模擬器] adb reboot（{host_port}）...")
    flow_log("模擬器", "重啟", "adb reboot", status="...")
    if ":" in host_port:
        subprocess.run(["adb", "connect", host_port], capture_output=True, text=True, timeout=15)
    r = subprocess.run(
        ["adb", "-s", host_port, "reboot"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if r.returncode != 0:
        log(f"[模擬器] adb reboot 失敗: {(r.stderr or r.stdout or '').strip()}")
        return False
    cfg = get_mumu_config()
    boot_timeout = float(cfg.get("adb_boot_timeout_sec", 120))
    time.sleep(5)
    ok = _wait_adb_device(host_port, boot_timeout)
    flow_log("模擬器", "重啟", "adb reboot 完成" if ok else "adb reboot 後未就緒", status="OK" if ok else "FAIL")
    return ok


def restart_emulator(
    device_id: str,
    emulator_key: str | None = None,
    *,
    prefer_mumu: bool = True,
) -> bool:
    """
    重啟模擬器：優先 MuMuManager control restart，失敗則 adb reboot。
    """
    if prefer_mumu and find_mumu_manager_path():
        if restart_emulator_mumu(device_id, emulator_key):
            return True
        log("[模擬器] MuMuManager 重啟失敗，改試 adb reboot...")
    return restart_emulator_adb_reboot(device_id)
