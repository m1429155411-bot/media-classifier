# -*- coding: utf-8 -*-
"""
媒体分类工具 —— 独立 exe 入口（图片 / 视频双板块）

双击即可运行：程序在进程内部启动一个仅监听 127.0.0.1 的临时服务（对用户完全透明），
再打开独立应用窗口。不需要启动任何服务、不需要手动打开浏览器、不显示命令行窗口。
窗口关闭或点击界面「退出程序」后，进程连同内部服务一起结束。

窗口优先级：
  1. WebView2 独立窗口（pywebview，无地址栏，标题为「图片分类工具」）
  2. QQBrowser / Edge 的 --app 独立应用窗口（独立配置目录，可监控关闭）
  3. 系统默认浏览器（最后兜底）
"""
import os
import sys
import time
import threading
import subprocess
import tempfile
import traceback

APP_NAME = "媒体分类工具"
LOG_DIR = os.path.join(tempfile.gettempdir(), "ImageClassifier")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "app.log")


def log(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass


def res_dir():
    """PyInstaller onefile 解包目录 / 源码目录。"""
    return getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))


def initial_theme():
    """读取界面主题，用于窗口创建前的背景色（避免浅色主题启动时闪黑）。"""
    try:
        import json
        p = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                         "ImageClassifier", "theme.json")
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        t = d.get("theme") if isinstance(d, dict) else d
        if t == "light":
            return "#f4f7fc"
    except Exception:
        pass
    return "#0b0e13"


def find_edge():
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if os.path.isfile(p):
            return p
    return ""


def start_inner_server():
    import server
    server.BASE = res_dir()          # index.html 随包释放在该目录
    srv = None
    for port in (8766, 0):
        try:
            srv = server.LocalServer((server.HOST, port), server.Handler)
            break
        except OSError:
            continue
    if srv is None:
        raise RuntimeError("无法启动内部服务")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://{server.HOST}:{srv.server_address[1]}/"
    return server, srv, url


def run_webview(server, url):
    """优先：WebView2 独立窗口。返回是否成功接管主流程。"""
    try:
        import webview
    except Exception as e:
        log("pywebview 不可用：" + repr(e))
        return False
    try:
        win = webview.create_window(
            APP_NAME, url, width=1460, height=940, min_size=(1080, 700),
            background_color=initial_theme(),
        )

        def watch_quit():
            server.EXIT_EVENT.wait()
            try:
                win.destroy()
            except Exception as e:
                log("destroy: " + repr(e))

        threading.Thread(target=watch_quit, daemon=True).start()
        log("窗口模式：WebView2")
        webview.start()
        return True
    except Exception as e:
        log("WebView2 启动失败：" + repr(e))
        log(traceback.format_exc())
        return False


def run_browser_app(server, url):
    """回退：Chromium --app 独立应用窗口（无地址栏），进程退出即结束服务。"""
    exe = server.find_qqbrowser() or find_edge()
    if not exe:
        log("未找到 QQBrowser / Edge")
        return False
    try:
        profile = os.path.join(LOG_DIR, "app-profile")
        args = [exe, f"--app={url}", f"--user-data-dir={profile}",
                "--window-size=1460,940", "--window-position=60,40",
                "--no-first-run", "--no-default-browser-check",
                "--disable-extensions", "--disable-background-networking"]
        proc = subprocess.Popen(args)
        log(f"窗口模式：{os.path.basename(exe)} --app (pid {proc.pid})")
        while proc.poll() is None:
            if server.EXIT_EVENT.wait(0.5):
                break
        if proc.poll() is None and server.EXIT_EVENT.is_set():
            try:
                proc.terminate()
            except Exception:
                pass
        return True
    except Exception as e:
        log("浏览器应用窗口失败：" + repr(e))
        log(traceback.format_exc())
        return False


def main():
    try:
        server, srv, url = start_inner_server()
        log("启动：" + url + (" [frozen]" if getattr(sys, "frozen", False) else " [source]"))
        ok = run_webview(server, url)
        if not ok:
            ok = run_browser_app(server, url)
        if not ok:
            import webbrowser
            webbrowser.open(url)
            log("窗口模式：系统默认浏览器")
            server.EXIT_EVENT.wait()
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
        log("已退出")
    except Exception as e:
        log("致命错误：" + repr(e))
        log(traceback.format_exc())


if __name__ == "__main__":
    main()
