r"""本地域名解析 + 回调服务 综合 CLI 入口。

子命令:
  hosts      管理 Windows hosts 文件条目
    add/remove/check/list + --ip + --hostname

  callback   启动 C5 回调 HTTP 服务
    --host 0.0.0.0 --port 80

  dns        启动本地 DNS 解析（需要 dnslib）
    --ip 127.0.0.1 --upstream 114.114.114.114 --host 0.0.0.0 --port 53

  status     一键检查：hosts 配置 + 端口占用 + 回调服务健康

  setup      快速启动（= hosts add + callback run）—— 一键部署
  teardown   快速回收（= callback stop + hosts remove）

使用：
  cd F:\steamdt-project\buy\汰换
  python proxy_server\server_cli.py hosts add --hostname 866868686.xyz
  python proxy_server\server_cli.py callback run --port 80
  python proxy_server\server_cli.py setup --hostname 866868686.xyz --port 80
"""
import argparse
import os
import socket
import sys
import urllib.request
import urllib.error

# 支持两种启动方式：
# 1) cd 汰换 && python proxy_server\server_cli.py
# 2) cd 汰换\proxy_server && python server_cli.py
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from utils.config import (
    C5_WHITELIST_DOMAIN,
    C5_CALLBACK_LISTEN_HOST,
    C5_CALLBACK_DEFAULT_PORT,
)


def _check_http(url, timeout=2):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()[:200]
    except Exception as e:
        return None, str(e)


# ---------- hosts 子命令 ----------

def cmd_hosts(args):
    from proxy_server.hosts_manager import (
        add_host_entry, remove_host_entry, check_host_entry, list_host_entries)
    action = args.action
    host = args.hostname
    ip = args.ip
    try:
        if action == "add":
            print(add_host_entry(host, ip))
        elif action == "remove":
            print(remove_host_entry(host))
        elif action == "check":
            ok = check_host_entry(host, ip)
            print(f"hosts: {'已配置' if ok else '未配置'}  "
                  f"{host} -> {ip}")
            sys.exit(0 if ok else 5)
        elif action == "list":
            for ip, names in list_host_entries():
                print(f"{ip:20s}  {', '.join(names)}")
    except PermissionError as e:
        print(f"[权限错误] {e}")
        sys.exit(2)
    except Exception as e:
        print(f"[错误] {e}")
        sys.exit(3)


# ---------- callback 子命令 ----------

def cmd_callback(args):
    from proxy_server.c5_callback_server import start_callback_server
    action = args.action
    if action != "run":
        print(f"未知 action: {action}")
        sys.exit(1)
    try:
        start_callback_server(args.host, args.port)
    except PermissionError as e:
        print(f"[权限错误] {e}")
        sys.exit(2)
    except OSError as e:
        print(f"[网络错误] {e}")
        sys.exit(3)


# ---------- dns 子命令 ----------

def cmd_dns(args):
    try:
        from dnslib import DNSRecord  # noqa: F401
    except ImportError:
        print("[错误] 缺少 dnslib。请运行:  pip install dnslib")
        print("       或直接使用 hosts 方案，无需 DNS 服务。")
        sys.exit(4)
    from proxy_server.dns_resolver import LocalDNSResolver
    server = LocalDNSResolver(
        whitelist_domain=args.domain,
        resolved_ip=args.ip,
        upstream_dns=(args.upstream, args.upstream_port),
        listen_host=args.host,
        listen_port=args.port,
    )
    try:
        server.serve_forever()
    except PermissionError as e:
        print(f"[权限错误] {e}")
        sys.exit(2)
    except OSError as e:
        print(f"[网络错误] {e}")
        sys.exit(3)


# ---------- status ----------

def cmd_status(args):
    host = args.hostname
    ip = "127.0.0.1"
    port = args.port
    errs = 0
    print("=" * 60)
    print("🛰️  本地解析服务健康体检")
    print("=" * 60)

    # 1. hosts 检查
    from proxy_server.hosts_manager import check_host_entry
    try:
        ok = check_host_entry(host, ip)
    except PermissionError as e:
        print(f"[hosts   ] ❌ 无法读取: {e}")
        errs += 1
    else:
        print(f"[hosts   ] {'✅' if ok else '❌'}  {host} -> {ip}  "
              f"{'已配置' if ok else '未配置'}")
        if not ok:
            errs += 1

    # 2. DNS 实时解析（本机查询）
    try:
        resolved = socket.gethostbyname(host)
        ok = resolved == ip
        print(f"[DNS     ] {'✅' if ok else '⚠️'}  本机解析结果: "
              f"{host} -> {resolved}  "
              f"（{'OK' if ok else '≠ 127.0.0.1，请配置DNS或hosts'}）")
        if not ok:
            errs += 1
    except Exception as e:
        print(f"[DNS     ] ❌ 解析失败: {e}")
        errs += 1

    # 3. 端口占用
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    occupied = s.connect_ex(("127.0.0.1", port)) == 0
    s.close()
    print(f"[PORT    ] {'📡' if occupied else '🔜'}  127.0.0.1:{port}  "
          f"{'占用中（服务运行？）' if occupied else '空闲'}")

    # 4. HTTP 健康
    if occupied:
        code, body = _check_http(f"http://127.0.0.1:{port}/health")
        if code == 200:
            print(f"[HTTP    ] ✅ /health 返回 200")
        else:
            print(f"[HTTP    ] ⚠️  /health 异常: {body}")
            errs += 1
        # 带 Host 头访问白名单域名
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/health",
            headers={"Host": host})
        try:
            with urllib.request.urlopen(req, timeout=2) as r:
                if r.status == 200:
                    print(f"[VHOST   ] ✅ Host={host} 访问成功")
                else:
                    print(f"[VHOST   ] ⚠️  Host={host} 状态 {r.status}")
                    errs += 1
        except Exception as e:
            print(f"[VHOST   ] ⚠️  Host={host} 访问失败: {e}")
            errs += 1

    # 5. C5 回调地址配置
    from utils.config import C5_CALLBACK_URL
    print(f"[C5 CONF ] callback_url = {C5_CALLBACK_URL}")
    if host not in C5_CALLBACK_URL:
        print("         ⚠️  域名不匹配，请检查 utils/config.py 的 C5_WHITELIST_DOMAIN")

    print("=" * 60)
    if errs == 0:
        print("✅ 全部通过。")
    else:
        print(f"⚠️  发现 {errs} 个问题，参考上方提示。")
    sys.exit(0 if errs == 0 else 6)


# ---------- setup / teardown ----------

def cmd_setup(args):
    """部署：hosts add + 启动 callback 服务（阻塞）。"""
    from proxy_server.hosts_manager import add_host_entry
    try:
        print(add_host_entry(args.hostname, "127.0.0.1"))
    except PermissionError as e:
        print(f"[权限错误] {e}")
        sys.exit(2)
    print()

    from proxy_server.c5_callback_server import start_callback_server
    try:
        start_callback_server(args.host, args.port)
    except (PermissionError, OSError) as e:
        print(f"[错误] {e}")
        sys.exit(3)


def cmd_teardown(args):
    """回收：移除 hosts 条目。callback 服务直接按 Ctrl+C 即可停止。"""
    from proxy_server.hosts_manager import remove_host_entry
    try:
        print(remove_host_entry(args.hostname))
    except PermissionError as e:
        print(f"[权限错误] {e}")
        sys.exit(2)


# ---------- CLI ----------

def build_parser():
    p = argparse.ArgumentParser(
        prog="server_cli",
        description="汰换工具 · C5 白名单域名本地解析 + 回调服务")
    sub = p.add_subparsers(dest="cmd", required=True)

    # hosts
    ph = sub.add_parser("hosts", help="管理 Windows hosts 文件")
    ph.add_argument("action", choices=["add", "remove", "check", "list"])
    ph.add_argument("--hostname", default=C5_WHITELIST_DOMAIN)
    ph.add_argument("--ip", default="127.0.0.1")
    ph.set_defaults(func=cmd_hosts)

    # callback
    pc = sub.add_parser("callback", help="运行 C5 回调 HTTP 服务")
    pc.add_argument("action", nargs="?", default="run", choices=["run"])
    pc.add_argument("--host", default=C5_CALLBACK_LISTEN_HOST)
    pc.add_argument("--port", type=int, default=C5_CALLBACK_DEFAULT_PORT)
    pc.set_defaults(func=cmd_callback)

    # dns
    pd = sub.add_parser("dns", help="运行本地 DNS 解析（可选）")
    pd.add_argument("--domain", default=C5_WHITELIST_DOMAIN)
    pd.add_argument("--ip", default="127.0.0.1")
    pd.add_argument("--upstream", default="114.114.114.114")
    pd.add_argument("--upstream-port", type=int, default=53)
    pd.add_argument("--host", default="0.0.0.0")
    pd.add_argument("--port", type=int, default=53)
    pd.set_defaults(func=cmd_dns)

    # status
    ps = sub.add_parser("status", help="一键健康检查")
    ps.add_argument("--hostname", default=C5_WHITELIST_DOMAIN)
    ps.add_argument("--port", type=int, default=C5_CALLBACK_DEFAULT_PORT)
    ps.set_defaults(func=cmd_status)

    # setup
    p0 = sub.add_parser("setup", help="一键部署 = hosts add + callback run")
    p0.add_argument("--hostname", default=C5_WHITELIST_DOMAIN)
    p0.add_argument("--host", default=C5_CALLBACK_LISTEN_HOST)
    p0.add_argument("--port", type=int, default=C5_CALLBACK_DEFAULT_PORT)
    p0.set_defaults(func=cmd_setup)

    # teardown
    p1 = sub.add_parser("teardown", help="一键回收 = hosts remove")
    p1.add_argument("--hostname", default=C5_WHITELIST_DOMAIN)
    p1.set_defaults(func=cmd_teardown)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
