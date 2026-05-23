"""
公共配置加载模块
从 settings.json 读取模拟器、代理、游戏等配置
"""
import json
import os

_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

# 內存快取，避免每次函數呼叫都重複讀取磁碟
_cache: dict | None = None


def _load():
    """載入 settings.json（utf-8-sig 可相容 Windows 記事本 BOM），結果快取於記憶體"""
    global _cache
    if _cache is None:
        with open(_SETTINGS_PATH, encoding="utf-8-sig") as f:
            _cache = json.load(f)
    return _cache


def reload():
    """強制重新讀取 settings.json（設定有異動時呼叫）"""
    global _cache
    _cache = None
    return _load()


def get_emulator_ports():
    """取得模擬器端口映射 {"1": "127.0.0.1:16384", ...}"""
    settings = _load()
    return {k: v["id"] for k, v in settings["emulators"].items()}


def get_device_id(key="1"):
    """取得指定模擬器的設備ID"""
    return get_emulator_ports().get(str(key))


def get_default_device():
    """取得預設模擬器設備ID"""
    settings = _load()
    default_key = settings.get("default_emulator", "1")
    return settings["emulators"][default_key]["id"]


def get_all_devices(max_count: int | None = None):
    """取得所有已設定設備（依 key 由小到大），回傳 [{'key','id','name'}]。"""
    settings = _load()
    emulators = settings.get("emulators", {})
    keys = sorted(emulators.keys(), key=lambda x: int(str(x)) if str(x).isdigit() else str(x))
    items = []
    for key in keys:
        row = emulators.get(key) or {}
        dev_id = str(row.get("id", "")).strip()
        if not dev_id:
            continue
        items.append({
            "key": str(key),
            "id": dev_id,
            "name": str(row.get("name") or f"模擬器{key}"),
        })
    if max_count is not None:
        return items[:max(0, int(max_count))]
    return items


def get_run_device_ids(max_count: int = 2):
    """取得要執行的設備 ID 清單（預設最多 2 台）。"""
    return [x["id"] for x in get_all_devices(max_count=max_count)]


def get_proxy():
    """取得代理配置 {"host": "10.0.2.2", "port": 8080}"""
    return _load()["proxy"]


def get_game_package():
    """取得遊戲套件名稱"""
    return _load()["game"]["package"]


def get_game_activity():
    """取得遊戲啟動 Activity（可為空，由 adb monkey / MainActivity 處理）"""
    game = _load().get("game") or {}
    return str(game.get("activity") or "").strip() or None


def get_mumu_config() -> dict:
    """MuMu 模擬器控制設定（MuMuManager 路徑、重啟等待時間等）。"""
    defaults = {
        "manager_path": "",
        "restart_wait_sec": 90,
        "adb_boot_timeout_sec": 120,
    }
    cfg = _load().get("mumu") or {}
    return {**defaults, **cfg}


def get_cert_config():
    """取得憑證配置 {"hash": "...", "pem_path": "..."}"""
    return _load()["cert"]


def get_telegram_config():
    """Telegram 日誌：{"enabled": bool, "bot_token": str, "chat_id": str}"""
    return _load()["telegram"]


def get_features() -> dict:
    """取得功能開關設定 {"pet_full_check": bool, ...}"""
    return _load().get("features", {})


def get_pet_full_check() -> bool:
    """道具欄滿包偵測開關（預設 True）"""
    return bool(get_features().get("pet_full_check", True))


def get_auto_recovery() -> dict:
    """連續失敗時自動恢復（重啟遊戲／Web 自動重啟腳本）設定。"""
    defaults = {
        "enabled": True,
        "max_consecutive_fail": 3,
        "max_recovery_per_session": 0,  # 0=不限制本場次恢復次數
        "game_ready_timeout_sec": 180,
        "web_auto_restart": True,
        "web_restart_delay_sec": 8,
        # emulator=adb 強制結束重開；sl_ui=僅遊戲內設定離開；emulator_then_sl=先模擬器再 SL
        "restart_mode": "emulator",
        "force_stop_wait_sec": 5,
        "force_stop_retries": 3,
        "launch_wait_sec": 15,
        "use_game_icon_fallback": True,
        "prefer_launch_via_icon": True,
        "restart_script_after_recovery": True,
        "restart_script_after_test": True,
        "restart_emulator_on_recovery": True,
        "restart_emulator_after_game_restart_fail": True,
        "desktop_wait_after_reboot_sec": 10,
    }
    cfg = _load().get("auto_recovery") or {}
    return {**defaults, **cfg}


def set_pet_full_check(enabled: bool):
    """寫入道具欄滿包偵測開關至 settings.json"""
    global _cache
    with open(_SETTINGS_PATH, encoding="utf-8-sig") as f:
        cfg = json.load(f)
    cfg.setdefault("features", {})["pet_full_check"] = bool(enabled)
    with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    _cache = None  # 清除快取，下次讀取會重新載入


def set_telegram_config(
    *,
    enabled: bool | None = None,
    bot_token: str | None = None,
    chat_id: str | None = None,
    update_token_if_empty: bool = False,
) -> dict:
    """
    寫入 Telegram 設定至 settings.json。
    bot_token 為空字串時：預設不覆寫既有 token（避免儲存時清空）；update_token_if_empty=True 則允許清空。
    """
    global _cache
    with open(_SETTINGS_PATH, encoding="utf-8-sig") as f:
        cfg = json.load(f)
    tg = cfg.setdefault("telegram", {})
    if enabled is not None:
        tg["enabled"] = bool(enabled)
    if bot_token is not None:
        text = str(bot_token).strip()
        if text or update_token_if_empty:
            tg["bot_token"] = text
    if chat_id is not None:
        tg["chat_id"] = str(chat_id).strip()
    with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    _cache = None
    return dict(tg)
