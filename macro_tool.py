# -*- coding: utf-8 -*-
# 巨集自動化工具
# 功能: 自訂鍵盤與滑鼠動作序列, 支援移動滑鼠座標, 視窗尋找與固定位置,
#       畫面顏色判斷, 畫面圖片辨識判斷 (以上兩者符合條件時執行子步驟),
#       設定快捷鍵開始或停止巡迴執行, 使用全域快捷鍵記錄座標/顏色/圖片區塊,
#       並可儲存與載入設定檔
#
# 需求套件: pip install keyboard pynput pywin32 pillow opencv-python numpy
# 注意事項:
# 1. 在 Windows 上, keyboard 套件要監聽全域快捷鍵通常需要以系統管理員身分執行
# 2. 尋找視窗並固定位置功能僅支援 Windows, 需要 pywin32
# 3. 若螢幕顯示縮放比例不是 100%, 擷取到的座標與實際畫面位置可能有落差,
#    建議先將顯示器縮放設回 100% 再進行座標, 顏色, 圖片區塊的擷取與判斷

import json
import os
import time
import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

import keyboard
from pynput.mouse import Controller as MouseController, Button
from PIL import ImageGrab

import numpy as np
import cv2

try:
    import win32gui
    import win32con
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

# 滑鼠按鈕對照表
MOUSE_BUTTON_MAP = {
    "left": Button.left,
    "right": Button.right,
    "middle": Button.middle,
}

# 動作類型對照表, 用於介面顯示文字與內部儲存代號互相轉換
STEP_TYPE_DISPLAY = {
    "key_down": "按下按鍵",
    "key_up": "彈起按鍵",
    "key_click": "按一下按鍵",
    "move_mouse": "移動滑鼠到座標",
    "mouse_down": "滑鼠按下",
    "mouse_up": "滑鼠放開",
    "mouse_click": "滑鼠點擊",
    "wait": "等待秒數",
    "find_window_move": "尋找視窗並固定位置",
    "if_color": "判斷顏色 (符合則執行子步驟)",
    "if_image": "判斷圖片 (符合則執行子步驟)",
}
STEP_TYPE_REVERSE = {v: k for k, v in STEP_TYPE_DISPLAY.items()}

# 頂層步驟允許的所有類型
TOP_LEVEL_TYPES = [
    "key_down", "key_up", "key_click",
    "move_mouse", "mouse_down", "mouse_up", "mouse_click",
    "wait", "find_window_move", "if_color", "if_image",
]

# 判斷條件內部子步驟允許的類型 (不包含判斷類型, 避免無限巢狀)
CHILD_TYPES = [
    "key_down", "key_up", "key_click",
    "move_mouse", "mouse_down", "mouse_up", "mouse_click",
    "wait", "find_window_move",
]

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "macros.json")
PICS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pics")
os.makedirs(PICS_DIR, exist_ok=True)


# ---------------------- 共用工具函式 ----------------------

def hex_to_rgb(hex_str):
    # 將十六進位色碼字串轉成 (r, g, b) 數值
    hex_str = hex_str.strip().lstrip("#")
    r = int(hex_str[0:2], 16)
    g = int(hex_str[2:4], 16)
    b = int(hex_str[4:6], 16)
    return r, g, b


def find_and_move_window(title_keyword, x, y):
    # 依標題關鍵字尋找可見視窗, 找到後移動到指定座標 (不改變視窗大小)
    if not HAS_WIN32:
        return False
    result = {"hwnd": None}

    def enum_handler(hwnd, ctx):
        if win32gui.IsWindowVisible(hwnd) and title_keyword in win32gui.GetWindowText(hwnd):
            ctx["hwnd"] = hwnd

    win32gui.EnumWindows(enum_handler, result)
    hwnd = result["hwnd"]
    if hwnd:
        win32gui.SetWindowPos(hwnd, 0, int(x), int(y), 0, 0, win32con.SWP_NOSIZE | win32con.SWP_NOZORDER)
        return True
    return False


def check_color(x, y, hex_color, tolerance):
    # 判斷畫面上指定座標的顏色是否在容許誤差範圍內符合目標顏色
    try:
        img = ImageGrab.grab(bbox=(int(x), int(y), int(x) + 1, int(y) + 1))
        r, g, b = img.getpixel((0, 0))[:3]
    except Exception:
        return False
    tr, tg, tb = hex_to_rgb(hex_color)
    return abs(r - tr) <= tolerance and abs(g - tg) <= tolerance and abs(b - tb) <= tolerance


def check_image(region, image_path, confidence):
    # 在指定區塊內尋找目標圖片, 使用樣板比對, confidence 為相似度門檻 (0 到 1)
    try:
        x1, y1, x2, y2 = [int(v) for v in region]
        if x2 <= x1 or y2 <= y1:
            return False
        screenshot = ImageGrab.grab(bbox=(x1, y1, x2, y2))
        screen_np = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
        template = cv2.imread(image_path, cv2.IMREAD_COLOR)
        if template is None:
            return False
        if template.shape[0] > screen_np.shape[0] or template.shape[1] > screen_np.shape[1]:
            return False
        result = cv2.matchTemplate(screen_np, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(result)
        return max_val >= confidence
    except Exception:
        return False


# ---------------------- 步驟編輯器元件 ----------------------
# 此元件封裝了 "步驟清單 + 上移/下移/刪除/編輯子步驟 + 新增步驟表單"
# 主畫面的頂層步驟, 以及判斷條件內的子步驟, 都使用同一個元件實作, 差別只在
# allowed_types 傳入不同的可選動作類型清單, 以及 steps 傳入不同的清單參照

class StepsEditor:
    def __init__(self, master, app, steps, allowed_types):
        self.app = app
        self.steps = steps
        self.allowed_types = allowed_types

        self.frame = ttk.LabelFrame(master, text="動作步驟 (依序由上往下執行)")
        self.frame.pack(fill="both", expand=True, padx=5, pady=5)
        # 滑鼠移入此區域時, 將此編輯器設為目前作用中的編輯器, 供全域快捷鍵記錄座標使用
        self.frame.bind("<Enter>", lambda e: self.set_active())

        self.build_ui()
        self.refresh()

    def set_active(self):
        self.app.active_step_editor = self

    def build_ui(self):
        list_frame = ttk.Frame(self.frame)
        list_frame.pack(fill="both", expand=True, padx=5, pady=5)

        self.listbox = tk.Listbox(list_frame, height=10)
        self.listbox.pack(side="left", fill="both", expand=True)

        btn_frame = ttk.Frame(list_frame)
        btn_frame.pack(side="left", fill="y", padx=5)
        ttk.Button(btn_frame, text="上移", command=self.move_up).pack(fill="x", pady=2)
        ttk.Button(btn_frame, text="下移", command=self.move_down).pack(fill="x", pady=2)
        ttk.Button(btn_frame, text="刪除", command=self.delete_step).pack(fill="x", pady=2)
        ttk.Button(btn_frame, text="編輯子步驟", command=self.edit_children).pack(fill="x", pady=(10, 2))

        add_frame = ttk.LabelFrame(self.frame, text="新增動作步驟")
        add_frame.pack(fill="x", padx=5, pady=5)

        ttk.Label(add_frame, text="動作類型:").grid(row=0, column=0, padx=5, pady=5, sticky="w")
        display_values = [STEP_TYPE_DISPLAY[t] for t in self.allowed_types]
        self.type_var = tk.StringVar(value=display_values[0])
        type_combo = ttk.Combobox(
            add_frame, textvariable=self.type_var, values=display_values, state="readonly", width=22
        )
        type_combo.grid(row=0, column=1, padx=5, pady=5, sticky="w")
        type_combo.bind("<<ComboboxSelected>>", lambda e: self.rebuild_fields())

        ttk.Button(add_frame, text="新增此步驟", command=self.add_step).grid(row=0, column=2, padx=10, pady=5)

        self.fields_frame = ttk.Frame(add_frame)
        self.fields_frame.grid(row=1, column=0, columnspan=4, sticky="w")

        self.rebuild_fields()

    def get_selected_type(self):
        return STEP_TYPE_REVERSE[self.type_var.get()]

    def add_field(self, label, var, row, col, width=15, colspan=1):
        ttk.Label(self.fields_frame, text=label).grid(row=row, column=col, padx=5, pady=3, sticky="w")
        entry = ttk.Entry(self.fields_frame, textvariable=var, width=width)
        entry.grid(row=row, column=col + 1, padx=5, pady=3, sticky="w", columnspan=colspan)
        entry.bind("<FocusIn>", lambda e: self.set_active())
        return entry

    def rebuild_fields(self):
        # 依照目前選擇的動作類型, 重新產生對應的輸入欄位
        for w in self.fields_frame.winfo_children():
            w.destroy()

        t = self.get_selected_type()

        if t in ("key_down", "key_up", "key_click"):
            self.key_var = tk.StringVar()
            key_entry = self.add_field("按鍵名稱:", self.key_var, row=0, col=0, width=15)
            ttk.Button(self.fields_frame, text="錄製",
                       command=lambda: self.app.start_key_capture(self.key_var)).grid(row=0, column=2, padx=5, pady=3)
            ttk.Label(self.fields_frame, text="手動輸入或按「錄製」按鍵").grid(
                row=1, column=0, columnspan=4, sticky="w", padx=5
            )

        elif t == "move_mouse":
            self.x_var = tk.StringVar()
            self.y_var = tk.StringVar()
            self.add_field("X座標:", self.x_var, row=0, col=0, width=8)
            self.add_field("Y座標:", self.y_var, row=0, col=2, width=8)
            ttk.Label(self.fields_frame, text="按下設定的抓取座標快捷鍵, 可自動填入目前滑鼠位置").grid(
                row=1, column=0, columnspan=4, sticky="w", padx=5
            )

        elif t in ("mouse_down", "mouse_up", "mouse_click"):
            self.mouse_var = tk.StringVar(value="left")
            ttk.Label(self.fields_frame, text="滑鼠按鈕:").grid(row=0, column=0, padx=5, pady=3, sticky="w")
            ttk.Combobox(
                self.fields_frame, textvariable=self.mouse_var,
                values=["left", "right", "middle"], state="readonly", width=10
            ).grid(row=0, column=1, padx=5, pady=3, sticky="w")
            ttk.Label(
                self.fields_frame, text="此動作在目前滑鼠所在位置執行, 請先用 移動滑鼠到座標 步驟定位, 避免滑鼠亂跑"
            ).grid(row=1, column=0, columnspan=4, sticky="w", padx=5)

        elif t == "wait":
            self.wait_var = tk.StringVar(value="0.1")
            self.add_field("秒數:", self.wait_var, row=0, col=0, width=8)

        elif t == "find_window_move":
            self.window_title_var = tk.StringVar()
            self.x_var = tk.StringVar()
            self.y_var = tk.StringVar()
            self.add_field("視窗標題(部分符合):", self.window_title_var, row=0, col=0, width=20)
            self.add_field("固定於X:", self.x_var, row=1, col=0, width=8)
            self.add_field("固定於Y:", self.y_var, row=1, col=2, width=8)

        elif t == "if_color":
            self.x_var = tk.StringVar()
            self.y_var = tk.StringVar()
            self.color_var = tk.StringVar()
            self.tolerance_var = tk.StringVar(value="20")
            self.add_field("X座標:", self.x_var, row=0, col=0, width=8)
            self.add_field("Y座標:", self.y_var, row=0, col=2, width=8)
            self.add_field("顏色(RRGGBB):", self.color_var, row=1, col=0, width=10)
            self.add_field("容許誤差:", self.tolerance_var, row=1, col=2, width=8)
            ttk.Label(
                self.fields_frame, text="按下設定的抓取顏色快捷鍵, 可自動抓取目前滑鼠位置與該處顏色"
            ).grid(row=2, column=0, columnspan=4, sticky="w", padx=5)

        elif t == "if_image":
            self.rx1_var = tk.StringVar()
            self.ry1_var = tk.StringVar()
            self.rx2_var = tk.StringVar()
            self.ry2_var = tk.StringVar()
            self.image_path_var = tk.StringVar()
            self.confidence_var = tk.StringVar(value="0.9")
            self.add_field("區塊左上X:", self.rx1_var, row=0, col=0, width=8)
            self.add_field("區塊左上Y:", self.ry1_var, row=0, col=2, width=8)
            self.add_field("區塊右下X:", self.rx2_var, row=1, col=0, width=8)
            self.add_field("區塊右下Y:", self.ry2_var, row=1, col=2, width=8)
            self.add_field("圖片檔案:", self.image_path_var, row=2, col=0, width=32, colspan=3)
            self.add_field("辨識率(0~1):", self.confidence_var, row=3, col=0, width=8)
            ttk.Label(
                self.fields_frame,
                text="先在區塊左上角按下區塊起點快捷鍵, 移到右下角再按區塊終點快捷鍵, 會自動擷取並儲存圖片",
            ).grid(row=4, column=0, columnspan=4, sticky="w", padx=5)

    def format_step(self, step):
        t = step["type"]
        display = STEP_TYPE_DISPLAY[t]
        if t in ("key_down", "key_up", "key_click"):
            return "{} [{}]".format(display, step["key"])
        if t in ("mouse_down", "mouse_up", "mouse_click"):
            return "{} [{}]".format(display, step["button"])
        if t == "move_mouse":
            return "{} [{}, {}]".format(display, step["x"], step["y"])
        if t == "wait":
            return "{} [{} 秒]".format(display, step["seconds"])
        if t == "find_window_move":
            return "{} [標題含:{} -> ({},{})]".format(display, step["title"], step["x"], step["y"])
        if t == "if_color":
            return "{} [({},{}) = #{} 誤差{}] -> 內含{}個子步驟".format(
                display, step["x"], step["y"], step["color"], step["tolerance"], len(step.get("then", []))
            )
        if t == "if_image":
            return "{} [區塊({},{})-({},{}) 辨識率{}] -> 內含{}個子步驟".format(
                display, step["rx1"], step["ry1"], step["rx2"], step["ry2"], step["confidence"], len(step.get("then", []))
            )
        return display

    def refresh(self):
        self.listbox.delete(0, tk.END)
        for idx, step in enumerate(self.steps, start=1):
            self.listbox.insert(tk.END, "{}. {}".format(idx, self.format_step(step)))

    def get_selected_index(self):
        sel = self.listbox.curselection()
        if not sel:
            return None
        return sel[0]

    def add_step(self):
        t = self.get_selected_type()
        try:
            if t in ("key_down", "key_up", "key_click"):
                key = self.key_var.get().strip()
                if not key:
                    messagebox.showwarning("輸入錯誤", "請輸入按鍵名稱")
                    return
                step = {"type": t, "key": key}

            elif t == "move_mouse":
                step = {"type": t, "x": int(self.x_var.get()), "y": int(self.y_var.get())}

            elif t in ("mouse_down", "mouse_up", "mouse_click"):
                step = {"type": t, "button": self.mouse_var.get()}

            elif t == "wait":
                step = {"type": t, "seconds": float(self.wait_var.get())}

            elif t == "find_window_move":
                title = self.window_title_var.get().strip()
                if not title:
                    messagebox.showwarning("輸入錯誤", "請輸入視窗標題關鍵字")
                    return
                step = {"type": t, "title": title, "x": int(self.x_var.get()), "y": int(self.y_var.get())}

            elif t == "if_color":
                color = self.color_var.get().strip().lstrip("#").upper()
                if len(color) != 6:
                    messagebox.showwarning("輸入錯誤", "顏色請輸入 6 碼十六進位, 例如 554941")
                    return
                step = {
                    "type": t,
                    "x": int(self.x_var.get()),
                    "y": int(self.y_var.get()),
                    "color": color,
                    "tolerance": int(self.tolerance_var.get()),
                    "then": [],
                }

            elif t == "if_image":
                image_path = self.image_path_var.get().strip()
                if not image_path or not os.path.exists(image_path):
                    messagebox.showwarning("輸入錯誤", "請先使用區塊快捷鍵擷取圖片, 或確認圖片檔案存在")
                    return
                step = {
                    "type": t,
                    "rx1": int(self.rx1_var.get()),
                    "ry1": int(self.ry1_var.get()),
                    "rx2": int(self.rx2_var.get()),
                    "ry2": int(self.ry2_var.get()),
                    "image_path": image_path,
                    "confidence": float(self.confidence_var.get()),
                    "then": [],
                }
            else:
                return
        except ValueError:
            messagebox.showwarning("輸入錯誤", "數值欄位請輸入正確的數字格式")
            return

        self.steps.append(step)
        self.refresh()

    def move_up(self):
        idx = self.get_selected_index()
        if idx is None or idx == 0:
            return
        self.steps[idx - 1], self.steps[idx] = self.steps[idx], self.steps[idx - 1]
        self.refresh()
        self.listbox.selection_set(idx - 1)

    def move_down(self):
        idx = self.get_selected_index()
        if idx is None or idx >= len(self.steps) - 1:
            return
        self.steps[idx + 1], self.steps[idx] = self.steps[idx], self.steps[idx + 1]
        self.refresh()
        self.listbox.selection_set(idx + 1)

    def delete_step(self):
        idx = self.get_selected_index()
        if idx is None:
            return
        del self.steps[idx]
        self.refresh()

    def edit_children(self):
        idx = self.get_selected_index()
        if idx is None:
            messagebox.showinfo("提示", "請先選擇一個步驟")
            return
        step = self.steps[idx]
        if step["type"] not in ("if_color", "if_image"):
            messagebox.showinfo("提示", "只有判斷顏色或判斷圖片的步驟可以編輯子步驟")
            return
        step.setdefault("then", [])

        win = tk.Toplevel(self.app.root)
        win.title("編輯子步驟 - 條件成立時依序執行")
        win.geometry("680x480")
        StepsEditor(win, self.app, step["then"], CHILD_TYPES)

    # ---------------------- 供全域快捷鍵記錄呼叫 ----------------------

    def capture_position(self, x, y):
        t = self.get_selected_type()
        if t in ("move_mouse", "find_window_move", "if_color") and hasattr(self, "x_var"):
            self.x_var.set(str(x))
            self.y_var.set(str(y))

    def capture_color(self, x, y, hex_color):
        if self.get_selected_type() == "if_color" and hasattr(self, "color_var"):
            self.x_var.set(str(x))
            self.y_var.set(str(y))
            self.color_var.set(hex_color)

    def capture_region_start(self, x, y):
        if self.get_selected_type() == "if_image" and hasattr(self, "rx1_var"):
            self.rx1_var.set(str(x))
            self.ry1_var.set(str(y))

    def capture_region_end(self, x, y):
        if self.get_selected_type() == "if_image" and hasattr(self, "rx2_var"):
            self.rx2_var.set(str(x))
            self.ry2_var.set(str(y))
            self.app.capture_and_save_image(self)


# ---------------------- 主應用程式 ----------------------

class MacroApp:
    def __init__(self, root):
        self.root = root
        self.root.title("巨集自動化工具")
        self.root.geometry("900x680")

        # 資料結構: profiles = { 名稱: {"hotkey": 字串, "steps": [step, ...]} }
        self.profiles = {}
        # 每組巨集目前是否正在執行
        self.running_flags = {}
        # 每組巨集對應的執行緒
        self.threads = {}
        # 每組巨集註冊的全域快捷鍵代碼, 用於之後移除
        self.hotkey_handles = {}

        # 目前滑鼠所在位置作用中的步驟編輯器, 供記錄快捷鍵填入資料使用
        self.active_step_editor = None

        # 記錄用快捷鍵設定與已註冊的代碼
        self.record_hotkeys = {
            "capture_pos": "f8",
            "capture_color": "f9",
            "region_start": "f10",
            "region_end": "f11",
        }
        self.record_hotkey_handles = {}

        # 按鍵錄製狀態
        self._capturing = False
        self._capture_var = None
        self._capture_modifiers = set()
        self._hook_id = None

        self.mouse = MouseController()

        self.load_data()
        self.build_ui()
        self.refresh_profile_list()
        self.register_record_hotkeys()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # 程式啟動時, 依照已儲存的資料重新註冊所有巨集快捷鍵
        for name in self.profiles:
            self.register_hotkey(name)

    # ---------------------- 資料存取 ----------------------

    def load_data(self):
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    self.profiles = json.load(f)
            except Exception:
                self.profiles = {}
        else:
            self.profiles = {}

        for name in self.profiles:
            self.running_flags[name] = False

    def save_data(self):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(self.profiles, f, ensure_ascii=False, indent=2)
        messagebox.showinfo("儲存完成", "巨集設定已儲存")

    # ---------------------- 介面建立 ----------------------

    def build_ui(self):
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill="both", expand=True)

        # 左側: 巨集清單區域
        left_frame = ttk.Frame(main_frame)
        left_frame.pack(side="left", fill="y", padx=(0, 10))

        ttk.Label(left_frame, text="巨集清單").pack(anchor="w")
        self.profile_listbox = tk.Listbox(left_frame, width=22, height=25, exportselection=False)
        self.profile_listbox.pack(fill="y", expand=True)
        self.profile_listbox.bind("<<ListboxSelect>>", self.on_profile_select)

        profile_btn_frame = ttk.Frame(left_frame)
        profile_btn_frame.pack(fill="x", pady=5)
        ttk.Button(profile_btn_frame, text="新增", command=self.add_profile).pack(side="left", expand=True, fill="x")
        ttk.Button(profile_btn_frame, text="刪除", command=self.delete_profile).pack(side="left", expand=True, fill="x")

        ttk.Button(left_frame, text="重新命名", command=self.rename_profile).pack(fill="x", pady=2)
        ttk.Button(left_frame, text="儲存全部設定", command=self.save_data).pack(fill="x", pady=(10, 2))

        # 右側: 選定巨集的詳細設定
        right_frame = ttk.Frame(main_frame)
        right_frame.pack(side="left", fill="both", expand=True)

        # 巨集開始/停止快捷鍵設定區域
        hotkey_frame = ttk.LabelFrame(right_frame, text="巨集快捷鍵設定 (開始 / 停止 切換)")
        hotkey_frame.pack(fill="x", pady=(0, 5))

        ttk.Label(hotkey_frame, text="快捷鍵:").grid(row=0, column=0, padx=5, pady=5)
        self.hotkey_var = tk.StringVar()
        self.hotkey_entry = ttk.Entry(hotkey_frame, textvariable=self.hotkey_var, width=15)
        self.hotkey_entry.grid(row=0, column=1, padx=5, pady=5)
        ttk.Button(hotkey_frame, text="錄製",
                   command=lambda: self.start_key_capture(self.hotkey_var)).grid(row=0, column=2, padx=3, pady=5)
        ttk.Label(hotkey_frame, text="手動輸入或按「錄製」, 例如 f6").grid(row=0, column=3, padx=5, pady=5)
        ttk.Button(hotkey_frame, text="套用", command=self.apply_hotkey).grid(row=0, column=4, padx=5, pady=5)
        ttk.Button(hotkey_frame, text="手動開始/停止", command=self.manual_toggle).grid(row=0, column=5, padx=5, pady=5)

        self.status_label = ttk.Label(hotkey_frame, text="狀態: 尚未選擇巨集", foreground="gray")
        self.status_label.grid(row=1, column=0, columnspan=5, padx=5, pady=(0, 5), sticky="w")

        # 記錄用快捷鍵設定區域
        record_frame = ttk.LabelFrame(right_frame, text="記錄快捷鍵設定 (滑鼠移到目標處後按下, 自動填入目前作用中的表單)")
        record_frame.pack(fill="x", pady=(0, 5))

        self.record_pos_var = tk.StringVar(value=self.record_hotkeys["capture_pos"])
        self.record_color_var = tk.StringVar(value=self.record_hotkeys["capture_color"])
        self.record_rstart_var = tk.StringVar(value=self.record_hotkeys["region_start"])
        self.record_rend_var = tk.StringVar(value=self.record_hotkeys["region_end"])

        ttk.Label(record_frame, text="抓座標:").grid(row=0, column=0, padx=3, pady=5)
        e = ttk.Entry(record_frame, textvariable=self.record_pos_var, width=6)
        e.grid(row=0, column=1, padx=3, pady=5)
        ttk.Button(record_frame, text="錄製", width=4,
                   command=lambda: self.start_key_capture(self.record_pos_var)).grid(row=0, column=2, padx=1, pady=5)
        ttk.Label(record_frame, text="抓顏色:").grid(row=0, column=3, padx=3, pady=5)
        e = ttk.Entry(record_frame, textvariable=self.record_color_var, width=6)
        e.grid(row=0, column=4, padx=3, pady=5)
        ttk.Button(record_frame, text="錄製", width=4,
                   command=lambda: self.start_key_capture(self.record_color_var)).grid(row=0, column=5, padx=1, pady=5)
        ttk.Label(record_frame, text="區塊起點:").grid(row=0, column=6, padx=3, pady=5)
        e = ttk.Entry(record_frame, textvariable=self.record_rstart_var, width=6)
        e.grid(row=0, column=7, padx=3, pady=5)
        ttk.Button(record_frame, text="錄製", width=4,
                   command=lambda: self.start_key_capture(self.record_rstart_var)).grid(row=0, column=8, padx=1, pady=5)
        ttk.Label(record_frame, text="區塊終點:").grid(row=0, column=9, padx=3, pady=5)
        e = ttk.Entry(record_frame, textvariable=self.record_rend_var, width=6)
        e.grid(row=0, column=10, padx=3, pady=5)
        ttk.Button(record_frame, text="錄製", width=4,
                   command=lambda: self.start_key_capture(self.record_rend_var)).grid(row=0, column=11, padx=1, pady=5)
        ttk.Button(record_frame, text="套用", command=self.apply_record_hotkeys).grid(row=0, column=12, padx=5, pady=5)

        # 步驟編輯器 (頂層步驟)
        self.steps_editor = StepsEditor(right_frame, self, [], TOP_LEVEL_TYPES)

    # ---------------------- 巨集清單操作 ----------------------

    def refresh_profile_list(self):
        self.profile_listbox.delete(0, tk.END)
        for name in self.profiles:
            self.profile_listbox.insert(tk.END, name)

    def get_selected_profile(self):
        selection = self.profile_listbox.curselection()
        if not selection:
            return None
        return self.profile_listbox.get(selection[0])

    def add_profile(self):
        name = simpledialog.askstring("新增巨集", "請輸入巨集名稱, 例如: 按鍵1")
        if not name:
            return
        if name in self.profiles:
            messagebox.showwarning("名稱重複", "此名稱已存在, 請使用其他名稱")
            return
        self.profiles[name] = {"hotkey": "", "steps": []}
        self.running_flags[name] = False
        self.refresh_profile_list()

    def delete_profile(self):
        name = self.get_selected_profile()
        if not name:
            return
        if messagebox.askyesno("確認刪除", "確定要刪除巨集 [{}] 嗎".format(name)):
            self.stop_macro(name)
            self.unregister_hotkey(name)
            del self.profiles[name]
            self.running_flags.pop(name, None)
            self.refresh_profile_list()
            self.hotkey_var.set("")
            self.steps_editor.steps = []
            self.steps_editor.refresh()
            self.status_label.config(text="狀態: 尚未選擇巨集", foreground="gray")

    def rename_profile(self):
        old_name = self.get_selected_profile()
        if not old_name:
            return
        new_name = simpledialog.askstring("重新命名", "請輸入新名稱", initialvalue=old_name)
        if not new_name or new_name == old_name:
            return
        if new_name in self.profiles:
            messagebox.showwarning("名稱重複", "此名稱已存在")
            return

        self.stop_macro(old_name)
        self.unregister_hotkey(old_name)

        self.profiles[new_name] = self.profiles.pop(old_name)
        self.running_flags[new_name] = self.running_flags.pop(old_name, False)

        self.register_hotkey(new_name)
        self.refresh_profile_list()

    def on_profile_select(self, event=None):
        name = self.get_selected_profile()
        if not name:
            return
        profile = self.profiles[name]
        self.hotkey_var.set(profile.get("hotkey", ""))
        self.steps_editor.steps = profile["steps"]
        self.steps_editor.refresh()
        self.update_status_label(name)

    # ---------------------- 巨集快捷鍵操作 ----------------------

    def apply_hotkey(self):
        name = self.get_selected_profile()
        if not name:
            messagebox.showwarning("尚未選擇", "請先在左側選擇一組巨集")
            return

        new_hotkey = self.hotkey_var.get().strip()
        if not new_hotkey:
            messagebox.showwarning("輸入錯誤", "請輸入快捷鍵, 例如 f6")
            return

        self.unregister_hotkey(name)
        self.profiles[name]["hotkey"] = new_hotkey
        self.register_hotkey(name)
        messagebox.showinfo("設定完成", "快捷鍵 [{}] 已套用於巨集 [{}]".format(new_hotkey, name))

    def register_hotkey(self, name):
        hotkey = self.profiles.get(name, {}).get("hotkey", "")
        if not hotkey:
            return
        try:
            handle = keyboard.add_hotkey(hotkey, lambda n=name: self.toggle_macro(n))
            self.hotkey_handles[name] = handle
        except Exception as e:
            messagebox.showerror("快捷鍵註冊失敗", "無法註冊快捷鍵 [{}]: {}".format(hotkey, e))

    def unregister_hotkey(self, name):
        handle = self.hotkey_handles.get(name)
        if handle is not None:
            try:
                keyboard.remove_hotkey(handle)
            except Exception:
                pass
            self.hotkey_handles.pop(name, None)

    # ---------------------- 記錄快捷鍵操作 ----------------------

    def apply_record_hotkeys(self):
        self.record_hotkeys["capture_pos"] = self.record_pos_var.get().strip()
        self.record_hotkeys["capture_color"] = self.record_color_var.get().strip()
        self.record_hotkeys["region_start"] = self.record_rstart_var.get().strip()
        self.record_hotkeys["region_end"] = self.record_rend_var.get().strip()
        self.register_record_hotkeys()
        messagebox.showinfo("設定完成", "記錄快捷鍵已套用")

    def register_record_hotkeys(self):
        self.unregister_record_hotkeys()
        try:
            if self.record_hotkeys["capture_pos"]:
                self.record_hotkey_handles["capture_pos"] = keyboard.add_hotkey(
                    self.record_hotkeys["capture_pos"], self.on_capture_position
                )
            if self.record_hotkeys["capture_color"]:
                self.record_hotkey_handles["capture_color"] = keyboard.add_hotkey(
                    self.record_hotkeys["capture_color"], self.on_capture_color
                )
            if self.record_hotkeys["region_start"]:
                self.record_hotkey_handles["region_start"] = keyboard.add_hotkey(
                    self.record_hotkeys["region_start"], self.on_capture_region_start
                )
            if self.record_hotkeys["region_end"]:
                self.record_hotkey_handles["region_end"] = keyboard.add_hotkey(
                    self.record_hotkeys["region_end"], self.on_capture_region_end
                )
        except Exception as e:
            messagebox.showerror("快捷鍵註冊失敗", str(e))

    def unregister_record_hotkeys(self):
        for handle in self.record_hotkey_handles.values():
            try:
                keyboard.remove_hotkey(handle)
            except Exception:
                pass
        self.record_hotkey_handles = {}

    # ---------------------- 按鍵錄製功能 ----------------------

    _MODIFIER_KEYS = {'ctrl', 'ctrl_l', 'ctrl_r', 'alt', 'alt_l', 'alt_r',
                       'shift', 'shift_l', 'shift_r', 'windows', 'win_l', 'win_r'}

    def start_key_capture(self, target_var):
        if self._capturing:
            return
        self._capturing = True
        self._capture_var = target_var
        self._capture_modifiers = set()
        target_var.set("請按鍵...")
        self._hook_id = keyboard.hook(self._on_capture_event)

    def _on_capture_event(self, event):
        if event.event_type != "down":
            return
        name = event.name.lower()
        if name in self._MODIFIER_KEYS:
            self._capture_modifiers.add(name)
            return
        # Escape 無修飾鍵 → 取消
        if name == 'esc' and not self._capture_modifiers:
            self.root.after(0, lambda: self._finish_capture(cancelled=True))
            return
        parts = []
        if any(m.startswith('ctrl') for m in self._capture_modifiers):
            parts.append('ctrl')
        if any(m.startswith('alt') for m in self._capture_modifiers):
            parts.append('alt')
        if any(m.startswith('shift') for m in self._capture_modifiers):
            parts.append('shift')
        parts.append(name)
        hotkey_str = '+'.join(parts)
        self.root.after(0, lambda hk=hotkey_str: self._finish_capture(hk))

    def _finish_capture(self, hotkey_str=None, cancelled=False):
        if self._hook_id is not None:
            keyboard.unhook(self._hook_id)
            self._hook_id = None
        self._capturing = False
        if not cancelled and hotkey_str:
            self._capture_var.set(hotkey_str)
        self._capture_var = None
        self._capture_modifiers.clear()

    def on_capture_position(self):
        if not self.active_step_editor:
            return
        x, y = self.mouse.position
        x, y = int(x), int(y)
        self.root.after(0, lambda: self.active_step_editor.capture_position(x, y))

    def on_capture_color(self):
        if not self.active_step_editor:
            return
        x, y = self.mouse.position
        x, y = int(x), int(y)
        try:
            img = ImageGrab.grab(bbox=(x, y, x + 1, y + 1))
            r, g, b = img.getpixel((0, 0))[:3]
            hex_color = "{:02X}{:02X}{:02X}".format(r, g, b)
        except Exception:
            return
        self.root.after(0, lambda: self.active_step_editor.capture_color(x, y, hex_color))

    def on_capture_region_start(self):
        if not self.active_step_editor:
            return
        x, y = self.mouse.position
        x, y = int(x), int(y)
        self.root.after(0, lambda: self.active_step_editor.capture_region_start(x, y))

    def on_capture_region_end(self):
        if not self.active_step_editor:
            return
        x, y = self.mouse.position
        x, y = int(x), int(y)
        self.root.after(0, lambda: self.active_step_editor.capture_region_end(x, y))

    def capture_and_save_image(self, editor):
        # 讀取編輯器目前的區塊座標, 排序後擷取畫面並儲存成 png 檔案
        try:
            x1 = int(editor.rx1_var.get())
            y1 = int(editor.ry1_var.get())
            x2 = int(editor.rx2_var.get())
            y2 = int(editor.ry2_var.get())
        except ValueError:
            return

        x1, x2 = sorted((x1, x2))
        y1, y2 = sorted((y1, y2))
        if x2 - x1 < 2 or y2 - y1 < 2:
            messagebox.showwarning("擷取失敗", "區塊範圍過小, 請重新選取")
            return

        editor.rx1_var.set(str(x1))
        editor.ry1_var.set(str(y1))
        editor.rx2_var.set(str(x2))
        editor.ry2_var.set(str(y2))

        filename = "pic_{}.png".format(int(time.time() * 1000))
        filepath = os.path.join(PICS_DIR, filename)
        try:
            img = ImageGrab.grab(bbox=(x1, y1, x2, y2))
            img.save(filepath)
        except Exception as e:
            messagebox.showerror("擷取失敗", str(e))
            return

        editor.image_path_var.set(filepath)

    # ---------------------- 巨集執行邏輯 ----------------------

    def manual_toggle(self):
        name = self.get_selected_profile()
        if not name:
            messagebox.showwarning("尚未選擇", "請先在左側選擇一組巨集")
            return
        self.toggle_macro(name)

    def toggle_macro(self, name):
        if self.running_flags.get(name):
            self.stop_macro(name)
        else:
            self.start_macro(name)

    def start_macro(self, name):
        if not self.profiles[name]["steps"]:
            messagebox.showwarning("無法開始", "此巨集尚未設定任何步驟")
            return
        if self.running_flags.get(name):
            return
        self.running_flags[name] = True
        thread = threading.Thread(target=self.run_loop, args=(name,), daemon=True)
        self.threads[name] = thread
        thread.start()
        self.root.after(0, lambda: self.update_status_label(name))

    def stop_macro(self, name):
        self.running_flags[name] = False
        self.root.after(0, lambda: self.update_status_label(name))

    def run_loop(self, name):
        # 持續執行該巨集的頂層步驟, 直到狀態被設為停止為止
        while self.running_flags.get(name):
            self.execute_steps(name, self.profiles[name]["steps"])

    def execute_steps(self, name, steps):
        # 依序執行一組步驟清單, 支援判斷條件內的子步驟遞迴呼叫
        for step in steps:
            if not self.running_flags.get(name):
                break
            self.execute_step(name, step)

    def execute_step(self, name, step):
        step_type = step["type"]

        if step_type == "key_down":
            keyboard.press(step["key"])

        elif step_type == "key_up":
            keyboard.release(step["key"])

        elif step_type == "key_click":
            keyboard.send(step["key"])

        elif step_type == "move_mouse":
            self.mouse.position = (step["x"], step["y"])

        elif step_type == "mouse_down":
            self.mouse.press(MOUSE_BUTTON_MAP[step["button"]])

        elif step_type == "mouse_up":
            self.mouse.release(MOUSE_BUTTON_MAP[step["button"]])

        elif step_type == "mouse_click":
            self.mouse.click(MOUSE_BUTTON_MAP[step["button"]])

        elif step_type == "wait":
            time.sleep(step["seconds"])

        elif step_type == "find_window_move":
            find_and_move_window(step["title"], step["x"], step["y"])

        elif step_type == "if_color":
            if check_color(step["x"], step["y"], step["color"], step["tolerance"]):
                self.execute_steps(name, step.get("then", []))

        elif step_type == "if_image":
            region = (step["rx1"], step["ry1"], step["rx2"], step["ry2"])
            if check_image(region, step["image_path"], step["confidence"]):
                self.execute_steps(name, step.get("then", []))

    def update_status_label(self, name):
        if self.get_selected_profile() != name:
            return
        if self.running_flags.get(name):
            self.status_label.config(text="狀態: 執行中 (快捷鍵可再次按下以停止)", foreground="green")
        else:
            self.status_label.config(text="狀態: 已停止", foreground="red")

    # ---------------------- 關閉程式 ----------------------

    def on_close(self):
        if self._hook_id is not None:
            keyboard.unhook(self._hook_id)
            self._hook_id = None
        for name in list(self.profiles.keys()):
            self.stop_macro(name)
            self.unregister_hotkey(name)
        self.unregister_record_hotkeys()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = MacroApp(root)
    root.mainloop()
