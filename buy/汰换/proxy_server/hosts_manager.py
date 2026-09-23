"""Windows hosts 文件管理：为白名单域名写入本地回环解析。"""
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

WINDOWS_HOSTS = Path(r"C:\Windows\System32\drivers\etc\hosts")
BACKUP_DIR = Path(__file__).resolve().parent / "hosts_backups"


def _is_admin() -> bool:
    """检查当前进程是否具有管理员权限（Windows）。"""
    try:
        if os.name == "nt":
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        pass
    return False


def _backup_hosts() -> Path:
    """备份 hosts 文件到 proxy_server/hosts_backups/。"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"hosts.{stamp}.bak"
    shutil.copy2(WINDOWS_HOSTS, dst)
    return dst


def check_host_entry(hostname: str, ip: str = "127.0.0.1") -> bool:
    """检查 hosts 中是否已存在 hostname -> ip 的解析条目。"""
    if not WINDOWS_HOSTS.exists():
        return False
    try:
        with open(WINDOWS_HOSTS, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                line = raw.split("#", 1)[0].strip()
                if not line:
                    continue
                # 简单分词，忽略多空白
                tokens = line.split()
                if len(tokens) < 2:
                    continue
                if tokens[0] == ip and hostname in tokens[1:]:
                    return True
    except PermissionError:
        raise PermissionError(
            "无法读取 hosts 文件，请以管理员身份运行。")
    return False


def list_host_entries():
    """返回 hosts 中所有非注释条目 [(ip, [hosts])]。"""
    if not WINDOWS_HOSTS.exists():
        return []
    entries = []
    try:
        with open(WINDOWS_HOSTS, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                line = raw.split("#", 1)[0].strip()
                if not line:
                    continue
                tokens = line.split()
                if len(tokens) >= 2:
                    entries.append((tokens[0], tokens[1:]))
    except PermissionError:
        raise PermissionError(
            "无法读取 hosts 文件，请以管理员身份运行。")
    return entries


def add_host_entry(hostname: str, ip: str = "127.0.0.1") -> str:
    """追加 hostname -> ip 到 Windows hosts（去重）。返回状态消息。"""
    if os.name != "nt":
        raise OSError("hosts 管理仅支持 Windows 系统。")
    if not _is_admin():
        raise PermissionError(
            "写入 hosts 需要管理员权限，请右键终端选择「以管理员身份运行」。")
    if check_host_entry(hostname, ip):
        return f"已存在：{hostname} -> {ip}"

    backup = _backup_hosts()
    # 先移除同名的旧条目（避免冲突），再追加
    kept_lines = []
    with open(WINDOWS_HOSTS, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            body = raw.split("#", 1)[0].strip()
            tokens = body.split()
            if len(tokens) >= 2 and hostname in tokens[1:]:
                # 跳过（移除）所有包含该 hostname 的行
                continue
            kept_lines.append(raw.rstrip("\n"))

    marker_start = f"# >>> BEGIN 汰换 proxy entry for {hostname} (DO NOT EDIT) >>>"
    marker_end = f"# <<< END 汰换 proxy entry for {hostname} <<<"
    entry_line = f"{ip} {hostname}"

    # 移除可能残留的旧 marker 区块
    cleaned = []
    skip_block = False
    for line in kept_lines:
        if line.strip() == marker_start:
            skip_block = True
            continue
        if line.strip() == marker_end:
            skip_block = False
            continue
        if skip_block:
            continue
        cleaned.append(line)

    cleaned.append("")
    cleaned.append(marker_start)
    cleaned.append(entry_line)
    cleaned.append(marker_end)
    cleaned.append("")

    with open(WINDOWS_HOSTS, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(cleaned))

    # 刷新 DNS 缓存
    try:
        import subprocess
        subprocess.run(["ipconfig", "/flushdns"],
                       capture_output=True, timeout=10)
    except Exception:
        pass

    return (f"已写入：{hostname} -> {ip}\n"
            f"备份文件：{backup}")


def remove_host_entry(hostname: str) -> str:
    """移除包含该 hostname 的所有条目以及汰换 marker 区块。"""
    if os.name != "nt":
        raise OSError("hosts 管理仅支持 Windows 系统。")
    if not _is_admin():
        raise PermissionError(
            "写入 hosts 需要管理员权限，请右键终端选择「以管理员身份运行」。")

    found = False
    backup = _backup_hosts()
    marker_start = f"# >>> BEGIN 汰换 proxy entry for {hostname} (DO NOT EDIT) >>>"
    marker_end = f"# <<< END 汰换 proxy entry for {hostname} <<<"

    new_lines = []
    skip_block = False
    with open(WINDOWS_HOSTS, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            stripped = raw.rstrip("\n")
            body = stripped.split("#", 1)[0].strip()
            tokens = body.split()

            if stripped.strip() == marker_start:
                skip_block = True
                found = True
                continue
            if stripped.strip() == marker_end:
                skip_block = False
                continue
            if skip_block:
                continue

            if len(tokens) >= 2 and hostname in tokens[1:]:
                found = True
                continue
            new_lines.append(stripped)

    if not found:
        return f"未找到 {hostname} 的解析条目，无需移除。"

    with open(WINDOWS_HOSTS, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(new_lines))

    try:
        import subprocess
        subprocess.run(["ipconfig", "/flushdns"],
                       capture_output=True, timeout=10)
    except Exception:
        pass

    return f"已移除 {hostname} 的解析条目。\n备份文件：{backup}"


if __name__ == "__main__":
    try:
        if len(sys.argv) < 2:
            print("用法:")
            print("  python hosts_manager.py check <hostname> [ip]")
            print("  python hosts_manager.py add <hostname> [ip]")
            print("  python hosts_manager.py remove <hostname>")
            print("  python hosts_manager.py list")
            sys.exit(1)
        cmd = sys.argv[1].lower()
        if cmd == "list":
            for ip, hosts in list_host_entries():
                print(f"{ip:20s}  {', '.join(hosts)}")
        elif cmd == "check":
            h = sys.argv[2]
            ip = sys.argv[3] if len(sys.argv) > 3 else "127.0.0.1"
            print("已配置" if check_host_entry(h, ip) else "未配置")
        elif cmd == "add":
            h = sys.argv[2]
            ip = sys.argv[3] if len(sys.argv) > 3 else "127.0.0.1"
            print(add_host_entry(h, ip))
        elif cmd == "remove":
            h = sys.argv[2]
            print(remove_host_entry(h))
        else:
            print(f"未知命令: {cmd}")
    except PermissionError as e:
        print(f"[权限错误] {e}")
        sys.exit(2)
    except Exception as e:
        print(f"[错误] {e}")
        sys.exit(3)
