"""
UnderGuild 自動化工具類
包含所有通用的自動化方法
純 adb 實作，不使用 uiautomator2
"""
import cv2
import json
import ssl
import sys
import time
import os
import zlib

# Windows CP950/GBK 終端機無法印出 BMP 以外的 Unicode 字元（如 ≥ ≦ … 等）
# 在模組載入時強制將 stdout/stderr 切換為 UTF-8，讓所有 log 不再崩潰
if sys.platform == "win32":
    os.environ.setdefault("PYTHONUTF8", "1")
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import gc
import queue
import subprocess
import threading
import numpy as np
from datetime import datetime

# 圖片目錄
PIC_DIR = os.path.join(os.path.dirname(__file__), "../pic")
CACHE_DIR = os.path.join(PIC_DIR, "cache")

# 確保目錄存在
os.makedirs(CACHE_DIR, exist_ok=True)

# 模板匹配閾值容差：OpenCV 回傳的相關係數可能是 0.69999，印成兩位小數卻顯示 0.90，導致「看起來等於閾值卻點不到」
MATCH_THRESHOLD_EPS = 1e-5

# 全域設備 ID
device_id = None

# 微信推送設定 (PushPlus: http://www.pushplus.plus/ 取得)
PUSHPLUS_TOKEN = ""  # 填入你的 Token
ENABLE_WECHAT = True  # 是否發送微信通知

# Telegram 推送設定（https://t.me/BotFather 取得 token；亦可於 settings.json 的 telegram 區塊填寫）
TELEGRAM_BOT_TOKEN = "8679880392:AAHFMSjrsVPRtCiL1Xbi8lMR4fph_KyLY6g"  # 填入你的 Bot Token
TELEGRAM_CHAT_ID = "-1003118224439"  # 填入 Chat ID
ENABLE_TELEGRAM = True  # 是否發送 Telegram 通知（含 log 轉發）
# 是否將每則 log() 同步轉發至 Telegram（關閉後僅 send_telegram 會發送）
TELEGRAM_FORWARD_LOG = True

# HTTPS 憑證驗證：若出現 CERTIFICATE_VERIFY_FAILED（mitmproxy/公司代理自簽憑證），可改 False 或設環境變數 TELEGRAM_SSL_VERIFY=0（僅建議本機除錯，有中間人風險）
TELEGRAM_VERIFY_SSL = False

# Telegram：背景佇列合併發送，避免每行 log 都打 HTTP
_telegram_queue = queue.Queue()
_telegram_worker_started = False
_telegram_worker_lock = threading.Lock()


def _telegram_urlopen_kwargs(url, timeout):
    """Telegram API 專用：依設定決定是否跳過 SSL 憑證驗證。"""
    kw = {"timeout": timeout}
    if not url.lower().startswith("https:") or "api.telegram.org" not in url.lower():
        return kw
    verify = TELEGRAM_VERIFY_SSL
    env = (os.environ.get("TELEGRAM_SSL_VERIFY") or "").strip().lower()
    if env in ("0", "false", "no", "off"):
        verify = False
    elif env in ("1", "true", "yes", "on"):
        verify = True
    if not verify:
        kw["context"] = ssl._create_unverified_context()
    return kw


def _http_post_json(url, payload, timeout=15):
    """POST application/json，僅使用標準庫（不需安裝 requests）。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urlopen(req, **_telegram_urlopen_kwargs(url, timeout)) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.getcode(), raw
    except HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return e.code, raw
    except URLError as e:
        reason = getattr(e.reason, "strerror", None) or str(e.reason)
        raise OSError(reason) from e


def _get_telegram_config():
    """
    合併本檔常數、settings.get_telegram_config()、環境變數 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID。
    常數為空時由 settings.json 補 token/chat_id；enabled 為 ENABLE_TELEGRAM 且（可讀取 settings 時）telegram.enabled。
    """
    tok = (TELEGRAM_BOT_TOKEN or "").strip()
    cid = str(TELEGRAM_CHAT_ID or "").strip()
    js_enabled = True
    try:
        from settings import get_telegram_config

        m = get_telegram_config()
        if not tok:
            tok = (m.get("bot_token") or m.get("botToken") or "").strip()
        if not cid:
            x = m.get("chat_id")
            if x is None:
                x = m.get("chatId")
            cid = str(x if x is not None else "").strip()
        js_enabled = bool(m.get("enabled", True))
    except Exception:
        pass

    env_tok = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    env_cid = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if env_tok:
        tok = env_tok
    if env_cid:
        cid = env_cid

    enabled = bool(ENABLE_TELEGRAM) and js_enabled
    return {"enabled": enabled, "bot_token": tok, "chat_id": cid}


def _telegram_worker_loop():
    """背景執行緒：聚合約 1.2 秒內的 log 再 sendMessage"""
    while True:
        first = _telegram_queue.get()
        lines = [first]
        batch_until = time.time() + 1.2
        while time.time() < batch_until:
            try:
                while True:
                    lines.append(_telegram_queue.get_nowait())
            except queue.Empty:
                time.sleep(0.03)

        cfg = _get_telegram_config()
        if not cfg.get("enabled") or not cfg.get("bot_token") or not cfg.get("chat_id"):
            continue

        text = "\n".join(lines)
        if len(text) > 4096:
            text = text[:4000] + "\n...(截斷)"

        url = f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage"
        try:
            _http_post_json(
                url,
                {"chat_id": cfg["chat_id"], "text": text},
                timeout=15,
            )
        except Exception:
            pass


def set_telegram_log_forward(enabled: bool):
    """開關：log() 是否轉發到 Telegram（腳本可設 False，只保留 send_telegram）。"""
    global TELEGRAM_FORWARD_LOG
    TELEGRAM_FORWARD_LOG = bool(enabled)


def _telegram_enqueue_line(line: str):
    cfg = _get_telegram_config()
    if not cfg.get("enabled") or not cfg.get("bot_token") or not cfg.get("chat_id"):
        return

    global _telegram_worker_started
    with _telegram_worker_lock:
        if not _telegram_worker_started:
            threading.Thread(target=_telegram_worker_loop, daemon=True).start()
            _telegram_worker_started = True
    _telegram_queue.put(line)


def log(msg, telegram=None):
    """
    帶時間戳之日誌輸出。
    telegram: None=依 TELEGRAM_FORWARD_LOG；True=強制轉發；False=不轉發。
    """
    timestamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] {msg}"
    # Windows CP950/GBK 終端機遇到 BMP 以外字元會拋出 UnicodeEncodeError，加上 errors='replace' 避免崩潰
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode(sys.stdout.encoding or "utf-8", errors="replace")
                  .decode(sys.stdout.encoding or "utf-8", errors="replace"))
    if telegram is False:
        return
    if telegram is True:
        _telegram_enqueue_line(line)
        return
    if TELEGRAM_FORWARD_LOG:
        _telegram_enqueue_line(line)


def send_telegram(text: str) -> bool:
    """
    透過 Telegram Bot API 發送訊息
    text: 內容（最長約 4096 字元）；送出前會自動加上 [YYYY-MM-DD HH:MM:SS] 前綴
    傳回: True=成功, False=失敗
    """
    cfg = _get_telegram_config()
    # 與本機 log 區隔：TG 使用完整日期時間
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = f"[{ts}] {text or ''}".strip()
    if not cfg.get("enabled"):
        log("[Telegram] 已停用，略過通知")
        return False
    if not cfg.get("bot_token") or not cfg.get("chat_id"):
        log(
            "[警告] 未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID（或於 settings.json 的 telegram 區塊填寫），略過 Telegram 通知"
        )
        return False

    body = (text or "")[:4096]
    url = f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage"
    try:
        code, raw = _http_post_json(
            url,
            {"chat_id": cfg["chat_id"], "text": body},
            timeout=15,
        )
        ok = code == 200
        if not ok:
            log(f"[Telegram] HTTP {code}: {raw[:200]}")
        return ok
    except Exception as e:
        log(f"[Telegram] 傳送異常: {e}")
        return False


def connect_device(dev_id):
    """連接設備（純 adb 驗證）"""
    global device_id
    
    # 驗證設備是否可用
    result = subprocess.run(
        ["adb", "-s", dev_id, "get-state"],
        capture_output=True,
        text=True
    )
    
    # 去除空白與換行後檢查
    state = result.stdout.strip()
    if result.returncode == 0 and state == "device":
        device_id = dev_id
        log(f"設備已連接: {dev_id}")
        return dev_id
    else:
        log(f"設備狀態: returncode={result.returncode}, stdout='{state}', stderr='{result.stderr.strip()}'")
        raise Exception(f"設備連線失敗: {dev_id}")


def get_device_id():
    """取得目前設備 ID"""
    global device_id
    if device_id is None:
        raise Exception("設備未連線，請先呼叫 connect_device()")
    return device_id


def _atomic_write_bytes(target_path, data):
    """先寫暫存檔再原子替換，避免讀到半寫入的 PNG。"""
    folder = os.path.dirname(target_path) or "."
    os.makedirs(folder, exist_ok=True)
    tmp_path = os.path.join(
        folder, f".{os.path.basename(target_path)}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with open(tmp_path, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        last_err = None
        for _ in range(6):
            try:
                os.replace(tmp_path, target_path)
                last_err = None
                break
            except PermissionError as ex:
                # Windows 上若目標檔被短暫占用（防毒/索引/其他讀取）會失敗，短暫退避後重試
                last_err = ex
                time.sleep(0.02)
        if last_err is not None:
            # 最後退回直接覆寫，避免整個流程因瞬時鎖檔中斷
            with open(target_path, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def _is_valid_png_bytes(data):
    """完整檢查 PNG chunk 長度/邊界/CRC，避免損壞圖交給 libpng。"""
    if not data or len(data) < 33:
        return False
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False

    pos = 8
    seen_iend = False
    data_len = len(data)
    while pos + 12 <= data_len:
        # length(4) + type(4) + data(length) + crc(4)
        length = int.from_bytes(data[pos:pos + 4], "big", signed=False)
        ctype = data[pos + 4:pos + 8]
        chunk_start = pos + 8
        chunk_end = chunk_start + length
        crc_end = chunk_end + 4
        if chunk_end > data_len or crc_end > data_len:
            return False
        payload = data[chunk_start:chunk_end]
        crc_expected = int.from_bytes(data[chunk_end:crc_end], "big", signed=False)
        crc_actual = zlib.crc32(ctype)
        crc_actual = zlib.crc32(payload, crc_actual) & 0xFFFFFFFF
        if crc_actual != crc_expected:
            return False
        pos = crc_end
        if ctype == b"IEND":
            seen_iend = True
            break

    # IEND 必須存在，且不得有多餘尾巴
    return seen_iend and pos == data_len


def _decode_png_bytes_safe(data):
    """僅解碼已驗證通過的 PNG bytes，避免 libpng 報錯刷屏。"""
    if not _is_valid_png_bytes(data):
        return None
    arr = np.frombuffer(data, np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _imread_with_retry(path, retries=3, delay=0.04):
    """先驗證 PNG 完整性再解碼，降低讀到半寫入檔時的 libpng 錯誤。"""
    for i in range(max(1, int(retries))):
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except Exception:
            raw = b""
        img = _decode_png_bytes_safe(raw)
        if img is not None:
            return img
        if i < retries - 1:
            time.sleep(delay)
    return None


def take_screenshot(save_path=None):
    """截取螢幕（純 adb 方式）
    使用 exec-out 直接 pipe，省去 push/pull/rm 三次往返，速度提升約 2-3 倍
    """
    global device_id
    if save_path is None:
        save_path = os.path.join(CACHE_DIR, "screen.png")

    for attempt in range(3):
        result = subprocess.run(
            ["adb", "-s", device_id, "exec-out", "screencap", "-p"],
            capture_output=True,
        )
        if result.returncode == 0 and result.stdout:
            png_bytes = result.stdout
            if not _is_valid_png_bytes(png_bytes):
                log(f"[截圖] exec-out PNG 檢查失敗，重試 {attempt + 1}/3")
                time.sleep(0.05)
                continue
            # 僅對已驗證通過的 PNG 進行解碼確認
            if _decode_png_bytes_safe(png_bytes) is None:
                log(f"[截圖] exec-out PNG 解碼失敗，重試 {attempt + 1}/3")
                time.sleep(0.05)
                continue
            _atomic_write_bytes(save_path, png_bytes)
            return save_path

    # fallback：舊版 adb 不支援 exec-out 時退回三步驟
    remote_path = "/data/local/tmp/screen.png"
    subprocess.run(
        ["adb", "-s", device_id, "shell", "screencap", "-p", remote_path],
        capture_output=True,
    )
    pull = subprocess.run(
        ["adb", "-s", device_id, "pull", remote_path, save_path],
        capture_output=True,
    )
    subprocess.run(
        ["adb", "-s", device_id, "shell", "rm", remote_path],
        capture_output=True,
    )
    # fallback 檔案也做一次讀取驗證，避免後續流程反覆報 libpng 錯誤
    if pull.returncode == 0 and _imread_with_retry(save_path, retries=2) is not None:
        return save_path
    raise RuntimeError("截圖失敗：adb screencap 回傳無效 PNG")


def load_and_match(template_path, threshold=0.8, screen_path=None, multiscale=False):
    """
    模板比對
    multiscale: True 時對模板做多種縮放再比對（緩解解析度/DPI 與截圖模板不一致）
    傳回: (is_found, click_x, click_y, similarity)
    """
    if screen_path is None:
        screen_path = take_screenshot()
    
    # 處理相對路徑
    if not os.path.isabs(template_path):
        template_path = os.path.join(PIC_DIR, template_path)
    
    img = _imread_with_retry(screen_path, retries=3)
    template = _imread_with_retry(template_path, retries=2)
    
    if img is None or template is None:
        return False, None, None, 0.0
    
    img_h, img_w = img.shape[:2]
    max_val = 0.0
    max_loc = (0, 0)
    tw = th = 0

    if multiscale:
        th0, tw0 = template.shape[:2]
        best_val = -1.0
        best_loc = (0, 0)
        best_tw, best_th = 8, 8
        # 從 17 步縮為 9 步，覆蓋關鍵縮放點，CPU 耗時減少約 47%
        for scale in np.linspace(0.7, 1.3, 9):
            rw = max(8, int(round(tw0 * scale)))
            rh = max(8, int(round(th0 * scale)))
            if rw >= img_w or rh >= img_h:
                continue
            interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
            tmpl = cv2.resize(template, (rw, rh), interpolation=interp)
            res = cv2.matchTemplate(img, tmpl, cv2.TM_CCOEFF_NORMED)
            _, mv, _, ml = cv2.minMaxLoc(res)
            if mv > best_val:
                best_val, best_loc, best_tw, best_th = mv, ml, rw, rh
            del tmpl, res
        if best_val < 0:
            del img, template
            gc.collect()
            return False, None, None, 0.0
        max_val, max_loc, tw, th = best_val, best_loc, best_tw, best_th
    else:
        th, tw = template.shape[:2]
        if tw >= img_w or th >= img_h:
            del img, template
            gc.collect()
            return False, None, None, 0.0
        res = cv2.matchTemplate(img, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        del res

    # 使用微小容差，避免浮點邊界與「日誌四捨五入後看似已達標」的誤解
    if max_val + MATCH_THRESHOLD_EPS >= threshold:
        click_x = max_loc[0] + tw // 2
        click_y = max_loc[1] + th // 2
        result = (True, click_x, click_y, max_val)
    else:
        result = (False, None, None, max_val)

    del img, template
    gc.collect()
    return result


def load_and_match_all_peaks(
    template_path,
    threshold=0.75,
    screen_path=None,
    multiscale=False,
    max_peaks=12,
    nms_factor=0.42,
):
    """
    收集畫面上所有達閾值的模板中心 [(cx, cy, sim), ...]，由高到低；鄰近重複以 NMS 壓掉。
    multiscale=True 時先依「單點最高分」選縮放（與 load_and_match 一致），再於該縮放下取多峰。
    """
    if screen_path is None:
        screen_path = take_screenshot()
    if not os.path.isabs(template_path):
        template_path = os.path.join(PIC_DIR, template_path)
    img = _imread_with_retry(screen_path, retries=3)
    template = _imread_with_retry(template_path, retries=2)
    if img is None or template is None:
        return []

    img_h, img_w = img.shape[:2]
    final_tmpl = None
    tw = th = 0

    if multiscale:
        th0, tw0 = template.shape[:2]
        best_val = -1.0
        best_scale = None
        for scale in np.linspace(0.7, 1.3, 9):
            rw = max(8, int(round(tw0 * scale)))
            rh = max(8, int(round(th0 * scale)))
            if rw >= img_w or rh >= img_h:
                continue
            interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
            tmpl = cv2.resize(template, (rw, rh), interpolation=interp)
            res = cv2.matchTemplate(img, tmpl, cv2.TM_CCOEFF_NORMED)
            _, mv, _, _ = cv2.minMaxLoc(res)
            if mv > best_val:
                best_val = mv
                best_scale = scale
            del tmpl, res
        if best_scale is None or best_val < 0:
            del img, template
            gc.collect()
            return []
        rw = max(8, int(round(tw0 * best_scale)))
        rh = max(8, int(round(th0 * best_scale)))
        interp = cv2.INTER_AREA if best_scale < 1.0 else cv2.INTER_LINEAR
        final_tmpl = cv2.resize(template, (rw, rh), interpolation=interp)
        tw, th = rw, rh
    else:
        th, tw = template.shape[:2]
        if tw >= img_w or th >= img_h:
            del img, template
            gc.collect()
            return []
        final_tmpl = template

    res = cv2.matchTemplate(img, final_tmpl, cv2.TM_CCOEFF_NORMED)
    work = res.copy()
    mx_dist = max(int(tw * nms_factor), 8)
    my_dist = max(int(th * nms_factor), 8)
    peaks: list[tuple[int, int, float]] = []
    for _ in range(max_peaks):
        _, max_val, _, max_loc = cv2.minMaxLoc(work)
        if max_val + MATCH_THRESHOLD_EPS < threshold:
            break
        cx = int(max_loc[0] + tw // 2)
        cy = int(max_loc[1] + th // 2)
        peaks.append((cx, cy, float(max_val)))
        x0 = max(0, max_loc[0] - mx_dist)
        y0 = max(0, max_loc[1] - my_dist)
        x1 = min(work.shape[1], max_loc[0] + tw + mx_dist)
        y1 = min(work.shape[0], max_loc[1] + th + my_dist)
        work[y0:y1, x0:x1] = 0.0

    del img, template, res, work
    if multiscale:
        del final_tmpl
    gc.collect()
    return peaks


def click_by_image(img, threshold=0.8, delay=0.5, name="", show_log=True, multiscale=False):
    """
    圖像辨識點擊（純 adb）
    img: 圖片路徑（相對於 pic 目錄或絕對路徑）
    threshold: 相似度閾值
    delay: 點擊後延遲
    name: 按鈕名稱（用於日誌）
    multiscale: 是否多尺度匹配模板
    傳回: True=找到並點擊, False=未找到
    """
    global device_id
    found, x, y, sim = load_and_match(img, threshold, multiscale=multiscale)
    
    if found:
        # 純 adb 點擊
        subprocess.run(
            ["adb", "-s", device_id, "shell", "input", "tap", str(x), str(y)],
            capture_output=True
        )
        if show_log:
            log(f"點擊 {name or img}，座標: ({x}, {y}), 相似度: {sim:.2f}")
        time.sleep(delay)
        return True
    else:
        if show_log:
            log(f"未找到 {name or img}，相似度: {sim:.2f}")
        return False


def image_exists(img, threshold=0.8):
    """檢查圖片是否存在於螢幕上"""
    found, _, _, _ = load_and_match(img, threshold)
    return found


def wait_for_image(img, timeout=10, interval=0.5, threshold=0.8, multiscale=False):
    """
    等待圖片出現
    傳回: True=找到, False=逾時
    multiscale: 與 load_and_match 一致，建議與同畫面其他按鈕的 wait_and_click 一致
    """
    start = time.time()
    while time.time() - start < timeout:
        found, _, _, _ = load_and_match(img, threshold, multiscale=multiscale)
        if found:
            return True
        time.sleep(interval)
    return False


def click_fixed(x, y, delay=0.5, name=""):
    """固定座標點擊（純 adb）"""
    global device_id
    subprocess.run(
        ["adb", "-s", device_id, "shell", "input", "tap", str(x), str(y)],
        capture_output=True
    )
    log(f"點擊 {name}，座標: ({x}, {y})")
    time.sleep(delay)


def wait_and_click(
    img,
    timeout=10,
    interval=0.5,
    threshold=0.8,
    delay=0.5,
    name="",
    multiscale=False,
    heartbeat_sec=0,
):
    """
    等待圖片出現並點擊
    img: 圖片路徑
    timeout: 逾時時間（0=無限等待）
    interval: 檢查間隔
    heartbeat_sec: 每隔多少秒打一次相似度日誌（0=關閉），避免長時間無輸出
    傳回: True=找到並點擊, False=逾時
    """
    global device_id
    log(f"等待 {name or img}...")
    start = time.time()
    next_heartbeat = start + heartbeat_sec if heartbeat_sec > 0 else None

    while True:
        # 每輪只截圖匹配一次：心跳與是否點擊共用同一結果，避免「心跳顯示夠高、下一幀點擊卻失敗」
        found, tap_x, tap_y, sim = load_and_match(img, threshold, multiscale=multiscale)

        if heartbeat_sec > 0 and next_heartbeat is not None and time.time() >= next_heartbeat:
            log(
                f"仍在等待 {name or img}... 目前最高相似度: {sim:.4f}（閾值 {threshold}，需>=閾值才點擊）"
            )
            next_heartbeat = time.time() + heartbeat_sec

        if found and tap_x is not None and tap_y is not None:
            subprocess.run(
                ["adb", "-s", device_id, "shell", "input", "tap", str(tap_x), str(tap_y)],
                capture_output=True,
            )
            log(f"點擊 {name or img} 成功，相似度: {sim:.4f}")
            time.sleep(delay)
            return True

        # timeout=0 時無限等待
        if timeout > 0 and time.time() - start >= timeout:
            log(f"等待 {name or img} 逾時（最後相似度: {sim:.4f}）")
            return False

        time.sleep(interval)


def wait_and_click_any(
    img_list,
    timeout=10,
    interval=0.5,
    threshold=0.8,
    delay=0.5,
    name="",
    labels=None,
    multiscale=False,
    heartbeat_sec=0,
):
    """
    同一張截圖上依序嘗試多個模板，任一達閾值即點擊（適用簡繁不同文案的同一按鈕）
    img_list: 模板檔名列表（相對 pic）
    labels: 與 img_list 對應的日誌標籤，長度不足時回退為檔名
    """
    global device_id
    if not img_list:
        log(f"[錯誤] wait_and_click_any: img_list 為空")
        return False
    if labels is None:
        labels = list(img_list)
    log(f"等待 {name}...")
    start = time.time()
    next_heartbeat = start + heartbeat_sec if heartbeat_sec > 0 else None

    while True:
        screen_path = take_screenshot()
        best_sim = -1.0
        best_label = labels[0] if labels else ""
        chosen = None

        for idx, img in enumerate(img_list):
            found, tap_x, tap_y, sim = load_and_match(
                img, threshold, screen_path=screen_path, multiscale=multiscale
            )
            lab = labels[idx] if idx < len(labels) else img
            if sim > best_sim:
                best_sim = sim
                best_label = lab
            if found and tap_x is not None and tap_y is not None:
                chosen = (tap_x, tap_y, sim, lab)
                break

        if heartbeat_sec > 0 and next_heartbeat is not None and time.time() >= next_heartbeat:
            log(
                f"仍在等待 {name}... 本輪最高相似度: {best_sim:.4f}（{best_label}，閾值 {threshold}）"
            )
            next_heartbeat = time.time() + heartbeat_sec

        if chosen:
            tap_x, tap_y, sim, lab = chosen
            subprocess.run(
                ["adb", "-s", device_id, "shell", "input", "tap", str(tap_x), str(tap_y)],
                capture_output=True,
            )
            log(f"點擊 {name} 成功（{lab}），相似度: {sim:.4f}")
            time.sleep(delay)
            return True

        if timeout > 0 and time.time() - start >= timeout:
            log(f"等待 {name} 逾時（本輪最高相似度: {best_sim:.4f}）")
            return False

        time.sleep(interval)


def wait_for_disappear(
    img,
    timeout=30,
    interval=0.5,
    threshold=0.9,
    name="",
    multiscale=False,
    heartbeat_sec=0,
):
    """
    等待圖片消失
    timeout: 逾時時間（0=無限等待）
    heartbeat_sec: 仍顯示時每隔幾秒打一條日誌（0=關閉）
    傳回: True=已消失, False=逾時
    """
    log(f"等待 {name or img} 消失...")
    start = time.time()
    next_heartbeat = start + heartbeat_sec if heartbeat_sec > 0 else None

    while True:
        screen_path = take_screenshot()
        found, _, _, sim = load_and_match(
            img, threshold, screen_path=screen_path, multiscale=multiscale
        )

        if not found:
            log(f"{name or img} 已消失")
            return True

        if heartbeat_sec > 0 and next_heartbeat is not None and time.time() >= next_heartbeat:
            log(f"仍在等待 {name or img} 消失...（畫面上仍可匹配，相似度 {sim:.4f}）")
            next_heartbeat = time.time() + heartbeat_sec

        if timeout > 0 and time.time() - start >= timeout:
            log(f"等待 {name or img} 消失逾時（最後相似度: {sim:.4f}）")
            return False

        time.sleep(interval)


def wait_for_disappear_any(
    img_list,
    timeout=30,
    interval=0.5,
    threshold=0.9,
    name="",
    multiscale=False,
):
    """任一模板仍能被匹配則視為尚未消失（與 wait_and_click_any 成對使用）"""
    if not img_list:
        log(f"{name or '目標'} 已消失（無模板可偵測）")
        return True
    log(f"等待 {name} 消失...")
    start = time.time()

    while True:
        screen_path = take_screenshot()
        any_hit = False
        for img in img_list:
            found, _, _, _ = load_and_match(
                img, threshold, screen_path=screen_path, multiscale=multiscale
            )
            if found:
                any_hit = True
                break

        if not any_hit:
            log(f"{name} 已消失")
            return True

        if timeout > 0 and time.time() - start >= timeout:
            log(f"等待 {name} 消失逾時")
            return False

        time.sleep(interval)


def wait_for_loading_disappear(timeout=30, interval=0.5):
    """
    等待 loading.png 消失（專用方法）
    timeout: 逾時時間（秒，0=無限等待）
    interval: 檢查間隔（秒）
    傳回: True=已消失, False=逾時
    """
    return wait_for_disappear("loading.png", timeout=timeout, interval=interval, threshold=0.9, name="Loading")


def swipe(x1, y1, x2, y2, duration=0.5):
    """滑動（純 adb）"""
    global device_id
    duration_ms = int(duration * 1000)
    subprocess.run(
        ["adb", "-s", device_id, "shell", "input", "swipe", 
         str(x1), str(y1), str(x2), str(y2), str(duration_ms)],
        capture_output=True
    )


def clear_cache(async_mode=False):
    """清理快取檔案與記憶體
    async_mode: 是否非同步執行（不阻塞主執行緒）
    """
    import glob
    import threading
    
    def _do_clear():
        cache_files = glob.glob(os.path.join(CACHE_DIR, "*.png"))
        for f in cache_files:
            try:
                os.remove(f)
            except:
                pass
        gc.collect()
        log(f"已清理 {len(cache_files)} 個快取檔案")
    
    if async_mode:
        t = threading.Thread(target=_do_clear, daemon=True)
        t.start()
    else:
        _do_clear()


def force_stop_app(package_name):
    """強制停止應用程式（純 adb）"""
    global device_id
    subprocess.run(
        ["adb", "-s", device_id, "shell", "am", "force-stop", package_name],
        capture_output=True
    )
    log(f"已強制停止應用程式: {package_name}")


def start_app(package_name, activity_name=None):
    """啟動應用程式（純 adb）
    package_name: 套件名稱，如 com.example.game
    activity_name: Activity 完整類別名稱，如 com.google.firebase.MessagingUnityPlayerActivity
                   若為 None 則使用 {package_name}/.MainActivity
    """
    global device_id
    if activity_name is None:
        component = f"{package_name}/.MainActivity"
    else:
        # 若 activity_name 不含套件名，則拼接
        if "/" in activity_name or "." not in activity_name:
            component = f"{package_name}/{activity_name}"
        else:
            # 已是完整類別名稱
            component = f"{package_name}/{activity_name}"
    
    subprocess.run(
        ["adb", "-s", device_id, "shell", "am", "start", "-n", component],
        capture_output=True
    )
    log(f"已啟動應用程式: {package_name}")


def restart_app(package_name, activity_name=None, wait_time=3):
    """重啟應用程式（純 adb）
    wait_time: 停止後等待時間（秒）
    """
    log(f"正在重啟應用程式: {package_name}")
    force_stop_app(package_name)
    time.sleep(wait_time)
    start_app(package_name, activity_name)
    time.sleep(2)  # 等待應用程式啟動
    log("應用程式重啟完成")


def send_wechat(title, message=""):
    """
    透過 PushPlus 發送微信通知
    title: 標題（必填）
    message: 內容（選填，支援 Markdown/HTML）
    傳回: True=成功, False=失敗
    """
    if not ENABLE_WECHAT:
        log("[微信] 已停用，略過通知")
        return False
    
    if not PUSHPLUS_TOKEN:
        log("[警告] 未設定 PUSHPLUS_TOKEN，略過微信通知")
        return False
    
    url = "http://www.pushplus.plus/send"
    data = {
        "token": PUSHPLUS_TOKEN,
        "title": title,
        "content": message,
        "template": "txt"  # 可選: html, txt, json, markdown
    }
    
    try:
        _code, raw = _http_post_json(url, data, timeout=10)
        try:
            result = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            result = {}
        if result.get("code") == 200:
            log(f"[微信] 通知已傳送: {title}")
            return True
        else:
            log(f"[微信] 傳送失敗: {result.get('msg')}")
            return False
    except Exception as e:
        log(f"[微信] 傳送異常: {e}")
        return False


__all__ = [
    'log',
    'set_telegram_log_forward',
    'connect_device',
    'get_device_id',
    'take_screenshot',
    'load_and_match',
    'load_and_match_all_peaks',
    'click_by_image',
    'image_exists',
    'wait_for_image',
    'click_fixed',
    'wait_and_click',
    'wait_and_click_any',
    'wait_for_disappear',
    'wait_for_disappear_any',
    'wait_for_loading_disappear',
    'swipe',
    'clear_cache',
    'force_stop_app',
    'start_app',
    'restart_app',
    'send_wechat',
    'send_telegram',
    'PIC_DIR',
    'CACHE_DIR',
]
