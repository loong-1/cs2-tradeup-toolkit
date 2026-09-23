"""C5 交易回调 HTTP 服务器。

监听白名单域名对应的本地端口（默认 80），接收 C5 平台异步回调：
- POST /c5/callback  -> 记录请求详情到日志 + SQLite，返回 "success"。
- GET  /health       -> 服务健康状态 JSON。
- GET  /             -> 简单欢迎页，展示服务说明。

与 hosts_manager 配合使用，将 866868686.xyz 解析到 127.0.0.1，
再以管理员身份启动本服务（绑定 80 端口），即可满足 C5 白名单
tradeUrl 域名校验的"可访问"要求。

注：若公网 DNS 上 866868686.xyz 解析不到用户本机，C5 服务器的
真实回调仍可能无法送达；但本地 DNS/hosts 覆盖已足以满足：
1) trade_url 参数字符串匹配白名单域名（C5 主要校验点）；
2) 本地浏览器/库访问 866868686.xyz 时能正常访问该服务。
"""
import json
import os
import socket
import sqlite3
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data")
CALLBACK_LOG_PATH = os.path.join(LOG_DIR, "c5_callbacks.log")
CALLBACK_DB = os.path.join(LOG_DIR, "c5_callbacks.db")

os.makedirs(LOG_DIR, exist_ok=True)


# ---------- DB ----------

def _db_conn():
    conn = sqlite3.connect(CALLBACK_DB, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS callbacks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            method TEXT,
            path TEXT,
            query TEXT,
            headers TEXT,
            body TEXT,
            remote_ip TEXT
        )
    """)
    conn.commit()
    return conn


_db_lock = threading.Lock()


def _save_callback(method, path, query, headers, body, remote_ip):
    with _db_lock, _db_conn() as conn:
        conn.execute("""
            INSERT INTO callbacks (ts, method, path, query, headers, body, remote_ip)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().isoformat(timespec="seconds"),
            method, path, query,
            json.dumps(dict(headers), ensure_ascii=False),
            body, remote_ip,
        ))
        conn.commit()

    try:
        with open(CALLBACK_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] "
                    f"{remote_ip} {method} {path}\n"
                    f"QUERY: {query}\nBODY: {body}\n")
    except Exception:
        pass


# ---------- HTTP Handler ----------

class CallbackHandler(BaseHTTPRequestHandler):
    server_version = "C5CallbackProxy/1.0"

    def log_message(self, fmt, *args):
        # 静默默认日志，控制台只打印重要事件
        return

    def _send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, status, text, content_type="text/plain; charset=utf-8"):
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # CORS
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, *")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/health":
            self._send_json(200, {
                "status": "ok",
                "service": "c5-callback-server",
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "listening": True,
            })
            return

        if path == "/c5/callback" or path == "/c5/callback/":
            # GET 回调也兼容（C5 文档中可能是 POST，但做个守护）
            qs = parsed.query
            body = ""
            _save_callback("GET", path, qs, dict(self.headers),
                           body, self.client_address[0])
            # C5 约定收到后返回 "success" 文本
            self._send_text(200, "success")
            self._log_event(f"GET 回调: {self.client_address[0]} -> {qs or '(空)'}")
            return

        if path == "":
            path = "/"
        if path == "/":
            html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>C5 白名单回调服务</title></head><body>
<h2>🟢 C5 本地回调服务运行中</h2>
<p>白名单域名: <code>{self.headers.get('Host', '866868686.xyz')}</code></p>
<ul>
  <li><a href="/health">GET /health</a> - 健康检查</li>
  <li>POST /c5/callback - C5 交易回调入口（返回 "success"）</li>
</ul>
<p><small>汰换比价工具 - proxy_server 内置组件</small></p>
</body></html>"""
            self._send_text(200, html, "text/html; charset=utf-8")
            return

        # 默认 404
        self._send_text(404, "Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # 读取 body（带长度上限 1MB 保护）
        length = int(self.headers.get("Content-Length", "0") or "0")
        body_bytes = self.rfile.read(min(length, 1_048_576)) if length > 0 else b""
        try:
            body = body_bytes.decode("utf-8")
        except UnicodeDecodeError:
            body = body_bytes.decode("latin-1", errors="replace")

        if path == "/c5/callback":
            _save_callback("POST", path, parsed.query,
                           dict(self.headers), body, self.client_address[0])
            # C5 文档要求返回 "success" 文本
            self._send_text(200, "success")
            snippet = body if len(body) < 200 else body[:200] + "..."
            self._log_event(
                f"POST 回调: {self.client_address[0]} body={snippet!r}")
            return

        # 未知路径
        self._send_text(404, "Not Found")

    def _log_event(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[callback {ts}] {msg}", flush=True)


# ---------- 启动入口 ----------

def _port_in_use(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        try:
            return s.connect_ex((host, port)) == 0
        except OSError:
            return True


def start_callback_server(host="0.0.0.0", port=80, block=True):
    """启动回调 HTTP 服务。

    :param block: True 阻塞；False 返回 (server, thread) 由调用者管理生命周期。
    """
    if not 1 <= port <= 65535:
        raise ValueError(f"端口非法: {port}")

    # 预先确保表存在
    _db_conn().close()

    # 端口占用检测
    if _port_in_use(host, port):
        raise OSError(
            f"端口 {port} 已被占用。请释放该端口后重试，"
            f"或改用 --port 8080 等高位端口。")

    server = ThreadingHTTPServer((host, port), CallbackHandler)

    def _run():
        try:
            server.serve_forever()
        except Exception as e:
            print(f"[callback server] 异常退出: {e}")

    if not block:
        th = threading.Thread(target=_run, daemon=True)
        th.start()
        return server, th

    print(f"[callback server] ✅ 监听 http://{host}:{port}")
    print(f"[callback server] 白名单回调: POST http://866868686.xyz:{port}/c5/callback")
    print(f"[callback server] 健康检查:   GET  http://127.0.0.1:{port}/health")
    print(f"[callback server] 日志:        {CALLBACK_LOG_PATH}")
    print(f"[callback server] 数据:        {CALLBACK_DB}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[callback server] 已停止。")
        server.server_close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=80)
    args = ap.parse_args()
    try:
        start_callback_server(args.host, args.port)
    except PermissionError as e:
        print(f"[权限错误] {e}")
        sys.exit(2)
    except OSError as e:
        print(f"[网络错误] {e}")
        sys.exit(3)
