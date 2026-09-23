"""购买列表面板：从查询结果加入的商品，待用户确认后批量购买。"""
from datetime import datetime

from PySide6.QtCore import Qt, Signal, QThread, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
    QPlainTextEdit, QGroupBox, QFormLayout, QLineEdit,
)

from core import buy_queue_manager as bqm
from core import user_settings as _us
from core.eco_web_api import (
    _parse_cookie_str, _ECO_WEB_REQUIRED_COOKIE_KEYS,
)
from gui.workers import BuyWorker


QUEUE_COLS = ["选择", "物品名称", "平台", "订单ID", "价格", "磨损值",
              "磨损等级", "paintseed", "assetid", "状态", "加入时间"]

# 状态显示文本
STATUS_TEXT = {
    bqm.STATUS_PENDING: "⏳ 待购买",
    bqm.STATUS_BOUGHT:  "✅ 已购买",
    bqm.STATUS_FAILED:  "❌ 失败",
    bqm.STATUS_REMOVED: "🗑 已移除",
}

# 可勾选的状态（待购买 + 失败）：待购买可购买，失败可删除/重试
CHECKABLE_STATUSES = {bqm.STATUS_PENDING, bqm.STATUS_FAILED}


class BuyQueuePanel(QWidget):
    """购买列表标签页。"""

    # 请求刷新历史记录面板（购买完成后通知）
    request_refresh_history = Signal()

    def __init__(self):
        super().__init__()
        self._worker = None
        self._current_items = []  # 当前表格数据（供购买时读取 goods_id）
        self._build_ui()
        self._reload()

    # ---------- UI ----------

    def _build_ui(self):
        root = QVBoxLayout(self)

        # --- 操作按钮 ---
        btn_row = QHBoxLayout()
        self.btn_select_all = QPushButton("☑ 全选")
        self.btn_select_all.clicked.connect(self._on_select_all)
        btn_row.addWidget(self.btn_select_all)

        self.btn_select_none = QPushButton("☐ 取消全选")
        self.btn_select_none.clicked.connect(self._on_select_none)
        btn_row.addWidget(self.btn_select_none)

        self.btn_remove = QPushButton("➖ 移除选中")
        self.btn_remove.clicked.connect(self._on_remove)
        btn_row.addWidget(self.btn_remove)

        self.btn_retry = QPushButton("🔄 重试失败项")
        self.btn_retry.clicked.connect(self._on_retry_failed)
        btn_row.addWidget(self.btn_retry)

        self.btn_clear_bought = QPushButton("🗑 清除已购买")
        self.btn_clear_bought.clicked.connect(self._on_clear_bought)
        btn_row.addWidget(self.btn_clear_bought)

        self.btn_clear_all = QPushButton("清空列表")
        self.btn_clear_all.clicked.connect(self._on_clear_all)
        btn_row.addWidget(self.btn_clear_all)

        self.btn_c5_auth = QPushButton("🔐 C5凭证管理")
        self.btn_c5_auth.clicked.connect(self._on_c5_auth_dialog)
        btn_row.addWidget(self.btn_c5_auth)

        btn_row.addStretch()
        self.lbl_count = QLabel("0 项")
        btn_row.addWidget(self.lbl_count)
        root.addLayout(btn_row)

        # --- 购买确认按钮 ---
        buy_row = QHBoxLayout()
        self.lbl_total = QLabel("合计：¥0.00 (0 件)")
        buy_row.addWidget(self.lbl_total)
        buy_row.addStretch()
        self.btn_buy = QPushButton("💳 确认购买选中商品")
        self.btn_buy.clicked.connect(self._on_buy)
        self.btn_buy.setStyleSheet(
            "QPushButton { background-color: #ff7043; color: white; "
            "padding: 8px 20px; font-weight: bold; }")
        buy_row.addWidget(self.btn_buy)
        root.addLayout(buy_row)

        # --- 🔐 ECO Cookie 填写（直接购买面板）—— 覆盖 config.py，不用手改源码 ---
        eco_box = QGroupBox("🔐 ECO 官网登录 Cookie（www.ecosteam.cn）")
        eco_box.setStyleSheet(
            "QGroupBox { font-weight: bold; margin-top: 6px; padding-top: 4px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }"
        )
        eco_lay = QFormLayout(eco_box)
        eco_lay.setSpacing(4)
        eco_lay.setContentsMargins(8, 10, 8, 6)

        tip = QLabel(
            "① <a href=\"https://www.ecosteam.cn/forge/recipe/create\""
            " style=\"color:#2196F3;text-decoration:underline;\">"
            "点此打开 ECO 汰换创建页（登录账号）</a>　→　"
            "② F12 → Network → 刷新 → 第一个 www.ecosteam.cn 请求 → "
            "③ 从 Request Headers 的 Cookie 里分别复制 clientId / refreshToken / "
            "loginToken 三段 →　④ 粘贴到下面三个输入框，点『校验并保存』。")
        tip.setWordWrap(True)
        tip.setOpenExternalLinks(False)
        tip.linkActivated.connect(
            lambda url: QDesktopServices.openUrl(QUrl(url)))
        eco_lay.addRow(tip)

        # 拆分成 3 个独立输入框，方便分段粘贴
        self.eco_client_edit = QLineEdit()
        self.eco_client_edit.setPlaceholderText("clientId=xxx")
        self.eco_client_edit.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self.eco_client_edit.textChanged.connect(self._refresh_eco_cookie_status)
        eco_lay.addRow("clientId:", self.eco_client_edit)

        self.eco_refresh_edit = QLineEdit()
        self.eco_refresh_edit.setPlaceholderText("refreshToken=xxx")
        self.eco_refresh_edit.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self.eco_refresh_edit.textChanged.connect(self._refresh_eco_cookie_status)
        eco_lay.addRow("refreshToken:", self.eco_refresh_edit)

        self.eco_login_edit = QLineEdit()
        self.eco_login_edit.setPlaceholderText("loginToken=xxx")
        self.eco_login_edit.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self.eco_login_edit.textChanged.connect(self._refresh_eco_cookie_status)
        eco_lay.addRow("loginToken:", self.eco_login_edit)

        row = QHBoxLayout()
        self.btn_eco_playwright = QPushButton("🌐 弹出浏览器登录自动抓取（推荐）")
        self.btn_eco_playwright.setStyleSheet(
            "QPushButton { background-color: #2196F3; color: white; "
            "padding: 6px 14px; font-weight: bold; }")
        self.btn_eco_playwright.setToolTip(
            "弹出 Edge 登录 www.ecosteam.cn，登录后自动抓取三段 Cookie 并保存。\n"
            "最可靠：不受浏览器运行锁影响；登录一次长期复用（超时 10 分钟）。\n"
            "首次使用需：pip install playwright && playwright install chromium")
        self.btn_eco_playwright.clicked.connect(self._on_eco_playwright_login)
        row.addWidget(self.btn_eco_playwright)

        self.btn_open_eco_login = QPushButton("🌐 系统浏览器打开（手动抓）")
        self.btn_open_eco_login.clicked.connect(self._on_open_eco_login)
        row.addWidget(self.btn_open_eco_login)
        self.btn_validate_eco_cookie = QPushButton("✅ 校验并保存")
        self.btn_validate_eco_cookie.setStyleSheet(
            "QPushButton { background-color: #4CAF50; color: white; "
            "padding: 6px 14px; font-weight: bold; }")
        self.btn_validate_eco_cookie.clicked.connect(
            self._on_save_eco_cookie)
        row.addWidget(self.btn_validate_eco_cookie)
        self.btn_clear_eco_cookie = QPushButton("🧹 清除（恢复 config.py 默认值）")
        self.btn_clear_eco_cookie.clicked.connect(self._on_clear_eco_cookie)
        row.addWidget(self.btn_clear_eco_cookie)
        row.addStretch()
        self.lbl_eco_cookie_status = QLabel("状态：未校验。")
        self.lbl_eco_cookie_status.setStyleSheet(
            "color:#888;font-size:10px;")
        self.lbl_eco_cookie_status.setWordWrap(True)
        row.addWidget(self.lbl_eco_cookie_status)
        eco_lay.addRow(row)

        # 启动时填入已保存的 GUI 覆盖值（如果有），并刷新状态
        self._eco_startup_saved_len = 0   # 暂存，log_view 创建好后再写日志
        saved = _us.get_eco_web_cookie()
        self._eco_startup_saved_len = len(saved or "")
        if saved:
            _parsed = _parse_cookie_str(saved)
            self.eco_client_edit.setText(_parsed.get("clientId") or "")
            self.eco_refresh_edit.setText(_parsed.get("refreshToken") or "")
            self.eco_login_edit.setText(_parsed.get("loginToken") or "")
            self._refresh_eco_cookie_status()

        root.addWidget(eco_box)

        # --- 购买列表表格 ---
        self.table = QTableWidget(0, len(QUEUE_COLS))
        self.table.setHorizontalHeaderLabels(QUEUE_COLS)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        # 勾选状态变化时刷新合计
        self.table.itemChanged.connect(self._on_table_item_changed)
        root.addWidget(self.table, 2)

        # --- 日志区 ---
        root.addWidget(QLabel("实时日志:"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(120)
        root.addWidget(self.log_view)

        # --- 状态栏 ---
        self.status = QLabel("就绪。")
        root.addWidget(self.status)

        # 启动日志（必须放在 self.log_view 创建之后，否则 AttributeError）
        self._append_log(
            f"[ECO Cookie] 读取顺序：用户填写 GUI 覆盖 "
            f"({getattr(self, '_eco_startup_saved_len', 0)} 字节) "
            f"> config.ECO_WEB_COOKIE 兜底。")

    # ============================================================
    # ECO Cookie：实时状态校验 / 保存 / 清空 / 打开登录页
    # ============================================================

    def _read_eco_cookie_dict(self) -> dict:
        """从 3 个独立输入框读值，拼成 cookie dict（键取回显后的小写形式）。

        输入框里可能填的是完整键值对（"clientId=xxx"）也可能只有值（"xxx"），
        这里统一解析成 {'clientId': xxx, 'refreshToken': xxx, 'loginToken': xxx}。
        """
        from core.eco_web_api import _parse_cookie_str
        result = {
            "clientId": self.eco_client_edit.text().strip(),
            "refreshToken": self.eco_refresh_edit.text().strip(),
            "loginToken": self.eco_login_edit.text().strip(),
        }
        # 兼容在单个框里填成 "clientId=xxx" 的情况：解析后覆盖
        for key, val in list(result.items()):
            if "=" in val:
                _kv = _parse_cookie_str(val)
                if key.lower() in _kv:
                    result[key] = _kv[key.lower()]
        return result

    def _refresh_eco_cookie_status(self):
        """实时刷新状态：解析 3 个输入框内容 → 缺键/格式错误用红字标出。"""
        parsed = self._read_eco_cookie_dict() if hasattr(self, "eco_client_edit") else {}
        missing = [k for k in _ECO_WEB_REQUIRED_COOKIE_KEYS
                   if not str(parsed.get(k) or "").strip()]
        have_all = len(missing) == 0 and any(
            str(parsed.get(k) or "").strip() for k in _ECO_WEB_REQUIRED_COOKIE_KEYS)
        # 构造状态文本（脱敏，只取每个值前 8 字符）
        if not have_all and not any(
                str(parsed.get(k) or "").strip() for k in _ECO_WEB_REQUIRED_COOKIE_KEYS):
            self.lbl_eco_cookie_status.setStyleSheet("color:#888;font-size:10px;")
            self.lbl_eco_cookie_status.setText(
                "状态：未填写（会使用 config.py 里的 ECO_WEB_COOKIE 作为兜底）。")
            return
        parts = []
        for k in _ECO_WEB_REQUIRED_COOKIE_KEYS:
            v = parsed.get(k) or ""
            masked = (str(v)[:8] + "…") if len(str(v)) > 8 else str(v)
            ok = "✅" if str(v).strip() else "❌"
            parts.append(f"{ok} {k}={masked!r}")
        summary = "   ".join(parts)
        if have_all:
            # 3 键齐全，只需要用户点保存
            self.lbl_eco_cookie_status.setStyleSheet(
                "color:#2E7D32;font-size:10px;")  # dark green
            self.lbl_eco_cookie_status.setText(
                f"状态：✅ 3 键齐全（可点『校验并保存』生效）。  {summary}")
        else:
            self.lbl_eco_cookie_status.setStyleSheet(
                "color:#C62828;font-size:10px;")  # dark red
            self.lbl_eco_cookie_status.setText(
                f"状态：❌ 缺少必填字段 {missing} — 保存后会 4001『用户未登录』。  "
                f"{summary}")

    def _on_open_eco_login(self):
        QDesktopServices.openUrl(QUrl(
            "https://www.ecosteam.cn/forge/recipe/create"))
        self._append_log(
            "[ECO Cookie] 已打开浏览器登录页：登录后按 F12 → Network → "
            "第一个 www.ecosteam.cn 请求 → 分别复制 Cookie 里的 "
            "clientId / refreshToken / loginToken 三段，粘贴到上方 3 个输入框。")

    def _on_eco_playwright_login(self):
        """弹出 Edge 登录 ECO，自动抓取三段 Cookie 并保存（后台线程跑 Playwright）。"""
        from core import eco_web_api

        class _T(QThread):
            done = Signal(object)   # 完整 cookie dict
            failed = Signal(str)

            def run(self):
                try:
                    self.done.emit(
                        eco_web_api.login_with_playwright(timeout_seconds=600))
                except RuntimeError as e:
                    self.failed.emit(str(e))
                except Exception as e:
                    self.failed.emit(f"Playwright 登录异常: {e!r}")

        self.btn_eco_playwright.setEnabled(False)
        self._append_log(
            "[ECO Cookie] 正在启动 Playwright 浏览器，请在弹出的 Edge 窗口中"
            "登录 www.ecosteam.cn …（登录成功后自动抓取保存，超时 10 分钟）")
        self._eco_pw_thread = _T(self)
        self._eco_pw_thread.done.connect(self._on_eco_playwright_done)
        self._eco_pw_thread.failed.connect(self._on_eco_playwright_failed)
        self._eco_pw_thread.start()

    def _on_eco_playwright_done(self, cookie_dict: dict):
        """登录成功：三键回显输入框 + 完整 Cookie（含风控键）写入 user_settings。"""
        from core.eco_web_api import _dict_to_cookie_str
        self.btn_eco_playwright.setEnabled(True)
        parsed = {
            "clientId": str(cookie_dict.get("clientId") or "").strip(),
            "refreshToken": str(cookie_dict.get("refreshToken") or "").strip(),
            "loginToken": str(cookie_dict.get("loginToken") or "").strip(),
        }
        if not all(parsed.values()):
            self._on_eco_playwright_failed("抓取的 Cookie 缺少必填三键，请重试。")
            return
        try:
            # 完整 Cookie 串（含 HMACCOUNT / acw_tc 等风控键）写入 GUI 覆盖值
            cleaned = _dict_to_cookie_str(cookie_dict)
            final = _us.set_eco_web_cookie(cleaned)
        except OSError as e:
            QMessageBox.critical(
                self, "保存 ECO Cookie 失败",
                f"写入 data/user_settings.json 失败：\n{e}")
            return
        # 三键回显输入框（触发实时状态刷新 → 绿色"3 键齐全"）
        self.eco_client_edit.setText(parsed["clientId"])
        self.eco_refresh_edit.setText(parsed["refreshToken"])
        self.eco_login_edit.setText(parsed["loginToken"])
        self.lbl_eco_cookie_status.setStyleSheet(
            "color:#2E7D32;font-size:11px;font-weight:bold;")
        self.lbl_eco_cookie_status.setText(
            f"✅ Playwright 登录成功，已自动保存 {len(cookie_dict)} 条 cookie"
            f"（含风控键），立即生效。  data/user_settings.json 更新时间："
            f"{final.get('_updated_at', '')}")
        self._append_log(
            "[ECO Cookie] ✅ Playwright 登录成功：抓到 {} 条 ecosteam.cn cookie"
            "（loginToken={}…），已写入 GUI 覆盖值，优先级 > config.py。"
            .format(len(cookie_dict), parsed["loginToken"][:8]))

    def _on_eco_playwright_failed(self, msg: str):
        self.btn_eco_playwright.setEnabled(True)
        self.lbl_eco_cookie_status.setStyleSheet(
            "color:#C62828;font-size:10px;")
        self.lbl_eco_cookie_status.setText(f"❌ {msg}")
        self._append_log(f"[ECO Cookie] ❌ Playwright 登录失败: {msg}")

    def _on_save_eco_cookie(self):
        """点『校验并保存』：从 3 个输入框读值→检查必填 3 键 → 写 user_settings → 日志提示成功。

        生效范围：后续所有 ECO 网页 API（StartSimulation / 保存配方 / FormulaDetail 等）
        只要没显式传 cookies 参数，都会读取这个 GUI 保存的 Cookie，优先级高于
        config.py 的 ECO_WEB_COOKIE。
        """
        from core.eco_web_api import _dict_to_cookie_str
        parsed = self._read_eco_cookie_dict()
        missing = [k for k in _ECO_WEB_REQUIRED_COOKIE_KEYS
                   if not str(parsed.get(k) or "").strip()]
        if missing:
            QMessageBox.warning(
                self,
                "ECO Cookie 字段不完整",
                f"必填字段缺失：{missing}\n\n"
                f"已填内容（值脱敏）：\n"
                + "\n".join(f"  {k} = {(str(v)[:10] + '…') if len(str(v)) > 10 else str(v)!r}"
                            for k, v in parsed.items())
                + f"\n\n请按提示栏步骤重新抓 Cookie（3 个键 clientId / refreshToken / "
                  f"loginToken 要同时存在）。"
            )
            self._refresh_eco_cookie_status()
            return
        # 拼成规范 Cookie 字符串并写入 user_settings.json（GUI 覆盖值）
        cleaned = _dict_to_cookie_str(parsed)
        try:
            final = _us.set_eco_web_cookie(cleaned)
        except OSError as e:
            QMessageBox.critical(
                self, "保存 ECO Cookie 失败",
                f"写入 data/user_settings.json 失败：\n{e}"
            )
            return
        # 回显时去掉 == 键前缀，只保留值（避免误粘贴的键名重复叠加）
        self.eco_client_edit.setText(parsed.get("clientId") or "")
        self.eco_refresh_edit.setText(parsed.get("refreshToken") or "")
        self.eco_login_edit.setText(parsed.get("loginToken") or "")
        # 高亮状态
        self.lbl_eco_cookie_status.setStyleSheet(
            "color:#2E7D32;font-size:11px;font-weight:bold;")
        self.lbl_eco_cookie_status.setText(
            f"✅ 已保存，立即生效（下次发 ECO 请求会用这组 Cookie）。"
            f"  data/user_settings.json 更新时间："
            f"{final.get('_updated_at','')}")
        self._append_log(
            "[ECO Cookie] ✅ 已保存 GUI 覆盖值（{} 字节），优先级 > config.py。"
            " 下次执行 StartSimulation / 保存配方 / 真实对比 会自动使用。"
            .format(len(cleaned)))

    def _on_clear_eco_cookie(self):
        """移除 GUI 覆盖 → 下次读取重新回退到 config.py 的默认值。"""
        reply = QMessageBox.question(
            self, "确认清除 ECO Cookie GUI 覆盖？",
            "清除后，将重新使用 config.py 里的 ECO_WEB_COOKIE 值，不再使用本次粘贴/保存的值。\n\n确认继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        _us.clear_eco_web_cookie()
        self.eco_client_edit.clear()
        self.eco_refresh_edit.clear()
        self.eco_login_edit.clear()
        self._refresh_eco_cookie_status()
        self._append_log(
            "[ECO Cookie] 🧹 已清除 GUI 覆盖值，回退到 config.py 的 ECO_WEB_COOKIE。")

    # ---------- 对外接口 ----------

    def add_order(self, order: dict):
        """从查询面板加入一条订单到购买列表。"""
        bqm.add_item(order)
        self._reload()
        self._append_log(
            f"已加入购买列表: {order.get('platform','')} "
            f"¥{float(order.get('price',0)):.2f} "
            f"{order.get('item_name','')}")

    # ---------- 表格加载 ----------

    def _reload(self):
        items = bqm.list_items()
        self._current_items = items  # 保存供购买时读取 goods_id
        self.table.blockSignals(True)
        self.table.setRowCount(len(items))
        for r, it in enumerate(items):
            # 选择框
            chk = QTableWidgetItem()
            chk.setFlags(chk.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            # 待购买和失败状态可勾选（失败可删除/重试）；已购买禁用
            if it["status"] in CHECKABLE_STATUSES:
                chk.setCheckState(Qt.CheckState.Unchecked)
            else:
                chk.setFlags(chk.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            chk.setData(Qt.ItemDataRole.UserRole, it["id"])
            self.table.setItem(r, 0, chk)

            self.table.setItem(r, 1, QTableWidgetItem(it["item_name"] or "-"))
            self.table.setItem(r, 2, QTableWidgetItem(it["platform"] or "-"))
            self.table.setItem(r, 3, QTableWidgetItem(it["order_id"] or "-"))
            self.table.setItem(
                r, 4, QTableWidgetItem(f"¥{it['price']:.2f}" if it['price'] is not None else "-"))
            self.table.setItem(
                r, 5, QTableWidgetItem(f"{it['wear']:.17f}" if it['wear'] is not None else "-"))
            self.table.setItem(r, 6, QTableWidgetItem(it["wear_name"] or "-"))
            self.table.setItem(r, 7, QTableWidgetItem(it["paintseed"] or "-"))
            self.table.setItem(r, 8, QTableWidgetItem(it["assetid"] or "-"))

            status_text = STATUS_TEXT.get(it["status"], it["status"])
            status_item = QTableWidgetItem(status_text)
            if it["status"] == bqm.STATUS_BOUGHT:
                status_item.setForeground(Qt.GlobalColor.darkGreen)
            elif it["status"] == bqm.STATUS_FAILED:
                status_item.setForeground(Qt.GlobalColor.red)
            self.table.setItem(r, 9, status_item)

            self.table.setItem(r, 10, QTableWidgetItem(it["added_at"] or "-"))
        self.table.blockSignals(False)
        self.lbl_count.setText(f"{len(items)} 项")
        self._update_total()

    # ---------- 选择/合计 ----------

    def _on_table_item_changed(self, item):
        if item.column() == 0:
            self._update_total()

    def _update_total(self):
        total = 0.0
        count = 0
        for r in range(self.table.rowCount()):
            chk = self.table.item(r, 0)
            if chk and chk.checkState() == Qt.CheckState.Checked:
                price_item = self.table.item(r, 4)
                if price_item:
                    try:
                        # 价格格式 "¥123.45"
                        total += float(price_item.text().lstrip("¥"))
                        count += 1
                    except (ValueError, AttributeError):
                        pass
        self.lbl_total.setText(f"合计：¥{total:.2f} ({count} 件)")

    def _on_select_all(self):
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            chk = self.table.item(r, 0)
            if chk and (chk.flags() & Qt.ItemFlag.ItemIsEnabled):
                chk.setCheckState(Qt.CheckState.Checked)
        self.table.blockSignals(False)
        self._update_total()

    def _on_select_none(self):
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            chk = self.table.item(r, 0)
            if chk:
                chk.setCheckState(Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        self._update_total()

    # ---------- 增删 ----------

    def _on_remove(self):
        removed = 0
        # 从后向前删，避免索引变化
        for r in range(self.table.rowCount() - 1, -1, -1):
            chk = self.table.item(r, 0)
            if chk and chk.checkState() == Qt.CheckState.Checked:
                item_id = chk.data(Qt.ItemDataRole.UserRole)
                if item_id is not None:
                    bqm.remove_item(item_id)
                    removed += 1
        if removed == 0:
            QMessageBox.information(self, "提示", "请先勾选要移除的商品。")
            return
        self._reload()
        self._append_log(f"已移除 {removed} 件商品。")

    def _on_clear_bought(self):
        bqm.clear_bought()
        self._reload()
        self._append_log("已清除所有已购买/失败的记录。")

    def _on_retry_failed(self):
        """将所有失败项重置为待购买，然后勾选并购买。"""
        if self._worker and self._worker.isRunning():
            QMessageBox.information(self, "提示", "购买进行中，请稍候。")
            return
        failed = bqm.list_failed()
        if not failed:
            QMessageBox.information(self, "提示", "没有失败项可重试。")
            return
        # 重置为 pending
        failed_ids = [f["id"] for f in failed]
        for fid in failed_ids:
            bqm.reset_to_pending(fid)
        self._append_log(f"已将 {len(failed_ids)} 个失败项重置为待购买。")
        self._reload()
        # 勾选这些刚重置的项
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            chk = self.table.item(r, 0)
            if chk and (chk.flags() & Qt.ItemFlag.ItemIsEnabled):
                item_id = chk.data(Qt.ItemDataRole.UserRole)
                if item_id in failed_ids:
                    chk.setCheckState(Qt.CheckState.Checked)
        self.table.blockSignals(False)
        self._update_total()
        # 直接触发购买
        self._on_buy()

    def _on_clear_all(self):
        if QMessageBox.question(
            self, "确认", "确定清空整个购买列表？"
        ) != QMessageBox.StandardButton.Yes:
            return
        bqm.clear_all()
        self._reload()
        self._append_log("已清空购买列表。")

    # ---------- C5 凭证管理（网页版 API 全自动购买）----------

    def _on_c5_auth_dialog(self):
        """打开 C5 凭证管理弹窗：状态 / 自动导入 / 手动粘贴 / 查余额。"""
        dlg = _C5AuthDialog(self)
        dlg.log.connect(self._append_log)
        dlg.exec()
        self._reload()

    # ---------- 购买 ----------

    def _on_buy(self):
        if self._worker and self._worker.isRunning():
            QMessageBox.information(self, "提示", "购买进行中，请稍候。")
            return
        # 收集勾选的 pending 商品
        selected = []
        for r in range(self.table.rowCount()):
            chk = self.table.item(r, 0)
            if chk and chk.checkState() == Qt.CheckState.Checked:
                item_id = chk.data(Qt.ItemDataRole.UserRole)
                # 读取该行数据组装为 order dict
                order = {
                    "id": item_id,
                    "item_name": self.table.item(r, 1).text(),
                    "platform": self.table.item(r, 2).text(),
                    "order_id": self.table.item(r, 3).text(),
                    "price": float(self.table.item(r, 4).text().lstrip("¥")),
                    "wear": float(self.table.item(r, 5).text()),
                    "wear_name": self.table.item(r, 6).text(),
                    # 从数据模型读取 goods_id（表格未显示该列）
                    "goods_id": self._current_items[r].get("goods_id", "")
                    if r < len(self._current_items) else "",
                }
                if order["platform"] == "-":
                    order["platform"] = ""
                if order["order_id"] == "-":
                    order["order_id"] = ""
                selected.append(order)
        if not selected:
            QMessageBox.warning(self, "提示", "请先勾选要购买的商品。")
            return

        # 二次确认
        total = sum(o["price"] for o in selected)
        msg = (f"即将购买 {len(selected)} 件商品，合计 ¥{total:.2f}。\n\n"
               "购买操作无撤回，请确认。")
        if QMessageBox.question(
            self, "购买确认", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return

        # 用第一个物品的 item_name 作为 BuyWorker 的 item_name（批量购买可能跨物品，
        # 但 BuyWorker 设计上需要单一 item_name；实际购买脚本 buy_item 内部会
        # 根据 order 中的 assetid/order_id 精确定位，item_name 仅用于日志）
        item_name = selected[0].get("item_name", "")
        self._set_busy(True, "正在购买...")
        self._worker = BuyWorker(selected, item_name)
        self._worker.progress.connect(self._on_progress)
        self._worker.item_done.connect(self._on_buy_item_done)
        self._worker.finished.connect(self._on_buy_finished)
        self._worker.start()

    def _on_progress(self, msg):
        self.status.setText(msg)
        self._append_log(msg)

    def _on_buy_item_done(self, idx, total, success, msg, order_id):
        # 更新对应行状态
        # 找到第 idx 个勾选的行（1-based idx 对应 selected 列表）
        # 简化处理：从表格中找 id 对应行
        target_id = None
        # 从 worker 中按 idx 取回 order
        if self._worker and idx <= len(self._worker.orders):
            target_id = self._worker.orders[idx - 1].get("id")
        if target_id is not None:
            if success:
                bqm.mark_bought(target_id, order_no=order_id, remark=msg)
            else:
                bqm.mark_failed(target_id, remark=msg)
        tag = "✅" if success else "❌"
        self._append_log(f"{tag} 第 {idx}/{total} 件: {msg}")

    def _on_buy_finished(self, ok, fail):
        self._set_busy(False, f"购买完成: 成功 {ok} 件, 失败 {fail} 件。")
        self._reload()
        self.request_refresh_history.emit()

    # ---------- 工具 ----------

    def _get_checked_items(self) -> list:
        """返回当前勾选的商品列表（从 _current_items 取完整字段）。"""
        result = []
        for r in range(self.table.rowCount()):
            chk = self.table.item(r, 0)
            if chk and chk.checkState() == Qt.CheckState.Checked:
                item_id = chk.data(Qt.ItemDataRole.UserRole)
                if r < len(self._current_items):
                    item = dict(self._current_items[r])
                    item["id"] = item_id
                    result.append(item)
        return result

    def _append_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{ts}] {msg}")

    def _set_busy(self, busy: bool, msg: str = ""):
        self.btn_buy.setEnabled(not busy)
        self.btn_c5_auth.setEnabled(not busy)
        self.btn_remove.setEnabled(not busy)
        self.btn_retry.setEnabled(not busy)
        self.btn_clear_all.setEnabled(not busy)
        self.btn_clear_bought.setEnabled(not busy)
        self.btn_select_all.setEnabled(not busy)
        self.btn_select_none.setEnabled(not busy)
        if msg:
            self.status.setText(msg)

    def stop_if_running(self):
        if self._worker and self._worker.isRunning():
            self._worker.wait(3000)


class _C5AuthDialog(QDialog):
    """C5 凭证管理弹窗：状态查看 / 自动导入 / 手动粘贴 / 查余额 / 清除。

    C5 购买流程（全自动）：
      merchant API（无权限）→ 网页版 API（curl.exe 绕 WAF）
      凭证 = 浏览器 Cookie + NC5_accessToken，过期后重新导入。
    """

    log = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("🔐 C5 凭证管理（网页版自动购买）")
        self.resize(640, 520)
        self._thread = None

        lay = QVBoxLayout(self)

        # 状态显示
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumHeight(110)
        lay.addWidget(self.view)

        # 按钮行
        row1 = QHBoxLayout()
        self.btn_playwright = QPushButton("🌐 弹出浏览器登录（推荐）")
        self.btn_playwright.setToolTip(
            "弹出 Edge 登录 www.c5game.com，登录后自动抓取 Cookie。\n"
            "最可靠：不受浏览器运行锁 / app-bound 加密影响；登录一次长期缓存。\n"
            "首次使用需：pip install playwright && playwright install chromium")
        self.btn_playwright.setStyleSheet(
            "QPushButton { background-color: #2196F3; color: white; "
            "padding: 6px 14px; font-weight: bold; }")
        self.btn_playwright.clicked.connect(self._on_playwright_login)
        row1.addWidget(self.btn_playwright)

        self.btn_import = QPushButton("🔄 从本机 Chrome/Edge 导入")
        self.btn_import.setToolTip(
            "自动解密本机浏览器 Cookie（需完全关闭 Edge/Chrome；\n"
            "浏览器运行中会锁定 Cookie 库导致导入失败）")
        self.btn_import.clicked.connect(self._on_import_browser)
        row1.addWidget(self.btn_import)

        self.btn_money = QPushButton("💰 查询余额（验证凭证）")
        self.btn_money.clicked.connect(self._on_check_money)
        row1.addWidget(self.btn_money)

        self.btn_clear = QPushButton("🧹 清除缓存")
        self.btn_clear.clicked.connect(self._on_clear)
        row1.addWidget(self.btn_clear)
        lay.addLayout(row1)

        # 手动导入区
        box = QGroupBox("✍️ 手动导入（两种格式任选其一）")
        form = QVBoxLayout(box)
        tip = QLabel(
            "方式一：Chrome/Edge 登录 www.c5game.com → F12 → Network → 刷新 → "
            "任一 c5game 请求 → 右键 Copy → Copy as cURL (cmd) → 粘贴到下面。\n"
            "方式二：直接粘贴完整 Cookie 串（含 NC5_accessToken=...）。\n"
            "提示：购买报「Not login / WAF 拦截」时，重新导入一次即可。")
        tip.setWordWrap(True)
        form.addWidget(tip)

        self.txt = QPlainTextEdit()
        self.txt.setPlaceholderText(
            "curl 'https://www.c5game.com/api/v1/...' \\\n"
            "  -b 'i18n_redirected=zh; NC5_accessToken=eyJ...; ...' \\\n"
            "  -H 'x-access-token: eyJ...' ...\n\n"
            "或直接粘贴 cookie 串: i18n_redirected=zh; NC5_accessToken=eyJ...; ...")
        self.txt.setMaximumHeight(140)
        form.addWidget(self.txt)

        row2 = QHBoxLayout()
        self.btn_manual = QPushButton("💾 解析并保存")
        self.btn_manual.setStyleSheet(
            "QPushButton { background-color: #4CAF50; color: white; "
            "padding: 6px 14px; font-weight: bold; }")
        self.btn_manual.clicked.connect(self._on_manual_import)
        row2.addWidget(self.btn_manual)
        row2.addStretch()
        form.addLayout(row2)
        lay.addWidget(box)

        # 关闭按钮
        row3 = QHBoxLayout()
        row3.addStretch()
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        row3.addWidget(btn_close)
        lay.addLayout(row3)

        self._refresh_status()

    # ---------- 内部 ----------

    def _refresh_status(self):
        from core import c5_auth
        self.view.setPlainText(c5_auth.status_text())

    def _on_import_browser(self):
        """从本机 Chrome/Edge Cookies 解密导入（后台线程，避免卡 UI）。"""
        from core import c5_auth

        class _T(QThread):
            done = Signal(object)   # C5WebAuth or None

            def run(self):
                try:
                    self.done.emit(c5_auth.get_auth(force_refresh=True))
                except Exception:
                    self.done.emit(None)

        self.btn_import.setEnabled(False)
        self.view.setPlainText("正在从本机 Chrome / Edge Cookies 读取 C5 登录态 ...")
        self._thread = _T(self)
        self._thread.done.connect(self._on_import_done)
        self._thread.start()

    def _on_import_done(self, auth):
        self.btn_import.setEnabled(True)
        if auth is None:
            self.view.setPlainText(
                "❌ 未在本机 Chrome/Edge 中找到 C5 登录 Cookie。\n"
                "常见原因：\n"
                "  ① Edge/Chrome 正在运行 → Cookie 库被独占锁定，读不了\n"
                "     （需完全退出浏览器后再点一次导入）\n"
                "  ② 该浏览器从未登录过 www.c5game.com\n"
                "  ③ 新版浏览器 app-bound 加密，解不开 cookie\n"
                "推荐直接点「🌐 弹出浏览器登录」或使用下方「✍️ 手动导入」。")
            return
        self._refresh_status()
        self.log.emit(f"[C5凭证] ✅ 已从 {auth.source} 导入登录态（token 前缀 {auth.token[:20]}…）")

    def _on_playwright_login(self):
        """弹出 Edge 登录 C5，自动抓取 Cookie（后台线程，Playwright 需独占线程）。"""
        from core import c5_auth

        class _T(QThread):
            done = Signal(object)   # C5WebAuth
            failed = Signal(str)

            def run(self):
                try:
                    self.done.emit(c5_auth.login_with_playwright(timeout_seconds=600))
                except RuntimeError as e:
                    self.failed.emit(str(e))
                except Exception as e:
                    self.failed.emit(f"Playwright 登录异常: {e!r}")

        self.btn_playwright.setEnabled(False)
        self.btn_import.setEnabled(False)
        self.view.setPlainText(
            "正在启动 Playwright 浏览器，请在弹出的 Edge 窗口中登录 www.c5game.com …\n"
            "（登录成功后窗口会自动关闭，凭证将缓存到本地长期复用；超时 10 分钟）")
        self._thread = _T(self)
        self._thread.done.connect(self._on_playwright_done)
        self._thread.failed.connect(self._on_playwright_failed)
        self._thread.start()

    def _on_playwright_done(self, auth):
        self.btn_playwright.setEnabled(True)
        self.btn_import.setEnabled(True)
        self._refresh_status()
        self.log.emit(f"[C5凭证] ✅ Playwright 登录成功（token 前缀 {auth.token[:20]}…）")
        QMessageBox.information(
            self, "登录成功",
            "C5 登录态已自动抓取并缓存。\n\n建议点「💰 查询余额」验证凭证有效性。")

    def _on_playwright_failed(self, msg: str):
        self.btn_playwright.setEnabled(True)
        self.btn_import.setEnabled(True)
        self.view.setPlainText(f"❌ {msg}")
        self.log.emit(f"[C5凭证] ❌ Playwright 登录失败: {msg}")

    def _on_manual_import(self):
        """解析粘贴的 curl 文本 / cookie 串并保存。"""
        from core import c5_auth
        text = self.txt.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "提示", "请先粘贴内容。")
            return
        try:
            auth = c5_auth.import_curl_text(text)
        except ValueError as e:
            QMessageBox.warning(self, "导入失败", str(e))
            return
        self.txt.clear()
        self._refresh_status()
        self.log.emit(f"[C5凭证] ✅ 手动导入成功（token 前缀 {auth.token[:20]}…）")
        QMessageBox.information(
            self, "导入成功",
            f"已保存 C5 凭证（来源：{auth.source}）。\n\n"
            "建议点「💰 查询余额」验证凭证有效性。")

    def _on_check_money(self):
        """curl 查询 C5 余额，验证凭证（后台线程，curl 最多 30s）。"""
        from core import c5_web_api

        class _T(QThread):
            done = Signal(bool, str)

            def run(self):
                try:
                    ok, msg, _ = c5_web_api.check_money()
                    self.done.emit(ok, msg)
                except Exception as e:
                    self.done.emit(False, f"查询异常: {e}")

        self.btn_money.setEnabled(False)
        self.view.setPlainText("正在查询 C5 余额 ...")
        self._thread = _T(self)
        self._thread.done.connect(self._on_money_done)
        self._thread.start()

    def _on_money_done(self, ok: bool, msg: str):
        self.btn_money.setEnabled(True)
        tag = "✅" if ok else "❌"
        self.view.setPlainText(f"{tag} {msg}\n\n"
                               "（❌ 时：Not login → 重新导入；WAF 拦截 → 重新导入）")
        self.log.emit(f"[C5凭证] {'✅' if ok else '❌'} 余额检查: {msg}")

    def _on_clear(self):
        from core import c5_auth
        if QMessageBox.question(
            self, "确认清除",
            "确定清除本地 C5 凭证缓存？\n清除后购买 C5 商品前需重新导入。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        c5_auth.clear_all()
        self._refresh_status()
        self.log.emit("[C5凭证] 🧹 已清除本地缓存。")
