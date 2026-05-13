"""
Under Dark 自動戰鬥腳本
流程：進入 → 點擊戰鬥 → 驗證碼處理 → 等待戰鬥結束 → 領取獎勵
"""

import sys
import os
import time
import io
import argparse
import subprocess
import threading
import queue
from collections import Counter

import cv2
import numpy as np

from utils import (
    log,
    set_telegram_log_forward,
    connect_device,
    take_screenshot,
    load_and_match,
    click_by_image,
    image_exists,
    wait_for_image,
    wait_and_click,
    wait_and_click_any,
    wait_for_disappear,
    wait_for_disappear_any,
    click_fixed,
    CACHE_DIR,
    send_telegram,
)

# ========== 配置區 ==========
# 設備 ID（從 settings.json 讀取）
from settings import get_default_device, get_all_devices

DEVICE_ID = get_default_device()
NUM = 5000

# 圖片路徑（相對於 pic 目錄）
IMG = {
    'enter': "enter.png",
    'enter_fight': "enter_fight.png",
    'adx2': "adx2.png",             # 簡體等：圖上為「獎勵 x2」類文案
    'adx2_tw': "adx2_tw.png",       # 繁體：畫面上為「廣告 x2」時請截此圖放入 pic（必須自行截圖）
    'confirm_fight': "confirm_fight.png",
    'confirm_rewards': "confirm_rewards.png",
    'captcha': "captcha.png",       # 驗證碼彈窗特徵圖（需要截圖提供）
    'pet_full': "pet_full.png",     # 寵物道具已滿提示
    'enter_backpack': "enter_backpack.png",  # 進入背包按鈕
    'pet_bag': "pet_bag.png",       # 背包內寵物分頁
    'composite': "composite.png",   # 合成按鈕
    'composite_confirm': "composite_confirm.png",  # 合成確認
    'left': "left.png",             # 合成品質左切換
    'legendary_quality': "legendary_quality.png",  # 傳說品質樣式
    'home': "home.png",             # 回主頁按鈕
}

# 廣告倍數按鈕：簡繁文案不同，模板需分開；同螢幕只會出現其中一種
ADX2_TEMPLATES = [IMG['adx2'], IMG['adx2_tw']]
ADX2_LABELS = ["獎勵x2(簡)", "廣告x2(繁)"]

# 驗證碼區域配置（根據 Under Dark 遊戲實際介面調整）
#
# --- 校準步驟（位置錯時必做）---
# 1) 出現驗證碼時看 log 的全螢幕寬高（例如 900x1600），或用 pic/cache/screen.png。
# 2) CAPTCHA_REF_WIDTH / HEIGHT 必須與「你量座標時用的那一張截圖」相同寬高。
# 3) 用小畫家等開 screen.png：游標在四位數字「左上角」看狀態列 (x1,y1)，「右下角」 (x2,y2)。
# 4) 設 CAPTCHA_REGION = (x1, y1, x2, y2)。y2-y1 建議至少約 70～120px，勿只切到細線。
# 5) 數字區應在數字鍵盤「上方」；若改動較大，請用同方式重設 BUTTON_REGION（鍵盤外框）。
#
# 參考解析度請與 adb 截圖「寬高一致」（例如 900x1600），再在同解析度全螢幕圖上量 CAPTCHA_REGION、BUTTON_REGION。
# 下列數值為由舊版 450x990 換算到 900x1600 的約略值；若仍「辨識對、點偏」請在截圖上重框鍵盤外框。
CAPTCHA_REF_WIDTH = 900
# 須與 adb 截圖高度一致；誤填 1500、實際 1600 會導致整排按鍵上下錯位
CAPTCHA_REF_HEIGHT = 1600
CAPTCHA_REGION = (220, 645, 660, 771)
# BUTTON_REGION = (60, 795, 820, 1055)
BUTTON_REGION = (60, 750, 820, 1010)
# 在 BUTTON_REGION 四邊內縮再均分 5×2（外框含陰影時可略調；與 stretch 併用時勿過大以免「愈縮愈偏」）
BUTTON_GRID_INSET_RATIO = 0.0
# 鍵盤座標必須與下方 scale_region（CAPTCHA 裁切）一致：「stretch」= 長寬各自縮放（預設，強烈建議）
# 「uniform」= 等比例置中，僅在整支腳本改為同一套 uniform 映射時才用；否則鍵盤與裁切區會錯位
BUTTON_COORD_MODE = "stretch"
# 辨識正確但點偏時：微調（實際螢幕像素，右為正、下為正）
BUTTON_TAP_OFFSET_X = 0
BUTTON_TAP_OFFSET_Y = 0
BUTTON_SECOND_ROW_EXTRA_Y = 0
# 每格「幾何中心」再微調（像素）：綠框已對齊鍵盤但紅點落在鍵的左下／右上時，可右移(+)、上移(-Y)
BUTTON_CELL_NUDGE_PX_X = 0
BUTTON_CELL_NUDGE_PX_Y = 0
# 連續點數字鍵間隔（秒），過快可能被遊戲忽略
CAPTCHA_DIGIT_CLICK_DELAY = 0.28
# 尋找 confirm.png 時僅搜尋螢幕「由上往下」超過此比例的高度（0~1），避免誤匹配頂部其他「確認」字樣
CONFIRM_SEARCH_MIN_Y_RATIO = 0.35
# 寵物滿包判定與合成參數
PET_FULL_THRESHOLD = 0.80
PET_FLOW_THRESHOLD = 0.78
PET_COMPOSITE_MAX_ROUNDS = 12
PET_QUALITY_THRESHOLD = 0.76
PET_QUALITY_MAX_SHIFT = 8
pet_compose_pending = False


# ========== 驗證碼處理區 ==========
def _warn_captcha_ref_mismatch(screen_w, screen_h):
    """CAPTCHA_REF 必須與 adb 截圖寬高一致，否則換算後按鍵會整片偏移（常見錯：高度寫 1500 實際 1600）。"""
    if screen_w == CAPTCHA_REF_WIDTH and screen_h == CAPTCHA_REF_HEIGHT:
        return
    log(
        f"[驗證碼][重要] 截圖 {screen_w}x{screen_h} 與 CAPTCHA_REF {CAPTCHA_REF_WIDTH}x{CAPTCHA_REF_HEIGHT} 不一致，"
        "按鍵／裁切會錯。請把 CAPTCHA_REF_WIDTH、CAPTCHA_REF_HEIGHT 改成與 log「全螢幕」完全相同。"
    )


def _warn_button_coord_mode_mismatch():
    """uniform 與 CAPTCHA 的 stretch 裁切並存時，長寬比一不一致就會整片錯位。"""
    if (BUTTON_COORD_MODE or "stretch").lower() != "uniform":
        return
    log(
        "[驗證碼][注意] BUTTON_COORD_MODE=uniform 但 CAPTCHA 裁切仍為 stretch；"
        "除非整支腳本改同一套映射，否則建議改回 stretch。"
    )


def _save_captcha_overlay(img_bgr, screen_w, screen_h):
    """在全螢幕截圖上畫出數字區、鍵盤框與預設點擊點，存成 captcha_overlay.png 方便對照"""
    try:
        vis = img_bgr.copy()
        cx1, cy1, cx2, cy2 = scale_region(CAPTCHA_REGION, screen_w, screen_h)
        cv2.rectangle(vis, (cx1, cy1), (cx2, cy2), (0, 165, 255), 2)  # 數字 OCR 區
        b = _button_outer_rect_float(screen_w, screen_h)
        bx1, by1, bx2, by2 = int(round(b[0])), int(round(b[1])), int(round(b[2])), int(round(b[3]))
        cv2.rectangle(vis, (bx1, by1), (bx2, by2), (0, 255, 0), 2)  # 鍵盤外框
        gi = _button_grid_rect_float(screen_w, screen_h)
        g1, g2, g3, g4 = int(round(gi[0])), int(round(gi[1])), int(round(gi[2])), int(round(gi[3]))
        if float(BUTTON_GRID_INSET_RATIO) > 0:
            cv2.rectangle(vis, (g1, g2), (g3, g4), (255, 220, 0), 2)  # 實際均分網格用區（內縮後）
        pos = get_button_positions_for_screen(screen_w, screen_h)
        for d, (px, py) in pos.items():
            try:
                cv2.drawMarker(
                    vis,
                    (px, py),
                    (0, 0, 255),
                    markerType=cv2.MARKER_CROSS,
                    markerSize=22,
                    thickness=2,
                )
            except Exception:
                cv2.circle(vis, (px, py), 12, (0, 0, 255), 2)
            cv2.putText(
                vis,
                d,
                (px + 14, py - 12),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (0, 255, 255),
                2,
            )
        out = os.path.join(CACHE_DIR, "captcha_overlay.png")
        cv2.imwrite(out, vis)
        log(
            "[驗證碼] 已寫入 captcha_overlay.png（橘=數字區、綠=BUTTON_REGION、黃=內縮後均分區、紅十字=點擊）"
        )
    except Exception as e:
        log(f"[驗證碼] 無法產生對照圖: {e}")


def scale_region_float(region_xyxy, screen_w, screen_h):
    """與 CAPTCHA_REGION 裁切相同：長寬各自依參考解析度縮放（stretch），回傳浮點矩形。"""
    x1, y1, x2, y2 = region_xyxy
    rw, rh = CAPTCHA_REF_WIDTH, CAPTCHA_REF_HEIGHT
    if rw <= 0 or rh <= 0:
        return float(x1), float(y1), float(x2), float(y2)
    sx = screen_w / float(rw)
    sy = screen_h / float(rh)
    return (x1 * sx, y1 * sy, x2 * sx, y2 * sy)


def scale_region(region_xyxy, screen_w, screen_h):
    """依參考解析度將矩形映射到目前截圖（與鍵盤 stretch 映射一致）"""
    x1, y1, x2, y2 = scale_region_float(region_xyxy, screen_w, screen_h)
    return (
        int(round(x1)),
        int(round(y1)),
        int(round(x2)),
        int(round(y2)),
    )


def _button_outer_rect_float(screen_w, screen_h):
    """
    將參考座標下的 BUTTON_REGION 映到目前螢幕（浮點，供取中心點）。
    stretch（預設）：與 scale_region 完全相同，確保橘框裁切與綠框鍵盤同一座標系。
    uniform：等比例置中；若 CAPTCHA 仍用 stretch 裁切，兩者會不一致，僅供特殊用途。
    """
    rw, rh = CAPTCHA_REF_WIDTH, CAPTCHA_REF_HEIGHT
    x1r, y1r, x2r, y2r = BUTTON_REGION
    if rw <= 0 or rh <= 0:
        return float(x1r), float(y1r), float(x2r), float(y2r)
    if (BUTTON_COORD_MODE or "stretch").lower() == "uniform":
        s = min(screen_w / float(rw), screen_h / float(rh))
        ox = (screen_w - rw * s) * 0.5
        oy = (screen_h - rh * s) * 0.5
        return (
            ox + x1r * s,
            oy + y1r * s,
            ox + x2r * s,
            oy + y2r * s,
        )
    return scale_region_float(BUTTON_REGION, screen_w, screen_h)


def _button_grid_rect_float(screen_w, screen_h):
    """取得用於 5×2 均分的矩形（可相對 BUTTON_REGION 內縮，使落點在按鍵正中）"""
    x1, y1, x2, y2 = _button_outer_rect_float(screen_w, screen_h)
    r = float(BUTTON_GRID_INSET_RATIO)
    if r <= 0:
        return x1, y1, x2, y2
    w = x2 - x1
    h = y2 - y1
    dx = w * r
    dy = h * r
    return x1 + dx, y1 + dy, x2 - dx, y2 - dy


def get_button_positions_for_screen(screen_w, screen_h):
    """依鍵盤外框（可內縮）計算 5x2 數字鍵格中心座標（含微調偏移）"""
    x1, y1, x2, y2 = _button_grid_rect_float(screen_w, screen_h)
    btn_w = (x2 - x1) / 5.0
    btn_h = (y2 - y1) / 2.0
    ox = float(BUTTON_TAP_OFFSET_X)
    oy = float(BUTTON_TAP_OFFSET_Y)
    row2_extra = float(BUTTON_SECOND_ROW_EXTRA_Y)
    nx = int(BUTTON_CELL_NUDGE_PX_X)
    ny = int(BUTTON_CELL_NUDGE_PX_Y)
    positions = {}
    for i, digit in enumerate(["1", "2", "3", "4", "5"]):
        cx = x1 + btn_w * (i + 0.5) + ox
        cy = y1 + btn_h * 0.5 + oy
        positions[digit] = (int(round(cx)) + nx, int(round(cy)) + ny)
    for i, digit in enumerate(["6", "7", "8", "9", "0"]):
        cx = x1 + btn_w * (i + 0.5) + ox
        cy = y1 + btn_h * 1.5 + row2_extra + oy
        positions[digit] = (int(round(cx)) + nx, int(round(cy)) + ny)
    return positions


def _captcha_variants_bgr(bgr):
    """多種放大／灰階／二值化／反色，供 ddddocr（白字深底時反色常較易辨識）"""
    out = []

    def add(im, scale):
        ih, iw = im.shape[:2]
        nh, nw = max(8, int(ih * scale)), max(8, int(iw * scale))
        out.append(cv2.resize(im, (nw, nh), interpolation=cv2.INTER_CUBIC))

    # 白邊，避免字貼邊
    pad = cv2.copyMakeBorder(bgr, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=(0, 0, 0))
    add(pad, 2.0)
    add(pad, 3.0)

    add(bgr, 2.0)
    add(bgr, 3.0)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    add(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), 2.0)
    add(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), 3.0)

    # 白字黑底 → 反成黑字白底，貼近 ddddocr 常見訓練分佈
    inv = cv2.bitwise_not(gray)
    add(cv2.cvtColor(inv, cv2.COLOR_GRAY2BGR), 2.5)
    add(cv2.cvtColor(inv, cv2.COLOR_GRAY2BGR), 3.0)

    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    cl = clahe.apply(gray)
    add(cv2.cvtColor(cl, cv2.COLOR_GRAY2BGR), 3.0)

    _, otsu_bin = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    _, otsu_inv = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    _, fixed_bin = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY)
    for bn in (otsu_bin, otsu_inv, fixed_bin):
        g3 = cv2.resize(bn, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        out.append(cv2.cvtColor(g3, cv2.COLOR_GRAY2BGR))
    inv2 = cv2.bitwise_not(otsu_bin)
    g4 = cv2.resize(inv2, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    out.append(cv2.cvtColor(g4, cv2.COLOR_GRAY2BGR))
    return out


_OCR_INSTANCES = {}


def _new_dddd_ocr(beta):
    """重用實例，減少重複初始化與廣告輸出"""
    if beta in _OCR_INSTANCES:
        return _OCR_INSTANCES[beta]
    import ddddocr

    try:
        inst = ddddocr.DdddOcr(beta=beta, show_ad=False)
    except TypeError:
        inst = ddddocr.DdddOcr(beta=beta)
    _OCR_INSTANCES[beta] = inst
    return inst


def _normalize_ocr_digits(text):
    """將 OCR 字串正規化為只含數字；常見 o/O/〇/○ 視為 0。"""
    if not text:
        return ""
    # 只做保守替換：明確與 0 高度混淆的字元
    trans = str.maketrans({
        "o": "0",
        "O": "0",
        "Ｏ": "0",
        "〇": "0",
        "○": "0",
    })
    s = str(text).translate(trans)
    return "".join(c for c in s if c.isdigit())


def check_captcha():
    """檢查是否出現驗證碼彈窗"""
    found, _, _, sim = load_and_match(IMG['captcha'], threshold=0.95)
    log(f"[驗證碼偵測] 相似度: {sim:.3f}, 結果: {'有驗證碼' if found else '無驗證碼'}")
    return found


def recognize_digits(captcha_img):
    """使用 ddddocr：多變體輪詢 + 投票；僅在取得 4 位數時回傳。"""
    try:
        from PIL import Image
    except ImportError:
        log("[驗證碼] 需要安裝 Pillow: pip install Pillow")
        return None

    if captcha_img is None or captcha_img.size == 0:
        log("[驗證碼] 裁切區域為空（請檢查 CAPTCHA_REGION 是否依參考解析度設定並需 scale_region）")
        return None

    def bgr_to_png_bytes(bgr_im):
        rgb = cv2.cvtColor(bgr_im, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(rgb)
        bio = io.BytesIO()
        pil.save(bio, format="PNG")
        return bio.getvalue()

    try:
        variants = _captcha_variants_bgr(captcha_img)
        hits = []
        last_raw = []
        rank = 0
        for beta in (True, False):
            ocr = _new_dddd_ocr(beta)
            for var in variants:
                try:
                    text = ocr.classification(bgr_to_png_bytes(var))
                except Exception as ex:
                    log(f"[驗證碼] OCR 呼叫異常: {ex}")
                    rank += 1
                    continue
                last_raw.append((beta, rank, text))
                d = _normalize_ocr_digits(text)
                if len(d) == 4:
                    hits.append((rank, d))
                rank += 1

        if hits:
            cnt = Counter(x[1] for x in hits)
            best_n = max(cnt.values())
            finalists = [dig for dig, n in cnt.items() if n == best_n]
            if len(finalists) == 1:
                chosen = finalists[0]
            else:
                first = {}
                for r, d in hits:
                    if d in finalists and d not in first:
                        first[d] = r
                chosen = min(finalists, key=lambda x: first[x])
                log(f"[驗證碼] 多候選平票 {finalists}，依管線優先: {chosen}")
            log(f"[驗證碼] 識別結果: {chosen}")
            return chosen

        log(f"[驗證碼] 首次識別 0 組 4 位數，嘗試備用方法...（共 {len(variants)} 種變體）")
        for beta, r, text in last_raw[-8:]:
            norm = _normalize_ocr_digits(text)
            log(f"[驗證碼] OCR 原始 beta={beta} #{r}: {repr(text)} -> norm={repr(norm)}")
        log("[驗證碼] 仍失敗：請對照 pic/cache/captcha_debug.png 調整 CAPTCHA_REGION / CAPTCHA_REF_*")
        return None
    except ImportError:
        log("[驗證碼] 需要安裝 ddddocr: pip install ddddocr")
        return None


def _click_confirm_in_roi_once(threshold):
    """
    只在螢幕下方 ROI 內匹配 confirm.png，避免全螢幕搜尋點到 y≈200 的錯誤 UI。
    傳回 True=已點擊
    """
    screen_path = take_screenshot()
    img = cv2.imread(screen_path)
    if img is None:
        return False
    h, w = img.shape[:2]
    y0 = int(h * float(CONFIRM_SEARCH_MIN_Y_RATIO))
    roi = img[y0:h, 0:w]
    roi_path = os.path.join(CACHE_DIR, "confirm_search_roi.png")
    cv2.imwrite(roi_path, roi)
    found, rx, ry, sim = load_and_match(
        "confirm.png",
        threshold=threshold,
        screen_path=roi_path,
        multiscale=True,
    )
    if not found or rx is None or ry is None:
        return False
    tx, ty = int(rx), int(ry) + y0
    click_fixed(tx, ty, delay=0.35, name=f"驗證碼確認(相似度 {sim:.2f}, y>={y0})")
    return True


def _click_captcha_confirm():
    """確認鈕：下半螢幕 ROI 內多閾值匹配（避免誤點頂部確認）"""
    time.sleep(0.35)
    for th in (0.78, 0.72, 0.66, 0.60):
        if _click_confirm_in_roi_once(th):
            return True
        time.sleep(0.12)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if _click_confirm_in_roi_once(0.58):
            return True
        time.sleep(0.35)
    log(
        "[驗證碼] 下半螢幕未找到 confirm.png；請裁「僅驗證碼彈窗上的確認」為模板，或調 CONFIRM_SEARCH_MIN_Y_RATIO"
    )
    return False


def solve_captcha(debug=False):
    """識別驗證碼並點擊數字（純 ADB）。CAPTCHA_REGION 為參考解析度下座標，必須 scale 到實際螢幕。"""
    log("[驗證碼] 開始識別...")

    screen_path = take_screenshot()
    img = cv2.imread(screen_path)
    if img is None:
        log("[驗證碼] 無法讀取截圖")
        return False

    ih, iw = img.shape[:2]
    _warn_captcha_ref_mismatch(iw, ih)
    _warn_button_coord_mode_mismatch()

    x1, y1, x2, y2 = scale_region(CAPTCHA_REGION, iw, ih)
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(iw, x2), min(ih, y2)
    if x2 <= x1 or y2 <= y1:
        log(f"[驗證碼] 裁切無效 ({x1},{y1})-({x2},{y2})，螢幕 {iw}x{ih}；請檢查 CAPTCHA_REGION")
        return False

    captcha_img = img[y1:y2, x1:x2]
    if debug:
        _save_captcha_overlay(img, iw, ih)
        cv2.imwrite(os.path.join(CACHE_DIR, "captcha_debug.png"), captcha_img)
        bxf = _button_outer_rect_float(iw, ih)
        bx1, by1, bx2, by2 = (int(round(bxf[0])), int(round(bxf[1])), int(round(bxf[2])), int(round(bxf[3])))
        log(
            f"[驗證碼] 已寫入 captcha_debug.png，裁切 {captcha_img.shape[1]}x{captcha_img.shape[0]}，"
            f"全螢幕 {iw}x{ih}，參考 {CAPTCHA_REF_WIDTH}x{CAPTCHA_REF_HEIGHT}"
        )
        log(
            f"[驗證碼] 鍵盤外框 ({bx1},{by1})-({bx2},{by2})，模式={BUTTON_COORD_MODE}，"
            f"網格內縮 BUTTON_GRID_INSET_RATIO={BUTTON_GRID_INSET_RATIO}；"
            "紅十字應在每鍵正中，否則調內縮或 BUTTON_CELL_NUDGE_PX_*"
        )

    digits = recognize_digits(captcha_img)
    if not digits or len(digits) != 4:
        log("[驗證碼] 辨識失敗")
        return False

    positions = get_button_positions_for_screen(iw, ih)
    ddelay = float(CAPTCHA_DIGIT_CLICK_DELAY)
    for digit in digits:
        if digit in positions:
            x, y = positions[digit]
            click_fixed(x, y, delay=ddelay, name=f"數字{digit}")
        else:
            log(f"[驗證碼] 未知字元: {digit}")
            return False

    log("[驗證碼] 輸入完成")
    if not _click_captcha_confirm():
        log("[驗證碼] 找不到確認鈕，請更新 pic/confirm.png")
        return False
    return True


def solve_captcha_if_needed(max_retry=3, fight_no=None):
    """
    偵測並解決驗證碼，驗證是否通過
    fight_no: 當前第幾場（由 run_fight 傳入），用於 Telegram 通知
    傳回: True=通過/無驗證碼, False=失敗
    """
    # 場次標籤，若未提供則不顯示
    tag = f"（第 {fight_no}/{NUM} 場）" if fight_no is not None else ""

    time.sleep(0.5)  # 等待彈窗載入（原 1.0s，縮短為 0.5s）

    for attempt in range(max_retry):
        if not check_captcha():
            log("[驗證碼] 未偵測到驗證碼，繼續執行")
            return True

        log(f"[驗證碼] 偵測到驗證碼，嘗試第 {attempt + 1}/{max_retry} 次")
        if not solve_captcha(debug=True):
            log("[驗證碼] 本次嘗試未完成（裁切／OCR／確認鈕）")
        time.sleep(0.5)  # 等遊戲處理驗證結果（原 1.0s）

        # 驗證碼彈窗消失 = 通過
        if not check_captcha():
            log("[驗證碼] 驗證通過")
            send_telegram(f"驗證碼通過{tag}")
            return True
        else:
            log("[驗證碼] 驗證未通過，準備重試...")
            time.sleep(0.3)  # 重試前短暫等待（原 0.5s）
            send_telegram(f"驗證碼重試 {attempt + 1}/{max_retry} 次{tag}")

    log(f"[驗證碼] 重試 {max_retry} 次後仍未通過")
    send_telegram(f"驗證碼重試 {max_retry} 次後仍未通過{tag}")
    return False


def _click_if_exists(img_key, threshold=0.8, delay=0.35, name="", multiscale=True):
    """畫面存在才點擊；避免流程中斷。"""
    if not image_exists(IMG[img_key], threshold=threshold):
        return False
    return click_by_image(
        IMG[img_key],
        threshold=threshold,
        delay=delay,
        name=name or img_key,
        multiscale=multiscale,
    )


def handle_pet_full_if_needed():
    """若偵測到寵物道具已滿，依指定流程進背包合成並回主頁。"""
    if not image_exists(IMG['pet_full'], threshold=PET_FULL_THRESHOLD):
        return False

    log("[寵物] 偵測到寵物道具已滿，開始自動合成流程（home -> 背包 -> 寵物 -> 合成）")
    send_telegram("偵測到寵物道具已滿，開始自動合成")

    # 先確保在主頁（你提供的流程第一步）
    _click_if_exists('home', threshold=PET_FLOW_THRESHOLD, delay=0.5, name="回主頁")

    # 優先點「進入背包」，若沒抓到則嘗試先點滿包提示本體
    opened = click_by_image(
        IMG['enter_backpack'],
        threshold=PET_FLOW_THRESHOLD,
        delay=0.6,
        name="進入背包",
        multiscale=True,
    )
    if not opened:
        _click_if_exists('pet_full', threshold=PET_FULL_THRESHOLD, delay=0.4, name="寵物已滿提示")
        opened = wait_and_click(
            IMG['enter_backpack'],
            timeout=6,
            interval=0.5,
            threshold=PET_FLOW_THRESHOLD,
            delay=0.6,
            name="進入背包",
            multiscale=True,
        )
    if not opened:
        log("[寵物] 未找到進入背包按鈕，略過本次合成")
        return False

    # 切到寵物分頁
    _click_if_exists('pet_bag', threshold=PET_FLOW_THRESHOLD, delay=0.45, name="寵物分頁")

    merged_any = False
    for i in range(PET_COMPOSITE_MAX_ROUNDS):
        if not image_exists(IMG['composite'], threshold=PET_FLOW_THRESHOLD):
            break

        if not click_by_image(
            IMG['composite'],
            threshold=PET_FLOW_THRESHOLD,
            delay=0.45,
            name=f"寵物合成#{i + 1}",
            multiscale=True,
        ):
            break

        # 進入合成視窗後，先把品質切到「傳說」（left 用來調整品質）
        shifted = False
        for _ in range(PET_QUALITY_MAX_SHIFT):
            if image_exists(IMG['legendary_quality'], threshold=PET_QUALITY_THRESHOLD):
                shifted = True
                break
            if not _click_if_exists('left', threshold=PET_FLOW_THRESHOLD, delay=0.22, name="調整品質"):
                break
        if not shifted and image_exists(IMG['legendary_quality'], threshold=PET_QUALITY_THRESHOLD):
            shifted = True
        if not shifted:
            log("[寵物] 未偵測到傳說品質樣式，仍嘗試合成確認")

        # 合成確認按鈕可能有淡入，給一次等待窗口
        wait_and_click(
            IMG['composite_confirm'],
            timeout=4,
            interval=0.35,
            threshold=0.72,
            delay=0.55,
            name="合成確認",
            multiscale=True,
        )
        merged_any = True
        time.sleep(0.25)

    if merged_any:
        log("[寵物] 自動合成完成，準備回主頁並返回戰鬥流程")
        send_telegram("寵物合成完成，繼續戰鬥")
    else:
        log("[寵物] 已進入背包，但本次沒有可執行的合成")

    # 用 home 回主頁，避免把 left 誤當返回鍵
    _click_if_exists('home', threshold=PET_FLOW_THRESHOLD, delay=0.6, name="回主頁")

    return True


def _detect_pet_full_once(stage=""):
    """單次檢查是否出現寵物滿包提示；用於本輪內先記錄，下輪再處理。"""
    hit = image_exists(IMG['pet_full'], threshold=PET_FULL_THRESHOLD)
    if hit:
        tag = f"（{stage}）" if stage else ""
        log(f"[寵物] 本輪偵測到滿包{tag}，將在下一次戰鬥前處理")
    return hit


# ========== 主流程區 ==========
def run_fight(fight_no=None):
    """執行一次完整的戰鬥流程。回傳 (success, pet_full_seen_in_round)。
    fight_no: 當前場次編號，由 main() 傳入，供 Telegram 通知顯示
    """

    # 進入前先處理上輪殘留的寵物滿包（偵測到才執行，否則跳過）
    if _detect_pet_full_once("開戰前"):
        handle_pet_full_if_needed()

    # 1. 等待並點擊入口（enter.png 實際為「地下城」按鈕截圖；須與當前解析度一致）
    log("[步驟1] 等待並點擊進入")
    if not wait_and_click(
        IMG['enter'],
        timeout=120,
        interval=1,
        name="地下城入口",
        multiscale=True,
        heartbeat_sec=5,
    ):
        log("[錯誤] 步驟1 逾時：未匹配到 enter.png，請確認已在遊戲主介面或重新截取模板圖")
        send_telegram("步驟1 逾時：未匹配到 enter.png，請確認已在遊戲主介面或重新截取模板圖")
        return False, False

    # 進入列表後常有轉場/載入，立刻截圖可能對到過渡畫面，相似度會偏低
    # 0.2s 已足夠避開第一幀過渡畫面；若偶有誤匹配可調回 0.5
    time.sleep(0.2)

    # 2. 等待進入戰鬥按鈕出現並點擊（timeout=0 表示一直等；步驟1成功後才會很快出現）
    # enter_fight.png 建議只裁「進入」二字或固定邊框，勿包含體力數字（5/10 等會變，分數易掉到 0.5 以下）
    log("[步驟2] 等待進入戰鬥按鈕...")
    wait_and_click(
        IMG['enter_fight'],
        timeout=0,
        interval=0.5,
        name="進入戰鬥",
        threshold=0.9,
        multiscale=True,
        heartbeat_sec=5,
    )

    pet_full_seen = _detect_pet_full_once("進入戰鬥後")

    # 3. 驗證碼判斷與輸入
    log("[步驟3] 檢查驗證碼...")
    if not solve_captcha_if_needed(max_retry=3, fight_no=fight_no):
        log("[警告] 驗證碼處理失敗，可能需要手動介入")
        send_telegram("😱😱😱😱😱😱驗證碼處理失敗，可能需要手動介入")
        return False, pet_full_seen
    pet_full_seen = pet_full_seen or _detect_pet_full_once("驗證碼後")

    # 4. 戰鬥中，每 5 秒同時檢查簡繁兩種 ADx2 模板（任一匹配即點擊）
    log("[步驟4] 戰鬥中，等待 ADx2...")
    wait_and_click_any(
        ADX2_TEMPLATES,
        timeout=0,
        interval=5,
        name="ADx2",
        labels=ADX2_LABELS,
        heartbeat_sec=30,
    )

    # 5. 等待 ADx2 消失（簡繁兩種模板皆消失才算完成）
    log("[步驟5] 等待 ADx2 消失...")
    wait_for_disappear_any(ADX2_TEMPLATES, timeout=0, interval=1, name="ADx2")

    # 6. 點擊確認戰鬥（模板 confirm_fight.png，勿誤用 adx2）
    log("[步驟6] 等待確認戰鬥：將以模板匹配並點擊...")
    wait_and_click(
        IMG['confirm_fight'],
        timeout=100,
        interval=0.5,
        name="確認戰鬥",
        multiscale=True,
        heartbeat_sec=5,
    )

    # 7. 等待確認戰鬥按鈕／介面消失
    log("[步驟7] 等待確認戰鬥消失：直到畫面上不再匹配 confirm_fight...")
    wait_for_disappear(
        IMG['confirm_fight'],
        timeout=100,
        interval=1,
        name="確認戰鬥",
        multiscale=True,
        heartbeat_sec=5,
    )
    pet_full_seen = pet_full_seen or _detect_pet_full_once("戰鬥結束")

    # # 8. 等待確認獎勵出現並點擊（持續心跳 log 方便排查）
    # log("[步驟8] 等待確認獎勵：將以 confirm_rewards.png 匹配並點擊...")
    # wait_and_click(
    #     IMG['confirm_rewards'],
    #     timeout=0,
    #     interval=1,
    #     name="確認獎勵",
    #     multiscale=True,
    #     heartbeat_sec=5,
    # )

    log("[完成] 本輪戰鬥流程結束")

    return True, pet_full_seen


def _ensure_device_connected(dev_id: str):
    """嘗試 adb connect（TCP 設備），失敗不阻斷，交由 connect_device 最終驗證。"""
    text = str(dev_id or "").strip()
    if not text:
        return
    if ":" not in text:
        return
    try:
        subprocess.run(["adb", "connect", text], capture_output=True, text=True)
    except Exception:
        pass


def _run_single_device(dev_id: str, worker_name: str = ""):
    """單設備執行主流程（供主程序與子程序共用）。"""
    tag = f"[{worker_name}] " if worker_name else ""

    # 僅 send_telegram 推到 TG，一般 log 留在本機終端機
    set_telegram_log_forward(False)

    log("=" * 50)
    log(f"{tag}Under Dark 自動戰鬥腳本")
    log("=" * 50)
    send_telegram(f"{tag}開始腳本, 預計執行{NUM}次戰鬥")

    _ensure_device_connected(dev_id)
    connect_device(dev_id)

    success_count    = 0
    fail_count       = 0
    consecutive_fail = 0          # 連續失敗計數
    MAX_CONSECUTIVE  = 3          # 連續失敗上限，超過則停止腳本

    for i in range(NUM):
        log("=" * 50)
        log(f"{tag}第 {i+1}/{NUM} 次戰鬥開始（成功 {success_count} / 失敗 {fail_count}）")
        log("=" * 50)
        try:
            result = run_fight(fight_no=i + 1)
            # run_fight 可能回傳 (bool, bool) 或單純 bool（相容兩種版本）
            ok = result[0] if isinstance(result, tuple) else bool(result)
            if ok:
                success_count += 1
                consecutive_fail = 0      # 成功一次就重置連續失敗計數
            else:
                fail_count += 1
                consecutive_fail += 1
                log(f"{tag}[警告] 第 {i+1} 次戰鬥回傳失敗（連續 {consecutive_fail}/{MAX_CONSECUTIVE}），繼續下一輪")
        except Exception as exc:
            fail_count += 1
            consecutive_fail += 1
            log(f"{tag}[異常] 第 {i+1} 次戰鬥拋出例外: {exc}（連續 {consecutive_fail}/{MAX_CONSECUTIVE}）")
            time.sleep(2)

        log("=" * 50)
        log(f"{tag}第 {i+1}/{NUM} 次戰鬥完成")
        log("=" * 50)

        # 連續失敗超過上限：發一則 TG 通知後停止，避免刷屏
        if consecutive_fail >= MAX_CONSECUTIVE:
            msg = f"連續失敗 {consecutive_fail} 次，腳本自動停止。請檢查設備與環境。（成功 {success_count} / 失敗 {fail_count} / 共 {i+1} 場）"
            log(f"{tag}[停止] {msg}")
            send_telegram(f"{tag}{msg}")
            return 1

    summary = f"所有戰鬥完成：成功 {success_count} / 失敗 {fail_count} / 共 {NUM} 次"
    log("=" * 50)
    log(f"{tag}{summary}")
    log("=" * 50)
    send_telegram(f"{tag}腳本停止。{summary}")
    return 0


def _stream_worker_output(proc, prefix, out_q):
    """讀取子程序輸出並轉交佇列。"""
    for line in iter(proc.stdout.readline, ""):
        out_q.put(f"[{prefix}] {line.rstrip()}")
    out_q.put(f"[{prefix}] 程序結束，exit_code={proc.poll()}")


def _run_multi_devices():
    """多設備並行模式：最多同時跑兩台模擬器。"""
    devices = get_all_devices(max_count=2)
    if len(devices) <= 1:
        only = devices[0]["id"] if devices else DEVICE_ID
        only_name = devices[0]["name"] if devices else "模擬器1"
        return _run_single_device(only, only_name)

    log("=" * 50)
    log("多設備模式啟動")
    log("=" * 50)

    workers = []
    out_q = queue.Queue()
    for idx, dev in enumerate(devices, start=1):
        dev_id = dev["id"]
        name = dev.get("name") or f"模擬器{idx}"
        cmd = [sys.executable, "-u", os.path.abspath(__file__), "--device-id", dev_id, "--worker-name", name]
        proc = subprocess.Popen(
            cmd,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        workers.append((proc, name, dev_id))
        threading.Thread(target=_stream_worker_output, args=(proc, name, out_q), daemon=True).start()
        log(f"[主控] 已啟動 {name}（{dev_id}），PID={proc.pid}")

    running = len(workers)
    while running > 0:
        try:
            line = out_q.get(timeout=0.5)
            if line:
                print(line)
        except queue.Empty:
            pass
        running = sum(1 for p, _, _ in workers if p.poll() is None)

    exit_codes = []
    for p, name, dev_id in workers:
        code = p.wait()
        exit_codes.append(code)
        log(f"[主控] {name}（{dev_id}）結束，exit_code={code}")
    return 0 if all(c == 0 for c in exit_codes) else 1


def main():
    """主入口"""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--device-id", default="")
    parser.add_argument("--worker-name", default="")
    args, _ = parser.parse_known_args()

    # 子程序（指定設備）走單設備流程；未指定則由主程序決定單/雙設備模式
    if args.device_id:
        return _run_single_device(args.device_id, args.worker_name)
    return _run_multi_devices()


if __name__ == "__main__":
    raise SystemExit(main())
