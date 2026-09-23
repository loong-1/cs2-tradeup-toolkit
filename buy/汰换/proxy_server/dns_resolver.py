"""简易本地 DNS 解析器（替代方案）。

功能：
- 对请求的域名为白名单域名（866868686.xyz，含任意子域名）时，
  直接返回 127.0.0.1（或 --ip 指定的地址）。
- 其他域名通过 TCP+UDP 向上游 DNS（默认 114.114.114.114:53）转发。

依赖：
    pip install dnslib
若未安装 dnslib，脚本会给出提示并退出。使用方法：

1) 以管理员身份运行：
   python dns_resolver.py --ip 127.0.0.1 --upstream 114.114.114.114

2) 在 Windows 网络适配器中，把首选 DNS 服务器改为 127.0.0.1
   （或在 hosts_manager 写 hosts 即可，无需启用本服务）。

本服务是 hosts 文件方案的"高级替代"，适合不想动 hosts 的场景。
"""
import argparse
import socket
import sys
import threading
from datetime import datetime

try:
    from dnslib import DNSRecord, DNSHeader, RR, QTYPE, A, RCODE
except ImportError:  # pragma: no cover
    print("[错误] 缺少 dnslib 依赖。请运行:  pip install dnslib")
    print("       或者直接使用 hosts_manager.py 写 Windows hosts 即可，无需 DNS 服务。")
    sys.exit(4)


class LocalDNSResolver:
    """UDP DNS 服务 + 上游 TCP/UDP 转发。"""

    def __init__(self, whitelist_domain, resolved_ip="127.0.0.1",
                 upstream_dns=("114.114.114.114", 53),
                 listen_host="0.0.0.0", listen_port=53):
        self.whitelist = whitelist_domain.lower()
        self.resolved_ip = resolved_ip
        self.upstream = upstream_dns
        self.listen_host = listen_host
        self.listen_port = listen_port
        self._sock = None

    def _matches(self, qname: str) -> bool:
        q = qname.lower().rstrip(".")
        return q == self.whitelist or q.endswith("." + self.whitelist)

    def _build_reply(self, request_data: bytes) -> bytes:
        req = DNSRecord.parse(request_data)
        reply = DNSRecord(DNSHeader(id=req.header.id, qr=1, aa=1, ra=1),
                          q=req.q)
        qname = str(req.q.qname)
        if self._matches(qname) and req.q.qtype in (QTYPE.A, QTYPE.ANY):
            reply.add_answer(RR(rname=req.q.qname, rtype=QTYPE.A,
                                rclass=1, ttl=300,
                                rdata=A(self.resolved_ip)))
            return reply.pack()
        # 其他查询转发到上游
        try:
            upstream_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            upstream_sock.settimeout(3.0)
            upstream_sock.sendto(request_data, self.upstream)
            data, _ = upstream_sock.recvfrom(4096)
            upstream_sock.close()
            return data
        except Exception as e:
            # 失败则返回 SERVFAIL
            err = DNSRecord(
                DNSHeader(id=req.header.id, qr=1, rcode=RCODE.SERVFAIL),
                q=req.q)
            return err.pack()

    def _handle_one(self, data, client_addr):
        try:
            resp = self._build_reply(data)
            self._sock.sendto(resp, client_addr)
        except Exception as e:
            now = datetime.now().strftime("%H:%M:%S")
            print(f"[dns {now}] 处理 {client_addr} 出错: {e}", flush=True)

    def serve_forever(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._sock.bind((self.listen_host, self.listen_port))
        except PermissionError:
            raise PermissionError(
                f"绑定 53 端口需要管理员权限，或使用 --port 指定高位端口。")
        except OSError as e:
            raise OSError(f"DNS 端口绑定失败: {e}")

        print(f"[dns server] ✅ 监听 udp://{self.listen_host}:{self.listen_port}")
        print(f"[dns server] 白名单: *.{self.whitelist} -> {self.resolved_ip}")
        print(f"[dns server] 上游:   {self.upstream[0]}:{self.upstream[1]}")
        while True:
            try:
                data, addr = self._sock.recvfrom(2048)
                threading.Thread(
                    target=self._handle_one,
                    args=(data, addr), daemon=True).start()
            except KeyboardInterrupt:
                print("\n[dns server] 已停止。")
                self._sock.close()
                return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", default="866868686.xyz",
                    help="白名单域名（默认 866868686.xyz）")
    ap.add_argument("--ip", default="127.0.0.1",
                    help="白名单域名解析到的 IP（默认 127.0.0.1）")
    ap.add_argument("--upstream", default="114.114.114.114",
                    help="上游 DNS 服务器（默认 114.114.114.114）")
    ap.add_argument("--upstream-port", type=int, default=53)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=53)
    args = ap.parse_args()

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


if __name__ == "__main__":
    main()
