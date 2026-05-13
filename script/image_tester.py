"""
Under Dark 控制台 - Flask 網頁介面
啟動後在瀏覽器開啟 http://127.0.0.1:5050
功能：
  1. 模板管理：上傳 / 刪除 / 預覽 pic/ 下的模板圖（支援子目錄 whitelist / blacklist）
  2. 腳本控制：啟動 / 停止 auto_fight.py，即時串流 log
  3. 設定：調整執行場次、ADB 設備 ID（寫入 settings.json）
  4. OCR 測試：上傳截圖驗證驗證碼識別與模板比對
"""

import base64
import io
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections import Counter

import cv2
import numpy as np

try:
    from flask import (Flask, Response, jsonify, render_template_string,
                       request, send_file)
except ImportError:
    print("請先安裝 Flask：pip install flask")
    sys.exit(1)

# ── 路徑設定 ──────────────────────────────────────────────────
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
PIC_DIR      = os.path.join(SCRIPT_DIR, "../pic")
CACHE_DIR    = os.path.join(PIC_DIR, "cache")
SETTINGS_PATH = os.path.join(SCRIPT_DIR, "settings.json")
AUTO_FIGHT_PY = os.path.join(SCRIPT_DIR, "auto_fight.py")
RUN_AUTO_FIGHT_BAT = os.path.join(SCRIPT_DIR, "run_auto_fight.bat")
RUN_AUTO_FIGHT_WEB_BAT = os.path.join(SCRIPT_DIR, "run_auto_fight_web.bat")
SL_RUN_CANDIDATES = [
    os.path.join(SCRIPT_DIR, "run_sl_web.bat"),
    os.path.join(SCRIPT_DIR, "run_sl.bat"),
    os.path.join(SCRIPT_DIR, "run_auto_fight_sl_web.bat"),
    os.path.join(SCRIPT_DIR, "run_auto_fight_sl.bat"),
]
SL_PY_CANDIDATES = [
    os.path.join(SCRIPT_DIR, "auto_fight_sl.py"),
    os.path.join(SCRIPT_DIR, "sl_flow.py"),
]
os.makedirs(CACHE_DIR, exist_ok=True)

# 允許上傳的子目錄（空字串=pic/ 根目錄）
ALLOWED_SUBDIRS = ["", "whitelist", "blacklist"]

# 預設驗證碼區域參數
DEFAULT_CAPTCHA_REF_W  = 900
DEFAULT_CAPTCHA_REF_H  = 1600
DEFAULT_CAPTCHA_REGION = (220, 645, 660, 771)
DEFAULT_BUTTON_REGION  = (60, 750, 820, 1010)

app = Flask(__name__)

# ════════════════════════════════════════════════════════════
# 腳本程序管理
# ════════════════════════════════════════════════════════════

_proc: subprocess.Popen | None = None
_proc_lock = threading.Lock()
_log_queue: queue.Queue = queue.Queue(maxsize=2000)
_log_history: list[str] = []          # 保留最近 500 條給新連線
_log_lock = threading.Lock()

MAX_LOG_HISTORY = 500


def _enqueue_log(line: str):
    ts   = time.strftime("%H:%M:%S")
    msg  = f"[{ts}] {line.rstrip()}"
    with _log_lock:
        _log_history.append(msg)
        if len(_log_history) > MAX_LOG_HISTORY:
            _log_history.pop(0)
    try:
        _log_queue.put_nowait(msg)
    except queue.Full:
        pass


def _read_stream(stream):
    """持續讀取子程序 stdout/stderr，塞入 queue"""
    for line in iter(stream.readline, ""):
        if line:
            _enqueue_log(line)
    _enqueue_log("[腳本] 程序已結束")


def start_script(
    num: int | None = None,
    device_id: str | None = None,
    device_id_1: str | None = None,
    device_id_2: str | None = None,
):
    """啟動控制台專用 bat（已有程序在跑則不重複啟動）"""
    global _proc
    with _proc_lock:
        if _proc and _proc.poll() is None:
            return False, "腳本已在執行中"

        # 寫入臨時參數到 settings.json（可選）
        if num is not None or device_id is not None or device_id_1 is not None or device_id_2 is not None:
            _patch_settings(
                num=num,
                device_id=device_id,
                device_id_1=device_id_1,
                device_id_2=device_id_2,
            )

        run_bat = RUN_AUTO_FIGHT_WEB_BAT if os.path.exists(RUN_AUTO_FIGHT_WEB_BAT) else RUN_AUTO_FIGHT_BAT
        if not os.path.exists(run_bat):
            return False, f"找不到啟動檔：{run_bat}"

        # 一般「啟動腳本」固定只跑第一組設備；雙設備請使用 SL 流程按鈕
        cur = _read_current_devices()
        run_device = (
            str(device_id_1 or "").strip()
            or str(device_id or "").strip()
            or str((cur or {}).get("device_id_1") or "").strip()
        )
        if not run_device:
            return False, "未設定第一組設備 ID（請先到設定頁填入 ADB 設備 ID #1）"

        env = os.environ.copy()
        try:
            _proc = subprocess.Popen(
                ["cmd.exe", "/c", run_bat, "--device-id", run_device, "--worker-name", "模擬器1"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=SCRIPT_DIR,
                env=env,
                # Windows 上避免彈出黑色命令列視窗
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except Exception as e:
            return False, str(e)

        threading.Thread(target=_read_stream, args=(_proc.stdout,), daemon=True).start()
        _enqueue_log(f"[系統] 腳本已啟動（第一組設備: {run_device}, PID {_proc.pid}）")
        return True, f"腳本已啟動（第一組設備: {run_device}, PID {_proc.pid}）"


def _resolve_sl_launch_command():
    """解析 SL 流程啟動命令（優先 bat，其次 python 腳本）。"""
    for bat in SL_RUN_CANDIDATES:
        if os.path.exists(bat):
            return ["cmd.exe", "/c", bat], f"BAT: {os.path.basename(bat)}"
    for py_file in SL_PY_CANDIDATES:
        if os.path.exists(py_file):
            return [sys.executable, "-u", py_file], f"PY: {os.path.basename(py_file)}"
    return None, ""


def start_sl_script(
    num: int | None = None,
    device_id_1: str | None = None,
    device_id_2: str | None = None,
):
    """啟動 SL 流程腳本（已有程序在跑則不重複啟動）。"""
    global _proc
    with _proc_lock:
        if _proc and _proc.poll() is None:
            return False, "已有腳本在執行中，請先停止"

        # 延用同一組設定入口，讓 SL 腳本可共用兩台設備設定
        if num is not None or device_id_1 is not None or device_id_2 is not None:
            _patch_settings(
                num=num,
                device_id_1=device_id_1,
                device_id_2=device_id_2,
            )

        cmd, picked = _resolve_sl_launch_command()
        if not cmd:
            return False, (
                "找不到 SL 流程啟動檔。請新增其一："
                "run_sl_web.bat / run_sl.bat / run_auto_fight_sl_web.bat / run_auto_fight_sl.bat "
                "或 auto_fight_sl.py / sl_flow.py"
            )

        env = os.environ.copy()
        try:
            _proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=SCRIPT_DIR,
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except Exception as e:
            return False, str(e)

        threading.Thread(target=_read_stream, args=(_proc.stdout,), daemon=True).start()
        _enqueue_log(f"[系統] SL 流程已啟動 ({picked}, PID {_proc.pid})")
        return True, f"SL 流程已啟動 ({picked}, PID {_proc.pid})"


def stop_script():
    """停止 run_auto_fight.bat（含其子程序）"""
    global _proc
    with _proc_lock:
        if _proc is None or _proc.poll() is not None:
            return False, "腳本未在執行中"

        # Windows: bat 通常會帶出子程序（py/adb），先嘗試整棵程序樹停止
        if sys.platform == "win32":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(_proc.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except Exception:
                pass
        else:
            _proc.terminate()

        try:
            _proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _proc.kill()
        _enqueue_log("[系統] 腳本已停止")
        return True, "腳本已停止"


def script_status():
    with _proc_lock:
        if _proc is None:
            return "idle"
        return "running" if _proc.poll() is None else "idle"


def _patch_settings(
    num: int | None,
    device_id: str | None = None,
    device_id_1: str | None = None,
    device_id_2: str | None = None,
):
    """將 num 寫入 auto_fight.py；設備 ID 寫入 settings.json（支援 1/2 號模擬器）。"""
    if num is not None:
        _patch_auto_fight_num(num)
    if (device_id is not None or device_id_1 is not None or device_id_2 is not None) and os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, encoding="utf-8-sig") as f:
                cfg = json.load(f)
            emulators = cfg.setdefault("emulators", {})
            emulators.setdefault("1", {"id": "", "name": "模擬器1"})
            emulators.setdefault("2", {"id": "", "name": "模擬器2"})

            # 相容舊前端：僅傳 device_id 時，仍寫入 default_emulator 指向的那一組
            if device_id is not None:
                default_key = str(cfg.get("default_emulator", "1"))
                emulators.setdefault(default_key, {"id": "", "name": f"模擬器{default_key}"})
                emulators[default_key]["id"] = str(device_id).strip()
            if device_id_1 is not None:
                emulators["1"]["id"] = str(device_id_1).strip()
            if device_id_2 is not None:
                emulators["2"]["id"] = str(device_id_2).strip()

            # 預設始終指向 1 號，主腳本會自動取前兩組有效設備並行執行
            cfg["default_emulator"] = "1"
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def _patch_auto_fight_num(num: int):
    """就地修改 auto_fight.py 的 NUM = <n> 行"""
    try:
        with open(AUTO_FIGHT_PY, encoding="utf-8") as f:
            lines = f.readlines()
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith("NUM") and "=" in stripped:
                indent = line[: len(line) - len(stripped)]
                lines[i] = f"{indent}NUM = {num}\n"
                break
        with open(AUTO_FIGHT_PY, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception:
        pass


def _read_current_num():
    """從 auto_fight.py 讀取目前 NUM 值"""
    try:
        with open(AUTO_FIGHT_PY, encoding="utf-8") as f:
            for line in f:
                stripped = line.lstrip()
                if stripped.startswith("NUM") and "=" in stripped:
                    return int(stripped.split("=")[1].split("#")[0].strip())
    except Exception:
        pass
    return 500


def _read_current_devices():
    """從 settings.json 讀取 1/2 號設備 ID"""
    try:
        with open(SETTINGS_PATH, encoding="utf-8-sig") as f:
            cfg = json.load(f)
        emulators = cfg.get("emulators", {})
        dev1 = str((emulators.get("1") or {}).get("id") or "").strip()
        dev2 = str((emulators.get("2") or {}).get("id") or "").strip()
        return {"device_id_1": dev1, "device_id_2": dev2}
    except Exception:
        return {"device_id_1": "", "device_id_2": ""}


# ════════════════════════════════════════════════════════════
# 模板工具
# ════════════════════════════════════════════════════════════

def list_templates():
    """列出 pic/ 及允許子目錄的所有 PNG，回傳 [{name, subdir, path, size}]"""
    result = []
    for subdir in ALLOWED_SUBDIRS:
        folder = os.path.join(PIC_DIR, subdir) if subdir else PIC_DIR
        if not os.path.isdir(folder):
            continue
        for fname in sorted(os.listdir(folder)):
            if not fname.lower().endswith(".png"):
                continue
            full = os.path.join(folder, fname)
            # 略過 cache 目錄內的檔案
            if os.path.abspath(full).startswith(os.path.abspath(CACHE_DIR)):
                continue
            result.append({
                "name":   fname,
                "subdir": subdir,
                "rel":    os.path.join(subdir, fname) if subdir else fname,
                "size":   os.path.getsize(full),
            })
    return result


def safe_template_path(subdir: str, filename: str):
    """安全地組合模板路徑（防止路徑穿越攻擊）"""
    if subdir not in ALLOWED_SUBDIRS:
        return None
    filename = os.path.basename(filename)
    if not filename.endswith(".png"):
        return None
    folder = os.path.join(PIC_DIR, subdir) if subdir else PIC_DIR
    return os.path.join(folder, filename)


# ════════════════════════════════════════════════════════════
# OCR / 模板比對工具
# ════════════════════════════════════════════════════════════

def scale_region(region_xyxy, ref_w, ref_h, screen_w, screen_h):
    x1, y1, x2, y2 = region_xyxy
    if ref_w <= 0 or ref_h <= 0:
        return x1, y1, x2, y2
    sx, sy = screen_w / ref_w, screen_h / ref_h
    return (int(round(x1*sx)), int(round(y1*sy)),
            int(round(x2*sx)), int(round(y2*sy)))


def get_button_positions(bx1, by1, bx2, by2):
    bw = (bx2-bx1)/5
    bh = (by2-by1)/2
    pos = {}
    for i, d in enumerate(["1","2","3","4","5"]):
        pos[d] = (int(bx1+bw*(i+0.5)), int(by1+bh*0.5))
    for i, d in enumerate(["6","7","8","9","0"]):
        pos[d] = (int(bx1+bw*(i+0.5)), int(by1+bh*1.5))
    return pos


def draw_overlay(img_bgr, ref_w, ref_h, captcha_region, button_region):
    vis = img_bgr.copy()
    ih, iw = vis.shape[:2]
    cx1, cy1, cx2, cy2 = scale_region(captcha_region, ref_w, ref_h, iw, ih)
    cv2.rectangle(vis, (cx1,cy1), (cx2,cy2), (0,140,255), 2)
    cv2.putText(vis,"CAPTCHA",(cx1,max(cy1-6,14)),cv2.FONT_HERSHEY_SIMPLEX,0.55,(0,140,255),2)
    bx1,by1,bx2,by2 = scale_region(button_region, ref_w, ref_h, iw, ih)
    cv2.rectangle(vis,(bx1,by1),(bx2,by2),(0,220,60),2)
    cv2.putText(vis,"BUTTON",(bx1,max(by1-6,14)),cv2.FONT_HERSHEY_SIMPLEX,0.55,(0,220,60),2)
    for d,(px,py) in get_button_positions(bx1,by1,bx2,by2).items():
        try:
            cv2.drawMarker(vis,(px,py),(0,0,220),markerType=cv2.MARKER_CROSS,markerSize=18,thickness=2)
        except Exception:
            cv2.circle(vis,(px,py),8,(0,0,220),2)
        cv2.putText(vis,d,(px+10,py-8),cv2.FONT_HERSHEY_SIMPLEX,0.48,(0,220,220),1)
    return vis, (cx1,cy1,cx2,cy2)


def bgr_to_data_url(bgr):
    _, buf = cv2.imencode(".png", bgr)
    return "data:image/png;base64," + base64.b64encode(buf).decode()


def run_captcha_ocr(crop_bgr):
    try:
        from PIL import Image
        import ddddocr
    except ImportError as e:
        return None, [f"缺少套件：{e}"]

    def to_png_bytes(bgr_im):
        from PIL import Image as PI
        rgb = cv2.cvtColor(bgr_im, cv2.COLOR_BGR2RGB)
        bio = io.BytesIO()
        PI.fromarray(rgb).save(bio, format="PNG")
        return bio.getvalue()

    def norm(t):
        s = str(t).translate(str.maketrans({"o":"0","O":"0","Ｏ":"0","〇":"0","○":"0"}))
        return "".join(c for c in s if c.isdigit())

    def variants(bgr):
        out=[]
        pad=cv2.copyMakeBorder(bgr,10,10,10,10,cv2.BORDER_CONSTANT,value=(0,0,0))
        for img in (pad,bgr):
            for s in (2.0,3.0):
                h,w=img.shape[:2]
                out.append(cv2.resize(img,(max(8,int(w*s)),max(8,int(h*s))),interpolation=cv2.INTER_CUBIC))
        gray=cv2.cvtColor(bgr,cv2.COLOR_BGR2GRAY)
        for g in (gray,cv2.bitwise_not(gray)):
            for s in (2.0,3.0):
                h,w=g.shape[:2]
                out.append(cv2.resize(cv2.cvtColor(g,cv2.COLOR_GRAY2BGR),(max(8,int(w*s)),max(8,int(h*s))),interpolation=cv2.INTER_CUBIC))
        _,otsu=cv2.threshold(gray,0,255,cv2.THRESH_BINARY+cv2.THRESH_OTSU)
        for bn in (otsu,cv2.bitwise_not(otsu)):
            out.append(cv2.resize(cv2.cvtColor(bn,cv2.COLOR_GRAY2BGR),None,fx=3,fy=3,interpolation=cv2.INTER_CUBIC))
        return out

    logs=[]
    hits=[]
    cache={}
    for beta in (True,False):
        if beta not in cache:
            try:
                cache[beta]=ddddocr.DdddOcr(beta=beta,show_ad=False)
            except TypeError:
                cache[beta]=ddddocr.DdddOcr(beta=beta)
        ocr=cache[beta]
        for idx,var in enumerate(variants(crop_bgr)):
            try:
                raw=ocr.classification(to_png_bytes(var))
                d=norm(raw)
                logs.append(f"beta={beta} #{idx}: {repr(raw)} → '{d}'")
                if len(d)==4:
                    hits.append(d)
            except Exception as ex:
                logs.append(f"beta={beta} #{idx}: 錯誤 {ex}")
    if not hits:
        return None, logs
    return Counter(hits).most_common(1)[0][0], logs


def match_template_bgr(screen_bgr, template_bgr, multiscale=True, threshold=0.80):
    ih,iw=screen_bgr.shape[:2]
    th0,tw0=template_bgr.shape[:2]
    if multiscale:
        best_val,best_loc,best_tw,best_th=-1.0,(0,0),8,8
        for scale in np.linspace(0.7,1.3,9):
            rw=max(8,int(round(tw0*scale)))
            rh=max(8,int(round(th0*scale)))
            if rw>=iw or rh>=ih:
                continue
            interp=cv2.INTER_AREA if scale<1.0 else cv2.INTER_LINEAR
            tmpl=cv2.resize(template_bgr,(rw,rh),interpolation=interp)
            res=cv2.matchTemplate(screen_bgr,tmpl,cv2.TM_CCOEFF_NORMED)
            _,mv,_,ml=cv2.minMaxLoc(res)
            if mv>best_val:
                best_val,best_loc,best_tw,best_th=mv,ml,rw,rh
        sim,loc,tw,th=best_val,best_loc,best_tw,best_th
    else:
        if tw0>=iw or th0>=ih:
            return False,0,0,0.0
        res=cv2.matchTemplate(screen_bgr,template_bgr,cv2.TM_CCOEFF_NORMED)
        _,sim,_,loc=cv2.minMaxLoc(res)
        tw,th=tw0,th0
    cx=loc[0]+tw//2
    cy=loc[1]+th//2
    return sim>=threshold, cx, cy, float(sim)


# ════════════════════════════════════════════════════════════
# Flask 路由
# ════════════════════════════════════════════════════════════

# ── 頁面 ─────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(HTML_PAGE)


# ── 模板管理 ──────────────────────────────────────────────────
@app.route("/api/templates")
def api_templates():
    return jsonify(list_templates())


@app.route("/api/template-image")
def api_template_image():
    """提供模板縮圖（顯示用）"""
    subdir   = request.args.get("subdir", "")
    filename = request.args.get("name", "")
    path     = safe_template_path(subdir, filename)
    if not path or not os.path.exists(path):
        return "Not found", 404
    return send_file(path, mimetype="image/png")


@app.route("/api/upload-template", methods=["POST"])
def api_upload_template():
    """上傳模板圖至 pic/ 或子目錄"""
    subdir = request.form.get("subdir", "")
    if subdir not in ALLOWED_SUBDIRS:
        return jsonify({"error": f"不允許的子目錄：{subdir}"}), 400

    files = request.files.getlist("images")
    if not files:
        return jsonify({"error": "未上傳檔案"}), 400

    saved = []
    errors = []
    for f in files:
        fname = os.path.basename(f.filename or "")
        if not fname.lower().endswith(".png"):
            errors.append(f"{fname}：只接受 PNG 格式")
            continue
        target_dir = os.path.join(PIC_DIR, subdir) if subdir else PIC_DIR
        os.makedirs(target_dir, exist_ok=True)
        dest = os.path.join(target_dir, fname)
        f.save(dest)
        saved.append(fname)

    return jsonify({"saved": saved, "errors": errors})


@app.route("/api/delete-template", methods=["POST"])
def api_delete_template():
    body     = request.json or {}
    subdir   = body.get("subdir", "")
    filename = body.get("name", "")
    path     = safe_template_path(subdir, filename)
    if not path or not os.path.exists(path):
        return jsonify({"error": "檔案不存在"}), 404
    os.remove(path)
    return jsonify({"ok": True})


# ── 腳本控制 ──────────────────────────────────────────────────
@app.route("/api/script/status")
def api_script_status():
    return jsonify({"status": script_status()})


@app.route("/api/script/start", methods=["POST"])
def api_script_start():
    body      = request.json or {}
    num       = int(body["num"]) if "num" in body else None
    device_id = body.get("device_id") or None
    device_id_1 = body.get("device_id_1")
    device_id_2 = body.get("device_id_2")
    ok, msg   = start_script(
        num=num,
        device_id=device_id,
        device_id_1=device_id_1,
        device_id_2=device_id_2,
    )
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/script/start-sl", methods=["POST"])
def api_script_start_sl():
    body      = request.json or {}
    num       = int(body["num"]) if "num" in body else None
    device_id_1 = body.get("device_id_1")
    device_id_2 = body.get("device_id_2")
    ok, msg   = start_sl_script(
        num=num,
        device_id_1=device_id_1,
        device_id_2=device_id_2,
    )
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/script/stop", methods=["POST"])
def api_script_stop():
    ok, msg = stop_script()
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/script/logs/stream")
def api_logs_stream():
    """SSE：即時推送 log 行"""
    def generate():
        # 先把 history 傳給新連線
        with _log_lock:
            hist = list(_log_history)
        for line in hist:
            yield f"data: {line}\n\n"

        # 之後持續推送新行
        while True:
            try:
                line = _log_queue.get(timeout=1.0)
                yield f"data: {line}\n\n"
            except queue.Empty:
                yield ": keepalive\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/script/logs/clear", methods=["POST"])
def api_logs_clear():
    with _log_lock:
        _log_history.clear()
    return jsonify({"ok": True})


# ── 設定讀寫 ──────────────────────────────────────────────────
@app.route("/api/settings")
def api_settings_get():
    devs = _read_current_devices()
    return jsonify({
        "num":       _read_current_num(),
        "device_id": devs.get("device_id_1", ""),  # 相容舊前端
        "device_id_1": devs.get("device_id_1", ""),
        "device_id_2": devs.get("device_id_2", ""),
    })


@app.route("/api/settings", methods=["POST"])
def api_settings_set():
    body      = request.json or {}
    num       = int(body["num"]) if "num" in body else None
    device_id = body.get("device_id") or None
    device_id_1 = body.get("device_id_1")
    device_id_2 = body.get("device_id_2")
    _patch_settings(
        num=num,
        device_id=device_id,
        device_id_1=device_id_1,
        device_id_2=device_id_2,
    )
    return jsonify({"ok": True})


# ── OCR 測試 ──────────────────────────────────────────────────
@app.route("/api/overlay", methods=["POST"])
def api_overlay():
    file = request.files.get("image")
    if not file:
        return jsonify({"error": "未上傳圖檔"}), 400
    data = np.frombuffer(file.read(), np.uint8)
    bgr  = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if bgr is None:
        return jsonify({"error": "無法解碼圖檔"}), 400

    def iv(k, d):
        try: return int(request.form.get(k, d))
        except: return int(d)

    ref_w = iv("ref_w", DEFAULT_CAPTCHA_REF_W)
    ref_h = iv("ref_h", DEFAULT_CAPTCHA_REF_H)
    cap_r = (iv("cap_x1",DEFAULT_CAPTCHA_REGION[0]), iv("cap_y1",DEFAULT_CAPTCHA_REGION[1]),
             iv("cap_x2",DEFAULT_CAPTCHA_REGION[2]), iv("cap_y2",DEFAULT_CAPTCHA_REGION[3]))
    btn_r = (iv("btn_x1",DEFAULT_BUTTON_REGION[0]),  iv("btn_y1",DEFAULT_BUTTON_REGION[1]),
             iv("btn_x2",DEFAULT_BUTTON_REGION[2]),  iv("btn_y2",DEFAULT_BUTTON_REGION[3]))

    overlay,(cx1,cy1,cx2,cy2) = draw_overlay(bgr, ref_w, ref_h, cap_r, btn_r)
    ih,iw = bgr.shape[:2]
    cv2.imwrite(os.path.join(CACHE_DIR, "current_screen.png"), bgr)

    cx1,cy1=max(0,cx1),max(0,cy1)
    cx2,cy2=min(iw,cx2),min(ih,cy2)
    crop = bgr[cy1:cy2,cx1:cx2] if cx2>cx1 and cy2>cy1 else np.zeros((1,1,3),np.uint8)

    return jsonify({
        "overlay":      bgr_to_data_url(overlay),
        "captcha_crop": bgr_to_data_url(crop),
        "width": iw, "height": ih,
    })


@app.route("/api/ocr", methods=["POST"])
def api_ocr():
    screen_path = os.path.join(CACHE_DIR, "current_screen.png")
    if not os.path.exists(screen_path):
        return jsonify({"error": "請先上傳截圖"}), 400
    bgr = cv2.imread(screen_path)
    ih,iw = bgr.shape[:2]

    def iv(k,d):
        try: return int((request.json or {}).get(k, d))
        except: return int(d)

    ref_w = iv("ref_w",DEFAULT_CAPTCHA_REF_W)
    ref_h = iv("ref_h",DEFAULT_CAPTCHA_REF_H)
    cap_r = (iv("cap_x1",DEFAULT_CAPTCHA_REGION[0]), iv("cap_y1",DEFAULT_CAPTCHA_REGION[1]),
             iv("cap_x2",DEFAULT_CAPTCHA_REGION[2]), iv("cap_y2",DEFAULT_CAPTCHA_REGION[3]))

    cx1,cy1,cx2,cy2 = scale_region(cap_r, ref_w, ref_h, iw, ih)
    cx1,cy1=max(0,cx1),max(0,cy1)
    cx2,cy2=min(iw,cx2),min(ih,cy2)
    if cx2<=cx1 or cy2<=cy1:
        return jsonify({"error":"CAPTCHA_REGION 無效"}), 400

    crop = bgr[cy1:cy2,cx1:cx2]
    cv2.imwrite(os.path.join(CACHE_DIR,"captcha_debug.png"), crop)
    digits,logs = run_captcha_ocr(crop)
    return jsonify({"digits":digits,"success":digits is not None,
                    "logs":logs[-30:],"crop_data_url":bgr_to_data_url(crop)})


@app.route("/api/match", methods=["POST"])
def api_match():
    screen_path = os.path.join(CACHE_DIR,"current_screen.png")
    if not os.path.exists(screen_path):
        return jsonify({"error":"請先上傳截圖"}), 400
    body       = request.json or {}
    tpl_rel    = body.get("template","")
    multiscale = bool(body.get("multiscale",True))
    threshold  = float(body.get("threshold",0.80))
    tpl_path   = os.path.join(PIC_DIR, tpl_rel)
    if not os.path.exists(tpl_path):
        return jsonify({"error":f"模板不存在：{tpl_rel}"}), 400
    screen_bgr   = cv2.imread(screen_path)
    template_bgr = cv2.imread(tpl_path)
    th0,tw0      = template_bgr.shape[:2]
    found,cx,cy,sim = match_template_bgr(screen_bgr, template_bgr, multiscale, threshold)
    vis   = screen_bgr.copy()
    color = (0,200,0) if found else (0,0,220)
    try:
        cv2.drawMarker(vis,(cx,cy),color,markerType=cv2.MARKER_STAR,markerSize=30,thickness=2)
    except Exception:
        cv2.circle(vis,(cx,cy),14,color,2)
    cv2.putText(vis,f"{os.path.basename(tpl_rel)} {sim:.3f}",
                (max(0,cx-60),max(14,cy-18)),cv2.FONT_HERSHEY_SIMPLEX,0.6,color,2)
    return jsonify({"found":found,"similarity":round(sim,4),"x":cx,"y":cy,
                    "tpl_w":tw0,"tpl_h":th0,"result_img":bgr_to_data_url(vis)})


# ════════════════════════════════════════════════════════════
# HTML 頁面
# ════════════════════════════════════════════════════════════

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<title>Under Dark 控制台</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0f0f1a;--sidebar:#16213e;--card:#1a2744;--border:#2a3f6a;--accent:#e94560;--green:#27ae60;--orange:#e67e22;--text:#dde4f0;--muted:#7a8aaa}
body{font-family:"Segoe UI",system-ui,sans-serif;background:var(--bg);color:var(--text);height:100vh;display:flex;flex-direction:column;overflow:hidden}
/* ── 頂部 Header ── */
header{background:var(--sidebar);padding:10px 18px;display:flex;align-items:center;gap:14px;border-bottom:2px solid var(--border);flex-shrink:0}
header h1{font-size:1.05rem;color:var(--accent);white-space:nowrap}
.status-dot{width:10px;height:10px;border-radius:50%;background:#555;flex-shrink:0;transition:.3s}
.status-dot.running{background:#27ae60;box-shadow:0 0 6px #27ae60}
#statusText{font-size:.82rem;color:var(--muted)}
/* ── Tab 列 ── */
.tabs{display:flex;background:var(--card);border-bottom:2px solid var(--border);flex-shrink:0}
.tab{padding:10px 22px;cursor:pointer;font-size:.85rem;font-weight:600;color:var(--muted);border-bottom:3px solid transparent;transition:.15s;white-space:nowrap}
.tab:hover{color:var(--text)}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
/* ── 主體 ── */
.tab-content{display:none;flex:1;overflow:hidden}
.tab-content.active{display:flex;overflow:hidden}
/* ── 通用 ── */
.panel{padding:16px;overflow-y:auto}
.btn{padding:7px 14px;border:none;border-radius:6px;cursor:pointer;font-size:.83rem;font-weight:600;transition:.15s}
.btn-accent{background:var(--accent);color:#fff}.btn-accent:hover{background:#c73652}
.btn-green{background:var(--green);color:#fff}.btn-green:hover{background:#1e8449}
.btn-purple{background:#8e44ad;color:#fff}.btn-purple:hover{background:#7d3c98}
.btn-orange{background:var(--orange);color:#fff}.btn-orange:hover{background:#ca6f1e}
.btn-gray{background:#2a3f6a;color:var(--text)}.btn-gray:hover{background:#3a5080}
.btn-danger{background:#c0392b;color:#fff}.btn-danger:hover{background:#a93226}
.btn:disabled{opacity:.4;cursor:not-allowed}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.field{display:flex;flex-direction:column;gap:4px}
.field label{font-size:.76rem;color:var(--muted)}
.field input,.field select{background:#0d1526;border:1px solid var(--border);color:var(--text);padding:6px 10px;border-radius:5px;font-size:.83rem}
.field input:focus,.field select:focus{outline:none;border-color:var(--accent)}
.card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px;margin-bottom:12px}
.card-title{font-size:.78rem;font-weight:700;letter-spacing:.06em;color:var(--accent);margin-bottom:10px;text-transform:uppercase}
.badge{padding:2px 10px;border-radius:12px;font-size:.75rem;font-weight:700;display:inline-block}
.badge-ok{background:var(--green);color:#fff}.badge-fail{background:#c0392b;color:#fff}
/* ── 模板網格 ── */
#templateGrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:10px;margin-top:10px}
.tpl-card{background:#0d1526;border:1px solid var(--border);border-radius:6px;overflow:hidden;position:relative;cursor:pointer;transition:.15s}
.tpl-card:hover{border-color:var(--accent)}
.tpl-card img{width:100%;height:80px;object-fit:contain;background:#060c1a;display:block}
.tpl-card .tpl-name{font-size:.7rem;padding:4px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--muted)}
.tpl-card .tpl-del{position:absolute;top:3px;right:3px;background:rgba(192,57,43,.85);color:#fff;border:none;border-radius:4px;padding:1px 6px;font-size:.7rem;cursor:pointer;display:none}
.tpl-card:hover .tpl-del{display:block}
.tpl-card.selected{border-color:var(--green)}
/* ── 拖放上傳區 ── */
.dropzone{border:2px dashed var(--border);border-radius:8px;padding:22px;text-align:center;cursor:pointer;color:var(--muted);font-size:.85rem;transition:.2s}
.dropzone:hover,.dropzone.drag-over{border-color:var(--accent);color:var(--accent)}
/* ── 螢幕框選截圖 ── */
.capture-modal{position:fixed;inset:0;background:rgba(0,0,0,.78);display:none;align-items:center;justify-content:center;z-index:9999}
.capture-modal.show{display:flex}
.capture-panel{width:min(92vw,1200px);max-height:92vh;background:#101b34;border:1px solid var(--border);border-radius:10px;padding:12px;display:flex;flex-direction:column;gap:10px}
.capture-toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.capture-hint{font-size:.8rem;color:var(--muted)}
#captureCanvas{max-width:100%;max-height:72vh;display:block;border:1px solid var(--border);border-radius:6px;background:#060c1a;cursor:crosshair}
.capture-preview{display:flex;align-items:center;gap:10px;min-height:56px}
.capture-preview img{max-height:52px;max-width:240px;border:1px solid var(--border);border-radius:4px;background:#060c1a}
.capture-preview .empty{font-size:.78rem;color:var(--muted)}
/* ── Log 區 ── */
#logBox{background:#060c1a;border:1px solid var(--border);border-radius:6px;padding:10px;font-size:.76rem;font-family:monospace;height:calc(100vh - 180px);overflow-y:auto;color:#7ecf7e;line-height:1.6;white-space:pre-wrap;word-break:break-all}
/* ── OCR 測試 ── */
.ocr-layout{display:flex;gap:14px;flex:1;overflow:hidden}
.ocr-left{width:280px;flex-shrink:0;overflow-y:auto;padding:14px}
.ocr-right{flex:1;overflow:auto;padding:14px;display:flex;align-items:flex-start;justify-content:flex-start;background:#060c1a}
#ocrPreview{max-width:100%;border:1px solid var(--border);border-radius:4px}
.result-box{background:#060c1a;border:1px solid var(--border);border-radius:6px;padding:10px;font-size:.76rem;font-family:monospace;white-space:pre-wrap;word-break:break-all;max-height:200px;overflow-y:auto;color:#7ecf7e;line-height:1.5;margin-top:6px}
.field-grid{display:grid;grid-template-columns:auto 1fr;gap:4px 8px;align-items:center}
.field-grid label{font-size:.78rem;color:var(--muted);white-space:nowrap}
.field-grid input{background:#0d1526;border:1px solid var(--border);color:var(--text);padding:4px 8px;border-radius:4px;font-size:.8rem}
/* 分頁捲軸 */
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#2a3f6a;border-radius:3px}
</style>
</head>
<body>

<!-- ═══ Header ═══ -->
<header>
  <h1>⚔ Under Dark 控制台</h1>
  <div class="status-dot" id="statusDot"></div>
  <span id="statusText">讀取中…</span>
  <div style="flex:1"></div>
  <button class="btn btn-green" id="btnStart" onclick="startScript()">▶ 啟動腳本</button>
  <button class="btn btn-purple" id="btnStartSL" onclick="startSlScript()">⟳ 啟動SL流程</button>
  <button class="btn btn-danger" id="btnStop" onclick="stopScript()">■ 停止腳本</button>
</header>

<!-- ═══ Tabs ═══ -->
<div class="tabs">
  <div class="tab active" onclick="switchTab('tabScript')">🖥 腳本執行</div>
  <div class="tab" onclick="switchTab('tabTemplates')">🖼 模板管理</div>
  <div class="tab" onclick="switchTab('tabOcr')">🔍 OCR 測試</div>
  <div class="tab" onclick="switchTab('tabSettings')">⚙ 設定</div>
</div>

<!-- ═══ Tab: 腳本執行 ═══ -->
<div class="tab-content active" id="tabScript">
  <div style="flex:1;display:flex;flex-direction:column;padding:12px;gap:8px;overflow:hidden">
    <div class="row" style="flex-shrink:0">
      <span style="font-size:.8rem;color:var(--muted)">即時 Log</span>
      <button class="btn btn-gray" onclick="clearLogs()" style="padding:4px 10px;font-size:.75rem">清除</button>
      <label style="font-size:.78rem;color:var(--muted)">
        <input type="checkbox" id="autoScroll" checked> 自動捲動
      </label>
    </div>
    <div id="logBox"></div>
  </div>
</div>

<!-- ═══ Tab: 模板管理 ═══ -->
<div class="tab-content" id="tabTemplates">
  <div class="panel" style="flex:1">
    <div class="card">
      <div class="card-title">上傳模板圖（PNG）</div>
      <div class="row" style="margin-bottom:10px">
        <div class="field">
          <label>存放目錄</label>
          <select id="uploadSubdir" style="width:140px">
            <option value="">pic/（根目錄）</option>
            <option value="whitelist">pic/whitelist/</option>
            <option value="blacklist">pic/blacklist/</option>
          </select>
        </div>
        <button class="btn btn-orange" type="button" style="margin-top:18px" onclick="captureTemplateFromScreen()">🖼 框選螢幕截圖</button>
      </div>
      <div class="dropzone" id="uploadZone"
           onclick="document.getElementById('uploadInput').click()"
           ondragover="event.preventDefault();this.classList.add('drag-over')"
           ondragleave="this.classList.remove('drag-over')"
           ondrop="handleUploadDrop(event)">
        點擊或拖曳 PNG 至此（可多選）
      </div>
      <input type="file" id="uploadInput" accept=".png" multiple style="display:none" onchange="handleUploadFiles(this.files)">
      <div id="uploadResult" style="margin-top:8px;font-size:.8rem;color:var(--green)"></div>
    </div>

    <div class="card">
      <div class="card-title">現有模板
        <span style="color:var(--muted);font-weight:400;font-size:.75rem;margin-left:8px" id="tplCount"></span>
        <button class="btn btn-gray" onclick="loadTemplates()" style="float:right;padding:3px 10px;font-size:.74rem">重新整理</button>
      </div>
      <div class="row" style="gap:6px;margin-bottom:8px">
        <span style="font-size:.76rem;color:var(--muted)">篩選目錄：</span>
        <button class="btn btn-gray tpl-filter active" onclick="filterTemplates('')" data-dir="">全部</button>
        <button class="btn btn-gray tpl-filter" onclick="filterTemplates('')" data-dir="root">根目錄</button>
        <button class="btn btn-gray tpl-filter" onclick="filterTemplates('whitelist')" data-dir="whitelist">whitelist</button>
        <button class="btn btn-gray tpl-filter" onclick="filterTemplates('blacklist')" data-dir="blacklist">blacklist</button>
      </div>
      <div id="templateGrid"></div>
    </div>
  </div>
</div>

<!-- ═══ Tab: OCR 測試 ═══ -->
<div class="tab-content" id="tabOcr">
  <div class="ocr-layout">
    <!-- 左側控制 -->
    <div class="ocr-left">
      <div class="card">
        <div class="card-title">上傳截圖</div>
        <div class="dropzone" style="padding:12px" onclick="document.getElementById('ocrFileInput').click()"
             ondragover="event.preventDefault();this.classList.add('drag-over')"
             ondragleave="this.classList.remove('drag-over')"
             ondrop="handleOcrDrop(event)">
          點擊或拖曳截圖
        </div>
        <input type="file" id="ocrFileInput" accept="image/*" style="display:none" onchange="handleOcrFile(this.files[0])">
        <div id="ocrImgInfo" style="font-size:.76rem;color:var(--muted);margin-top:4px"></div>
      </div>

      <div class="card">
        <div class="card-title">驗證碼區域參數</div>
        <div class="field-grid">
          <label>REF_W</label><input id="ref_w" type="number" value="900">
          <label>REF_H</label><input id="ref_h" type="number" value="1600">
          <label>數字 x1</label><input id="cap_x1" type="number" value="220">
          <label>數字 y1</label><input id="cap_y1" type="number" value="645">
          <label>數字 x2</label><input id="cap_x2" type="number" value="660">
          <label>數字 y2</label><input id="cap_y2" type="number" value="771">
          <label>鍵盤 x1</label><input id="btn_x1" type="number" value="60">
          <label>鍵盤 y1</label><input id="btn_y1" type="number" value="750">
          <label>鍵盤 x2</label><input id="btn_x2" type="number" value="820">
          <label>鍵盤 y2</label><input id="btn_y2" type="number" value="1010">
        </div>
        <button class="btn btn-gray" style="width:100%;margin-top:8px" onclick="ocrUploadAndDraw()">↺ 套用並重繪</button>
      </div>

      <div class="card">
        <div class="card-title">驗證碼 OCR</div>
        <div id="ocrCropWrap"></div>
        <button class="btn btn-orange" style="width:100%;margin-top:6px" id="btnOcr" disabled onclick="runOcr()">🔍 執行 OCR</button>
        <div id="ocrResult" class="result-box">（尚未執行）</div>
      </div>

      <div class="card">
        <div class="card-title">模板比對</div>
        <select id="ocrTplSelect" size="6" style="width:100%;background:#0d1526;border:1px solid var(--border);color:var(--text);border-radius:4px;font-size:.8rem;height:100px"></select>
        <div class="row" style="margin-top:6px">
          <div class="field">
            <label>閾值</label>
            <input id="ocrThreshold" type="number" step="0.01" min="0" max="1" value="0.80" style="width:70px">
          </div>
          <label style="font-size:.8rem;margin-top:16px"><input type="checkbox" id="ocrMultiscale" checked> 多尺度</label>
        </div>
        <button class="btn btn-green" style="width:100%;margin-top:6px" id="btnMatch" disabled onclick="runMatch()">🖼 比對模板</button>
        <div id="matchResult" class="result-box">（尚未執行）</div>
      </div>
    </div>

    <!-- 右側圖片預覽 -->
    <div class="ocr-right">
      <img id="ocrPreview" src="" alt="請上傳截圖" style="max-width:100%;display:block">
    </div>
  </div>
</div>

<!-- ═══ Tab: 設定 ═══ -->
<div class="tab-content" id="tabSettings">
  <div class="panel" style="max-width:480px">
    <div class="card">
      <div class="card-title">腳本執行設定</div>
      <div class="field" style="margin-bottom:12px">
        <label>執行場次 (NUM)</label>
        <input id="cfgNum" type="number" min="1" value="500" style="width:140px">
      </div>
      <div class="field" style="margin-bottom:12px">
        <label>ADB 設備 ID #1（如 127.0.0.1:16384）</label>
        <input id="cfgDevice1" type="text" style="width:320px" placeholder="127.0.0.1:16384">
      </div>
      <div class="field" style="margin-bottom:16px">
        <label>ADB 設備 ID #2（如 127.0.0.1:16416）</label>
        <input id="cfgDevice2" type="text" style="width:320px" placeholder="127.0.0.1:16416">
      </div>
      <button class="btn btn-green" onclick="saveSettings()">💾 儲存設定</button>
      <span id="settingsSaved" style="font-size:.8rem;color:var(--green);margin-left:10px;display:none">✅ 已儲存</span>
    </div>
    <div class="card">
      <div class="card-title">說明</div>
      <ul style="font-size:.8rem;color:var(--muted);line-height:1.9;padding-left:16px">
        <li>「執行場次」會即時修改 <code>auto_fight.py</code> 的 <code>NUM</code> 值</li>
        <li>可同時設定兩組設備 ID，腳本啟動時會自動並行執行兩台模擬器</li>
        <li>設定儲存後，下次啟動腳本時才生效</li>
        <li>模板圖請存為 PNG；上傳後不需重啟伺服器</li>
      </ul>
    </div>
  </div>
</div>

<!-- ═══ 螢幕框選截圖 Modal ═══ -->
<div id="captureModal" class="capture-modal">
  <div class="capture-panel">
    <div class="capture-toolbar">
      <span class="capture-hint">請在畫面上拖曳框選要存成模板的範圍</span>
      <div style="flex:1"></div>
      <button class="btn btn-gray" type="button" onclick="resetCaptureSelection()">重設框選</button>
      <button class="btn btn-danger" type="button" onclick="closeCaptureModal()">取消</button>
      <button class="btn btn-green" type="button" id="btnSaveCapture" onclick="saveCapturedTemplate()" disabled>儲存截圖</button>
    </div>
    <div class="capture-preview" id="capturePreviewWrap">
      <span class="empty">尚未框選範圍</span>
    </div>
    <canvas id="captureCanvas"></canvas>
  </div>
</div>

<script>
// ════════════ Tab 切換 ════════════
function switchTab(id) {
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  const idx = ['tabScript','tabTemplates','tabOcr','tabSettings'].indexOf(id);
  document.querySelectorAll('.tab')[idx].classList.add('active');
  if (id === 'tabTemplates') loadTemplates();
  if (id === 'tabOcr')       loadOcrTemplates();
  if (id === 'tabSettings')  loadSettings();
}

// ════════════ 狀態輪詢 ════════════
let scriptRunning = false;
function appendLocalLog(msg, color = '#e67e22') {
  const box = document.getElementById('logBox');
  if (!box) return;
  const line = document.createElement('div');
  const now = new Date();
  const hh = String(now.getHours()).padStart(2, '0');
  const mm = String(now.getMinutes()).padStart(2, '0');
  const ss = String(now.getSeconds()).padStart(2, '0');
  line.textContent = `[${hh}:${mm}:${ss}] [前端] ${msg}`;
  line.style.color = color;
  box.appendChild(line);
  if (document.getElementById('autoScroll')?.checked) {
    box.scrollTop = box.scrollHeight;
  }
}

async function pollStatus() {
  try {
    const d = await fetch('/api/script/status').then(r=>r.json());
    scriptRunning = d.status === 'running';
  } catch(e) {
    scriptRunning = false;
    document.getElementById('statusDot').className = 'status-dot';
    document.getElementById('statusText').textContent = '控制台連線失敗';
    document.getElementById('btnStart').disabled = false;
    document.getElementById('btnStartSL').disabled = false;
    document.getElementById('btnStop').disabled  = true;
    return;
  }
  document.getElementById('statusDot').className = 'status-dot' + (scriptRunning ? ' running' : '');
  document.getElementById('statusText').textContent = scriptRunning ? '腳本執行中' : '閒置';
  document.getElementById('btnStart').disabled = scriptRunning;
  document.getElementById('btnStartSL').disabled = scriptRunning;
  document.getElementById('btnStop').disabled  = !scriptRunning;
}
setInterval(pollStatus, 2000);
pollStatus();

// ════════════ 腳本控制 ════════════
async function startScript() {
  const num      = parseInt(document.getElementById('cfgNum')?.value) || undefined;
  const deviceId1 = document.getElementById('cfgDevice1')?.value.trim() || undefined;
  try {
    const resp = await fetch('/api/script/start', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({num, device_id_1: deviceId1}),
    });
    const d = await resp.json();
    if (!d.ok) {
      appendLocalLog(`啟動失敗：${d.message}`, '#e74c3c');
      alert('啟動失敗：' + d.message);
    } else {
      appendLocalLog(d.message || '已送出啟動請求', '#2ecc71');
    }
  } catch (e) {
    const msg = `無法連線到控制台後端（${e?.message || e}）`;
    appendLocalLog(msg, '#e74c3c');
    alert(msg);
  }
  await pollStatus();
}

async function startSlScript() {
  const num      = parseInt(document.getElementById('cfgNum')?.value) || undefined;
  const deviceId1 = document.getElementById('cfgDevice1')?.value.trim() || undefined;
  const deviceId2 = document.getElementById('cfgDevice2')?.value.trim() || undefined;
  try {
    const resp = await fetch('/api/script/start-sl', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({num, device_id_1: deviceId1, device_id_2: deviceId2}),
    });
    const d = await resp.json();
    if (!d.ok) {
      appendLocalLog(`SL 啟動失敗：${d.message}`, '#e74c3c');
      alert('SL 啟動失敗：' + d.message);
    } else {
      appendLocalLog(d.message || '已送出 SL 啟動請求', '#9b59b6');
    }
  } catch (e) {
    const msg = `無法連線到控制台後端（${e?.message || e}）`;
    appendLocalLog(msg, '#e74c3c');
    alert(msg);
  }
  await pollStatus();
}

async function stopScript() {
  try {
    const resp = await fetch('/api/script/stop', {method:'POST'});
    const d = await resp.json();
    if (!d.ok) {
      appendLocalLog(`停止失敗：${d.message}`, '#e74c3c');
      alert('停止失敗：' + d.message);
    } else {
      appendLocalLog(d.message || '已送出停止請求', '#f39c12');
    }
  } catch (e) {
    const msg = `無法連線到控制台後端（${e?.message || e}）`;
    appendLocalLog(msg, '#e74c3c');
    alert(msg);
  }
  await pollStatus();
}

// ════════════ SSE Log ════════════
const logBox = document.getElementById('logBox');
const es = new EventSource('/api/script/logs/stream');
es.onmessage = ev => {
  const line = document.createElement('div');
  line.textContent = ev.data;
  // 根據內容上色
  if (ev.data.includes('[錯誤]') || ev.data.includes('[異常]') || ev.data.includes('失敗'))
    line.style.color = '#e74c3c';
  else if (ev.data.includes('通過') || ev.data.includes('成功') || ev.data.includes('完成'))
    line.style.color = '#2ecc71';
  else if (ev.data.includes('[驗證碼]'))
    line.style.color = '#f39c12';
  else if (ev.data.includes('[系統]'))
    line.style.color = '#9b59b6';
  logBox.appendChild(line);
  if (document.getElementById('autoScroll').checked)
    logBox.scrollTop = logBox.scrollHeight;
};

async function clearLogs() {
  await fetch('/api/script/logs/clear', {method:'POST'});
  logBox.innerHTML = '';
}

// ════════════ 模板管理 ════════════
let allTemplates = [];
let filterDir = '';
let captureState = {
  sourceCanvas: null,
  viewCanvas: null,
  viewCtx: null,
  scale: 1,
  selection: null,
  dragging: false,
  dragStart: null,
};

async function loadTemplates() {
  allTemplates = await fetch('/api/templates').then(r=>r.json());
  renderTemplates();
}

function filterTemplates(dir) {
  filterDir = dir;
  document.querySelectorAll('.tpl-filter').forEach(b => {
    b.classList.toggle('active', b.dataset.dir === dir || (dir==='' && b.dataset.dir===''));
  });
  renderTemplates();
}

function renderTemplates() {
  const grid = document.getElementById('templateGrid');
  const list = allTemplates.filter(t => filterDir === '' || t.subdir === filterDir);
  document.getElementById('tplCount').textContent = `共 ${list.length} 張`;
  grid.innerHTML = '';
  list.forEach(t => {
    const card = document.createElement('div');
    card.className = 'tpl-card';
    card.innerHTML = `
      <img src="/api/template-image?subdir=${encodeURIComponent(t.subdir)}&name=${encodeURIComponent(t.name)}"
           loading="lazy" alt="${t.name}">
      <div class="tpl-name" title="${t.rel}">${t.rel}</div>
      <button class="tpl-del" onclick="deleteTemplate(event,'${t.subdir}','${t.name}')">✕</button>`;
    grid.appendChild(card);
  });
}

async function deleteTemplate(ev, subdir, name) {
  ev.stopPropagation();
  if (!confirm(`確定刪除 ${name}？`)) return;
  const resp = await fetch('/api/delete-template',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body: JSON.stringify({subdir, name}),
  });
  const d = await resp.json();
  if (d.ok) loadTemplates();
  else alert('刪除失敗：' + d.error);
}

function handleUploadDrop(ev) {
  ev.preventDefault();
  document.getElementById('uploadZone').classList.remove('drag-over');
  handleUploadFiles(ev.dataTransfer.files);
}

async function handleUploadFiles(files) {
  if (!files.length) return;
  const subdir = document.getElementById('uploadSubdir').value;
  const fd = new FormData();
  fd.append('subdir', subdir);
  for (const f of files) fd.append('images', f);

  const resp = await fetch('/api/upload-template',{method:'POST',body:fd});
  const d = await resp.json();
  const el = document.getElementById('uploadResult');
  if (d.saved.length)  el.innerHTML = `✅ 已上傳：${d.saved.join(', ')}`;
  if (d.errors.length) el.innerHTML += `<br>❌ ${d.errors.join('; ')}`;
  loadTemplates();
}

// ════════════ 框選螢幕截圖上傳 ════════════
async function captureTemplateFromScreen() {
  if (!navigator.mediaDevices?.getDisplayMedia) {
    alert('此瀏覽器不支援螢幕擷取，請改用 Chrome/Edge。');
    return;
  }
  try {
    // 先讓使用者選擇要分享的畫面，再擷取一張靜態影像
    const stream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: false });
    const video = document.createElement('video');
    video.srcObject = stream;
    video.playsInline = true;
    video.muted = true;
    await video.play();
    await new Promise(resolve => {
      if (video.videoWidth > 0) resolve();
      else video.onloadedmetadata = resolve;
    });

    const src = document.createElement('canvas');
    src.width = video.videoWidth;
    src.height = video.videoHeight;
    src.getContext('2d').drawImage(video, 0, 0, src.width, src.height);

    stream.getTracks().forEach(t => t.stop());
    openCaptureModal(src);
  } catch (e) {
    alert(`螢幕擷取失敗：${e?.message || e}`);
  }
}

function openCaptureModal(sourceCanvas) {
  const modal = document.getElementById('captureModal');
  const canvas = document.getElementById('captureCanvas');
  const ctx = canvas.getContext('2d');
  captureState.sourceCanvas = sourceCanvas;
  captureState.viewCanvas = canvas;
  captureState.viewCtx = ctx;
  captureState.selection = null;
  captureState.dragging = false;
  captureState.dragStart = null;

  const maxW = Math.min(window.innerWidth * 0.9, 1150);
  const maxH = Math.min(window.innerHeight * 0.72, 760);
  const scale = Math.min(maxW / sourceCanvas.width, maxH / sourceCanvas.height, 1);
  captureState.scale = scale;
  canvas.width = Math.max(1, Math.round(sourceCanvas.width * scale));
  canvas.height = Math.max(1, Math.round(sourceCanvas.height * scale));

  canvas.onmousedown = onCaptureMouseDown;
  canvas.onmousemove = onCaptureMouseMove;
  window.onmouseup = onCaptureMouseUp;
  document.getElementById('btnSaveCapture').disabled = true;
  document.getElementById('capturePreviewWrap').innerHTML = '<span class="empty">尚未框選範圍</span>';
  redrawCaptureCanvas();
  modal.classList.add('show');
}

function closeCaptureModal() {
  document.getElementById('captureModal').classList.remove('show');
  const canvas = document.getElementById('captureCanvas');
  canvas.onmousedown = null;
  canvas.onmousemove = null;
  window.onmouseup = null;
}

function resetCaptureSelection() {
  captureState.selection = null;
  captureState.dragStart = null;
  captureState.dragging = false;
  document.getElementById('btnSaveCapture').disabled = true;
  document.getElementById('capturePreviewWrap').innerHTML = '<span class="empty">尚未框選範圍</span>';
  redrawCaptureCanvas();
}

function canvasPoint(ev) {
  const rect = captureState.viewCanvas.getBoundingClientRect();
  const x = Math.max(0, Math.min(captureState.viewCanvas.width, ev.clientX - rect.left));
  const y = Math.max(0, Math.min(captureState.viewCanvas.height, ev.clientY - rect.top));
  return { x, y };
}

function onCaptureMouseDown(ev) {
  const p = canvasPoint(ev);
  captureState.dragging = true;
  captureState.dragStart = p;
  captureState.selection = { x1: p.x, y1: p.y, x2: p.x, y2: p.y };
  redrawCaptureCanvas();
}

function onCaptureMouseMove(ev) {
  if (!captureState.dragging || !captureState.dragStart) return;
  const p = canvasPoint(ev);
  captureState.selection = {
    x1: Math.min(captureState.dragStart.x, p.x),
    y1: Math.min(captureState.dragStart.y, p.y),
    x2: Math.max(captureState.dragStart.x, p.x),
    y2: Math.max(captureState.dragStart.y, p.y),
  };
  redrawCaptureCanvas();
}

function onCaptureMouseUp() {
  if (!captureState.dragging) return;
  captureState.dragging = false;
  const s = captureState.selection;
  const valid = s && (s.x2 - s.x1) >= 6 && (s.y2 - s.y1) >= 6;
  document.getElementById('btnSaveCapture').disabled = !valid;
  renderCapturePreview(valid);
}

function redrawCaptureCanvas() {
  const { sourceCanvas, viewCanvas, viewCtx, selection } = captureState;
  if (!sourceCanvas || !viewCanvas || !viewCtx) return;
  viewCtx.clearRect(0, 0, viewCanvas.width, viewCanvas.height);
  viewCtx.drawImage(sourceCanvas, 0, 0, viewCanvas.width, viewCanvas.height);
  if (!selection) return;
  viewCtx.save();
  viewCtx.fillStyle = 'rgba(0,0,0,0.35)';
  viewCtx.fillRect(0, 0, viewCanvas.width, viewCanvas.height);
  const w = selection.x2 - selection.x1;
  const h = selection.y2 - selection.y1;
  viewCtx.clearRect(selection.x1, selection.y1, w, h);
  viewCtx.strokeStyle = '#e94560';
  viewCtx.lineWidth = 2;
  viewCtx.strokeRect(selection.x1 + 0.5, selection.y1 + 0.5, w, h);
  viewCtx.restore();
}

function getSelectionCropCanvas() {
  const s = captureState.selection;
  if (!s || !captureState.sourceCanvas) return null;
  const scale = captureState.scale || 1;
  const src = captureState.sourceCanvas;
  const x = Math.max(0, Math.round(s.x1 / scale));
  const y = Math.max(0, Math.round(s.y1 / scale));
  const w = Math.max(1, Math.round((s.x2 - s.x1) / scale));
  const h = Math.max(1, Math.round((s.y2 - s.y1) / scale));
  const crop = document.createElement('canvas');
  crop.width = w;
  crop.height = h;
  crop.getContext('2d').drawImage(src, x, y, w, h, 0, 0, w, h);
  return crop;
}

function renderCapturePreview(valid = true) {
  const wrap = document.getElementById('capturePreviewWrap');
  if (!valid) {
    wrap.innerHTML = '<span class="empty">框選太小，請重新選擇</span>';
    return;
  }
  const crop = getSelectionCropCanvas();
  if (!crop) {
    wrap.innerHTML = '<span class="empty">尚未框選範圍</span>';
    return;
  }
  wrap.innerHTML = `
    <span style="font-size:.78rem;color:var(--muted)">裁切預覽：</span>
    <img src="${crop.toDataURL('image/png')}" alt="裁切預覽">
    <span style="font-size:.75rem;color:var(--muted)">${crop.width} × ${crop.height}</span>
  `;
}

function sanitizeTemplateFileName(rawName) {
  const base = (rawName || '').trim().replace(/\.png$/i, '');
  if (!base) return '';
  // 避免路徑字元與特殊字元
  return base.replace(/[\\/:*?"<>|]/g, '_');
}

async function saveCapturedTemplate() {
  const s = captureState.selection;
  if (!s) return;
  const raw = prompt('請輸入模板檔名（不用加 .png）', 'new_template');
  if (raw === null) return;
  const safeName = sanitizeTemplateFileName(raw);
  if (!safeName) {
    alert('檔名不可為空。');
    return;
  }

  const crop = getSelectionCropCanvas();
  if (!crop) {
    alert('尚未選擇有效範圍。');
    return;
  }

  const blob = await new Promise(resolve => crop.toBlob(resolve, 'image/png'));
  if (!blob) {
    alert('截圖轉檔失敗，請重試。');
    return;
  }

  const subdir = document.getElementById('uploadSubdir').value;
  const fileName = `${safeName}.png`;
  try {
    const list = await fetch('/api/templates').then(r => r.json());
    const duplicated = (list || []).some(t => t.subdir === subdir && (t.name || '').toLowerCase() === fileName.toLowerCase());
    if (duplicated) {
      const folderLabel = subdir ? `pic/${subdir}` : 'pic';
      const shouldOverwrite = confirm(`${folderLabel} 已存在 ${fileName}，是否覆蓋？`);
      if (!shouldOverwrite) return;
    }
  } catch (e) {
    // 清單讀取失敗時，仍允許使用者繼續上傳
  }

  const fd = new FormData();
  fd.append('subdir', subdir);
  fd.append('images', blob, fileName);

  const resp = await fetch('/api/upload-template', { method: 'POST', body: fd });
  const d = await resp.json();
  const el = document.getElementById('uploadResult');
  if (d.saved?.length) {
    el.innerHTML = `✅ 已上傳：${d.saved.join(', ')}`;
    closeCaptureModal();
    loadTemplates();
    return;
  }
  el.innerHTML = `❌ 上傳失敗：${(d.errors || [d.error || '未知錯誤']).join('; ')}`;
}

// ════════════ OCR 測試 ════════════
let ocrFile = null;
let ocrParams = {};

async function loadOcrTemplates() {
  const list = await fetch('/api/templates').then(r=>r.json());
  const sel  = document.getElementById('ocrTplSelect');
  sel.innerHTML = '';
  list.forEach(t => {
    const opt = document.createElement('option');
    opt.value = t.rel;
    opt.textContent = t.rel;
    sel.appendChild(opt);
  });
}

function handleOcrDrop(ev) {
  ev.preventDefault();
  ev.currentTarget.classList.remove('drag-over');
  handleOcrFile(ev.dataTransfer.files[0]);
}

function handleOcrFile(file) {
  if (!file) return;
  ocrFile = file;
  ocrUploadAndDraw();
}

function getOcrParams() {
  const ids = ['ref_w','ref_h','cap_x1','cap_y1','cap_x2','cap_y2','btn_x1','btn_y1','btn_x2','btn_y2'];
  const p = {};
  ids.forEach(id => p[id] = document.getElementById(id)?.value || 0);
  return p;
}

async function ocrUploadAndDraw() {
  if (!ocrFile) return;
  const fd = new FormData();
  fd.append('image', ocrFile);
  const p = getOcrParams();
  Object.entries(p).forEach(([k,v]) => fd.append(k, v));
  ocrParams = p;
  const resp = await fetch('/api/overlay',{method:'POST',body:fd});
  const d = await resp.json();
  if (d.error) { alert(d.error); return; }
  document.getElementById('ocrPreview').src = d.overlay;
  document.getElementById('ocrImgInfo').textContent = `${d.width} × ${d.height}`;
  document.getElementById('btnOcr').disabled   = false;
  document.getElementById('btnMatch').disabled = false;
  document.getElementById('ocrCropWrap').innerHTML =
    `<div style="font-size:.73rem;color:var(--muted);margin-bottom:3px">驗證碼裁切：</div>
     <img src="${d.captcha_crop}" style="max-height:70px;border:1px solid var(--orange);border-radius:3px">`;
}

async function runOcr() {
  document.getElementById('ocrResult').textContent = 'OCR 執行中…';
  const resp = await fetch('/api/ocr',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(ocrParams)});
  const d = await resp.json();
  if (d.error) { document.getElementById('ocrResult').textContent = '❌ ' + d.error; return; }
  const badge = d.success
    ? `<span class="badge badge-ok">✅ 識別成功：${d.digits}</span>`
    : `<span class="badge badge-fail">❌ 未識別出 4 位數字</span>`;
  document.getElementById('ocrResult').innerHTML = badge + '\n\n── 記錄 ──\n' + d.logs.join('\n');
}

async function runMatch() {
  const tpl = document.getElementById('ocrTplSelect').value;
  if (!tpl) { alert('請選擇模板'); return; }
  document.getElementById('matchResult').textContent = '比對中…';
  const resp = await fetch('/api/match',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
    template: tpl,
    multiscale: document.getElementById('ocrMultiscale').checked,
    threshold:  parseFloat(document.getElementById('ocrThreshold').value),
  })});
  const d = await resp.json();
  if (d.error) { document.getElementById('matchResult').textContent = '❌ ' + d.error; return; }
  document.getElementById('ocrPreview').src = d.result_img;
  const badge = d.found
    ? `<span class="badge badge-ok">✅ 找到</span>`
    : `<span class="badge badge-fail">❌ 未找到</span>`;
  document.getElementById('matchResult').innerHTML =
    badge + `\n模板：${tpl}\n相似度：${d.similarity}\n座標：(${d.x}, ${d.y})\n模板尺寸：${d.tpl_w}×${d.tpl_h}`;
}

// ════════════ 設定 ════════════
async function loadSettings() {
  const d = await fetch('/api/settings').then(r=>r.json());
  document.getElementById('cfgNum').value    = d.num;
  document.getElementById('cfgDevice1').value = d.device_id_1 || d.device_id || '';
  document.getElementById('cfgDevice2').value = d.device_id_2 || '';
}

async function saveSettings() {
  const num      = parseInt(document.getElementById('cfgNum').value);
  const deviceId1 = document.getElementById('cfgDevice1').value.trim();
  const deviceId2 = document.getElementById('cfgDevice2').value.trim();
  await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
    body: JSON.stringify({num, device_id_1: deviceId1, device_id_2: deviceId2})});
  const el = document.getElementById('settingsSaved');
  el.style.display = 'inline';
  setTimeout(()=>el.style.display='none', 2000);
}

// 預先載入設定到 Header 的啟動按鈕用
loadSettings();
</script>
</body>
</html>
"""

# ════════════════════════════════════════════════════════════
# 入口
# ════════════════════════════════════════════════════════════

def open_browser():
    time.sleep(1)
    webbrowser.open("http://127.0.0.1:5050")


if __name__ == "__main__":
    print("=" * 50)
    print("Under Dark 控制台  http://127.0.0.1:5050")
    print("按 Ctrl+C 停止")
    print("=" * 50)
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host="127.0.0.1", port=5050, debug=False, threaded=True)
