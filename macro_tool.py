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
import sys
import time
import threading
import copy
import ctypes
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
    "if_key": "判斷按鍵 (按下則執行子步驟)",
}
STEP_TYPE_REVERSE = {v: k for k, v in STEP_TYPE_DISPLAY.items()}

# 頂層步驟允許的所有類型
TOP_LEVEL_TYPES = [
    "key_down", "key_up", "key_click",
    "move_mouse", "mouse_down", "mouse_up", "mouse_click",
    "wait", "find_window_move", "if_color", "if_image", "if_key",
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
    # 在指定區塊內尋找目標圖片, 使用多尺度樣板比對
    # 對範本做多個縮放比例比對, 避免因螢幕縮放或畫面尺寸不同而抓不到
    try:
        x1, y1, x2, y2 = [int(v) for v in region]
        if x2 <= x1 or y2 <= y1:
            return False
        screenshot = ImageGrab.grab(bbox=(x1, y1, x2, y2))
        screen_np = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
        template = cv2.imread(image_path, cv2.IMREAD_COLOR)
        if template is None:
            return False
        best = -1.0
        # 縮放比例範圍 0.5 到 2.0, 每級 0.1
        scale = 0.5
        while scale <= 2.0:
            h = int(template.shape[0] * scale + 0.5)
            w = int(template.shape[1] * scale + 0.5)
            if h <= screen_np.shape[0] and w <= screen_np.shape[1]:
                if abs(scale - 1.0) < 1e-6:
                    resized = template
                else:
                    resized = cv2.resize(template, (w, h), interpolation=cv2.INTER_LINEAR)
                result = cv2.matchTemplate(screen_np, resized, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, _ = cv2.minMaxLoc(result)
                if float(max_val) > best:
                    best = float(max_val)
            scale += 0.1
        return best >= confidence
    except Exception:
        return False


# ---------------------- 步驟編輯器元件 ----------------------
# 此元件封裝了 "步驟清單 + 上移/下移/刪除/編輯子步驟 + 新增步驟表單"
# 主畫面的頂層步驟, 以及判斷條件內的子步驟, 都使用同一個元件實作, 差別只在
# allowed_types 傳入不同的可選動作類型清單, 以及 steps 傳入不同的清單參照

class StepsEditor:
    def __init__(self, master, app, steps, allowed_types, parent_editor=None):
        self.app = app
        self.steps = steps
        self.allowed_types = allowed_types
        # 上一層的步驟編輯器 (編輯子步驟時傳入), 內容變動時同步更新上層畫面
        self.parent_editor = parent_editor
        # 目前正在編輯的步驟索引, None 表示新增模式
        self._editing_index = None
        # 記錄快捷鍵填入的目標欄位: "main" 表示主條件欄位, "exit" 表示退出條件欄位
        self.capture_target = "main"

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
        self.listbox.bind("<Double-Button-1>", lambda e: self.edit_selected_step())

        btn_frame = ttk.Frame(list_frame)
        btn_frame.pack(side="left", fill="y", padx=5)
        ttk.Button(btn_frame, text="上移", command=self.move_up).pack(fill="x", pady=2)
        ttk.Button(btn_frame, text="下移", command=self.move_down).pack(fill="x", pady=2)
        ttk.Button(btn_frame, text="移到最上", command=self.move_to_top).pack(fill="x", pady=2)
        ttk.Button(btn_frame, text="移到最下", command=self.move_to_bottom).pack(fill="x", pady=2)
        ttk.Button(btn_frame, text="刪除", command=self.delete_step).pack(fill="x", pady=2)
        ttk.Button(btn_frame, text="編輯", command=self.edit_selected_step).pack(fill="x", pady=2)
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

        self.add_btn = ttk.Button(add_frame, text="新增此步驟", command=self.add_step)
        self.add_btn.grid(row=0, column=2, padx=10, pady=5)
        self.cancel_btn = ttk.Button(add_frame, text="取消編輯", command=self.cancel_edit)
        self.cancel_btn.grid(row=0, column=4, padx=5, pady=5)
        self.cancel_btn.grid_remove()

        self.fields_frame = ttk.Frame(add_frame)
        self.fields_frame.grid(row=1, column=0, columnspan=4, sticky="w")

        self.rebuild_fields()

    def get_selected_type(self):
        return STEP_TYPE_REVERSE[self.type_var.get()]

    def add_field(self, label, var, row, col, width=15, colspan=1, target="main", frame=None):
        parent = frame if frame is not None else self.fields_frame
        ttk.Label(parent, text=label).grid(row=row, column=col, padx=5, pady=3, sticky="w")
        entry = ttk.Entry(parent, textvariable=var, width=width)
        entry.grid(row=row, column=col + 1, padx=5, pady=3, sticky="w", columnspan=colspan)
        entry.bind("<FocusIn>", lambda e, tg=target: self.on_field_focus(tg))
        return entry

    def on_field_focus(self, target):
        # 欄位取得焦點時, 記錄此編輯器為作用中, 並設定快捷鍵填入的目標欄位
        self.set_active()
        self.capture_target = target

    def rebuild_fields(self):
        # 依照目前選擇的動作類型, 重新產生對應的輸入欄位
        for w in self.fields_frame.winfo_children():
            w.destroy()
        self.capture_target = "main"

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
            self.build_loop_exit_fields()

        elif t == "if_image":
            self.rx1_var = tk.StringVar()
            self.ry1_var = tk.StringVar()
            self.rx2_var = tk.StringVar()
            self.ry2_var = tk.StringVar()
            self.image_path_var = tk.StringVar()
            self.confidence_var = tk.StringVar(value="0.8")
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
            self.build_loop_exit_fields()

        elif t == "if_key":
            self.key_var = tk.StringVar()
            self.add_field("按鍵名稱:", self.key_var, row=0, col=0, width=15)
            ttk.Button(self.fields_frame, text="錄製",
                       command=lambda: self.app.start_key_capture(self.key_var)).grid(row=0, column=2, padx=5, pady=3)
            ttk.Label(self.fields_frame, text="條件為該按鍵被按住時成立").grid(
                row=1, column=0, columnspan=4, sticky="w", padx=5
            )
            self.build_loop_exit_fields()

    def build_loop_exit_fields(self):
        # 在判斷類型表單下方新增「執行次數」與「退出條件」設定區塊
        # 退出條件支援多個, 任一符合即結束迴圈
        if not hasattr(self, "exit_conditions"):
            self.exit_conditions = []
        self._editing_exit_index = None

        self.loop_exit_frame = ttk.Frame(self.fields_frame)
        self.loop_exit_frame.grid(row=10, column=0, columnspan=4, sticky="w")

        self.loop_times_var = tk.StringVar(value="1")
        ttk.Label(self.loop_exit_frame, text="子步驟執行次數(0=無限):").grid(row=0, column=0, padx=5, pady=3, sticky="w")
        ttk.Entry(self.loop_exit_frame, textvariable=self.loop_times_var, width=8).grid(row=0, column=1, padx=5, pady=3, sticky="w")

        ttk.Label(self.loop_exit_frame, text="退出條件 (任一符合即結束):").grid(row=1, column=0, padx=5, pady=3, sticky="w")
        self.exit_type_var = tk.StringVar(value="退出顏色")
        exit_combo = ttk.Combobox(
            self.loop_exit_frame, textvariable=self.exit_type_var,
            values=["退出顏色", "退出圖片", "退出按鍵"], state="readonly", width=10
        )
        exit_combo.grid(row=1, column=1, padx=5, pady=3, sticky="w")
        exit_combo.bind("<<ComboboxSelected>>", lambda e: self.rebuild_exit_fields())

        self.add_exit_btn = ttk.Button(self.loop_exit_frame, text="加入條件", command=self.add_exit_condition)
        self.add_exit_btn.grid(row=1, column=2, padx=5, pady=3)

        self.exit_fields_frame = ttk.Frame(self.loop_exit_frame)
        self.exit_fields_frame.grid(row=2, column=0, columnspan=4, sticky="w")

        self.exit_listbox = tk.Listbox(self.loop_exit_frame, height=3)
        self.exit_listbox.grid(row=3, column=0, columnspan=3, sticky="ew", padx=5, pady=3)
        self.exit_listbox.bind("<<ListboxSelect>>", lambda e: self.load_exit_to_fields())
        ttk.Button(self.loop_exit_frame, text="刪除選取條件", command=self.delete_exit_condition).grid(
            row=3, column=3, padx=5, pady=3
        )

        ttk.Label(self.loop_exit_frame, text="設定好下方欄位後按「加入條件」, 快捷鍵會填入目前欄位").grid(
            row=4, column=0, columnspan=4, sticky="w", padx=5
        )

        self.rebuild_exit_fields()
        self.refresh_exit_list()

    def rebuild_exit_fields(self):
        # 依選擇的退出條件類型, 動態建立對應的輸入欄位
        for w in self.exit_fields_frame.winfo_children():
            w.destroy()

        t = self.exit_type_var.get()
        if t == "不設定":
            return

        if t == "退出顏色":
            self.exit_x_var = tk.StringVar()
            self.exit_y_var = tk.StringVar()
            self.exit_color_var = tk.StringVar()
            self.exit_tolerance_var = tk.StringVar(value="20")
            self.add_field("X:", self.exit_x_var, row=0, col=0, width=8, target="exit", frame=self.exit_fields_frame)
            self.add_field("Y:", self.exit_y_var, row=0, col=2, width=8, target="exit", frame=self.exit_fields_frame)
            self.add_field("顏色:", self.exit_color_var, row=1, col=0, width=10, target="exit", frame=self.exit_fields_frame)
            self.add_field("誤差:", self.exit_tolerance_var, row=1, col=2, width=8, target="exit", frame=self.exit_fields_frame)
            ttk.Label(self.exit_fields_frame, text="按下抓取顏色快捷鍵可填入此處").grid(
                row=2, column=0, columnspan=4, sticky="w", padx=5
            )

        elif t == "退出圖片":
            self.exit_rx1_var = tk.StringVar()
            self.exit_ry1_var = tk.StringVar()
            self.exit_rx2_var = tk.StringVar()
            self.exit_ry2_var = tk.StringVar()
            self.exit_image_path_var = tk.StringVar()
            self.exit_confidence_var = tk.StringVar(value="0.8")
            self.add_field("左上X:", self.exit_rx1_var, row=0, col=0, width=8, target="exit", frame=self.exit_fields_frame)
            self.add_field("左上Y:", self.exit_ry1_var, row=0, col=2, width=8, target="exit", frame=self.exit_fields_frame)
            self.add_field("右下X:", self.exit_rx2_var, row=1, col=0, width=8, target="exit", frame=self.exit_fields_frame)
            self.add_field("右下Y:", self.exit_ry2_var, row=1, col=2, width=8, target="exit", frame=self.exit_fields_frame)
            self.add_field("圖片:", self.exit_image_path_var, row=2, col=0, width=32, colspan=3, target="exit", frame=self.exit_fields_frame)
            self.add_field("辨識率:", self.exit_confidence_var, row=3, col=0, width=8, target="exit", frame=self.exit_fields_frame)
            ttk.Label(self.exit_fields_frame, text="按下區塊起點/終點快捷鍵可擷取此處圖片").grid(
                row=4, column=0, columnspan=4, sticky="w", padx=5
            )

        elif t == "退出按鍵":
            self.exit_key_var = tk.StringVar()
            self.add_field("按鍵名稱:", self.exit_key_var, row=0, col=0, width=15, target="exit", frame=self.exit_fields_frame)
            ttk.Button(self.exit_fields_frame, text="錄製",
                       command=lambda: self.app.start_key_capture(self.exit_key_var)).grid(row=0, column=2, padx=5, pady=3)
            ttk.Label(self.exit_fields_frame, text="按下此按鍵時跳出迴圈").grid(
                row=1, column=0, columnspan=4, sticky="w", padx=5
            )

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
        if t in ("if_color", "if_image", "if_key"):
            loop_times = int(step.get("loop_times", 1))
            loop_text = "無限" if loop_times == 0 else "{}次".format(loop_times)
            ex_text = self.format_exit_condition(step.get("exit_condition"))
            if t == "if_color":
                base = "{} [({},{}) = #{} 誤差{}]".format(display, step["x"], step["y"], step["color"], step["tolerance"])
            elif t == "if_image":
                base = "{} [區塊({},{})-({},{}) 辨識率{}]".format(
                    display, step["rx1"], step["ry1"], step["rx2"], step["ry2"], step["confidence"]
                )
            else:
                base = "{} [{}]".format(display, step["key"])
            return "{} 執行{} -> 內含{}個子步驟{}".format(
                base, loop_text, len(step.get("then", [])), ex_text
            )
        return display

    def format_exit_condition(self, ex):
        # 將退出條件轉成顯示文字, 未設定回傳空字串; 支援多個條件 (任一符合即結束)
        if not ex:
            return ""
        if isinstance(ex, dict):
            ex = [ex]
        texts = [self.format_single_exit(e) for e in ex]
        texts = [t for t in texts if t]
        if not texts:
            return ""
        return " 退出:{}".format("; ".join(texts))

    def refresh(self):
        # 記住目前的選取位置, 重建後恢復, 避免捲動跳回最上方
        sel = self.listbox.curselection()
        selected = sel[0] if sel else None
        self.listbox.delete(0, tk.END)
        for idx, step in enumerate(self.steps, start=1):
            self.listbox.insert(tk.END, "{}. {}".format(idx, self.format_step(step)))
        if selected is not None and selected < len(self.steps):
            self.listbox.selection_clear(0, tk.END)
            self.listbox.selection_set(selected)
            self.listbox.see(selected)
        # 子步驟編輯器變動時, 同步更新上層畫面的顯示
        if self.parent_editor is not None:
            self.parent_editor.refresh()

    def get_selected_index(self):
        sel = self.listbox.curselection()
        if not sel:
            return None
        return sel[0]

    def add_step(self):
        t = self.get_selected_type()
        try:
            new_step = self.build_step_from_type(t)
        except ValueError:
            messagebox.showwarning("輸入錯誤", "數值欄位請輸入正確的數字格式")
            return
        if new_step is None:
            return

        if self._editing_index is not None:
            old = self.steps[self._editing_index]
            # 編輯判斷步驟時, 保留原本的子步驟內容
            if "then" in old:
                new_step["then"] = old["then"]
            self.steps[self._editing_index] = new_step
            self.cancel_edit()
        else:
            self.steps.append(new_step)
        self.refresh()

    def build_step_from_type(self, t):
        # 依動作類型與目前表單欄位內容組出一個步驟資料, 驗證失敗回傳 None
        if t in ("key_down", "key_up", "key_click"):
            key = self.key_var.get().strip()
            if not key:
                messagebox.showwarning("輸入錯誤", "請輸入按鍵名稱")
                return None
            return {"type": t, "key": key}

        elif t == "move_mouse":
            return {"type": t, "x": int(self.x_var.get()), "y": int(self.y_var.get())}

        elif t in ("mouse_down", "mouse_up", "mouse_click"):
            return {"type": t, "button": self.mouse_var.get()}

        elif t == "wait":
            return {"type": t, "seconds": float(self.wait_var.get())}

        elif t == "find_window_move":
            title = self.window_title_var.get().strip()
            if not title:
                messagebox.showwarning("輸入錯誤", "請輸入視窗標題關鍵字")
                return None
            return {"type": t, "title": title, "x": int(self.x_var.get()), "y": int(self.y_var.get())}

        elif t == "if_color":
            color = self.color_var.get().strip().lstrip("#").upper()
            if len(color) != 6:
                messagebox.showwarning("輸入錯誤", "顏色請輸入 6 碼十六進位, 例如 554941")
                return None
            # 退出條件為多個條件清單, 任一符合即結束
            exit_condition = self.exit_conditions if self.exit_conditions else None
            return {
                "type": t,
                "x": int(self.x_var.get()),
                "y": int(self.y_var.get()),
                "color": color,
                "tolerance": int(self.tolerance_var.get()),
                "loop_times": int(self.loop_times_var.get()),
                "exit_condition": exit_condition,
                "then": [],
            }

        elif t == "if_image":
            image_path = self.image_path_var.get().strip()
            if not image_path or not os.path.exists(image_path):
                messagebox.showwarning("輸入錯誤", "請先使用區塊快捷鍵擷取圖片, 或確認圖片檔案存在")
                return None
            # 退出條件為多個條件清單, 任一符合即結束
            exit_condition = self.exit_conditions if self.exit_conditions else None
            return {
                "type": t,
                "rx1": int(self.rx1_var.get()),
                "ry1": int(self.ry1_var.get()),
                "rx2": int(self.rx2_var.get()),
                "ry2": int(self.ry2_var.get()),
                "image_path": image_path,
                "confidence": float(self.confidence_var.get()),
                "loop_times": int(self.loop_times_var.get()),
                "exit_condition": exit_condition,
                "then": [],
            }

        elif t == "if_key":
            key = self.key_var.get().strip()
            if not key:
                messagebox.showwarning("輸入錯誤", "請輸入按鍵名稱")
                return None
            # 退出條件為多個條件清單, 任一符合即結束
            exit_condition = self.exit_conditions if self.exit_conditions else None
            return {
                "type": t,
                "key": key,
                "loop_times": int(self.loop_times_var.get()),
                "exit_condition": exit_condition,
                "then": [],
            }
        return None

    def build_exit_condition_from_fields(self):
        # 依目前退出條件表單內容組出 exit_condition 資料, 未設定回傳 None
        t = self.exit_type_var.get()
        try:
            if t == "退出顏色":
                color = self.exit_color_var.get().strip().lstrip("#").upper()
                if len(color) != 6:
                    raise ValueError
                return {
                    "type": "if_color",
                    "x": int(self.exit_x_var.get()),
                    "y": int(self.exit_y_var.get()),
                    "color": color,
                    "tolerance": int(self.exit_tolerance_var.get()),
                }
            elif t == "退出圖片":
                image_path = self.exit_image_path_var.get().strip()
                if not image_path or not os.path.exists(image_path):
                    raise ValueError
                return {
                    "type": "if_image",
                    "rx1": int(self.exit_rx1_var.get()),
                    "ry1": int(self.exit_ry1_var.get()),
                    "rx2": int(self.exit_rx2_var.get()),
                    "ry2": int(self.exit_ry2_var.get()),
                    "image_path": image_path,
                    "confidence": float(self.exit_confidence_var.get()),
                }
            elif t == "退出按鍵":
                key = self.exit_key_var.get().strip()
                if not key:
                    raise ValueError
                return {"type": "if_key", "key": key}
        except ValueError:
            messagebox.showwarning("輸入錯誤", "退出條件欄位請檢查: 顏色需 6 碼, 圖片需存在, 數值格式正確")
            return None
        return None

    def fill_exit_fields(self, ex):
        # 將單一退出條件填入目前欄位
        t = ex.get("type")
        if t == "if_color":
            self.exit_type_var.set("退出顏色")
            self.rebuild_exit_fields()
            self.exit_x_var.set(str(ex["x"]))
            self.exit_y_var.set(str(ex["y"]))
            self.exit_color_var.set(ex["color"])
            self.exit_tolerance_var.set(str(ex["tolerance"]))
        elif t == "if_image":
            self.exit_type_var.set("退出圖片")
            self.rebuild_exit_fields()
            self.exit_rx1_var.set(str(ex["rx1"]))
            self.exit_ry1_var.set(str(ex["ry1"]))
            self.exit_rx2_var.set(str(ex["rx2"]))
            self.exit_ry2_var.set(str(ex["ry2"]))
            self.exit_image_path_var.set(ex["image_path"])
            self.exit_confidence_var.set(str(ex["confidence"]))
        elif t == "if_key":
            self.exit_type_var.set("退出按鍵")
            self.rebuild_exit_fields()
            self.exit_key_var.set(ex["key"])

    def add_exit_condition(self):
        # 將目前欄位的條件加入清單, 若在更新模式則取代選取項目
        ex = self.build_exit_condition_from_fields()
        if ex is None:
            return
        if self._editing_exit_index is not None and 0 <= self._editing_exit_index < len(self.exit_conditions):
            self.exit_conditions[self._editing_exit_index] = ex
            self._editing_exit_index = None
            self.add_exit_btn.config(text="加入條件")
        else:
            self.exit_conditions.append(ex)
        self.clear_exit_fields()
        self.refresh_exit_list()

    def clear_exit_fields(self):
        # 清空目前欄位內容, 方便繼續設定下一個條件
        t = self.exit_type_var.get()
        if t == "退出顏色":
            for v in (self.exit_x_var, self.exit_y_var, self.exit_color_var, self.exit_tolerance_var):
                v.set("")
        elif t == "退出圖片":
            for v in (self.exit_rx1_var, self.exit_ry1_var, self.exit_rx2_var, self.exit_ry2_var,
                      self.exit_image_path_var, self.exit_confidence_var):
                v.set("")
        elif t == "退出按鍵":
            self.exit_key_var.set("")

    def delete_exit_condition(self):
        # 刪除清單中選取的條件
        sel = self.exit_listbox.curselection()
        if not sel:
            messagebox.showinfo("提示", "請先選擇要刪除的退出條件")
            return
        self.exit_conditions.pop(sel[0])
        self._editing_exit_index = None
        self.add_exit_btn.config(text="加入條件")
        self.refresh_exit_list()

    def load_exit_to_fields(self):
        # 點選清單項目時, 將該條件載入欄位進入更新模式
        sel = self.exit_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx >= len(self.exit_conditions):
            return
        self.fill_exit_fields(self.exit_conditions[idx])
        self._editing_exit_index = idx
        self.add_exit_btn.config(text="更新條件")

    def refresh_exit_list(self):
        # 更新退出條件清單的顯示
        self.exit_listbox.delete(0, tk.END)
        for idx, ex in enumerate(self.exit_conditions):
            self.exit_listbox.insert(tk.END, "{}. {}".format(idx + 1, self.format_single_exit(ex)))
        if self._editing_exit_index is not None and self._editing_exit_index >= len(self.exit_conditions):
            self._editing_exit_index = None
            self.add_exit_btn.config(text="加入條件")

    def format_single_exit(self, ex):
        # 單一退出條件的顯示文字
        if ex.get("type") == "if_color":
            return "顏色({},{}) = #{} 誤差{}".format(ex["x"], ex["y"], ex["color"], ex["tolerance"])
        if ex.get("type") == "if_image":
            return "圖片({},{})-({},{}) 辨識率{}".format(ex["rx1"], ex["ry1"], ex["rx2"], ex["ry2"], ex["confidence"])
        if ex.get("type") == "if_key":
            return "按鍵({})".format(ex["key"])
        return ""

    def edit_selected_step(self):
        # 雙擊步驟或在按鈕列按「編輯」時, 將選取步驟的內容載入表單供修改
        idx = self.get_selected_index()
        if idx is None:
            messagebox.showinfo("提示", "請先選擇一個步驟")
            return
        step = self.steps[idx]
        self.type_var.set(STEP_TYPE_DISPLAY[step["type"]])
        self.rebuild_fields()
        self.fill_fields_from_step(step)
        self._editing_index = idx
        self.add_btn.config(text="儲存變更", command=self.add_step)
        self.cancel_btn.grid()

    def fill_fields_from_step(self, step):
        # 將既存步驟的資料填入目前表單欄位
        t = step["type"]
        if t in ("key_down", "key_up", "key_click"):
            self.key_var.set(step["key"])
        elif t == "move_mouse":
            self.x_var.set(str(step["x"]))
            self.y_var.set(str(step["y"]))
        elif t in ("mouse_down", "mouse_up", "mouse_click"):
            self.mouse_var.set(step["button"])
        elif t == "wait":
            self.wait_var.set(str(step["seconds"]))
        elif t == "find_window_move":
            self.window_title_var.set(step["title"])
            self.x_var.set(str(step["x"]))
            self.y_var.set(str(step["y"]))
        elif t == "if_color":
            self.x_var.set(str(step["x"]))
            self.y_var.set(str(step["y"]))
            self.color_var.set(step["color"])
            self.tolerance_var.set(str(step["tolerance"]))
            self.fill_loop_exit_from_step(step)
        elif t == "if_image":
            self.rx1_var.set(str(step["rx1"]))
            self.ry1_var.set(str(step["ry1"]))
            self.rx2_var.set(str(step["rx2"]))
            self.ry2_var.set(str(step["ry2"]))
            self.image_path_var.set(step["image_path"])
            self.confidence_var.set(str(step["confidence"]))
            self.fill_loop_exit_from_step(step)
        elif t == "if_key":
            self.key_var.set(step["key"])
            self.fill_loop_exit_from_step(step)

    def fill_loop_exit_from_step(self, step):
        # 將步驟的執行次數與退出條件回填到表單
        self.loop_times_var.set(str(step.get("loop_times", 1)))
        ex = step.get("exit_condition")
        if isinstance(ex, dict):
            ex = [ex]
        self.exit_conditions = list(ex) if ex else []
        self._editing_exit_index = None
        self.add_exit_btn.config(text="加入條件")
        self.rebuild_exit_fields()
        self.refresh_exit_list()

    def cancel_edit(self):
        # 取消編輯模式, 表單恢復成新增狀態
        self._editing_index = None
        self.add_btn.config(text="新增此步驟", command=self.add_step)
        self.cancel_btn.grid_remove()
        self.type_var.set(STEP_TYPE_DISPLAY[self.allowed_types[0]])
        self.exit_conditions = []
        self._editing_exit_index = None
        self.rebuild_fields()

    def move_up(self):
        idx = self.get_selected_index()
        if idx is None or idx == 0:
            return
        self.steps[idx - 1], self.steps[idx] = self.steps[idx], self.steps[idx - 1]
        self.refresh()
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(idx - 1)
        self.listbox.see(idx - 1)

    def move_down(self):
        idx = self.get_selected_index()
        if idx is None or idx >= len(self.steps) - 1:
            return
        self.steps[idx + 1], self.steps[idx] = self.steps[idx], self.steps[idx + 1]
        self.refresh()
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(idx + 1)
        self.listbox.see(idx + 1)

    def move_to_top(self):
        # 將選取的步驟移到清單最上方
        idx = self.get_selected_index()
        if idx is None or idx == 0:
            return
        step = self.steps.pop(idx)
        self.steps.insert(0, step)
        self.refresh()
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(0)
        self.listbox.see(0)

    def move_to_bottom(self):
        # 將選取的步驟移到清單最下方
        idx = self.get_selected_index()
        if idx is None or idx >= len(self.steps) - 1:
            return
        step = self.steps.pop(idx)
        self.steps.append(step)
        self.refresh()
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(len(self.steps) - 1)
        self.listbox.see(len(self.steps) - 1)

    def delete_step(self):
        idx = self.get_selected_index()
        if idx is None:
            return
        del self.steps[idx]
        self.refresh()
        # 刪除後自動選擇下一個動作, 若刪除的是最後一項則選擇新的最後一項
        if self.steps:
            next_idx = min(idx, len(self.steps) - 1)
            self.listbox.selection_clear(0, tk.END)
            self.listbox.selection_set(next_idx)
            self.listbox.see(next_idx)

    def edit_children(self):
        idx = self.get_selected_index()
        if idx is None:
            messagebox.showinfo("提示", "請先選擇一個步驟")
            return
        step = self.steps[idx]
        if step["type"] not in ("if_color", "if_image", "if_key"):
            messagebox.showinfo("提示", "只有判斷顏色/圖片/按鍵的步驟可以編輯子步驟")
            return
        step.setdefault("then", [])

        win = tk.Toplevel(self.app.root)
        win.title("編輯子步驟 - 條件成立時依序執行")
        win.geometry("680x480")
        StepsEditor(win, self.app, step["then"], CHILD_TYPES, parent_editor=self)

    # ---------------------- 供全域快捷鍵記錄呼叫 ----------------------

    def capture_position(self, x, y):
        t = self.get_selected_type()
        if self.capture_target == "exit" and t in ("if_color", "if_image", "if_key") and hasattr(self, "exit_x_var"):
            self.exit_x_var.set(str(x))
            self.exit_y_var.set(str(y))
        elif t in ("move_mouse", "find_window_move", "if_color") and hasattr(self, "x_var"):
            self.x_var.set(str(x))
            self.y_var.set(str(y))

    def capture_color(self, x, y, hex_color):
        t = self.get_selected_type()
        if self.capture_target == "exit" and t in ("if_color", "if_image", "if_key") and hasattr(self, "exit_color_var"):
            self.exit_x_var.set(str(x))
            self.exit_y_var.set(str(y))
            self.exit_color_var.set(hex_color)
        elif t == "if_color" and hasattr(self, "color_var"):
            self.x_var.set(str(x))
            self.y_var.set(str(y))
            self.color_var.set(hex_color)

    def capture_region_start(self, x, y):
        t = self.get_selected_type()
        if self.capture_target == "exit" and t in ("if_color", "if_image", "if_key") and hasattr(self, "exit_rx1_var"):
            self.exit_rx1_var.set(str(x))
            self.exit_ry1_var.set(str(y))
        elif t == "if_image" and hasattr(self, "rx1_var"):
            self.rx1_var.set(str(x))
            self.ry1_var.set(str(y))

    def capture_region_end(self, x, y):
        t = self.get_selected_type()
        if self.capture_target == "exit" and t in ("if_color", "if_image", "if_key") and hasattr(self, "exit_rx2_var"):
            self.exit_rx2_var.set(str(x))
            self.exit_ry2_var.set(str(y))
            self.app.capture_and_save_image(self, target="exit")
        elif t == "if_image" and hasattr(self, "rx2_var"):
            self.rx2_var.set(str(x))
            self.ry2_var.set(str(y))
            self.app.capture_and_save_image(self)


# ---------------------- 主應用程式 ----------------------

class MacroApp:
    def __init__(self, root):
        self.root = root
        self.root.title("巨集自動化工具")
        self.root.geometry("1100x800")
        self.root.minsize(1000, 700)

        # 資料結構: profiles = { 名稱: {"enabled": 布林值, "steps": [step, ...]} }
        # enabled 表示此巨集是否受統一快捷鍵控制, 未勾選則只能用手動按鈕開始/停止
        self.profiles = {}
        # 每組巨集目前是否正在執行
        self.running_flags = {}
        # 每組巨集對應的執行緒
        self.threads = {}

        # 統一的巨集開始/停止快捷鍵 (單一字串), 取代原本每組巨集各自的快捷鍵
        self.global_hotkey = ""
        # 統一快捷鍵註冊後的代碼, 用於之後移除
        self.global_hotkey_handle = None

        # 目前在左側清單中被選取的巨集名稱, 取代原本用 Listbox 選取索引取得
        self.selected_profile_name = None
        # 巨集清單每個項目的元件參照, 名稱 -> {"row": Frame, "label": Label, "var": BooleanVar}
        self.profile_row_widgets = {}

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

        # 追蹤目前被按住的鍵盤按鍵與滑鼠按鈕, 用於停止時全部釋放
        self._held_keys = set()
        self._held_mouse_buttons = set()

        self.mouse = MouseController()

        # 滑鼠位置與顏色即時監看狀態
        self.mouse_monitor_on = False
        self._mouse_monitor_after = None

        # 最小化小視窗相關狀態
        self.mini_window = None
        self.mini_widgets = {}
        self._mini_refresh_after = None

        self.load_data()
        self.build_ui()
        self.refresh_profile_list()
        self.register_record_hotkeys()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Unmap>", self.on_unmap)

        # 程式啟動時, 依照已儲存的設定重新註冊統一快捷鍵
        self.register_global_hotkey()

    # ---------------------- 資料存取 ----------------------

    def load_data(self):
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        else:
            data = {}

        if isinstance(data, dict) and "profiles" in data:
            # 新格式: 包含統一快捷鍵與各巨集設定
            self.global_hotkey = data.get("global_hotkey", "")
            self.profiles = data.get("profiles", {})
        else:
            # 舊格式相容: 整份資料就是 profiles, 每組巨集原本各自有 hotkey 欄位
            # 改版後快捷鍵統一控制, 故舊的個別 hotkey 不再使用, 只保留 steps
            self.global_hotkey = ""
            self.profiles = data if isinstance(data, dict) else {}

        for name, profile in self.profiles.items():
            # 確保每組巨集都有 enabled 欄位 (是否受統一快捷鍵控制), 預設為勾選
            profile.setdefault("enabled", True)
            profile.setdefault("steps", [])
            self.running_flags[name] = False

    def save_data(self):
        data = {"global_hotkey": self.global_hotkey, "profiles": self.profiles}
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        messagebox.showinfo("儲存完成", "巨集設定已儲存")

    # ---------------------- 介面建立 ----------------------

    def build_ui(self):
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill="both", expand=True)

        # 左側: 巨集清單區域
        left_frame = ttk.Frame(main_frame)
        left_frame.pack(side="left", fill="y", padx=(0, 10))

        ttk.Label(left_frame, text="巨集清單 (打勾表示受統一快捷鍵控制)").pack(anchor="w")

        # 巨集清單改用 Canvas + Frame 實作, 讓每個項目前面可以放置打勾選取格
        list_container = ttk.Frame(left_frame, width=230, height=500)
        list_container.pack(fill="y", expand=True)
        list_container.pack_propagate(False)

        self.profile_list_canvas = tk.Canvas(list_container, width=210, highlightthickness=0)
        profile_scrollbar = ttk.Scrollbar(list_container, orient="vertical", command=self.profile_list_canvas.yview)
        self.profile_list_inner = ttk.Frame(self.profile_list_canvas)

        self.profile_list_inner.bind(
            "<Configure>",
            lambda e: self.profile_list_canvas.configure(scrollregion=self.profile_list_canvas.bbox("all")),
        )
        self.profile_list_canvas.create_window((0, 0), window=self.profile_list_inner, anchor="nw")
        self.profile_list_canvas.configure(yscrollcommand=profile_scrollbar.set)

        self.profile_list_canvas.pack(side="left", fill="both", expand=True)
        profile_scrollbar.pack(side="left", fill="y")

        profile_btn_frame = ttk.Frame(left_frame)
        profile_btn_frame.pack(fill="x", pady=5)
        ttk.Button(profile_btn_frame, text="新增", command=self.add_profile).pack(side="left", expand=True, fill="x")
        ttk.Button(profile_btn_frame, text="刪除", command=self.delete_profile).pack(side="left", expand=True, fill="x")

        rename_frame = ttk.Frame(left_frame)
        rename_frame.pack(fill="x", pady=2)
        ttk.Button(rename_frame, text="重新命名", command=self.rename_profile).pack(side="left", expand=True, fill="x")
        ttk.Button(rename_frame, text="複製", command=self.copy_profile).pack(side="left", expand=True, fill="x")
        ttk.Button(left_frame, text="儲存全部設定", command=self.save_data).pack(fill="x", pady=(10, 2))

        # 右側: 選定巨集的詳細設定 (可上下捲動)
        right_canvas = tk.Canvas(main_frame, highlightthickness=0)
        right_scrollbar = ttk.Scrollbar(main_frame, orient="vertical", command=right_canvas.yview)
        right_canvas.configure(yscrollcommand=right_scrollbar.set)
        right_frame = ttk.Frame(right_canvas)
        right_frame.bind(
            "<Configure>",
            lambda e: right_canvas.configure(scrollregion=right_canvas.bbox("all")),
        )

        def _sync_right_width(event):
            # 內層寬度跟隨捲動區寬度, 讓 fill="x" 的子元件正常貼合
            right_canvas.itemconfigure(_sync_right_width.window_id, width=event.width)
        _sync_right_width.window_id = right_canvas.create_window((0, 0), window=right_frame, anchor="nw")

        right_canvas.bind("<Configure>", _sync_right_width)
        right_canvas.bind("<MouseWheel>", lambda e: right_canvas.yview_scroll(int(-e.delta / 120), "units"))

        right_canvas.pack(side="left", fill="both", expand=True)
        right_scrollbar.pack(side="left", fill="y")

        # 統一巨集開始/停止快捷鍵設定區域
        # 此快捷鍵為全域單一設定, 按下時會同時切換所有 "已勾選" 巨集的執行狀態
        hotkey_frame = ttk.LabelFrame(right_frame, text="統一巨集快捷鍵設定 (開始 / 停止 切換, 僅影響清單中已打勾的巨集)")
        hotkey_frame.pack(fill="x", pady=(0, 5))

        ttk.Label(hotkey_frame, text="快捷鍵:").grid(row=0, column=0, padx=5, pady=5)
        self.global_hotkey_var = tk.StringVar(value=self.global_hotkey)
        self.hotkey_entry = ttk.Entry(hotkey_frame, textvariable=self.global_hotkey_var, width=15)
        self.hotkey_entry.grid(row=0, column=1, padx=5, pady=5)
        ttk.Button(hotkey_frame, text="錄製",
                   command=lambda: self.start_key_capture(self.global_hotkey_var)).grid(row=0, column=2, padx=3, pady=5)
        ttk.Label(hotkey_frame, text="手動輸入或按「錄製」, 例如 f6").grid(row=0, column=3, padx=5, pady=5)
        ttk.Button(hotkey_frame, text="套用", command=self.apply_global_hotkey).grid(row=0, column=4, padx=5, pady=5)

        # 手動控制區域: 不受打勾狀態限制, 永遠可對目前選取的巨集手動開始/停止
        manual_frame = ttk.LabelFrame(right_frame, text="手動控制 (僅作用於目前選取的巨集, 不受打勾狀態限制)")
        manual_frame.pack(fill="x", pady=(0, 5))

        ttk.Button(manual_frame, text="手動開始/停止", command=self.manual_toggle).grid(row=0, column=0, padx=5, pady=5)
        self.status_label = ttk.Label(manual_frame, text="狀態: 尚未選擇巨集", foreground="gray")
        self.status_label.grid(row=0, column=1, padx=5, pady=5, sticky="w")

        # 滑鼠位置與顏色即時監看區域
        monitor_frame = ttk.LabelFrame(right_frame, text="滑鼠位置與顏色即時監看 (需要時打勾啟用, 會持續更新)")
        monitor_frame.pack(fill="x", pady=(0, 5))

        self.mouse_monitor_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(monitor_frame, text="啟用即時顯示", variable=self.mouse_monitor_var,
                        command=self.toggle_mouse_monitor).grid(row=0, column=0, padx=5, pady=5)
        self.mouse_monitor_label = ttk.Label(monitor_frame, text="監看已停用", foreground="gray")
        self.mouse_monitor_label.grid(row=0, column=1, padx=5, pady=5, sticky="w")
        # 色塊: 以背景色即時顯示該點顏色
        self.mouse_monitor_color_label = tk.Label(monitor_frame, width=8, height=2,
                                                  relief="sunken", bd=2, background="lightgray")
        self.mouse_monitor_color_label.grid(row=0, column=2, padx=5, pady=5)
        ttk.Label(monitor_frame, text="注意: 螢幕縮放建議設為 100%, 否則顯示座標與實際位置會有落差").grid(
            row=0, column=3, padx=5, pady=5, sticky="w"
        )

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
        # 清空後依目前 profiles 重新建立每一列 (打勾選取格 + 巨集名稱)
        for w in self.profile_list_inner.winfo_children():
            w.destroy()
        self.profile_row_widgets = {}

        for name in self.profiles:
            self.build_profile_row(name)

        self.update_all_row_styles()

    def build_profile_row(self, name):
        profile = self.profiles[name]
        row = ttk.Frame(self.profile_list_inner)
        row.pack(fill="x")

        enabled_var = tk.BooleanVar(value=profile.get("enabled", True))
        cb = ttk.Checkbutton(
            row, variable=enabled_var,
            command=lambda n=name, v=enabled_var: self.on_enabled_toggle(n, v),
        )
        cb.pack(side="left", padx=(2, 0))

        label = tk.Label(row, text=name, anchor="w", width=20, cursor="hand2")
        label.pack(side="left", fill="x", expand=True, padx=(2, 0), pady=1)
        label.bind("<Button-1>", lambda e, n=name: self.select_profile(n))

        self.profile_row_widgets[name] = {"row": row, "label": label, "var": enabled_var}

    def on_enabled_toggle(self, name, var):
        # 打勾選取格切換時, 更新該巨集是否受統一快捷鍵控制
        self.profiles[name]["enabled"] = var.get()

    def get_selected_profile(self):
        return self.selected_profile_name

    def select_profile(self, name):
        # 點擊清單項目的名稱文字時, 將其設為目前選取的巨集, 並載入右側步驟編輯區
        self.selected_profile_name = name
        self.update_all_row_styles()

        profile = self.profiles[name]
        self.steps_editor.steps = profile["steps"]
        self.steps_editor.refresh()
        self.update_status_label(name)

    def update_all_row_styles(self):
        # 依目前選取狀態與各巨集執行狀態, 更新清單中每一列的顏色顯示
        for name, widgets in self.profile_row_widgets.items():
            label = widgets["label"]
            is_selected = (name == self.selected_profile_name)
            is_running = self.running_flags.get(name, False)
            label.config(
                bg="#cfe8ff" if is_selected else "SystemButtonFace",
                fg="green" if is_running else "black",
            )

    def add_profile(self):
        name = simpledialog.askstring("新增巨集", "請輸入巨集名稱, 例如: 按鍵1")
        if not name:
            return
        if name in self.profiles:
            messagebox.showwarning("名稱重複", "此名稱已存在, 請使用其他名稱")
            return
        self.profiles[name] = {"enabled": True, "steps": []}
        self.running_flags[name] = False
        self.refresh_profile_list()

    def delete_profile(self):
        name = self.get_selected_profile()
        if not name:
            return
        if messagebox.askyesno("確認刪除", "確定要刪除巨集 [{}] 嗎".format(name)):
            self.stop_macro(name)
            del self.profiles[name]
            self.running_flags.pop(name, None)
            self.selected_profile_name = None
            self.refresh_profile_list()
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

        self.profiles[new_name] = self.profiles.pop(old_name)
        self.running_flags[new_name] = self.running_flags.pop(old_name, False)
        if self.selected_profile_name == old_name:
            self.selected_profile_name = new_name

        self.refresh_profile_list()

    def copy_profile(self):
        # 複製目前選取的巨集, 產生一份內容相同的新巨集
        old_name = self.get_selected_profile()
        if not old_name:
            messagebox.showwarning("尚未選擇", "請先選擇要複製的巨集")
            return
        new_name = simpledialog.askstring("複製巨集", "請輸入新巨集名稱", initialvalue="{}_複製".format(old_name))
        if not new_name:
            return
        if new_name in self.profiles:
            messagebox.showwarning("名稱重複", "此名稱已存在, 請使用其他名稱")
            return

        # 使用深複製, 避免新舊巨集共用同一份步驟資料
        self.profiles[new_name] = copy.deepcopy(self.profiles[old_name])
        self.running_flags[new_name] = False
        self.selected_profile_name = new_name
        self.refresh_profile_list()
        self.select_profile(new_name)

    # ---------------------- 統一巨集快捷鍵操作 ----------------------

    def apply_global_hotkey(self):
        new_hotkey = self.global_hotkey_var.get().strip()
        if not new_hotkey:
            messagebox.showwarning("輸入錯誤", "請輸入快捷鍵, 例如 f6")
            return

        self.unregister_global_hotkey()
        self.global_hotkey = new_hotkey
        self.register_global_hotkey()
        messagebox.showinfo("設定完成", "統一快捷鍵 [{}] 已套用, 將控制所有已打勾的巨集".format(new_hotkey))

    def register_global_hotkey(self):
        if not self.global_hotkey:
            return
        try:
            self.global_hotkey_handle = keyboard.add_hotkey(self.global_hotkey, self.toggle_enabled_macros)
        except Exception as e:
            messagebox.showerror("快捷鍵註冊失敗", "無法註冊快捷鍵 [{}]: {}".format(self.global_hotkey, e))

    def unregister_global_hotkey(self):
        if self.global_hotkey_handle is not None:
            try:
                keyboard.remove_hotkey(self.global_hotkey_handle)
            except Exception:
                pass
            self.global_hotkey_handle = None

    def toggle_enabled_macros(self):
        # 統一快捷鍵觸發時的邏輯: 只作用於清單中已打勾 (enabled) 的巨集
        # 若已打勾的巨集中有任一組正在執行, 則全部停止; 否則將全部已打勾且已設定步驟的巨集開始
        enabled_names = [n for n, p in self.profiles.items() if p.get("enabled", True)]
        if not enabled_names:
            return

        any_running = any(self.running_flags.get(n) for n in enabled_names)
        if any_running:
            for n in enabled_names:
                if self.running_flags.get(n):
                    self.stop_macro(n)
        else:
            for n in enabled_names:
                if self.profiles[n]["steps"] and not self.running_flags.get(n):
                    self.start_macro(n)

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

    # ---------------------- 滑鼠位置與顏色即時監看 ----------------------

    def toggle_mouse_monitor(self):
        # 依打勾狀態啟用或停用滑鼠位置與顏色的即時監看
        self.mouse_monitor_on = self.mouse_monitor_var.get()
        if self.mouse_monitor_on:
            if self._mouse_monitor_after is None:
                self.update_mouse_monitor()
        else:
            if self._mouse_monitor_after is not None:
                self.root.after_cancel(self._mouse_monitor_after)
                self._mouse_monitor_after = None
            self.mouse_monitor_label.config(text="監看已停用", foreground="gray")
            self.mouse_monitor_color_label.config(background="lightgray")

    def update_mouse_monitor(self):
        # 定期讀取目前滑鼠位置與該處顏色, 顯示在監看區域
        if not self.mouse_monitor_on:
            self._mouse_monitor_after = None
            return
        try:
            x, y = self.mouse.position
            x, y = int(x), int(y)
            img = ImageGrab.grab(bbox=(x, y, x + 1, y + 1))
            r, g, b = img.getpixel((0, 0))[:3]
            hex_color = "{:02X}{:02X}{:02X}".format(r, g, b)
            self.mouse_monitor_label.config(
                text="X: {}  Y: {}  顏色: #{}  (RGB: {}, {}, {})".format(x, y, hex_color, r, g, b),
                foreground="black",
            )
            self.mouse_monitor_color_label.config(background="#{}".format(hex_color))
        except Exception:
            self.mouse_monitor_label.config(text="無法取得滑鼠位置或顏色", foreground="red")
            self.mouse_monitor_color_label.config(background="lightgray")
        self._mouse_monitor_after = self.root.after(80, self.update_mouse_monitor)

    # ---------------------- 最小化小視窗 ----------------------

    def on_unmap(self, event=None):
        # 主視窗被最小化時 (state 變為 iconic), 隱藏主視窗並顯示小視窗
        if self.mini_window is not None:
            return
        try:
            if self.root.state() == "iconic":
                self.root.withdraw()
                self.build_mini_window()
        except Exception:
            pass

    def build_mini_window(self):
        # 建立顯示巨集執行狀態的小視窗
        self.mini_window = tk.Toplevel(self.root)
        self.mini_window.title("巨集執行狀態")
        self.mini_window.attributes("-topmost", True)
        sw = self.mini_window.winfo_screenwidth()
        sh = self.mini_window.winfo_screenheight()
        self.mini_window.geometry("260x400+{}+{}".format(sw - 280, sh - 440))
        ttk.Label(self.mini_window, text="巨集執行狀態 (打勾表示受統一快捷鍵控制)").pack(padx=5, pady=5)
        self.mini_list_frame = ttk.Frame(self.mini_window)
        self.mini_list_frame.pack(fill="both", expand=True, padx=5, pady=5)
        self.mini_toggle_btn = ttk.Button(self.mini_window, text="開始全部",
                                          command=self.toggle_enabled_macros)
        self.mini_toggle_btn.pack(fill="x", padx=5, pady=2)
        ttk.Button(self.mini_window, text="還原主視窗",
                   command=self.restore_from_mini).pack(fill="x", padx=5, pady=(2, 5))
        self.mini_window.protocol("WM_DELETE_WINDOW", self.restore_from_mini)
        self._mini_refresh_after = self.root.after(300, self.refresh_mini_window)

    def refresh_mini_window(self):
        # 定期更新小視窗的執行狀態顯示
        if self.mini_window is None:
            self._mini_refresh_after = None
            return
        if set(self.mini_widgets.keys()) != set(self.profiles.keys()):
            for w in self.mini_list_frame.winfo_children():
                w.destroy()
            self.mini_widgets = {}
            for name in self.profiles:
                row = ttk.Frame(self.mini_list_frame)
                row.pack(fill="x")
                enabled_var = tk.BooleanVar(value=self.profiles[name].get("enabled", True))
                ttk.Checkbutton(row, variable=enabled_var,
                                command=lambda n=name, v=enabled_var: self.on_enabled_toggle(n, v)).pack(side="left")
                ttk.Label(row, text=name, width=12).pack(side="left", padx=(2, 5))
                status_label = ttk.Label(row, foreground="gray")
                status_label.pack(side="right", padx=5)
                self.mini_widgets[name] = status_label
        for name, label in self.mini_widgets.items():
            running = self.running_flags.get(name, False)
            label.config(text="執行中" if running else "已停止",
                         foreground="green" if running else "gray")
        # 依執行狀態動態切換按鈕文字
        enabled_names = [n for n, p in self.profiles.items() if p.get("enabled", True)]
        any_running = any(self.running_flags.get(n) for n in enabled_names)
        self.mini_toggle_btn.config(text="停止全部" if any_running else "開始全部")
        self._mini_refresh_after = self.root.after(300, self.refresh_mini_window)

    def restore_from_mini(self):
        # 從小視窗還原主視窗
        if self._mini_refresh_after is not None:
            self.root.after_cancel(self._mini_refresh_after)
            self._mini_refresh_after = None
        if self.mini_window is not None:
            self.mini_window.destroy()
            self.mini_window = None
        self.mini_widgets = {}
        self.root.deiconify()
        self.root.lift()
        # 同步主畫面左側清單的打勾狀態
        self.refresh_profile_list()

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

    def capture_and_save_image(self, editor, target="main"):
        # 讀取編輯器目前的區塊座標, 排序後擷取畫面並儲存成 png 檔案
        # target 決定填入主條件或退出條件的區塊欄位
        if target == "exit":
            var_x1 = editor.exit_rx1_var
            var_y1 = editor.exit_ry1_var
            var_x2 = editor.exit_rx2_var
            var_y2 = editor.exit_ry2_var
            var_path = editor.exit_image_path_var
        else:
            var_x1 = editor.rx1_var
            var_y1 = editor.ry1_var
            var_x2 = editor.rx2_var
            var_y2 = editor.ry2_var
            var_path = editor.image_path_var
        try:
            x1 = int(var_x1.get())
            y1 = int(var_y1.get())
            x2 = int(var_x2.get())
            y2 = int(var_y2.get())
        except ValueError:
            return

        x1, x2 = sorted((x1, x2))
        y1, y2 = sorted((y1, y2))
        if x2 - x1 < 2 or y2 - y1 < 2:
            messagebox.showwarning("擷取失敗", "區塊範圍過小, 請重新選取")
            return

        var_x1.set(str(x1))
        var_y1.set(str(y1))
        var_x2.set(str(x2))
        var_y2.set(str(y2))

        filename = "pic_{}.png".format(int(time.time() * 1000))
        filepath = os.path.join(PICS_DIR, filename)
        try:
            img = ImageGrab.grab(bbox=(x1, y1, x2, y2))
            img.save(filepath)
        except Exception as e:
            messagebox.showerror("擷取失敗", str(e))
            return

        var_path.set(filepath)

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
        self.root.after(0, self.update_all_row_styles)

    def _release_all(self):
        for key in list(self._held_keys):
            keyboard.release(key)
        self._held_keys.clear()
        for btn_name in list(self._held_mouse_buttons):
            if btn_name in MOUSE_BUTTON_MAP:
                self.mouse.release(MOUSE_BUTTON_MAP[btn_name])
        self._held_mouse_buttons.clear()

    def stop_macro(self, name):
        self.running_flags[name] = False
        self._release_all()
        self.root.after(0, lambda: self.update_status_label(name))
        self.root.after(0, self.update_all_row_styles)

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
            self._held_keys.add(step["key"])

        elif step_type == "key_up":
            keyboard.release(step["key"])
            self._held_keys.discard(step["key"])

        elif step_type == "key_click":
            keyboard.send(step["key"])

        elif step_type == "move_mouse":
            self.mouse.position = (step["x"], step["y"])

        elif step_type == "mouse_down":
            self.mouse.press(MOUSE_BUTTON_MAP[step["button"]])
            self._held_mouse_buttons.add(step["button"])

        elif step_type == "mouse_up":
            self.mouse.release(MOUSE_BUTTON_MAP[step["button"]])
            self._held_mouse_buttons.discard(step["button"])

        elif step_type == "mouse_click":
            self.mouse.click(MOUSE_BUTTON_MAP[step["button"]])

        elif step_type == "wait":
            time.sleep(step["seconds"])

        elif step_type == "find_window_move":
            find_and_move_window(step["title"], step["x"], step["y"])

        elif step_type in ("if_color", "if_image", "if_key"):
            if step_type == "if_color":
                matched = check_color(step["x"], step["y"], step["color"], step["tolerance"])
            elif step_type == "if_image":
                region = (step["rx1"], step["ry1"], step["rx2"], step["ry2"])
                matched = check_image(region, step["image_path"], step["confidence"])
            else:
                try:
                    matched = keyboard.is_pressed(step["key"])
                except Exception:
                    matched = False
            if not matched:
                # 主 if_image 條件未符合時, 儲存當下畫面供使用者診斷比對失敗的原因
                if step_type == "if_image":
                    self.save_debug_image(region)
                return
            # 條件符合時迴圈執行子步驟, 直到次數用完或退出條件出現
            loop_times = int(step.get("loop_times", 1))
            count = 0
            while self.running_flags.get(name):
                self.execute_steps(name, step.get("then", []))
                count += 1
                if loop_times and count >= loop_times:
                    break
                if self.check_exit_condition(step):
                    break

    def check_exit_condition(self, step):
        # 檢查迴圈的退出條件, 支援多個條件, 任一符合即結束 (OR)
        ex = step.get("exit_condition")
        if not ex:
            return False
        if isinstance(ex, dict):
            ex = [ex]
        for e in ex:
            try:
                if e.get("type") == "if_color":
                    if check_color(e["x"], e["y"], e["color"], e["tolerance"]):
                        return True
                elif e.get("type") == "if_image":
                    region = (e["rx1"], e["ry1"], e["rx2"], e["ry2"])
                    if check_image(region, e["image_path"], e["confidence"]):
                        return True
                elif e.get("type") == "if_key":
                    if keyboard.is_pressed(e["key"]):
                        return True
            except Exception:
                pass
        return False

    def save_debug_image(self, region):
        # 比對失敗時, 將當下的區域畫面存到 pics 資料夾供診斷
        try:
            x1, y1, x2, y2 = [int(v) for v in region]
            if x2 <= x1 or y2 <= y1:
                return
            filename = "fail_{}.png".format(int(time.time() * 1000))
            filepath = os.path.join(PICS_DIR, filename)
            img = ImageGrab.grab(bbox=(x1, y1, x2, y2))
            img.save(filepath)
        except Exception:
            pass

    def update_status_label(self, name):
        if self.get_selected_profile() != name:
            return
        if self.running_flags.get(name):
            self.status_label.config(text="狀態: 執行中 (可再次按手動按鈕或統一快捷鍵停止)", foreground="green")
        else:
            self.status_label.config(text="狀態: 已停止", foreground="red")

    # ---------------------- 關閉程式 ----------------------

    def on_close(self):
        if self._hook_id is not None:
            keyboard.unhook(self._hook_id)
            self._hook_id = None
        if self._mouse_monitor_after is not None:
            self.root.after_cancel(self._mouse_monitor_after)
            self._mouse_monitor_after = None
        if self._mini_refresh_after is not None:
            self.root.after_cancel(self._mini_refresh_after)
            self._mini_refresh_after = None
        if self.mini_window is not None:
            self.mini_window.destroy()
            self.mini_window = None
        for name in list(self.profiles.keys()):
            self.stop_macro(name)
        self.unregister_global_hotkey()
        self.unregister_record_hotkeys()
        self.root.destroy()


if __name__ == "__main__":
    # 建立固定名稱的互斥鎖, 確保同一個程式同時只能執行一個實例
    ctypes.windll.kernel32.CreateMutexW.restype = ctypes.c_void_p
    _single_instance_mutex = ctypes.windll.kernel32.CreateMutexW(
        None, False, "MacroTool_SingleInstance"
    )
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.user32.MessageBoxW(0, "巨集自動化工具已在執行中", "提示", 0x40)
        sys.exit(0)

    root = tk.Tk()
    app = MacroApp(root)
    root.mainloop()
