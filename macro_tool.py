# -*- coding: utf-8 -*-
# 巨集自動化工具
# 功能: 自訂鍵盤與滑鼠動作序列, 設定快捷鍵開始或停止, 並可儲存與載入設定檔
# 需求套件: pip install keyboard pynput
# 注意事項: 在 Windows 上, keyboard 套件要監聽全域快捷鍵通常需要以系統管理員身分執行

import json
import os
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

import keyboard
from pynput.mouse import Controller as MouseController, Button

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
    "mouse_down": "滑鼠按下",
    "mouse_up": "滑鼠放開",
    "mouse_click": "滑鼠點擊",
    "wait": "等待秒數",
}
STEP_TYPE_REVERSE = {v: k for k, v in STEP_TYPE_DISPLAY.items()}

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "macros.json")


class MacroApp:
    def __init__(self, root):
        self.root = root
        self.root.title("巨集自動化工具")
        self.root.geometry("820x560")

        # 資料結構: profiles = { 名稱: {"hotkey": 字串, "steps": [step, ...]} }
        self.profiles = {}
        # 每組巨集目前是否正在執行
        self.running_flags = {}
        # 每組巨集對應的執行緒
        self.threads = {}
        # 每組巨集註冊的全域快捷鍵代碼, 用於之後移除
        self.hotkey_handles = {}

        self.mouse = MouseController()

        self.load_data()
        self.build_ui()
        self.refresh_profile_list()

        # 關閉視窗時, 停止所有執行中的巨集並移除快捷鍵
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # 程式啟動時, 依照已儲存的資料重新註冊所有快捷鍵
        for name in self.profiles:
            self.register_hotkey(name)

    # ---------------------- 資料存取 ----------------------

    def load_data(self):
        # 讀取本地 JSON 檔案, 若不存在則使用空字典
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
        # 將目前所有巨集設定寫入 JSON 檔案
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

        # 快捷鍵設定區域
        hotkey_frame = ttk.LabelFrame(right_frame, text="快捷鍵設定 (開始 / 停止 切換)")
        hotkey_frame.pack(fill="x", pady=(0, 10))

        ttk.Label(hotkey_frame, text="快捷鍵:").grid(row=0, column=0, padx=5, pady=5)
        self.hotkey_var = tk.StringVar()
        self.hotkey_entry = ttk.Entry(hotkey_frame, textvariable=self.hotkey_var, width=20)
        self.hotkey_entry.grid(row=0, column=1, padx=5, pady=5)
        ttk.Label(hotkey_frame, text="例如: f6 或 ctrl+alt+z").grid(row=0, column=2, padx=5, pady=5)
        ttk.Button(hotkey_frame, text="套用快捷鍵", command=self.apply_hotkey).grid(row=0, column=3, padx=5, pady=5)

        self.status_label = ttk.Label(hotkey_frame, text="狀態: 尚未選擇巨集", foreground="gray")
        self.status_label.grid(row=1, column=0, columnspan=4, padx=5, pady=(0, 5), sticky="w")

        ttk.Button(hotkey_frame, text="手動開始 / 停止", command=self.manual_toggle).grid(
            row=0, column=4, padx=5, pady=5
        )

        # 步驟清單區域
        steps_frame = ttk.LabelFrame(right_frame, text="動作步驟 (依序由上往下執行, 執行完畢後自動重頭開始)")
        steps_frame.pack(fill="both", expand=True, pady=(0, 10))

        self.steps_listbox = tk.Listbox(steps_frame, height=14)
        self.steps_listbox.pack(side="left", fill="both", expand=True, padx=(5, 0), pady=5)

        steps_btn_frame = ttk.Frame(steps_frame)
        steps_btn_frame.pack(side="left", fill="y", padx=5, pady=5)
        ttk.Button(steps_btn_frame, text="上移", command=self.move_step_up).pack(fill="x", pady=2)
        ttk.Button(steps_btn_frame, text="下移", command=self.move_step_down).pack(fill="x", pady=2)
        ttk.Button(steps_btn_frame, text="刪除步驟", command=self.delete_step).pack(fill="x", pady=2)

        # 新增步驟區域
        add_step_frame = ttk.LabelFrame(right_frame, text="新增動作步驟")
        add_step_frame.pack(fill="x")

        ttk.Label(add_step_frame, text="動作類型:").grid(row=0, column=0, padx=5, pady=5)
        self.step_type_var = tk.StringVar(value=STEP_TYPE_DISPLAY["key_down"])
        self.step_type_combo = ttk.Combobox(
            add_step_frame,
            textvariable=self.step_type_var,
            values=list(STEP_TYPE_DISPLAY.values()),
            state="readonly",
            width=15,
        )
        self.step_type_combo.grid(row=0, column=1, padx=5, pady=5)
        self.step_type_combo.bind("<<ComboboxSelected>>", self.on_step_type_change)

        # 按鍵名稱輸入框 (用於 key_down, key_up, key_click)
        self.key_label = ttk.Label(add_step_frame, text="按鍵名稱:")
        self.key_label.grid(row=0, column=2, padx=5, pady=5)
        self.key_var = tk.StringVar()
        self.key_entry = ttk.Entry(add_step_frame, textvariable=self.key_var, width=12)
        self.key_entry.grid(row=0, column=3, padx=5, pady=5)

        # 滑鼠按鈕選擇框 (用於 mouse_down, mouse_up, mouse_click)
        self.mouse_label = ttk.Label(add_step_frame, text="滑鼠按鈕:")
        self.mouse_var = tk.StringVar(value="left")
        self.mouse_combo = ttk.Combobox(
            add_step_frame, textvariable=self.mouse_var, values=["left", "right", "middle"], state="readonly", width=10
        )

        # 等待秒數輸入框 (用於 wait)
        self.wait_label = ttk.Label(add_step_frame, text="秒數:")
        self.wait_var = tk.StringVar(value="0.1")
        self.wait_entry = ttk.Entry(add_step_frame, textvariable=self.wait_var, width=10)

        ttk.Button(add_step_frame, text="新增此步驟", command=self.add_step).grid(row=0, column=4, padx=10, pady=5)

        # 初始顯示狀態依照預設動作類型調整
        self.on_step_type_change()

    # ---------------------- 動作類型切換介面 ----------------------

    def on_step_type_change(self, event=None):
        # 依照選擇的動作類型, 顯示對應的輸入欄位, 隱藏不需要的欄位
        step_type_cn = self.step_type_var.get()
        step_type = STEP_TYPE_REVERSE[step_type_cn]

        self.key_label.grid_remove()
        self.key_entry.grid_remove()
        self.mouse_label.grid_remove()
        self.mouse_combo.grid_remove()
        self.wait_label.grid_remove()
        self.wait_entry.grid_remove()

        if step_type in ("key_down", "key_up", "key_click"):
            self.key_label.grid(row=0, column=2, padx=5, pady=5)
            self.key_entry.grid(row=0, column=3, padx=5, pady=5)
        elif step_type in ("mouse_down", "mouse_up", "mouse_click"):
            self.mouse_label.grid(row=0, column=2, padx=5, pady=5)
            self.mouse_combo.grid(row=0, column=3, padx=5, pady=5)
        elif step_type == "wait":
            self.wait_label.grid(row=0, column=2, padx=5, pady=5)
            self.wait_entry.grid(row=0, column=3, padx=5, pady=5)

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
            self.steps_listbox.delete(0, tk.END)
            self.hotkey_var.set("")
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
        self.refresh_steps_listbox(name)
        self.update_status_label(name)

    # ---------------------- 快捷鍵操作 ----------------------

    def apply_hotkey(self):
        name = self.get_selected_profile()
        if not name:
            messagebox.showwarning("尚未選擇", "請先在左側選擇一組巨集")
            return

        new_hotkey = self.hotkey_var.get().strip()
        if not new_hotkey:
            messagebox.showwarning("輸入錯誤", "請輸入快捷鍵, 例如 f6")
            return

        # 移除舊的快捷鍵註冊, 再註冊新的快捷鍵
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

    # ---------------------- 步驟清單操作 ----------------------

    def refresh_steps_listbox(self, name):
        self.steps_listbox.delete(0, tk.END)
        for idx, step in enumerate(self.profiles[name]["steps"], start=1):
            self.steps_listbox.insert(tk.END, "{}. {}".format(idx, self.format_step(step)))

    def format_step(self, step):
        # 將步驟資料轉換成人類可讀的顯示文字
        step_type = step["type"]
        display = STEP_TYPE_DISPLAY[step_type]
        if step_type in ("key_down", "key_up", "key_click"):
            return "{} [{}]".format(display, step["key"])
        if step_type in ("mouse_down", "mouse_up", "mouse_click"):
            return "{} [{}]".format(display, step["button"])
        if step_type == "wait":
            return "{} [{} 秒]".format(display, step["seconds"])
        return display

    def add_step(self):
        name = self.get_selected_profile()
        if not name:
            messagebox.showwarning("尚未選擇", "請先在左側選擇一組巨集")
            return

        step_type_cn = self.step_type_var.get()
        step_type = STEP_TYPE_REVERSE[step_type_cn]

        if step_type in ("key_down", "key_up", "key_click"):
            key = self.key_var.get().strip()
            if not key:
                messagebox.showwarning("輸入錯誤", "請輸入按鍵名稱")
                return
            step = {"type": step_type, "key": key}
        elif step_type in ("mouse_down", "mouse_up", "mouse_click"):
            step = {"type": step_type, "button": self.mouse_var.get()}
        elif step_type == "wait":
            try:
                seconds = float(self.wait_var.get())
            except ValueError:
                messagebox.showwarning("輸入錯誤", "秒數必須是數字")
                return
            step = {"type": step_type, "seconds": seconds}
        else:
            return

        self.profiles[name]["steps"].append(step)
        self.refresh_steps_listbox(name)

    def get_selected_step_index(self):
        selection = self.steps_listbox.curselection()
        if not selection:
            return None
        return selection[0]

    def move_step_up(self):
        name = self.get_selected_profile()
        idx = self.get_selected_step_index()
        if name is None or idx is None or idx == 0:
            return
        steps = self.profiles[name]["steps"]
        steps[idx - 1], steps[idx] = steps[idx], steps[idx - 1]
        self.refresh_steps_listbox(name)
        self.steps_listbox.selection_set(idx - 1)

    def move_step_down(self):
        name = self.get_selected_profile()
        idx = self.get_selected_step_index()
        if name is None or idx is None:
            return
        steps = self.profiles[name]["steps"]
        if idx >= len(steps) - 1:
            return
        steps[idx + 1], steps[idx] = steps[idx], steps[idx + 1]
        self.refresh_steps_listbox(name)
        self.steps_listbox.selection_set(idx + 1)

    def delete_step(self):
        name = self.get_selected_profile()
        idx = self.get_selected_step_index()
        if name is None or idx is None:
            return
        del self.profiles[name]["steps"][idx]
        self.refresh_steps_listbox(name)

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
        self.update_status_label(name)

    def stop_macro(self, name):
        self.running_flags[name] = False
        self.update_status_label(name)

    def run_loop(self, name):
        # 持續執行該巨集的步驟, 直到狀態被設為停止為止
        while self.running_flags.get(name):
            steps = self.profiles[name]["steps"]
            for step in steps:
                if not self.running_flags.get(name):
                    break
                self.execute_step(step)

    def execute_step(self, step):
        step_type = step["type"]
        if step_type == "key_down":
            keyboard.press(step["key"])
        elif step_type == "key_up":
            keyboard.release(step["key"])
        elif step_type == "key_click":
            keyboard.send(step["key"])
        elif step_type == "mouse_down":
            self.mouse.press(MOUSE_BUTTON_MAP[step["button"]])
        elif step_type == "mouse_up":
            self.mouse.release(MOUSE_BUTTON_MAP[step["button"]])
        elif step_type == "mouse_click":
            self.mouse.click(MOUSE_BUTTON_MAP[step["button"]])
        elif step_type == "wait":
            time.sleep(step["seconds"])

    def update_status_label(self, name):
        # 更新畫面上的執行狀態文字, 若目前選擇的巨集不是傳入的名稱則不更新
        if self.get_selected_profile() != name:
            return
        if self.running_flags.get(name):
            self.status_label.config(text="狀態: 執行中 (快捷鍵可再次按下以停止)", foreground="green")
        else:
            self.status_label.config(text="狀態: 已停止", foreground="red")

    # ---------------------- 關閉程式 ----------------------

    def on_close(self):
        for name in list(self.profiles.keys()):
            self.stop_macro(name)
            self.unregister_hotkey(name)
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = MacroApp(root)
    root.mainloop()
