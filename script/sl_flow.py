"""
SL 流程腳本
1) 裝置1：儲存遊戲進度
2) 裝置2：透過設定離開遊戲後，點桌面圖示重啟遊戲
3) 裝置2：洗道具（鑽石入口 → 等 select_item → 以其為基準左滑直到 goal_pet → 點購買）
以上 1～3 會重複執行，直到裝置2 畫面偵測到 sl_goal.png（或達 --max-loops 上限）。

速度：預設已略快於舊版 0.5s 輪詢；可用 --fast 或 --interval 再調（見檔案內 SL 說明）。
"""

import argparse
import os
import subprocess
import time
import cv2

from settings import get_device_id
from utils import (
    log,
    send_telegram,
    set_telegram_log_forward,
    connect_device,
    wait_and_click,
    wait_and_click_any,
    wait_for_image,
    image_exists,
    load_and_match,
    load_and_match_all_peaks,
    take_screenshot,
    swipe,
    click_fixed,
    CACHE_DIR,
)


# ===== 模板設定 =====
IMG_SETTINGS_RED = "settings_red.png"
IMG_SETTINGS_NO_RED = "settings_no_red.png"
SETTINGS_MENU_TEMPLATES = [IMG_SETTINGS_RED, IMG_SETTINGS_NO_RED]
SETTINGS_MENU_LABELS = ["設定(有紅點)", "設定(無紅點)"]
# 模板路徑為相對 pic/；若路徑錯誤，utils.load_and_match 讀不到圖會回傳相似度 0
IMG_SELECTIONS = "sellections.png"
IMG_SAVE_1 = "save_1_btn.png"
IMG_SAVE_2 = "save_2_btn.png"
IMG_SAVE_3 = "save_3_btn.png"
# 偵測 save_2 / save_3 提示（須與 wait_and_click 一致使用 multiscale，否則易低分逾時）
SAVE_PROMPT_WAIT_SEC = 25
SAVE_PROMPT_THRESHOLD = 0.72
IMG_SAVE_YES = "save_yes_btn.png"
IMG_LEAVE_BTN = "leave_btn.png"
IMG_GAME_ICON = "game_icon.png"
# 開設定前可能跳出的延遲／提示彈窗（兩種畫面皆需點 yes）
IMG_DELAY_ITEM = "delay_item.png"
IMG_DELAY_ITEM_2 = "delay_item_2.png"
IMG_DELAY_ITEM_VARIANTS = (IMG_DELAY_ITEM, IMG_DELAY_ITEM_2)
# delay 視窗上的「是／Yes」；若與儲存畫面不同請另截圖並改此檔名
IMG_DELAY_YES = "save_yes_btn.png"

# ===== 裝置2 洗道具模板（皆相對 pic/）=====
IMG_DIAMOND_ITEM = "diamond_item.png"
IMG_SELECT_ITEM = "select_item.png"
# 購買欄位對齊：若畫面上仍有下列標記則優先用 X 對齊；否則改以 select_item 或最高相似度 goal_pet
SELECT_GOAL_MARKERS = ("select_goal_pet.png", "select_goal_item.png")
# 進入洗道具後等待 select_item 的最長秒數
SELECT_ITEM_WAIT_SEC = 60
IMG_GOAL_PET = "goal_pet.png"
# goal_pet 模板中心到「購買」鈕的垂直距離（像素）；依實機畫面調整
GOAL_PET_BUY_OFFSET_Y = 85
# 在基礎偏移上再微調 Y（多組嘗試直到出現確認視窗）
GOAL_PET_BUY_Y_FINE = (-20, -16)
# 在基礎 X 上微調（避免跨欄位誤點）
GOAL_PET_BUY_X_FINE = (0, -8, 8)
# 購買確認視窗左側「是」；若與存檔按鈕視覺不同請另截圖為獨立模板並改此常數
IMG_PURCHASE_CONFIRM_YES = "save_yes_btn.png"
# 在 select_item 上向左滑動的最大次數
WASH_SWIPE_MAX = 20
# 左滑終止判定閾值（太低會誤判過早停止）
GOAL_PET_STOP_THRESHOLD = 0.70
# 以 select_item 為基準的左滑參數（拖曳距離太短時，遊戲常不判定為有效滑動）
SWIPE_SELECT_START_OFFSET_X = 260
SWIPE_SELECT_END_OFFSET_X = -180
SWIPE_SELECT_Y_OFFSET = 70
SWIPE_SELECT_DURATION = 0.24
# 是否輸出購買點擊標記圖到 pic/cache（方便人工調整座標）
DEBUG_SAVE_BUY_MARKUP = True

# ===== SL 結束條件（裝置2 畫面）=====
# 整輪 1→2→3 完成後在裝置2 檢查；出現即停止並結束程式
IMG_SL_GOAL = "sl_goal.png"
SL_GOAL_MATCH_THRESHOLD = 0.72

# ===== 速度參數（愈小愈快，但過小易誤判／點擊過快被遊戲忽略）=====
# 執行時可改：python sl_flow.py --fast
# 或自訂輪詢：python sl_flow.py --interval 0.22
SL: dict[str, float | int] = {
    "interval": 0.35,          # wait_* 每輪截圖比對間隔（秒）
    "delay_click": 0.2,      # 一般點擊後等待 UI
    "delay_heavy": 0.5,      # 存檔／確認等較慢畫面
    "delay_game_icon": 0.85,  # 桌面點圖示後等待啟動
    "heartbeat": 1,           # 等待中每隔幾秒打 log；0=關閉
    "heartbeat_game": 1,
    "esc_key_pause": 0.14,    # ESC keyevent 之間間隔
    "pause_after_popup": 0.28,
    "pause_desktop": 0.75,     # 離開遊戲後到點圖示前
    "after_esc": 0.22,         # 裝置1連按 ESC 後短暫等待
}


def configure_sl_speed(*, fast: bool = False, interval: float | None = None) -> None:
    """套用預設或 --fast；--interval 可再覆寫輪詢間隔。"""
    # 注意：未加 --fast 時保留檔案頂端 SL 字典的值（勿在此覆寫），否則手改速度會被洗掉。
    if fast:
        SL.update(
            interval=0.2,
            delay_click=0.32,
            delay_heavy=0.44,
            delay_game_icon=0.55,
            heartbeat=0,
            heartbeat_game=0,
            esc_key_pause=0.09,
            pause_after_popup=0.18,
            pause_desktop=0.4,
            after_esc=0.12,
        )
    if interval is not None:
        SL["interval"] = max(0.08, float(interval))
    log(
        f"[SL] 速度設定：interval={SL['interval']}s, delay_click={SL['delay_click']}s, "
        f"delay_heavy={SL['delay_heavy']}s, heartbeat={SL['heartbeat']}"
    )


def _ensure_adb_connect(dev_id: str) -> None:
    """TCP 設備先嘗試 adb connect，失敗不阻斷（由 connect_device 驗證）。"""
    text = str(dev_id or "").strip()
    if not text or ":" not in text:
        return
    try:
        subprocess.run(["adb", "connect", text], capture_output=True, text=True)
    except Exception:
        pass


def resolve_device_1(cli_device_id: str | None = None) -> str:
    """取得裝置1 ID：優先命令列，其次 settings.json 的 emulators['1']。"""
    if cli_device_id and str(cli_device_id).strip():
        return str(cli_device_id).strip()
    dev = get_device_id("1")
    if dev and str(dev).strip():
        return str(dev).strip()
    raise RuntimeError("找不到裝置1 ID，請先在設定頁填入 ADB 設備 ID #1")


def resolve_device_2(cli_device_id: str | None = None) -> str:
    """取得裝置2 ID：優先命令列，其次 settings.json 的 emulators['2']。"""
    if cli_device_id and str(cli_device_id).strip():
        return str(cli_device_id).strip()
    dev = get_device_id("2")
    if dev and str(dev).strip():
        return str(dev).strip()
    raise RuntimeError("找不到裝置2 ID，請先在設定頁填入 ADB 設備 ID #2")


def dismiss_delay_item_if_present() -> bool:
    """若出現 delay_item / delay_item_2 任一彈窗則點 yes；無則略過。可連關多層彈窗（最多 5 輪）。"""
    for round_idx in range(5):
        shown = None
        for tpl in IMG_DELAY_ITEM_VARIANTS:
            if image_exists(tpl, threshold=0.74):
                shown = tpl
                break
        if not shown:
            return True
        log(f"[SL][步驟1] 偵測到 delay 彈窗（{shown}），先點 yes 關閉（第 {round_idx + 1} 輪）")
        ok = wait_and_click(
            IMG_DELAY_YES,
            timeout=12,
            interval=float(SL["interval"]),
            threshold=0.74,
            delay=float(SL["delay_click"]),
            name="delay_item_yes",
            multiscale=True,
        )
        if not ok:
            log("[SL][步驟1] 失敗：delay 彈窗出現但無法點擊 yes")
            return False
        time.sleep(float(SL["pause_after_popup"]))
    if any(image_exists(tpl, threshold=0.74) for tpl in IMG_DELAY_ITEM_VARIANTS):
        log("[SL][步驟1] 失敗：delay 彈窗關閉超過 5 輪仍殘留")
        return False
    return True


def press_escape_key(device_id: str, count: int = 2) -> None:
    """送 Android KEYCODE_ESCAPE（111）count 次，等同鍵盤 ESC，用於關閉疊層／重置畫面。"""
    kc = "111"
    for _ in range(count):
        subprocess.run(
            ["adb", "-s", device_id, "shell", "input", "keyevent", kc],
            capture_output=True,
        )
        time.sleep(float(SL["esc_key_pause"]))
    log(f"[SL][步驟1] 已送 KEYCODE_ESCAPE（{kc}）×{count} 重置畫面")


def restart_game_on_device_2(device_id: str) -> bool:
    """裝置2：開設定 → 離開遊戲 → 點桌面 game_icon 重新開啟遊戲。"""
    log("=" * 50)
    log(f"[SL][步驟2] 開始：裝置2重啟遊戲（{device_id}）")
    log("=" * 50)

    _ensure_adb_connect(device_id)
    connect_device(device_id)

    # 步驟2-1：點設定（紅點/無紅點皆可）
    if not wait_and_click_any(
        SETTINGS_MENU_TEMPLATES,
        timeout=25,
        interval=float(SL["interval"]),
        threshold=0.76,
        delay=float(SL["delay_click"]),
        name="設定選單(裝置2)",
        labels=SETTINGS_MENU_LABELS,
        multiscale=True,
        heartbeat_sec=int(SL["heartbeat"]),
    ):
        log("[SL][步驟2] 失敗：找不到設定選單")
        return False

    # 步驟2-2：離開遊戲
    if not wait_and_click(
        IMG_LEAVE_BTN,
        timeout=20,
        interval=float(SL["interval"]),
        threshold=0.76,
        delay=float(SL["delay_heavy"]),
        name="leave_btn",
        multiscale=True,
        heartbeat_sec=int(SL["heartbeat"]),
    ):
        log("[SL][步驟2] 失敗：找不到 leave_btn.png")
        return False

    if not wait_and_click(
        IMG_SAVE_YES,
        timeout=20,
        interval=float(SL["interval"]),
        threshold=0.76,
        delay=float(SL["delay_heavy"]),
        name="save_yes_btn",
        multiscale=True,
        heartbeat_sec=int(SL["heartbeat"]),
    ):
        log("[SL][步驟2] 失敗：找不到 save_yes_btn.png")
        return False

    time.sleep(float(SL["pause_desktop"]))

    # 步驟2-3：點桌面遊戲圖示開啟遊戲
    if not wait_and_click(
        IMG_GAME_ICON,
        timeout=30,
        interval=float(SL["interval"]),
        threshold=0.76,
        delay=float(SL["delay_game_icon"]),
        name="game_icon",
        multiscale=True,
        heartbeat_sec=int(SL["heartbeat_game"]),
    ):
        log("[SL][步驟2] 失敗：找不到 game_icon.png")
        return False

    log("[SL][步驟2] 裝置2已透過圖示重新開啟遊戲")

    return True


def goal_pet_visible(threshold: float = GOAL_PET_STOP_THRESHOLD) -> bool:
    """畫面上已可辨識目標欄 goal_pet（左滑終止條件）。"""
    found, _, _, _ = load_and_match(IMG_GOAL_PET, threshold=threshold, multiscale=True)
    return bool(found)


def swipe_select_item_bar_from(cx: int, cy: int) -> None:
    """已知道具列錨點 (cx,cy) 時執行左滑（不依賴當下是否還能匹配到 select_item 圖）。"""
    x1 = int(cx) + SWIPE_SELECT_START_OFFSET_X
    y1 = int(cy) + SWIPE_SELECT_Y_OFFSET
    x2 = int(cx) + SWIPE_SELECT_END_OFFSET_X
    y2 = y1
    x1 = max(60, x1)
    y1 = max(60, y1)
    x2 = max(30, x2)
    log(f"[SL][步驟3] 左滑道具列：({x1},{y1}) -> ({x2},{y2})")
    swipe(x1, y1, x2, y2, duration=SWIPE_SELECT_DURATION)


def swipe_select_item_left_once() -> bool:
    """
    重新匹配 select_item 中心並左滑一輪；若畫面已變導致匹配失敗，請改用錨點 + swipe_select_item_bar_from。
    """
    found, cx, cy, sim = load_and_match(IMG_SELECT_ITEM, threshold=0.71, multiscale=True)
    if not found or cx is None or cy is None:
        log(f"[SL][步驟3] 找不到 {IMG_SELECT_ITEM}（相似度 {sim:.3f}）")
        return False
    swipe_select_item_bar_from(int(cx), int(cy))
    return True


def tap_purchase_below_goal_pet() -> bool:
    """
    選欄：優先用 select_goal 標記 X 對齊 goal_pet；否則用 select_item 的 X；再否則取相似度最高的一欄。
    再嘗試多組 (X,Y) 點「購買」，直到確認視窗出現並點「是」。
    """
    marker_name = None
    mx = my = 0
    for name in SELECT_GOAL_MARKERS:
        found, x, y, _sim = load_and_match(name, threshold=0.72, multiscale=True)
        if found and x is not None and y is not None:
            marker_name, mx, my = name, int(x), int(y)
            break
    peaks = load_and_match_all_peaks(
        IMG_GOAL_PET,
        threshold=0.68,
        multiscale=True,
        max_peaks=10,
    )
    if not peaks:
        log("[SL][步驟3] 失敗：找不到任何 goal_pet 匹配")
        return False

    si_x: int | None = None
    si_y: int | None = None
    found_si, six, siy, _sim_si = load_and_match(IMG_SELECT_ITEM, threshold=0.68, multiscale=True)
    if found_si and six is not None and siy is not None:
        si_x = int(six)
        si_y = int(siy)

    if marker_name is not None:
        px, py, psim = min(peaks, key=lambda t: abs(t[0] - mx))
        log(
            f"[SL][步驟3] 欄位對齊：標記={marker_name}@({mx},{my})，"
            f"選用 goal_pet@({px},{py}) sim={psim:.3f}，Δx={abs(px - mx)}"
        )
    elif si_x is not None and si_y is not None:
        # 限制在道具列附近挑選 goal_pet，避免匹配到上方排行榜同圖示
        near_bar = [p for p in peaks if abs(p[1] - si_y) <= 180]
        source = near_bar if near_bar else peaks
        px, py, psim = min(source, key=lambda t: abs(t[0] - si_x))
        log(
            f"[SL][步驟3] 無 select_goal 標記，以 select_item@({si_x},{si_y}) 對齊欄位："
            f"goal_pet@({px},{py}) sim={psim:.3f}"
        )
    else:
        px, py, psim = max(peaks, key=lambda t: t[2])
        log(
            f"[SL][步驟3] 無標記且未見 select_item，取 goal_pet 相似度最高："
            f"@({px},{py}) sim={psim:.3f}"
        )

    # 只以已選中的 goal_pet 中心 X 為主，避免混用其它基準導致 X 偏移過大
    x_bases = tuple(int(px) + int(dx) for dx in GOAL_PET_BUY_X_FINE)

    def save_buy_debug_markup(tap_x: int, tap_y: int, base_x: int, base_y: int, dx: int, dy: int) -> None:
        """截圖並標記購買點擊位置，便於手動校正偏移值。"""
        if not DEBUG_SAVE_BUY_MARKUP:
            return
        try:
            mark_path = os.path.join(CACHE_DIR, "buy_dbg_mark.png")
            take_screenshot(save_path=mark_path)
            img = cv2.imread(mark_path)
            if img is None:
                return
            h, w = img.shape[:2]
            tx = max(0, min(int(tap_x), w - 1))
            ty = max(0, min(int(tap_y), h - 1))
            bx = max(0, min(int(base_x), w - 1))
            by = max(0, min(int(base_y), h - 1))
            # 紅色框：當下點擊座標（購買按鈕目標）
            box_half = 18
            rx1 = max(0, tx - box_half)
            ry1 = max(0, ty - box_half)
            rx2 = min(w - 1, tx + box_half)
            ry2 = min(h - 1, ty + box_half)
            cv2.rectangle(img, (rx1, ry1), (rx2, ry2), (0, 0, 255), 2)
            cv2.circle(img, (bx, by), 8, (255, 180, 0), 2)
            cv2.putText(
                img,
                f"tap=({tx},{ty}) base=({bx},{by}) dx={dx} dy={dy}",
                (18, 34),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.72,
                (0, 0, 255),
                2,
                cv2.LINE_AA,
            )
            cv2.imwrite(mark_path, img)
            log(f"[SL][步驟3] 已輸出購買標記圖：{mark_path}")
        except Exception as e:
            log(f"[SL][步驟3] 輸出購買標記圖失敗：{e}")

    for dy in GOAL_PET_BUY_Y_FINE:
        tap_y = int(py) + GOAL_PET_BUY_OFFSET_Y + int(dy)
        for bx in x_bases:
            tap_x = int(bx)
            dx = tap_x - int(px)
            save_buy_debug_markup(tap_x, tap_y, int(px), int(py), int(dx), int(dy))
            click_fixed(
                tap_x,
                tap_y,
                delay=float(SL["delay_click"]),
                name=f"購買嘗試({tap_x},{tap_y})",
            )
            if wait_and_click(
                IMG_PURCHASE_CONFIRM_YES,
                timeout=3.8,
                interval=float(SL["interval"]),
                threshold=0.68,
                delay=float(SL["delay_click"]),
                name="購買確認_是",
                multiscale=True,
            ):
                log("[SL][步驟3] 已出現購買確認並點擊「是」")
                return True
    log(
        "[SL][步驟3] 失敗：多次座標嘗試仍未出現確認視窗。"
        "請調整 GOAL_PET_BUY_OFFSET_Y / GOAL_PET_BUY_Y_FINE，"
        "或將確認鍵另存模板並修改 IMG_PURCHASE_CONFIRM_YES"
    )
    return False


def wash_items_on_device_2(device_id: str) -> bool:
    """裝置2：鑽石入口 → 等 select_item → 以其為基準反覆左滑直到 goal_pet → 點購買並確認。"""
    log("=" * 50)
    log(f"[SL][步驟3] 開始：裝置2洗道具（{device_id}）")
    log("=" * 50)
    connect_device(device_id)

    if not wait_and_click(
        IMG_DIAMOND_ITEM,
        timeout=90,
        interval=float(SL["interval"]),
        threshold=0.72,
        delay=float(SL["delay_click"]),
        name="diamond_item",
        multiscale=True,
        heartbeat_sec=int(SL["heartbeat"]),
    ):
        log("[SL][步驟3] 失敗：逾時或找不到 diamond_item.png")
        return False

    # 先等到橫列參考圖，並記錄錨點；滑動後 select_item 可能暫時匹配不到，仍用錨點左滑直到 goal_pet
    log("[SL][步驟3] 等待 select_item 出現…")
    t0 = time.time()
    hb_next = t0
    hb_sec = int(SL["heartbeat"])
    anchor_cx: int | None = None
    anchor_cy: int | None = None
    while time.time() - t0 < float(SELECT_ITEM_WAIT_SEC):
        found_si, acx, acy, sim_si = load_and_match(IMG_SELECT_ITEM, threshold=0.71, multiscale=True)
        if found_si and acx is not None and acy is not None:
            anchor_cx, anchor_cy = int(acx), int(acy)
            log(f"[SL][步驟3] 已偵測 select_item（sim={sim_si:.3f}），錨點=({anchor_cx},{anchor_cy})，開始左滑直到 goal_pet")
            break
        if hb_sec > 0 and time.time() >= hb_next:
            log(f"[SL][步驟3] 仍等待 select_item… 目前最高相似度 {sim_si:.3f}")
            hb_next = time.time() + hb_sec
        time.sleep(float(SL["interval"]))
    else:
        log("[SL][步驟3] 失敗：逾時未見 select_item.png")
        return False

    for i in range(WASH_SWIPE_MAX):
        # 每輪先判斷是否已出現目標，避免上一滑後 goal_pet 已出現卻仍強制找 select_item
        if goal_pet_visible(threshold=GOAL_PET_STOP_THRESHOLD):
            log(f"[SL][步驟3] 已偵測 goal_pet（第 {i + 1} 輪，無需再滑）")
            break
        # 能匹配到 select_item 就更新錨點，否則沿用上一輪錨點繼續滑
        found_si, ncx, ncy, sim_si = load_and_match(IMG_SELECT_ITEM, threshold=0.71, multiscale=True)
        if found_si and ncx is not None and ncy is not None:
            anchor_cx, anchor_cy = int(ncx), int(ncy)
        elif anchor_cx is not None and anchor_cy is not None:
            log(f"[SL][步驟3] 本輪未匹配到 select_item（sim={sim_si:.3f}），沿用錨點 ({anchor_cx},{anchor_cy}) 左滑")
        else:
            log("[SL][步驟3] 無錨點可滑動，略過本輪")
            time.sleep(float(SL["interval"]))
            continue
        swipe_select_item_bar_from(anchor_cx, anchor_cy)
        time.sleep(float(SL["delay_click"]))
        if goal_pet_visible(threshold=GOAL_PET_STOP_THRESHOLD):
            log(f"[SL][步驟3] 左滑後已偵測 goal_pet（第 {i + 1} 輪）")
            break
    else:
        if not goal_pet_visible(threshold=GOAL_PET_STOP_THRESHOLD):
            log("[SL][步驟3] 失敗：滑動多次仍未偵測到 goal_pet")
            return False
        log("[SL][步驟3] 最後一滑後偵測到 goal_pet")

    if not tap_purchase_below_goal_pet():
        return False

    log("[SL][步驟3] 裝置2洗道具流程完成")
    return True


def save_progress_on_device_1(device_id: str) -> bool:
    """SL 第一步：裝置1儲存遊戲進度。"""
    log("=" * 50)
    log(f"[SL][步驟1] 開始：裝置1儲存進度（{device_id}）")
    log("=" * 50)

    # 先連線裝置1
    _ensure_adb_connect(device_id)
    connect_device(device_id)

    # 步驟1-1：點擊設定入口（有紅點/無紅點都可）
    has_red_dot = image_exists(IMG_SETTINGS_RED, threshold=0.76)
    if has_red_dot:
        log("[SL][步驟1] 偵測到紅點：代表活動發放道具，不影響 SL 儲存流程")
    else:
        log("[SL][步驟1] 未偵測到紅點，照常執行 SL 儲存流程")

    if not dismiss_delay_item_if_present():
        return False

    if not wait_and_click_any(
        SETTINGS_MENU_TEMPLATES,
        timeout=20,
        interval=float(SL["interval"]),
        threshold=0.76,
        delay=float(SL["delay_click"]),
        name="設定選單",
        labels=SETTINGS_MENU_LABELS,
        multiscale=True,
        heartbeat_sec=int(SL["heartbeat"]),
    ):
        log("[SL][步驟1] 失敗：找不到設定選單（settings_red/settings_no_red）")
        return False

    # 步驟1-2：點擊 sellections
    if not wait_and_click(
        IMG_SELECTIONS,
        timeout=20,
        interval=float(SL["interval"]),
        threshold=0.76,
        delay=float(SL["delay_click"]),
        name="sellections",
        multiscale=True,
        heartbeat_sec=int(SL["heartbeat"]),
    ):
        log("[SL][步驟1] 失敗：找不到 sellections.png")
        return False

    # 步驟1-3：點擊 save_1_btn
    if not wait_and_click(
        IMG_SAVE_1,
        timeout=20,
        interval=float(SL["interval"]),
        threshold=0.76,
        delay=float(SL["delay_heavy"]),
        name="save_1_btn",
        multiscale=True,
        heartbeat_sec=int(SL["heartbeat"]),
    ):
        log("[SL][步驟1] 失敗：找不到 save_1_btn.png")
        return False

    # 步驟1-4：判斷 save_2_btn 字樣，若有才接續
    if not wait_for_image(
        IMG_SAVE_2,
        timeout=SAVE_PROMPT_WAIT_SEC,
        interval=float(SL["interval"]),
        threshold=SAVE_PROMPT_THRESHOLD,
        multiscale=True,
    ):
        log("[SL][步驟1] 失敗：未偵測到 save_2_btn.png，流程中止")
        return False

    # 步驟1-5：點擊 save_yes_btn
    if not wait_and_click(
        IMG_SAVE_YES,
        timeout=20,
        interval=float(SL["interval"]),
        threshold=0.76,
        delay=float(SL["delay_heavy"]),
        name="save_yes_btn",
        multiscale=True,
        heartbeat_sec=int(SL["heartbeat"]),
    ):
        log("[SL][步驟1] 失敗：找不到 save_yes_btn.png")
        return False

    # 步驟1-6: 第二段存檔 UI（save_3）可能較晚出現；與按鈕點擊相同條件辨識
    time.sleep(0.45)
    if not wait_for_image(
        IMG_SAVE_3,
        timeout=SAVE_PROMPT_WAIT_SEC,
        interval=float(SL["interval"]),
        threshold=SAVE_PROMPT_THRESHOLD,
        multiscale=True,
    ):
        log("[SL][步驟1] 失敗：未偵測到 save_3_btn.png，流程中止")
        return False
    log("[SL][步驟1] 已偵測 save_3_btn.png，接續下一步")

    # 步驟1-7:點擊 save_yes_btn
    if not wait_and_click(
        IMG_SAVE_YES,
        timeout=20,
        interval=float(SL["interval"]),
        threshold=0.76,
        delay=float(SL["delay_heavy"]),
        name="save_yes_btn",
        multiscale=True,
        heartbeat_sec=int(SL["heartbeat"]),
    ):
        log("[SL][步驟1] 失敗：找不到 save_yes_btn.png")
        return False

    # 步驟1-8:點擊設定選單
    if not dismiss_delay_item_if_present():
        return False

    if not wait_and_click_any(
        SETTINGS_MENU_TEMPLATES,
        timeout=20,
        interval=float(SL["interval"]),
        threshold=0.76,
        delay=float(SL["delay_click"]),
        name="設定選單",
        labels=SETTINGS_MENU_LABELS,
        multiscale=True,
        heartbeat_sec=int(SL["heartbeat"]),
    ):
        log("[SL][步驟1] 失敗：找不到設定選單（settings_red/settings_no_red）")
        return False

    # 步驟1-9：點擊 sellections
    if not wait_and_click(
        IMG_SELECTIONS,
        timeout=20,
        interval=float(SL["interval"]),
        threshold=0.76,
        delay=float(SL["delay_click"]),
        name="sellections",
        multiscale=True,
        heartbeat_sec=int(SL["heartbeat"]),
    ):
        log("[SL][步驟1] 失敗：找不到 sellections.png")
        return False

    # 步驟1-10：點擊 save_1_btn
    if not wait_and_click(
        IMG_SAVE_1,
        timeout=20,
        interval=float(SL["interval"]),
        threshold=0.76,
        delay=float(SL["delay_heavy"]),
        name="save_1_btn",
        multiscale=True,
        heartbeat_sec=int(SL["heartbeat"]),
    ):
        log("[SL][步驟1] 失敗：找不到 save_1_btn.png")
        return False

    # 步驟1-11：判斷 save_2_btn 字樣，若有才接續
    if not wait_for_image(
        IMG_SAVE_2,
        timeout=SAVE_PROMPT_WAIT_SEC,
        interval=float(SL["interval"]),
        threshold=SAVE_PROMPT_THRESHOLD,
        multiscale=True,
    ):
        log("[SL][步驟1] 失敗：未偵測到 save_2_btn.png，流程中止")
        return False

    # 步驟1-12：點擊 save_yes_btn
    if not wait_and_click(
        IMG_SAVE_YES,
        timeout=20,
        interval=float(SL["interval"]),
        threshold=0.76,
        delay=float(SL["delay_heavy"]),
        name="save_yes_btn",
        multiscale=True,
        heartbeat_sec=int(SL["heartbeat"]),
    ):
        log("[SL][步驟1] 失敗：找不到 save_yes_btn.png")
        return False

    # 儲存完成後連按兩次 ESC，關閉選單／重置畫面
    press_escape_key(device_id, count=2)
    time.sleep(float(SL["after_esc"]))

    log("[SL][步驟1] 裝置1完成儲存進度")
    return True

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device-id", default="", help="指定裝置1 ID，未填則讀 settings.json 的 emulators[1]")
    parser.add_argument("--device-id-2", default="", help="指定裝置2 ID，未填則讀 settings.json 的 emulators[2]")
    parser.add_argument("--fast", action="store_true", help="較快輪詢與較短點擊後等待（較吃 CPU/ADB；不穩再關閉）")
    parser.add_argument("--interval", type=float, default=None, metavar="SEC", help="只覆寫輪詢間隔（秒），例 0.22")
    parser.add_argument(
        "--max-loops",
        type=int,
        default=80,
        metavar="N",
        help="完整 SL（步驟1→2→3）最多重跑幾輪，直到偵測 sl_goal；預設 80",
    )
    args = parser.parse_args()

    configure_sl_speed(fast=args.fast, interval=args.interval)

    set_telegram_log_forward(False)
    device_1 = resolve_device_1(args.device_id)
    try:
        device_2 = resolve_device_2(args.device_id_2)
    except RuntimeError as e:
        log(f"[SL] {e}")
        send_telegram(f"[SL] 無法取得裝置2：{e}")
        return 1

    max_loops = max(1, int(args.max_loops))
    for attempt in range(1, max_loops + 1):
        log("=" * 50)
        log(f"[SL] 第 {attempt}/{max_loops} 輪：開始完整流程（步驟1→2→3）")
        log("=" * 50)

        if not save_progress_on_device_1(device_1):
            send_telegram(f"[SL] 步驟1失敗：裝置1儲存進度失敗（{device_1}）")
            return 1

        if not restart_game_on_device_2(device_2):
            send_telegram(f"[SL] 步驟2失敗：裝置2重啟遊戲失敗（{device_2}）")
            return 1

        if not wash_items_on_device_2(device_2):
            send_telegram(f"[SL] 步驟3失敗：裝置2洗道具失敗（{device_2}）")
            return 1

        # 洗道具結束時已在裝置2；截圖檢查 SL 成功目標
        connect_device(device_2)
        found_sg, _, _, sim_sg = load_and_match(
            IMG_SL_GOAL, threshold=SL_GOAL_MATCH_THRESHOLD, multiscale=True
        )
        if found_sg:
            msg = f"[SL] 完成：第 {attempt} 輪後裝置2 已偵測 {IMG_SL_GOAL}（相似度 {sim_sg:.3f}）"
            log(msg)
            send_telegram(msg)
            return 0

        log(f"[SL] 第 {attempt} 輪結束：尚未偵測 {IMG_SL_GOAL}，將重跑整輪…")
        # 裝置2：關閉可能殘留的彈窗／重置畫面，再進入下一輪
        connect_device(device_2)
        press_escape_key(device_2, count=4)
        time.sleep(float(SL["interval"]))

    send_telegram(f"[SL] 失敗：已執行 {max_loops} 輪仍未偵測 {IMG_SL_GOAL}（裝置2）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

