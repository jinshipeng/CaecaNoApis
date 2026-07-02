#!/usr/bin/env python3
"""
供应链智能运营系统 - 一体化启动器

功能：
  - 自动检查并安装缺失的依赖（已安装则跳过）
  - 单实例保护：重复运行自动激活已有窗口
  - 统一 GUI 窗口展示 Django + Vue 运行日志
  - 关闭窗口 → 最小化到系统托盘（任务栏隐藏图标区）
  - 再次运行脚本 → 恢复已有窗口
  - 点击「结束系统」一键终止所有子进程
"""

import sys
import os
import time
import json
import socket
import logging
import threading
import subprocess
from pathlib import Path
from datetime import datetime
from queue import Queue, Empty

# ── 单实例 IPC 端口 ──────────────────────────────────────────────────
IPC_PORT = 19998
IPC_HOST = "127.0.0.1"
IPC_CMD_RESTORE = b"RESTORE"
IPC_CMD_PING = b"PING"
IPC_RESP_OK = b"OK"

# ── 路径 ──────────────────────────────────────────────────────────────
REPO_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = REPO_DIR / "system"
FRONTEND_DIR = REPO_DIR / "frontend"
VENV_DIR = SYSTEM_DIR / ".venv"
LOG_DIR = REPO_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ── 日志配置 ──────────────────────────────────────────────────────────
logger = logging.getLogger("launcher")
logger.setLevel(logging.INFO)
fh = logging.FileHandler(LOG_DIR / "launcher.log", encoding="utf-8")
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(fh)

# ── 依赖检查 ──────────────────────────────────────────────────────────
REQUIRED_PY_PKGS = [
    "django", "djangorestframework", "django-cors-headers",
    "django-filter", "django-import-export", "djangorestframework-authtoken",
    "django-extensions", "whitenoise", "numpy", "pandas",
    "python-dotenv", "Pillow", "openpyxl",
]


def _py_pkg_installed(name: str) -> bool:
    try:
        __import__(name.replace("-", "_"))
        return True
    except ImportError:
        return False


def check_dependencies() -> tuple[list[str], bool]:
    missing_py = [p for p in REQUIRED_PY_PKGS if not _py_pkg_installed(p)]
    node_missing = not (REPO_DIR / "frontend" / "node_modules").exists()
    return missing_py, node_missing


def install_dependencies(missing_py: list[str], missing_node: bool, log_func):
    python_exe = sys.executable

    if missing_py:
        log_func(f"安装 Python 依赖: {', '.join(missing_py)}")
        if VENV_DIR.exists():
            pip = str(VENV_DIR / ("Scripts" if os.name == "nt" else "bin") / "pip")
        else:
            pip = [python_exe, "-m", "pip"]
        cmd = [*pip, "install", *missing_py,
               "-i", "https://pypi.tuna.tsinghua.edu.cn/simple",
               "--trusted-host", "pypi.tuna.tsinghua.edu.cn",
               "--timeout", "120", "--retries", "3"]
        try:
            subprocess.run(cmd, check=True, cwd=str(SYSTEM_DIR))
            log_func("Python 依赖安装完成。")
        except subprocess.CalledProcessError:
            log_func("⚠ 清华镜像失败，尝试阿里云镜像...")
            cmd2 = [*pip, "install", *missing_py,
                    "-i", "https://mirrors.aliyun.com/pypi/simple/",
                    "--trusted-host", "mirrors.aliyun.com",
                    "--timeout", "120", "--retries", "3"]
            subprocess.run(cmd2, cwd=str(SYSTEM_DIR))

    if missing_node:
        log_func("安装 Node.js 依赖...")
        npm_cmd = "npm.cmd" if os.name == "nt" else "npm"
        subprocess.run(
            [npm_cmd, "install", "--registry=https://registry.npmmirror.com"],
            cwd=str(FRONTEND_DIR), check=True,
        )
        log_func("Node.js 依赖安装完成。")


def check_port(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.close()
        return True
    except OSError:
        return False


# ── 单实例检测 ──────────────────────────────────────────────────────────
def try_activate_existing() -> bool:
    """尝试连接已有实例的 IPC socket，发送恢复命令。
    返回 True 表示已有实例且已激活，当前进程应退出。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect((IPC_HOST, IPC_PORT))
        s.sendall(IPC_CMD_RESTORE)
        resp = s.recv(1024)
        s.close()
        return resp == IPC_RESP_OK
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


# ── 子进程管理 ────────────────────────────────────────────────────────
class ProcessManager:
    def __init__(self, output_queue: Queue):
        self.procs: list[subprocess.Popen] = []
        self.queue = output_queue
        self._stopping = False

    def _enqueue(self, source: str, line: str):
        try:
            self.queue.put_nowait((source, line.rstrip("\n")))
        except Exception:
            pass

    def _reader_thread(self, pipe, source: str):
        """逐行读取子进程输出，加入微量延迟实现公平交错"""
        try:
            for line in iter(pipe.readline, ""):
                if self._stopping:
                    break
                self._enqueue(source, line)
                time.sleep(0.005)  # 5ms 让出 CPU，实现两个源的输出交错
        except (ValueError, OSError):
            pass
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    def _start_process(self, cmd: list, cwd: str, label: str) -> subprocess.Popen | None:
        """启动子进程并添加错误诊断"""
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            p = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace",
                creationflags=creationflags,
            )
            self.procs.append(p)
            threading.Thread(target=self._reader_thread, args=(p.stdout, label), daemon=True).start()

            # 0.5 秒后检查进程是否已崩溃
            def check_startup():
                time.sleep(0.5)
                if p.poll() is not None:
                    self._enqueue("系统", f"⚠ {label} 进程启动失败(退出码 {p.returncode})，请检查环境配置。")

            threading.Thread(target=check_startup, daemon=True).start()
            return p
        except FileNotFoundError:
            self._enqueue("系统", f"⚠ 找不到 {label} 命令 ({cmd[0]})，请确认已安装对应工具。")
            return None
        except Exception as e:
            self._enqueue("系统", f"⚠ {label} 启动异常: {e}")
            return None

    def _find_npm(self) -> str:
        """查找 npm 可执行文件"""
        import shutil
        # 优先找 npm.cmd (Windows)
        for name in ["npm.cmd", "npm"]:
            path = shutil.which(name)
            if path:
                return path
        # 回退到常见安装路径
        for base in [os.path.expandvars(r"%ProgramFiles%\\nodejs"),
                      os.path.expandvars(r"%ProgramFiles(x86)%\\nodejs"),
                      os.path.expandvars(r"%APPDATA%\\npm")]:
            for name in ["npm.cmd", "npm"]:
                full = os.path.join(base, name)
                if os.path.isfile(full):
                    return full
        return "npm"  # 最后回退

    def start(self):
        python_exe = sys.executable

        self._enqueue("系统", "正在启动 Django 后端 (port 8000)...")
        self._start_process(
            [python_exe, str(SYSTEM_DIR / "manage.py"), "runserver", "0.0.0.0:8000"],
            SYSTEM_DIR, "Django",
        )

        self._enqueue("系统", "正在启动 Vue 前端 (port 3000)...")
        npm = self._find_npm()
        self._enqueue("系统", f"npm 路径: {npm}")
        self._start_process(
            [npm, "run", "dev"],
            FRONTEND_DIR, "Vite",
        )

    def stop(self):
        self._stopping = True
        for p in self.procs:
            try:
                p.terminate()
            except Exception:
                pass
        time.sleep(1.2)
        for p in self.procs:
            if p.poll() is None:
                try:
                    p.kill()
                except Exception:
                    pass
        self.procs.clear()
        self._enqueue("系统", "所有子进程已终止。")


# ── IPC 服务（运行在主线程的 tkinter after 循环中） ─────────────────────
class IPCServer:
    """在后台线程接受连接，通过 tkinter after 回调通知主线程"""

    def __init__(self, on_restore):
        self._on_restore = on_restore
        self._running = False
        self._server: socket.socket | None = None

    def start(self):
        self._running = True
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((IPC_HOST, IPC_PORT))
        self._server.listen(1)
        self._server.settimeout(1.0)
        t = threading.Thread(target=self._serve, daemon=True)
        t.start()

    def _serve(self):
        while self._running:
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                data = conn.recv(1024)
                if data in (IPC_CMD_RESTORE, IPC_CMD_PING):
                    conn.sendall(IPC_RESP_OK)
                    if data == IPC_CMD_RESTORE:
                        self._on_restore()
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def stop(self):
        self._running = False
        try:
            self._server.close()
        except Exception:
            pass


# ── 系统托盘 ──────────────────────────────────────────────────────────
TRAY_AVAILABLE = False
try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except ImportError:
    pass


def create_tray_image():
    """创建 64x64 系统托盘图标"""
    img = Image.new("RGBA", (64, 64), (30, 35, 50, 255))
    draw = ImageDraw.Draw(img)
    draw.ellipse((14, 14, 50, 50), fill="#5DAF5A")
    draw.polygon([(25, 21), (25, 43), (43, 32)], fill="white")
    return img


# ── GUI 窗口 ──────────────────────────────────────────────────────────
def run_gui():
    import tkinter as tk
    from tkinter import scrolledtext, messagebox, ttk

    root = tk.Tk()
    root.title("联宝智能 — 供应链智能运营系统")
    root.geometry("900x600")
    root.minsize(700, 400)
    try:
        root.iconbitmap(default="")
    except Exception:
        pass

    mgr: ProcessManager | None = None
    tray_icon: pystray.Icon | None = None

    # ── 顶部信息栏 ──
    top = ttk.Frame(root, padding=8)
    top.pack(fill=tk.X)
    status_label = ttk.Label(top, text="● 就绪", foreground="gray")
    status_label.pack(side=tk.LEFT)
    url_label = ttk.Label(top, text="", foreground="#6E9EF7", cursor="hand2")
    url_label.pack(side=tk.LEFT, padx=12)

    def open_frontend(_=None):
        os.startfile("http://localhost:3000")

    url_label.bind("<Button-1>", open_frontend)

    # ── 日志区 ──
    log_frame = ttk.Frame(root)
    log_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
    log_text = scrolledtext.ScrolledText(
        log_frame, wrap=tk.WORD, font=("Consolas", 10),
        bg="#1a1e29", fg="#E8EAED", insertbackground="white",
        selectbackground="#6E9EF7",
    )
    log_text.pack(fill=tk.BOTH, expand=True)
    log_text.tag_config("Django", foreground="#5DAF5A")
    log_text.tag_config("Vite", foreground="#409EFF")
    log_text.tag_config("系统", foreground="#E8B84E")
    log_text.tag_config("error", foreground="#F56C6C")

    # ── 底部按钮栏 ──
    bottom = ttk.Frame(root, padding=8)
    bottom.pack(fill=tk.X)
    start_btn = ttk.Button(bottom, text="▶ 启动系统", width=14)
    start_btn.pack(side=tk.LEFT, padx=4)
    stop_btn = ttk.Button(bottom, text="■ 结束系统", width=14, state=tk.DISABLED)
    stop_btn.pack(side=tk.LEFT, padx=4)

    tray_btn_text = "━ 隐藏到托盘" if TRAY_AVAILABLE else "━ 最小化"
    tray_btn = ttk.Button(bottom, text=tray_btn_text, width=14)
    tray_btn.pack(side=tk.RIGHT, padx=4)
    clear_btn = ttk.Button(bottom, text="清空日志", width=10)
    clear_btn.pack(side=tk.RIGHT, padx=4)

    # ── 日志函数 ──
    def log(source: str, msg: str):
        log_text.insert(tk.END, f"[{datetime.now():%H:%M:%S}] ", "系统")
        log_text.insert(tk.END, f"[{source}] ", source)
        log_text.insert(tk.END, f"{msg}\n")
        log_text.see(tk.END)
        if int(log_text.index("end-1c").split(".")[0]) > 2000:
            log_text.delete("1.0", "3.0")

    # ── 窗口显示/隐藏 ──
    def restore_window():
        root.deiconify()
        root.lift()
        root.focus_force()
        # Windows: 确保从任务栏恢复时窗口正常
        try:
            root.state("normal")
        except Exception:
            pass

    # ── 托盘 ──
    def _setup_tray():
        nonlocal tray_icon
        if not TRAY_AVAILABLE:
            return
        try:
            img = create_tray_image()

            def on_restore(icon, item):
                root.after(0, restore_window)

            def on_exit(icon, item):
                root.after(0, on_full_exit)

            menu = pystray.Menu(
                pystray.MenuItem("显示窗口", on_restore, default=True),
                pystray.MenuItem("结束系统", on_exit),
            )
            tray_icon = pystray.Icon("lianbao", img, "联宝智能 — 供应链智能运营系统", menu)
            threading.Thread(target=tray_icon.run, daemon=True).start()
        except Exception:
            pass

    def hide_to_tray():
        if tray_icon:
            root.withdraw()
            log("系统", "窗口已隐藏到系统托盘。双击托盘图标或再次运行启动脚本可恢复。")
        else:
            # 没有托盘支持，最小化到任务栏
            root.iconify()
            log("系统", "窗口已最小化到任务栏。")

    # ── 完整退出 ──
    def on_full_exit():
        if mgr:
            mgr.stop()
        if tray_icon:
            try:
                tray_icon.stop()
            except Exception:
                pass
        if ipc_server:
            ipc_server.stop()
        root.destroy()

    # ── 窗口关闭 → 隐藏到托盘 ──
    def on_window_close():
        hide_to_tray()

    # ── 启动/停止 ──
    def on_start():
        nonlocal mgr
        if not check_port(8000):
            messagebox.showwarning("端口冲突", "8000 端口已被占用，请先释放端口。")
            return
        if not check_port(3000):
            messagebox.showwarning("端口冲突", "3000 端口已被占用，请先释放端口。")
            return

        missing_py, missing_node = check_dependencies()
        if missing_py or missing_node:
            log("系统", "正在安装缺失的依赖...")
            install_dependencies(missing_py, missing_node, lambda m: log("系统", m))

        log("系统", "检查数据库迁移...")
        try:
            subprocess.run(
                [sys.executable, str(SYSTEM_DIR / "manage.py"), "migrate", "--run-syncdb"],
                cwd=str(SYSTEM_DIR), capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            log("系统", "数据库迁移完成。")
        except Exception as e:
            log("系统", f"数据库迁移异常: {e}")

        mgr = ProcessManager(Queue())
        mgr.start()
        status_label.config(text="● 运行中", foreground="#5DAF5A")
        url_label.config(text="http://localhost:3000")
        start_btn.config(state=tk.DISABLED)
        stop_btn.config(state=tk.NORMAL)
        root.after(100, process_output)

    def on_stop():
        nonlocal mgr
        if mgr:
            mgr.stop()
            mgr = None
        status_label.config(text="● 已停止", foreground="#909399")
        url_label.config(text="")
        start_btn.config(state=tk.NORMAL)
        stop_btn.config(state=tk.DISABLED)
        log("系统", "系统已停止。")

    def on_clear():
        log_text.delete("1.0", tk.END)

    def process_output():
        if mgr and mgr.queue:
            try:
                while True:
                    source, line = mgr.queue.get_nowait()
                    tag = "error" if any(k in line.lower() for k in ("error", "traceback", "exception")) else source
                    log(source, line)
            except Empty:
                pass
        root.after(100, process_output)

    def ipc_restore_callback():
        """IPC 收到恢复命令时的回调（在 tkinter 主线程执行）"""
        restore_window()
        log("系统", "检测到重复运行，已恢复窗口。")

    start_btn.config(command=on_start)
    stop_btn.config(command=on_full_exit)
    clear_btn.config(command=on_clear)
    tray_btn.config(command=hide_to_tray)
    root.protocol("WM_DELETE_WINDOW", on_window_close)

    # ── 初始化 ──
    _setup_tray()

    # 启动 IPC 服务（让后续运行能找到并激活本实例）
    ipc_server = IPCServer(ipc_restore_callback)
    ipc_server.start()

    log("系统", "欢迎使用联宝供应链智能运营系统。")
    log("系统", "点击「启动系统」开始运行。")
    log("系统", "关闭窗口 = 隐藏到后台（托盘/任务栏），不会退出系统。")
    log("系统", "再次运行启动脚本可恢复已隐藏的窗口。")
    log("系统", f"前端: http://localhost:3000  |  后端: http://localhost:8000")

    root.mainloop()

    # 窗口销毁后清理
    if ipc_server:
        ipc_server.stop()


# ── 命令行模式 ────────────────────────────────────────────────────────
def run_cli():
    print("=" * 50)
    print("  供应链智能运营系统 - CLI 模式")
    print("=" * 50)
    missing_py, missing_node = check_dependencies()
    if missing_py or missing_node:
        print("安装缺失依赖...")
        install_dependencies(missing_py, missing_node, print)
    print("启动 Django 后端 (port 8000)...")
    bp = subprocess.Popen(
        [sys.executable, str(SYSTEM_DIR / "manage.py"), "runserver", "0.0.0.0:8000"],
        cwd=str(SYSTEM_DIR),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    print("启动 Vue 前端 (port 3000)...")
    npm_cmd = "npm.cmd" if os.name == "nt" else "npm"
    fp = subprocess.Popen(
        [npm_cmd, "run", "dev"],
        cwd=str(FRONTEND_DIR),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    print("\n  前端: http://localhost:3000")
    print("  后端: http://localhost:8000")
    print("  按 Ctrl+C 结束所有进程\n")
    try:
        bp.wait()
        fp.wait()
    except KeyboardInterrupt:
        print("\n正在终止...")
        bp.terminate()
        fp.terminate()
        print("已结束。")


# ── 入口 ──────────────────────────────────────────────────────────────
def main():
    os.chdir(REPO_DIR)

    if "--cli" in sys.argv or "--no-gui" in sys.argv:
        run_cli()
    else:
        # 单实例检测：已有实例运行 → 激活它 → 自己退出
        if try_activate_existing():
            print("检测到已有系统窗口在运行，正在激活已有窗口...")
            sys.exit(0)

        try:
            run_gui()
        except Exception as e:
            print(f"GUI 启动失败: {e}")
            print("回退到命令行模式...")
            run_cli()


if __name__ == "__main__":
    main()
