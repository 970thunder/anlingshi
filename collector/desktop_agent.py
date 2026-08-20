"""Windows desktop authorization refresher for the authorized game account.

This tool never drives WeChat login. It starts a local mitmproxy listener,
guides the operator to open the mini-program manually, and uploads the newly
authorized JWT to the paired server. The pairing token is protected with the
current Windows user's DPAPI before it is written to disk.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from ctypes import POINTER, Structure, byref, c_byte, c_void_p, cast
from pathlib import Path
from tkinter import BooleanVar, StringVar, Tk, messagebox
from tkinter import ttk

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = Path(os.getenv("APPDATA", Path.home())) / "AnlingshiRefreshAgent"
CONFIG_PATH = APP_DIR / "agent.json"


class DataBlob(Structure):
    _fields_ = [("cbData", ctypes.c_uint), ("pbData", POINTER(c_byte))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("the desktop agent only supports Windows")
    source_buffer = ctypes.create_string_buffer(data)
    source = DataBlob(len(data), cast(source_buffer, POINTER(c_byte)))
    target = DataBlob()
    crypt32 = ctypes.windll.crypt32
    if protect:
        ok = crypt32.CryptProtectData(byref(source), "AnlingshiRefreshAgent", None, None, None, 0, byref(target))
    else:
        ok = crypt32.CryptUnprotectData(byref(source), None, None, None, None, 0, byref(target))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(c_void_p(cast(target.pbData, c_void_p).value))


def load_config() -> dict[str, str]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        token = _dpapi(base64.b64decode(raw.get("device_token_protected", "")), False).decode()
        return {"server_url": raw.get("server_url", ""), "device_id": raw.get("device_id", ""), "device_token": token, "port": str(raw.get("port", "8081")), "proxy_backup": raw.get("proxy_backup")}
    except Exception:
        return {}


def save_config(values: dict[str, object]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    protected = base64.b64encode(_dpapi(values["device_token"].encode(), True)).decode()
    CONFIG_PATH.write_text(json.dumps({"server_url": str(values["server_url"]).rstrip("/"), "device_id": values["device_id"], "device_token_protected": protected, "port": values["port"], "proxy_backup": values.get("proxy_backup")}, ensure_ascii=False), encoding="utf-8")


def notify_proxy_change() -> None:
    wininet = ctypes.windll.wininet
    wininet.InternetSetOptionW(0, 39, None, 0)
    wininet.InternetSetOptionW(0, 37, None, 0)


def read_windows_proxy() -> dict[str, object]:
    import winreg
    path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as key:
        def value(name: str, fallback: object) -> object:
            try:
                return winreg.QueryValueEx(key, name)[0]
            except FileNotFoundError:
                return fallback
        return {"enabled": int(value("ProxyEnable", 0)), "server": str(value("ProxyServer", "")), "override": str(value("ProxyOverride", ""))}


def set_windows_proxy(port: str, backup: dict[str, object] | None = None) -> None:
    import winreg
    path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
        if backup is None:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, f"127.0.0.1:{port}")
        else:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, int(backup["enabled"]))
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, str(backup["server"]))
            winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, str(backup["override"]))
    notify_proxy_change()


class AgentApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.process: subprocess.Popen[str] | None = None
        self.events: queue.Queue[str] = queue.Queue()
        current = load_config()
        self.server_url = StringVar(value=current.get("server_url", "http://127.0.0.1:8000"))
        self.device_id = StringVar(value=current.get("device_id", ""))
        self.device_token = StringVar(value=current.get("device_token", ""))
        self.port = StringVar(value=current.get("port", "8081"))
        self.proxy_backup = current.get("proxy_backup")
        self.proxy_enabled = BooleanVar(value=False)
        self.status = StringVar(value="等待配对")
        self.build()
        self.root.after(250, self.read_events)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def build(self) -> None:
        self.root.title("黯灵师授权刷新终端")
        self.root.geometry("620x460")
        frame = ttk.Frame(self.root, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="服务器地址").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.server_url, width=58).grid(row=0, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="设备 ID").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.device_id, width=58).grid(row=1, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="配对令牌").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.device_token, show="*", width=58).grid(row=2, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="本地代理端口").grid(row=3, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.port, width=15).grid(row=3, column=1, sticky="w", pady=5)
        controls = ttk.Frame(frame)
        controls.grid(row=4, column=0, columnspan=2, sticky="w", pady=12)
        ttk.Button(controls, text="保存配对", command=self.save).pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="启动授权采集", command=self.start).pack(side="left", padx=8)
        ttk.Button(controls, text="停止", command=self.stop).pack(side="left", padx=8)
        ttk.Checkbutton(controls, text="启用 Windows 代理", variable=self.proxy_enabled, command=self.toggle_proxy).pack(side="left", padx=8)
        ttk.Label(frame, textvariable=self.status, foreground="#285a8e").grid(row=5, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Label(frame, text="操作：从 /admij 创建设备并复制设备 ID、配对令牌。启动后手动打开已授权的小程序；看到“凭证已上传”即可关闭小程序。服务器会在凭证有效期内独立采集。", wraplength=560, justify="left").grid(row=6, column=0, columnspan=2, sticky="w", pady=10)
        self.log = __import__("tkinter").Text(frame, height=12, state="disabled", wrap="word")
        self.log.grid(row=7, column=0, columnspan=2, sticky="nsew")
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(7, weight=1)

    def values(self) -> dict[str, object]:
        return {"server_url": self.server_url.get().strip(), "device_id": self.device_id.get().strip(), "device_token": self.device_token.get().strip(), "port": self.port.get().strip(), "proxy_backup": self.proxy_backup}

    def save(self) -> bool:
        values = self.values()
        if not all(values.values()) or not values["port"].isdigit():
            messagebox.showerror("配置错误", "请填写服务器地址、设备 ID、配对令牌和端口。")
            return False
        try:
            save_config(values)
            self.status.set("配对已使用 Windows DPAPI 加密保存")
            return True
        except Exception as exc:
            messagebox.showerror("保存失败", type(exc).__name__)
            return False

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return
        if not self.save():
            return
        values = self.values()
        executable = shutil.which("mitmdump") or str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "mitmproxy" / "bin" / "mitmdump.exe")
        if not Path(executable).exists() and not shutil.which("mitmdump"):
            messagebox.showerror("缺少 mitmproxy", "未找到 mitmdump，请先安装 mitmproxy。")
            return
        env = os.environ.copy()
        env.update({"DEVICE_SERVER_URL": values["server_url"], "DEVICE_ID": values["device_id"], "DEVICE_TOKEN": values["device_token"], "TARGET_HOST": "anlingshiapi.mangqu.xin"})
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen([executable, "--listen-host", "127.0.0.1", "--listen-port", values["port"], "-s", str(ROOT / "collector" / "token_refresh_addon.py")], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=flags)
        threading.Thread(target=self.forward_output, daemon=True).start()
        self.status.set("采集已启动；请手动打开小程序完成授权")

    def forward_output(self) -> None:
        if self.process and self.process.stdout:
            for line in self.process.stdout:
                self.events.put(line.strip())

    def read_events(self) -> None:
        while not self.events.empty():
            line = self.events.get_nowait()
            self.log.configure(state="normal")
            self.log.insert("end", line + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
            if "credential uploaded" in line:
                self.status.set("凭证已上传，服务器正在独立采集")
        self.root.after(250, self.read_events)

    def toggle_proxy(self) -> None:
        try:
            if self.proxy_enabled.get():
                self.proxy_backup = read_windows_proxy()
                set_windows_proxy(self.port.get())
                self.status.set("Windows 代理已启用")
            else:
                set_windows_proxy(self.port.get(), self.proxy_backup)
                self.proxy_backup = None
                self.status.set("已恢复之前的 Windows 代理设置")
            self.save()
        except Exception as exc:
            messagebox.showerror("代理设置失败", type(exc).__name__)
            self.proxy_enabled.set(False)

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process = None
        self.status.set("采集已停止")

    def close(self) -> None:
        self.stop()
        self.root.destroy()


if __name__ == "__main__":
    root = Tk()
    AgentApp(root)
    root.mainloop()
