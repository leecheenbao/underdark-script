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


def get_cert_config():
    """取得憑證配置 {"hash": "...", "pem_path": "..."}"""
    return _load()["cert"]


def get_telegram_config():
    """Telegram 日誌：{"enabled": bool, "bot_token": str, "chat_id": str}"""
    return _load()["telegram"]
