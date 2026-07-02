"""
联宝智能 — 供应链智能运营系统

双击此文件即可启动（无命令行黑窗口，仅 GUI）。
再次双击可恢复已隐藏的窗口。

打包为单个 exe:
  pip install pyinstaller
  pyinstaller --onefile --windowed --name "联宝智能启动器" --add-data "system;system" --add-data "frontend;frontend" start_system.py
"""
import os, sys

# 确保工作目录是脚本所在目录
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SCRIPT_DIR)

# PyInstaller 打包后路径修正
if getattr(sys, 'frozen', False):
    SCRIPT_DIR = sys._MEIPASS
    os.chdir(SCRIPT_DIR)

sys.path.insert(0, SCRIPT_DIR)

# pythonw 没有控制台 → 重定向 stdio 避免崩溃
if sys.executable.endswith("pythonw.exe") or not sys.stdout:
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")

from start_system import main
main()
