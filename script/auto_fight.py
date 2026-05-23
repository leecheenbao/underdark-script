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
    flow_log,
    set_telegram_log_forward,
    connect_device,
    get_device_id,
    take_screenshot,
    load_and_match,
    click_by_image,
    image_exists,
    wait_for_image,
    force_restart_game_emulator,
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
from settings import (
    get_default_device,
    get_all_devices,
    get_pet_full_check,
    get_auto_recovery,
    get_game_package,
    get_game_activity,
)

DEVICE_ID = get_default_device()
NUM = 5000

# 道具欄滿包偵測開關（從 settings.json 的 features.pet_full_check 讀取）
ENABLE_PET_FULL_CHECK: bool = get_pet_full_check()

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
    'compose': "compose_btn.png",       # 合成（進入合成介面）
    'composite': "composite.png",       # 批量合成（點完合成後再按）
    'composite_confirm': "composite_confirm.png",  # 合成確認
    'left': "left.png",             # 合成品質左切換
    'legendary_quality': "legendary_quality.png",  # 僅「傳說」紅字（勿含左右箭頭，否則普通畫面會誤判）
    'common_quality': "common_quality.png",        # 「普通」等未達標品質（用於排除誤判）
    'home': "home.png",             # 回主頁（營地）按鈕
    'close': "close.png",           # 關閉彈窗／合成介面
    'compose_fail': "compose_fail.png",  # 「未達到合成條件。」提示
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
PET_QUALITY_THRESHOLD = 0.70       # 傳說紅字模板（僅文字）
PET_COMMON_QUALITY_THRESHOLD = 0.78  # 普通字樣（僅輔助 log；勿用於否定傳說）
PET_QUALITY_MAX_SHIFT = 8
PET_BATCH_DIALOG_WAIT = 0.55     # 點批量合成後等待彈窗
PET_HOME_RETURN_ATTEMPTS = 4     # 合成結束後回營地重試次數
PET_COMPOSE_FAIL_THRESHOLD = 0.72  # 未達合成條件提示

# 跨輪持久旗標：某輪偵測到滿包後設為 True，下輪開始前執行合成後清除
# （不依賴 pet_full.png 在新一輪仍在螢幕上，避免時序問題）
_pet_full_pending: bool = False


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


def _press_escape(count: int = 2) -> None:
    """送 ESC 關閉合成彈窗等疊層。"""
    dev = get_device_id()
    if not dev:
        return
    for _ in range(count):
        subprocess.run(
            ["adb", "-s", dev, "shell", "input", "keyevent", "111"],
            capture_output=True,
        )
        time.sleep(0.14)


def _is_on_camp_main() -> bool:
    """是否已在營地主畫面（地下城入口可見）。"""
    found, _, _, _ = load_and_match(IMG['enter'], threshold=0.72, multiscale=True)
    return bool(found)


def _close_compose_overlays(step: str) -> None:
    """關閉可能仍開著的合成／批量彈窗，避免擋住底部營地按鈕。"""
    time.sleep(0.35)
    _click_if_exists('close', threshold=PET_FLOW_THRESHOLD, delay=0.3, name="關閉", step=f"{step}-關閉")
    _press_escape(1)
    time.sleep(0.2)


def _return_to_home_after_compose(step: str = "5-回主頁") -> bool:
    """
    合成結束後回到營地主畫面，確認地下城入口可見後才視為成功。
    """
    flow_log("寵物", step, "合成結束，準備回營地主畫面")
    _close_compose_overlays(step)

    if _is_on_camp_main():
        flow_log("寵物", step, "已在營地主畫面", status="OK")
        return True

    for attempt in range(1, PET_HOME_RETURN_ATTEMPTS + 1):
        flow_log("寵物", step, f"第 {attempt}/{PET_HOME_RETURN_ATTEMPTS} 次點擊 {IMG['home']}")
        clicked = wait_and_click(
            IMG['home'],
            timeout=6,
            interval=0.4,
            threshold=PET_FLOW_THRESHOLD,
            delay=0.85,
            name="回營地",
            multiscale=True,
        )
        if not clicked:
            clicked = click_by_image(
                IMG['home'],
                threshold=PET_FLOW_THRESHOLD,
                delay=0.85,
                name="回營地",
                multiscale=True,
                show_log=True,
            )
        time.sleep(0.5)
        if _is_on_camp_main():
            flow_log("寵物", step, f"第 {attempt} 次已回營地（地下城入口可見）", status="OK")
            return True
        _close_compose_overlays(f"{step}-重試{attempt}")

    flow_log("寵物", step, "無法確認已回營地，戰鬥可能無法繼續", status="FAIL")
    send_telegram("寵物合成後回營地失敗，請手動點營地")
    return False


def _click_if_exists(img_key, threshold=0.8, delay=0.35, name="", multiscale=True, step: str = ""):
    """畫面存在才點擊；避免流程中斷。"""
    label = name or img_key
    st = step or label
    if not image_exists(IMG[img_key], threshold=threshold):
        flow_log("寵物", st, f"畫面無 {IMG[img_key]}，略過", status="SKIP")
        return False
    ok = click_by_image(
        IMG[img_key],
        threshold=threshold,
        delay=delay,
        name=label,
        multiscale=multiscale,
    )
    flow_log("寵物", st, f"已點擊 {label}（{IMG[img_key]}）", status="OK" if ok else "FAIL")
    return ok


def _is_compose_condition_failed() -> bool:
    """畫面是否出現「未達到合成條件。」提示。"""
    found, _, _, _ = load_and_match(
        IMG['compose_fail'],
        threshold=PET_COMPOSE_FAIL_THRESHOLD,
        multiscale=True,
    )
    return bool(found)


def _abort_compose_if_failed(step: str) -> bool:
    """回傳 True 表示應中止合成並回營地。"""
    if _is_compose_condition_failed():
        flow_log("寵物", step, "偵測到「未達到合成條件」，結束合成", status="SKIP")
        return True
    return False


def _quality_match(img_key: str, threshold: float) -> tuple[bool, float]:
    """品質模板比對，回傳 (是否達標, 相似度)。"""
    found, _, _, sim = load_and_match(
        IMG[img_key],
        threshold=threshold,
        multiscale=True,
    )
    return bool(found), float(sim or 0.0)


def _is_legendary_quality(step: str = "") -> bool:
    """
    是否已為傳說品質：以傳說紅字模板為準。
    common_quality 黑框在傳說畫面仍可能 sim≈0.74，不可拿來否定傳說判定。
    """
    leg_ok, leg_sim = _quality_match("legendary_quality", PET_QUALITY_THRESHOLD)
    common_ok, common_sim = _quality_match("common_quality", PET_COMMON_QUALITY_THRESHOLD)
    if step:
        flow_log(
            "寵物",
            step,
            f"品質偵測 傳說={leg_sim:.3f}({'Y' if leg_ok else 'N'}) "
            f"普通={common_sim:.3f}({'Y' if common_ok else 'N'})"
            + (" → 停止左切" if leg_ok else ""),
        )
    return leg_ok


def _wait_batch_quality_dialog(step: str, timeout: float = 5.0) -> bool:
    """批量合成彈窗出現（左鍵或品質文字任一可見）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for key, th in (
            ("left", PET_FLOW_THRESHOLD),
            ("common_quality", PET_COMMON_QUALITY_THRESHOLD),
            ("legendary_quality", PET_QUALITY_THRESHOLD),
        ):
            found, sim = _quality_match(key, th)
            if found:
                flow_log("寵物", step, f"批量合成彈窗已出現（{IMG[key]} sim={sim:.3f}）", status="OK")
                return True
        time.sleep(0.25)
    flow_log("寵物", step, "等待批量合成品質彈窗逾時", status="FAIL")
    return False


def _ensure_legendary_quality(step: str = "品質") -> bool:
    """
    若目前不是傳說品質，重複點 left.png 切換，直到符合 legendary_quality 或達上限。
    """
    if not _wait_batch_quality_dialog(step):
        return False

    if _is_legendary_quality(step=f"{step}-檢查"):
        flow_log("寵物", step, "已是傳說品質，無需左切", status="OK")
        return True

    flow_log("寵物", step, f"非傳說品質，開始點 {IMG['left']} 切換（最多 {PET_QUALITY_MAX_SHIFT} 次）")
    for n in range(1, PET_QUALITY_MAX_SHIFT + 1):
        if _is_legendary_quality(step=f"{step}-第{n}前"):
            flow_log("寵物", step, f"第 {n} 次檢查前已為傳說品質", status="OK")
            return True

        flow_log("寵物", step, f"第 {n}/{PET_QUALITY_MAX_SHIFT} 次：點 {IMG['left']}")
        clicked = wait_and_click(
            IMG['left'],
            timeout=3,
            interval=0.3,
            threshold=PET_FLOW_THRESHOLD,
            delay=0.35,
            name="品質左切",
            multiscale=True,
            heartbeat_sec=1.0,
        )
        if not clicked:
            clicked = click_by_image(
                IMG['left'],
                threshold=PET_FLOW_THRESHOLD,
                delay=0.35,
                name="品質左切",
                multiscale=True,
                show_log=True,
            )
        if not clicked:
            _, left_sim = _quality_match("left", PET_FLOW_THRESHOLD)
            flow_log(
                "寵物",
                step,
                f"第 {n} 次找不到 {IMG['left']}（最高 sim={left_sim:.3f}），停止左切",
                status="FAIL",
            )
            return False

        time.sleep(0.3)
        if _is_legendary_quality(step=f"{step}-第{n}後"):
            flow_log("寵物", step, f"第 {n} 次左切後已為傳說品質", status="OK")
            return True

    flow_log(
        "寵物",
        step,
        f"已左切 {PET_QUALITY_MAX_SHIFT} 次仍非傳說品質（請檢查 {IMG['legendary_quality']} / {IMG['left']}）",
        status="FAIL",
    )
    return False


def _detect_pet_full_once(stage: str = "") -> bool:
    """
    截圖檢查 pet_full.png 是否出現。
    若偵測到，設置跨輪旗標 _pet_full_pending = True，
    實際合成流程在「下一輪開始前」透過 _run_pet_full_flow() 執行。
    """
    global _pet_full_pending
    if not ENABLE_PET_FULL_CHECK:
        return False
    hit = image_exists(IMG['pet_full'], threshold=PET_FULL_THRESHOLD)
    if hit:
        _pet_full_pending = True
        tag = f"（{stage}）" if stage else ""
        log(f"[寵物] 偵測到滿包{tag}，下輪戰鬥前執行合成")
    return hit


def _run_pet_full_flow() -> bool:
    """
    執行完整的寵物滿包合成流程，執行完後清除 _pet_full_pending 旗標。
    不需要 pet_full.png 仍在螢幕上（旗標機制已跨輪保留狀態）。
    流程：回主頁 → 進入背包 → 切寵物分頁 → 合成→批量合成（循環）→ 回主頁
    """
    global _pet_full_pending

    flow_log("寵物", "流程", "開始滿包合成（home→背包→寵物分頁→合成→批量合成→確認→home）")
    send_telegram("偵測到寵物道具已滿，開始自動合成")

    # Step 1：先回主頁確保畫面正確
    flow_log("寵物", "1-回主頁", "嘗試點擊 home")
    _click_if_exists('home', threshold=PET_FLOW_THRESHOLD, delay=0.8, name="回主頁", step="1-回主頁")

    # Step 2：若此時 pet_full 提示仍在，先點提示本體；否則直接找進入背包按鈕
    flow_log("寵物", "2-進背包", "檢查滿包提示並進入背包")
    if image_exists(IMG['pet_full'], threshold=PET_FULL_THRESHOLD):
        _click_if_exists('pet_full', threshold=PET_FULL_THRESHOLD, delay=0.5, name="寵物已滿提示", step="2-滿包提示")

    opened = wait_and_click(
        IMG['enter_backpack'],
        timeout=10,
        interval=0.5,
        threshold=PET_FLOW_THRESHOLD,
        delay=0.7,
        name="進入背包",
        multiscale=True,
    )
    if not opened:
        flow_log("寵物", "2-進背包", f"未找到 {IMG['enter_backpack']}", status="FAIL")
        _pet_full_pending = False
        return False
    flow_log("寵物", "2-進背包", "已進入背包", status="OK")

    # Step 3：切到寵物分頁（等待出現再點，避免背包尚未完全載入）
    flow_log("寵物", "3-寵物分頁", f"等待 {IMG['pet_bag']}")
    pet_tab = wait_and_click(
        IMG['pet_bag'],
        timeout=6,
        interval=0.4,
        threshold=PET_FLOW_THRESHOLD,
        delay=0.5,
        name="寵物分頁",
        multiscale=True,
    )
    flow_log("寵物", "3-寵物分頁", "已切換寵物分頁" if pet_tab else "未找到寵物分頁", status="OK" if pet_tab else "FAIL")

    # Step 4：合成循環（合成 → 批量合成 → 調品質 → 確認）
    flow_log("寵物", "4-合成循環", f"最多 {PET_COMPOSITE_MAX_ROUNDS} 輪")
    merged_count = 0
    compose_fail_seen = False
    for i in range(PET_COMPOSITE_MAX_ROUNDS):
        round_no = i + 1
        if _abort_compose_if_failed(f"4-{round_no}-前"):
            compose_fail_seen = True
            break

        flow_log("寵物", f"4-{round_no}", f"第 {round_no} 輪：等待 {IMG['compose']}")
        found_compose = wait_and_click(
            IMG['compose'],
            timeout=4,
            interval=0.4,
            threshold=PET_FLOW_THRESHOLD,
            delay=0.5,
            name=f"合成#{i + 1}",
            multiscale=True,
        )
        if not found_compose:
            if _abort_compose_if_failed(f"4-{round_no}"):
                compose_fail_seen = True
            else:
                flow_log("寵物", f"4-{round_no}", f"合成未出現，結束（已完成 {merged_count} 次）", status="SKIP")
            break
        flow_log("寵物", f"4-{round_no}a", "已點合成", status="OK")
        if _abort_compose_if_failed(f"4-{round_no}a後"):
            compose_fail_seen = True
            break

        flow_log("寵物", f"4-{round_no}b", f"等待 {IMG['composite']} 批量合成")
        found_batch = wait_and_click(
            IMG['composite'],
            timeout=5,
            interval=0.4,
            threshold=PET_FLOW_THRESHOLD,
            delay=0.5,
            name=f"批量合成#{i + 1}",
            multiscale=True,
        )
        if not found_batch:
            if _abort_compose_if_failed(f"4-{round_no}b"):
                compose_fail_seen = True
            else:
                flow_log("寵物", f"4-{round_no}b", f"批量合成未出現，結束（已完成 {merged_count} 次）", status="FAIL")
            break
        flow_log("寵物", f"4-{round_no}b", "已點批量合成", status="OK")
        time.sleep(PET_BATCH_DIALOG_WAIT)
        if _abort_compose_if_failed(f"4-{round_no}b後"):
            compose_fail_seen = True
            break

        if not _ensure_legendary_quality(step=f"4-{round_no}c"):
            if _abort_compose_if_failed(f"4-{round_no}c"):
                compose_fail_seen = True
            else:
                flow_log("寵物", f"4-{round_no}c", "無法切到傳說品質，結束本輪合成", status="FAIL")
            break

        # 等待合成確認按鈕（有淡入動畫）
        flow_log("寵物", f"4-{round_no}d", f"等待 {IMG['composite_confirm']}")
        confirmed = wait_and_click(
            IMG['composite_confirm'],
            timeout=5,
            interval=0.35,
            threshold=0.72,
            delay=0.6,
            name="合成確認",
            multiscale=True,
        )
        if not confirmed:
            if _abort_compose_if_failed(f"4-{round_no}d"):
                compose_fail_seen = True
            else:
                flow_log("寵物", f"4-{round_no}d", f"合成確認未出現，結束（已完成 {merged_count} 次）", status="FAIL")
            break

        time.sleep(0.4)  # 等待合成結果／提示出現
        if _abort_compose_if_failed(f"4-{round_no}d後"):
            compose_fail_seen = True
            break

        merged_count += 1
        flow_log("寵物", f"4-{round_no}", f"第 {merged_count} 次合成完成", status="OK")
        time.sleep(0.3)  # 等待合成動畫

    # Step 5：回營地主畫面（須成功才繼續戰鬥）
    home_ok = _return_to_home_after_compose(step="5-回主頁")

    if compose_fail_seen:
        msg = "未達到合成條件，已回營地繼續戰鬥" if home_ok else "未達到合成條件，回營地失敗"
        flow_log("寵物", "流程", msg, status="OK" if home_ok else "FAIL")
        send_telegram(msg)
    elif merged_count > 0:
        msg = f"寵物合成完成（共 {merged_count} 次），已回營地，繼續戰鬥" if home_ok else (
            f"寵物合成完成（共 {merged_count} 次），但回營地失敗"
        )
        flow_log("寵物", "流程", msg, status="OK" if home_ok else "FAIL")
        send_telegram(msg)
    else:
        flow_log("寵物", "流程", "進入背包但無可合成項目", status="SKIP")

    _pet_full_pending = False
    return home_ok


def handle_pet_full_if_needed() -> bool:
    """
    相容舊版呼叫介面。
    優先依旗標執行（不需要 pet_full.png 在螢幕），
    旗標未設但畫面仍有提示時也能觸發。
    """
    global _pet_full_pending
    if not ENABLE_PET_FULL_CHECK:
        return False
    if _pet_full_pending or image_exists(IMG['pet_full'], threshold=PET_FULL_THRESHOLD):
        _pet_full_pending = True
        return _run_pet_full_flow()
    return False


# ========== 主流程區 ==========
def run_fight(fight_no=None):
    """執行一次完整的戰鬥流程。回傳 (success, pet_full_seen_in_round)。
    fight_no: 當前場次編號，由 main() 傳入，供 Telegram 通知顯示
    """

    # 進入前：若上輪留下滿包旗標，先執行合成流程
    # 用旗標而非重新偵測，避免回主頁後 pet_full.png 已消失導致處理被跳過
    if ENABLE_PET_FULL_CHECK and _pet_full_pending:
        flow_log("戰鬥", "0-滿包", "上輪滿包旗標，先執行合成流程")
        if not _run_pet_full_flow():
            flow_log("戰鬥", "0-滿包", "回營地未成功，再試一次")
            _return_to_home_after_compose(step="0-回營地重試")

    # 1. 等待並點擊入口（enter.png 實際為「地下城」按鈕截圖；須與當前解析度一致）
    flow_log("戰鬥", "1-進入", f"等待 {IMG['enter']}")
    if not wait_and_click(
        IMG['enter'],
        timeout=120,
        interval=1,
        name="地下城入口",
        multiscale=True,
        heartbeat_sec=5,
    ):
        flow_log("戰鬥", "1-進入", "逾時未匹配 enter.png", status="FAIL")
        send_telegram("步驟1 逾時：未匹配到 enter.png，請確認已在遊戲主介面或重新截取模板圖")
        return False, False
    flow_log("戰鬥", "1-進入", "已點地下城入口", status="OK")

    # 進入列表後常有轉場/載入，立刻截圖可能對到過渡畫面，相似度會偏低
    # 0.2s 已足夠避開第一幀過渡畫面；若偶有誤匹配可調回 0.5
    time.sleep(0.2)

    # 2. 等待進入戰鬥按鈕出現並點擊（timeout=0 表示一直等；步驟1成功後才會很快出現）
    # enter_fight.png 建議只裁「進入」二字或固定邊框，勿包含體力數字（5/10 等會變，分數易掉到 0.5 以下）
    flow_log("戰鬥", "2-進入戰鬥", f"等待 {IMG['enter_fight']}")
    wait_and_click(
        IMG['enter_fight'],
        timeout=0,
        interval=0.5,
        name="進入戰鬥",
        threshold=0.9,
        multiscale=True,
        heartbeat_sec=5,
    )

    flow_log("戰鬥", "2-進入戰鬥", "已點進入戰鬥", status="OK")
    _detect_pet_full_once("進入戰鬥後")

    # 3. 驗證碼判斷與輸入
    flow_log("戰鬥", "3-驗證碼", "檢查是否需要驗證碼")
    if not solve_captcha_if_needed(max_retry=3, fight_no=fight_no):
        flow_log("戰鬥", "3-驗證碼", "處理失敗", status="FAIL")
        send_telegram("😱😱😱😱😱😱驗證碼處理失敗，可能需要手動介入")
        return False, _pet_full_pending
    flow_log("戰鬥", "3-驗證碼", "通過", status="OK")
    _detect_pet_full_once("驗證碼後")

    # 4. 戰鬥中，每 5 秒同時檢查簡繁兩種 ADx2 模板（任一匹配即點擊）
    flow_log("戰鬥", "4-ADx2", "等待並點擊廣告倍數")
    wait_and_click_any(
        ADX2_TEMPLATES,
        timeout=0,
        interval=5,
        name="ADx2",
        labels=ADX2_LABELS,
        heartbeat_sec=30,
    )

    flow_log("戰鬥", "4-ADx2", "已點擊", status="OK")
    flow_log("戰鬥", "5-ADx2消失", "等待廣告介面關閉")
    wait_for_disappear_any(ADX2_TEMPLATES, timeout=0, interval=1, name="ADx2")
    flow_log("戰鬥", "5-ADx2消失", "已消失", status="OK")

    # 6. 點擊確認戰鬥（模板 confirm_fight.png，勿誤用 adx2）
    flow_log("戰鬥", "6-確認戰鬥", f"等待 {IMG['confirm_fight']}")
    wait_and_click(
        IMG['confirm_fight'],
        timeout=100,
        interval=0.5,
        name="確認戰鬥",
        multiscale=True,
        heartbeat_sec=5,
    )

    flow_log("戰鬥", "6-確認戰鬥", "已點擊", status="OK")
    flow_log("戰鬥", "7-確認消失", "等待確認戰鬥消失")
    wait_for_disappear(
        IMG['confirm_fight'],
        timeout=100,
        interval=1,
        name="確認戰鬥",
        multiscale=True,
        heartbeat_sec=5,
    )
    flow_log("戰鬥", "7-確認消失", "已消失", status="OK")
    _detect_pet_full_once("戰鬥結束")

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

    flow_log("戰鬥", "完成", f"本輪戰鬥結束（滿包旗標={_pet_full_pending}）", status="OK")

    return True, _pet_full_pending


def _force_restart_kwargs(cfg: dict, use_icon: bool) -> dict:
    """組裝 force_restart_game_emulator 參數（與 settings.auto_recovery 一致）。"""
    return {
        "stop_wait": float(cfg.get("force_stop_wait_sec", 5)),
        "launch_wait": float(cfg.get("launch_wait_sec", 15)),
        "kill_retries": int(cfg.get("force_stop_retries", 3)),
        "game_icon": "game_icon.png" if use_icon else None,
        "prefer_launch_via_icon": bool(cfg.get("prefer_launch_via_icon", True)),
    }


def _recover_via_sl_ui(dev_id: str, tag: str) -> bool:
    """備援：遊戲內設定離開後點桌面圖示（僅在模擬器強制重啟失敗時使用）。"""
    try:
        from sl_flow import restart_game_on_device_2

        connect_device(dev_id)
        return bool(restart_game_on_device_2(dev_id))
    except Exception as exc:
        log(f"{tag}[恢復] SL UI 重啟異常: {exc}")
        return False


def _emulator_key_for_device(dev_id: str) -> str | None:
    """依 ADB 設備 ID 反查 settings.emulators 的 key。"""
    for row in get_all_devices():
        if str(row.get("id") or "").strip() == str(dev_id or "").strip():
            return str(row.get("key") or "")
    return None


def _try_restart_emulator(dev_id: str, tag: str, emulator_key: str | None = None) -> bool:
    """重啟整台模擬器（MuMuManager 或 adb reboot）。"""
    try:
        from emulator_control import restart_emulator

        log(f"{tag}[恢復] 嘗試重啟模擬器（非僅遊戲）...")
        return bool(restart_emulator(dev_id, emulator_key))
    except Exception as exc:
        log(f"{tag}[恢復] 重啟模擬器異常: {exc}")
        return False


def _restart_game_after_emulator(dev_id: str, tag: str, cfg: dict, use_icon: bool) -> bool:
    """強制關閉並冷啟動遊戲（不含整機重啟）。"""
    connect_device(dev_id)
    return force_restart_game_emulator(
        get_game_package(),
        get_game_activity(),
        **_force_restart_kwargs(cfg, use_icon),
    )


def _recover_device_after_failures(dev_id: str, tag: str) -> bool:
    """
    連續失敗後恢復（預設流程）：
    1) 重啟整台模擬器（MuMuManager / adb reboot）
    2) 冷啟動遊戲並等待營地 enter.png
    3) 由呼叫端重新啟動戰鬥腳本程序繼續下一場
    """
    cfg = get_auto_recovery()
    ready_timeout = int(cfg.get("game_ready_timeout_sec", 180))
    restart_mode = str(cfg.get("restart_mode", "emulator")).strip().lower()
    use_icon = bool(cfg.get("use_game_icon_fallback", True))
    emu_key = _emulator_key_for_device(dev_id)
    restart_emu_first = bool(cfg.get("restart_emulator_on_recovery", True))
    restart_emu_on_fail = bool(cfg.get("restart_emulator_after_game_restart_fail", True))

    log(f"{tag}[恢復] 開始恢復（裝置 {dev_id}）...")
    send_telegram(f"{tag}連續失敗，開始恢復：重啟模擬器→重開遊戲→繼續腳本")

    # ── 步驟 1：重啟整台模擬器 ──
    if restart_emu_first:
        flow_log("恢復", "1-模擬器", "重啟整台模擬器", status="...")
        emu_ok = _try_restart_emulator(dev_id, tag, emu_key)
        if emu_ok:
            flow_log("恢復", "1-模擬器", "模擬器已重啟", status="OK")
        else:
            flow_log("恢復", "1-模擬器", "模擬器重啟失敗，改試僅重開遊戲", status="FAIL")
            log(f"{tag}[恢復] 模擬器重啟失敗，仍嘗試重開遊戲...")

    connect_device(dev_id)

    # ── 步驟 2：冷啟動遊戲 ──
    restarted = False
    if restart_mode == "sl_ui":
        flow_log("恢復", "2-遊戲", "SL UI 離開後重開", status="...")
        restarted = _recover_via_sl_ui(dev_id, tag)
    elif restart_mode in ("emulator", "emulator_then_sl"):
        flow_log("恢復", "2-遊戲", "冷啟動遊戲", status="...")
        try:
            restarted = _restart_game_after_emulator(dev_id, tag, cfg, use_icon)
        except Exception as exc:
            log(f"{tag}[恢復] 冷啟動遊戲異常: {exc}")
            restarted = False
        # 未先重啟模擬器時：遊戲重開失敗再試整機重啟
        if not restarted and (not restart_emu_first) and restart_emu_on_fail:
            if _try_restart_emulator(dev_id, tag, emu_key):
                connect_device(dev_id)
                try:
                    restarted = _restart_game_after_emulator(dev_id, tag, cfg, use_icon)
                except Exception as exc:
                    log(f"{tag}[恢復] 模擬器重啟後開遊戲失敗: {exc}")
        if not restarted and restart_mode == "emulator_then_sl":
            log(f"{tag}[恢復] 改試 SL UI...")
            flow_log("恢復", "2-遊戲", "SL UI 離開後重開", status="...")
            restarted = _recover_via_sl_ui(dev_id, tag)
    else:
        log(f"{tag}[恢復] 未知 restart_mode={restart_mode}，改用冷啟動遊戲")
        try:
            restarted = _restart_game_after_emulator(dev_id, tag, cfg, use_icon)
        except Exception as exc:
            log(f"{tag}[恢復] 冷啟動遊戲異常: {exc}")
            restarted = False

    if not restarted:
        flow_log("恢復", "2-遊戲", "無法重開遊戲", status="FAIL")
        log(f"{tag}[恢復] 恢復失敗")
        return False

    flow_log("恢復", "2-遊戲", "遊戲已重開", status="OK")
    flow_log("恢復", "3-營地", f"等待 {IMG['enter']}", status="...")
    log(f"{tag}[恢復] 等待營地主畫面（{IMG['enter']}，最長 {ready_timeout}s）...")
    ready = wait_for_image(
        IMG["enter"],
        timeout=ready_timeout,
        interval=1.0,
        threshold=0.72,
        multiscale=True,
    )
    if ready:
        flow_log("恢復", "就緒", "地下城入口已可見", status="OK")
        log(f"{tag}[恢復] 已回到營地主畫面，繼續執行腳本")
        return True

    flow_log("恢復", "就緒", "逾時未見地下城入口", status="FAIL")
    log(f"{tag}[恢復] 重啟後仍無法確認營地主畫面")
    return False


def _restart_battle_script_process(dev_id: str, worker_name: str, resume_fight: int = 1):
    """強制重啟遊戲後，重新啟動本戰鬥腳本程序（從指定場次繼續）。"""
    resume_fight = max(1, int(resume_fight))
    argv = [sys.executable, "-u", os.path.abspath(__file__), "--device-id", dev_id]
    if worker_name:
        argv += ["--worker-name", worker_name]
    if resume_fight > 1:
        argv += ["--resume-fight", str(resume_fight)]
    log(f"[恢復] 重新啟動戰鬥腳本（從第 {resume_fight} 場繼續）...")
    os.execv(sys.executable, argv)


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


def _run_single_device(dev_id: str, worker_name: str = "", resume_fight: int = 1):
    """單設備執行主流程（供主程序與子程序共用）。"""
    global _pet_full_pending
    tag = f"[{worker_name}] " if worker_name else ""

    # 僅 send_telegram 推到 TG，一般 log 留在本機終端機
    set_telegram_log_forward(False)

    recovery_cfg = get_auto_recovery()
    auto_recovery_enabled = bool(recovery_cfg.get("enabled", True))
    max_consecutive = int(recovery_cfg.get("max_consecutive_fail", 3))
    max_recovery = int(recovery_cfg.get("max_recovery_per_session", 0))
    restart_after_recovery = bool(recovery_cfg.get("restart_script_after_recovery", True))

    start_idx = max(0, int(resume_fight) - 1)
    total_planned = max(0, NUM - start_idx)

    log("=" * 50)
    log(f"{tag}Under Dark 自動戰鬥腳本")
    log("=" * 50)
    if start_idx > 0:
        send_telegram(f"{tag}恢復後繼續腳本，從第 {resume_fight} 場起（剩餘約 {total_planned} 場）")
        log(f"{tag}從第 {resume_fight}/{NUM} 場繼續（恢復後重啟）")
    else:
        send_telegram(f"{tag}開始腳本, 預計執行{NUM}次戰鬥")

    _ensure_device_connected(dev_id)
    connect_device(dev_id)

    success_count    = 0
    fail_count       = 0
    consecutive_fail = 0          # 連續失敗計數
    recovery_count   = 0          # 本場次已執行恢復次數

    for i in range(start_idx, NUM):
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
                log(f"{tag}[警告] 第 {i+1} 次戰鬥回傳失敗（連續 {consecutive_fail}/{max_consecutive}），繼續下一輪")
        except Exception as exc:
            fail_count += 1
            consecutive_fail += 1
            log(f"{tag}[異常] 第 {i+1} 次戰鬥拋出例外: {exc}（連續 {consecutive_fail}/{max_consecutive}）")
            time.sleep(2)

        log("=" * 50)
        log(f"{tag}第 {i+1}/{NUM} 次戰鬥完成")
        log("=" * 50)

        # 連續失敗達上限：重啟遊戲並繼續（不再直接停止腳本）
        if consecutive_fail >= max_consecutive:
            if not auto_recovery_enabled:
                msg = (
                    f"連續失敗 {consecutive_fail} 次，腳本自動停止。"
                    f"（成功 {success_count} / 失敗 {fail_count} / 共 {i+1} 場）"
                )
                log(f"{tag}[停止] {msg}")
                send_telegram(f"{tag}{msg}")
                return 1

            recovery_count += 1
            if max_recovery > 0 and recovery_count > max_recovery:
                msg = (
                    f"連續失敗 {consecutive_fail} 次，且本場次恢復已達上限 {max_recovery} 次，停止腳本。"
                    f"（成功 {success_count} / 失敗 {fail_count} / 共 {i+1} 場）"
                )
                log(f"{tag}[停止] {msg}")
                send_telegram(f"{tag}{msg}")
                return 1

            msg = (
                f"連續失敗 {consecutive_fail} 次，第 {recovery_count} 次自動恢復："
                f"重啟模擬器→重開遊戲→繼續腳本（成功 {success_count} / 失敗 {fail_count}）"
            )
            log(f"{tag}[恢復] {msg}")
            send_telegram(f"{tag}{msg}")

            _pet_full_pending = False
            if _recover_device_after_failures(dev_id, tag):
                consecutive_fail = 0
                connect_device(dev_id)
                retry_fight = i + 1  # 重試本輪失敗的場次（1-based）
                if restart_after_recovery:
                    log(f"{tag}[恢復] 恢復成功，重新啟動戰鬥腳本（從第 {retry_fight} 場）")
                    send_telegram(f"{tag}恢復完成，重新啟動戰鬥腳本（第 {retry_fight} 場起）")
                    _restart_battle_script_process(dev_id, worker_name, resume_fight=retry_fight)
                log(f"{tag}[恢復] 恢復成功，從第 {retry_fight}/{NUM} 輪繼續")
                continue

            fail_msg = (
                f"連續失敗 {consecutive_fail} 次且恢復失敗，腳本停止。"
                f"（成功 {success_count} / 失敗 {fail_count} / 共 {i+1} 場）"
            )
            log(f"{tag}[停止] {fail_msg}")
            send_telegram(f"{tag}{fail_msg}")
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


def _run_test_pet_flow():
    """
    測試模式：連接預設設備後直接執行一次滿包合成流程，完成後退出。
    用於驗證「進入背包 → 切寵物分頁 → 合成 → 回主頁」流程是否正確。
    """
    global _pet_full_pending
    set_telegram_log_forward(False)
    log("=" * 50)
    log("[測試] 滿包合成流程測試開始")
    log("=" * 50)

    connect_device(DEVICE_ID)

    # 強制設旗標，跳過截圖偵測，直接執行合成
    _pet_full_pending = True
    ok = _run_pet_full_flow()

    log("=" * 50)
    log(f"[測試] 流程結束，結果：{'成功' if ok else '失敗'}")
    log("=" * 50)
    return 0 if ok else 1


def main():
    """主入口"""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--device-id", default="")
    parser.add_argument("--worker-name", default="")
    parser.add_argument("--test-pet-flow", action="store_true",
                        help="測試模式：直接執行一次滿包合成流程後退出")
    parser.add_argument("--resume-fight", type=int, default=1,
                        help="從第 N 場戰鬥繼續（恢復後重啟腳本時使用）")
    args, _ = parser.parse_known_args()

    # 測試模式：直接跑合成流程
    if args.test_pet_flow:
        return _run_test_pet_flow()

    # 子程序（指定設備）走單設備流程；未指定則由主程序決定單/雙設備模式
    if args.device_id:
        return _run_single_device(
            args.device_id,
            args.worker_name,
            resume_fight=max(1, int(args.resume_fight or 1)),
        )
    return _run_multi_devices()


if __name__ == "__main__":
    raise SystemExit(main())
