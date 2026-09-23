"""PyInstaller 一键打包脚本（onedir 目录版）。

用法：
    python build_exe.py

产物：
    dist/汰换比价工具/汰换比价工具.exe  （可双击运行）
    dist/汰换比价工具/  （含 Qt 依赖、Python 运行时等，整目录可拷贝分发）

说明：
- vendor/ 子包通过 `from vendor import found_buff` 被 PyInstaller 静态发现并打包。
- 主 CSV（物品箱子磨损对照表1_有效磨损及goods_id.csv）通过 --add-data 打包到
  临时解压目录的 data/ 子目录，与 utils/config.py 中 MAIN_ITEMS_CSV 路径一致。
- 用户可变数据（purchases.db、items.csv、c5_callbacks.* 等）在运行时
  写入 exe 同级目录的 data/ 子目录，不污染临时目录。
- proxy_server/ 不打包（依赖外部 cloudflared.exe，由独立 PowerShell 维护）。
"""
import os
import shutil
import subprocess
import sys

APP_NAME = "汰换比价工具"
ROOT = os.path.dirname(os.path.abspath(__file__))
ENTRY = os.path.join(ROOT, "main.py")
VENDOR_DIR = os.path.join(ROOT, "vendor")
MAIN_CSV = os.path.join(ROOT, "data",
                       "物品箱子磨损对照表1_有效磨损及goods_id.csv")
DIST_DIR = os.path.join(ROOT, "dist")
BUILD_DIR = os.path.join(ROOT, "build")
SPEC_FILE = os.path.join(ROOT, f"{APP_NAME}.spec")


def ensure_pyinstaller():
    """确认 PyInstaller 已安装，否则自动 pip 安装。"""
    try:
        import PyInstaller  # noqa: F401
        return
    except ImportError:
        print("[*] 未检测到 PyInstaller，开始安装...")
        subprocess.check_call([sys.executable, "-m", "pip", "install",
                               "pyinstaller"])
        print("[*] PyInstaller 安装完成。")


def clean_old():
    """清理上次的构建产物，避免缓存干扰。"""
    for path in (DIST_DIR, BUILD_DIR, SPEC_FILE):
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass


def build():
    ensure_pyinstaller()
    clean_old()

    if not os.path.exists(MAIN_CSV):
        raise FileNotFoundError(
            f"未找到主 CSV：{MAIN_CSV}\n"
            f"请先将其从 buy/data/ 拷贝至 data/ 下。")

    # Windows 上 --add-data 用分号分隔 src;dest
    sep = ";" if os.name == "nt" else ":"
    add_data_args = [
        f"--add-data={MAIN_CSV}{sep}data",
    ]
    # 若 vendor 目录中存在 .py 之外的内容（例如 README），也一并打入
    if os.path.isdir(VENDOR_DIR):
        # vendor 内的 .py 已通过静态分析打包；这里仅追加非 .py 资源（如有）
        extra = [f for f in os.listdir(VENDOR_DIR)
                 if not f.endswith(".py") and not f.startswith("__")
                 and os.path.isfile(os.path.join(VENDOR_DIR, f))]
        if extra:
            # 单独逐个加入，避免目录前缀问题
            for fn in extra:
                add_data_args.append(
                    f"--add-data={os.path.join(VENDOR_DIR, fn)}{sep}vendor")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name", APP_NAME,
        "--windowed",                # GUI 应用，不弹控制台窗口
        "--noconsole",
        "--onedir",                  # 目录版（启动快、便于排错）
        # 隐藏导入保险（PyInstaller 静态分析通常已覆盖）
        "--hidden-import=vendor",
        "--hidden-import=vendor.found_buff",
        "--hidden-import=vendor.found_c5",
        "--hidden-import=vendor.buy_buff",
        "--hidden-import=vendor.buy_c5",
        "--hidden-import=vendor.eco_creds",
        "--hidden-import=cryptography",
        "--hidden-import=cryptography.hazmat.primitives.asymmetric.padding",
        "--hidden-import=cryptography.hazmat.primitives.hashes",
        "--hidden-import=cryptography.hazmat.primitives.serialization",
        "--hidden-import=cv2",
        "--hidden-import=pyautogui",
        "--hidden-import=pygetwindow",
        "--hidden-import=win32gui",
        "--hidden-import=win32con",
        "--hidden-import=PySide6",
        # 收集 PySide6 子模块（避免 Qt 插件缺失）
        "--collect-submodules=PySide6",
        # 收集 vendor 子包的二进制资源（若有）
        "--collect-data=vendor",
    ] + add_data_args + [ENTRY]

    print("[*] 执行 PyInstaller 打包命令：")
    print("    " + " ".join(cmd))
    print()
    subprocess.check_call(cmd)

    out_dir = os.path.join(DIST_DIR, APP_NAME)
    exe_path = os.path.join(out_dir, f"{APP_NAME}.exe")
    print()
    print("=" * 60)
    print("[OK] 打包完成！")
    print(f"    exe: {exe_path}")
    print(f"    目录: {out_dir}")
    print()
    print("使用说明：")
    print("  1) 双击 汰换比价工具.exe 启动；")
    print("  2) 首次启动会在 exe 同级创建 data/ 目录用于存放数据库与日志；")
    print("  3) 整个 dist/汰换比价工具/ 目录可拷贝至其他 Windows 机器运行；")
    print("  4) C5 回调服务（proxy_server/）仍需在 PowerShell 窗口独立运行。")
    print("=" * 60)


if __name__ == "__main__":
    build()
