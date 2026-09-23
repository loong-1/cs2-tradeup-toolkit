"""查询购买面板：输入参数 -> 比价 -> 选择 -> 购买。"""
from PySide6.QtCore import Qt, Signal, QThread
from PySide6.QtWidgets import (
    QComboBox, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget, QDialog, QDialogButtonBox, QDoubleSpinBox, QPlainTextEdit,
)

from core.data_manager import (
    load_items, save_items, load_items_from_main_csv, log_operation)
from gui.workers import BuyWorker, QueryWorker, DetectIpWorker
from core import buff_auth, price_fetcher


# 表格列定义
COLS = ["选择", "平台", "订单ID", "磨损值", "磨损等级", "价格"]
PLATFORM_OPTIONS = ["全部", "仅Buff", "仅C5", "仅ECO"]


class AddItemDialog(QDialog):
    """添加/编辑物品映射的对话框。"""

    def __init__(self, parent=None, defaults=None):
        super().__init__(parent)
        self.setWindowTitle("添加物品映射")
        self.setMinimumWidth(420)
        form = QFormLayout(self)
        self.name_edit = QLineEdit(defaults.get("item_name", "") if defaults else "")
        self.buff_edit = QLineEdit(defaults.get("buff_goods_id", "") if defaults else "")
        self.c5_edit = QLineEdit(defaults.get("c5_market_hash_name", "") if defaults else "")
        self.c5_app_edit = QLineEdit(defaults.get("c5_app_id", "730") if defaults else "730")
        form.addRow("物品名称 *", self.name_edit)
        form.addRow("Buff goods_id", self.buff_edit)
        form.addRow("C5 market_hash_name", self.c5_edit)
        form.addRow("C5 app_id", self.c5_app_edit)
        btns = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        form.addRow(btns)

    def get_values(self):
        return {
            "item_name": self.name_edit.text().strip(),
            "buff_goods_id": self.buff_edit.text().strip(),
            "c5_market_hash_name": self.c5_edit.text().strip(),
            "c5_app_id": self.c5_app_edit.text().strip() or "730",
        }


class QueryPanel(QWidget):
    """查询 + 购买主面板。"""

    # 请求将选中商品加入购买列表（payload 为 order dict）
    request_add_to_buy = Signal(dict)

    def __init__(self):
        super().__init__()
        self._worker = None          # 持有 QueryWorker 引用
        self._buy_worker = None       # 持有 BuyWorker 引用
        self._ip_worker = None       # 持有 DetectIpWorker 引用
        self._results = []            # 当前查询结果（归一化 dict 列表）
        self._build_ui()
        self._refresh_items()

    # ---------- UI 构建 ----------

    def _build_ui(self):
        root = QVBoxLayout(self)

        # --- 输入区 ---
        grp = QGroupBox("查询条件")
        grid = QGridLayout(grp)

        grid.addWidget(QLabel("物品名称:"), 0, 0)
        self.item_combo = QComboBox()
        self.item_combo.setMinimumWidth(340)
        self.item_combo.setEditable(True)
        self.item_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        comp = self.item_combo.completer()
        comp.setCompletionMode(comp.CompletionMode.PopupCompletion)
        comp.setFilterMode(Qt.MatchFlag.MatchContains)
        grid.addWidget(self.item_combo, 0, 1)

        self.btn_add_item = QPushButton("添加物品")
        self.btn_add_item.clicked.connect(self._on_add_item)
        grid.addWidget(self.btn_add_item, 0, 2)

        self.btn_refresh = QPushButton("刷新列表")
        self.btn_refresh.clicked.connect(self._refresh_items)
        grid.addWidget(self.btn_refresh, 0, 3)

        # 磨损范围
        grid.addWidget(QLabel("磨损下限:"), 1, 0)
        self.wear_min = QDoubleSpinBox()
        self.wear_min.setRange(0.0, 1.0)
        self.wear_min.setDecimals(4)
        self.wear_min.setSingleStep(0.01)
        self.wear_min.setValue(0.00)
        self.wear_min.setSpecialValueText("不限")
        grid.addWidget(self.wear_min, 1, 1)

        grid.addWidget(QLabel("磨损上限:"), 1, 2)
        self.wear_max = QDoubleSpinBox()
        self.wear_max.setRange(0.0, 1.0)
        self.wear_max.setDecimals(4)
        self.wear_max.setSingleStep(0.01)
        self.wear_max.setValue(0.00)
        self.wear_max.setSpecialValueText("不限")
        grid.addWidget(self.wear_max, 1, 3)

        # 价格范围
        grid.addWidget(QLabel("价格下限(¥):"), 2, 0)
        self.price_min = QDoubleSpinBox()
        self.price_min.setRange(0.0, 999999.0)
        self.price_min.setDecimals(2)
        self.price_min.setSingleStep(10.0)
        self.price_min.setValue(0.00)
        self.price_min.setSpecialValueText("不限")
        grid.addWidget(self.price_min, 2, 1)

        grid.addWidget(QLabel("价格上限(¥):"), 2, 2)
        self.price_max = QDoubleSpinBox()
        self.price_max.setRange(0.0, 999999.0)
        self.price_max.setDecimals(2)
        self.price_max.setSingleStep(10.0)
        self.price_max.setValue(0.00)
        self.price_max.setSpecialValueText("不限")
        grid.addWidget(self.price_max, 2, 3)

        # 平台 + 数量
        grid.addWidget(QLabel("查询平台:"), 3, 0)
        self.platform_combo = QComboBox()
        self.platform_combo.addItems(PLATFORM_OPTIONS)
        grid.addWidget(self.platform_combo, 3, 1)

        grid.addWidget(QLabel("购买数量:"), 3, 2)
        self.qty_spin = QSpinBox()
        self.qty_spin.setRange(1, 999)
        self.qty_spin.setValue(1)
        grid.addWidget(self.qty_spin, 3, 3)

        # 第 4 行：Buff 登录态 + C5 IP 工具按钮
        self.btn_buff_auth = QPushButton("🔐 Buff 登录态管理")
        self.btn_buff_auth.clicked.connect(self._on_buff_auth_dialog)
        grid.addWidget(self.btn_buff_auth, 4, 0, 1, 2)

        self.btn_detect_ip = QPushButton("🌐 检测 C5 当前 IP")
        self.btn_detect_ip.clicked.connect(self._on_detect_ip)
        grid.addWidget(self.btn_detect_ip, 4, 2, 1, 2)

        # 第 5 行：Buff 爬取强度配置（防 429）
        grid.addWidget(QLabel("Buff爬取页数:"), 5, 0)
        self.buff_pages_spin = QSpinBox()
        self.buff_pages_spin.setRange(1, 50)
        cur_pages, cur_delay = price_fetcher.get_buff_scrape_config()
        self.buff_pages_spin.setValue(cur_pages)
        self.buff_pages_spin.setToolTip("单个物品爬取的最大页数，越小越不容易触发429")
        grid.addWidget(self.buff_pages_spin, 5, 1)

        grid.addWidget(QLabel("页间间隔(秒):"), 5, 2)
        self.buff_delay_spin = QDoubleSpinBox()
        self.buff_delay_spin.setRange(0.1, 30.0)
        self.buff_delay_spin.setSingleStep(0.5)
        self.buff_delay_spin.setValue(cur_delay)
        self.buff_delay_spin.setToolTip("每页请求之间的间隔秒数，越大越安全")
        grid.addWidget(self.buff_delay_spin, 5, 3)

        root.addWidget(grp)

        # --- 按钮行 ---
        btn_row = QHBoxLayout()
        self.btn_query = QPushButton("🔍 查询比价")
        self.btn_query.clicked.connect(self._on_query)
        btn_row.addWidget(self.btn_query)

        self.btn_auto_select = QPushButton("自动选择最低价")
        self.btn_auto_select.clicked.connect(self._auto_select)
        btn_row.addWidget(self.btn_auto_select)

        self.btn_buy = QPushButton("💳 购买选中")
        self.btn_buy.clicked.connect(self._on_buy)
        btn_row.addWidget(self.btn_buy)

        self.btn_add_to_buy = QPushButton("📋 加入购买列表")
        self.btn_add_to_buy.clicked.connect(self._on_add_to_buy)
        btn_row.addWidget(self.btn_add_to_buy)

        self.btn_clear = QPushButton("清空结果")
        self.btn_clear.clicked.connect(self._clear_results)
        btn_row.addWidget(self.btn_clear)

        btn_row.addStretch()
        root.addLayout(btn_row)

        # --- 结果表格 ---
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        root.addWidget(self.table, 1)

        # --- 状态栏 ---
        self.status = QLabel("就绪。")
        root.addWidget(self.status)

    # ---------- 物品列表 ----------

    def _refresh_items(self):
        self.item_combo.clear()
        # 主 CSV（只读）作为主要来源，再叠加本地 items.csv 自定义条目
        main_items = load_items_from_main_csv()
        local_items = load_items()
        self._items = list(main_items) + list(local_items)
        for item in self._items:
            self.item_combo.addItem(item["item_name"], item)
        main_count = len(main_items)
        local_count = len(local_items)
        self.status.setText(
            f"已加载 {main_count + local_count} 个物品 "
            f"（主CSV {main_count} 个 + 自定义 {local_count} 个）")

    def _on_add_item(self):
        dlg = AddItemDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dlg.get_values()
        if not vals["item_name"]:
            QMessageBox.warning(self, "提示", "物品名称不能为空。")
            return
        # 新增条目只保存到本地 items.csv（主CSV只读）
        local_items = load_items()
        local_items.append(vals)
        try:
            save_items(local_items)
        except Exception as e:
            QMessageBox.critical(self, "错误", f"保存失败: {e}")
            return
        self._refresh_items()
        # 选中新添加的
        idx = self.item_combo.findText(vals["item_name"])
        if idx >= 0:
            self.item_combo.setCurrentIndex(idx)

    def select_item_by_config(self, config):
        """从外部（物品总览跳转）载入指定物品配置。

        优先用相同显示名匹配下拉项；否则按 goods_id / hash_name 匹配；
        都找不到则在末尾追加一项并选中。
        """
        name = config.get("item_name", "")
        # 1) 显示名完全匹配
        idx = self.item_combo.findText(name)
        if idx >= 0:
            self.item_combo.setCurrentIndex(idx)
            return
        # 2) 遍历 data 匹配 goods_id 或 hash_name
        gid = str(config.get("buff_goods_id", ""))
        hn = config.get("c5_market_hash_name", "")
        for i in range(self.item_combo.count()):
            d = self.item_combo.itemData(i)
            if not d:
                continue
            if gid and str(d.get("buff_goods_id", "")) == gid:
                self.item_combo.setCurrentIndex(i)
                return
            if hn and d.get("c5_market_hash_name", "") == hn:
                self.item_combo.setCurrentIndex(i)
                return
        # 3) 否则添加到临时下拉
        payload = {
            "item_name": name,
            "buff_goods_id": gid,
            "c5_market_hash_name": hn,
            "c5_app_id": str(config.get("c5_app_id", "730")),
        }
        self.item_combo.addItem(name, payload)
        self._items.append(payload)
        self.item_combo.setCurrentIndex(self.item_combo.count() - 1)

    # ---------- 查询 ----------

    def _on_query(self):
        if not self._items:
            QMessageBox.warning(self, "提示", "请先添加物品映射。")
            return
        # 应用 Buff 爬取强度配置
        price_fetcher.set_buff_scrape_config(
            self.buff_pages_spin.value(),
            self.buff_delay_spin.value())
        item_config = self.item_combo.currentData()
        # 可编辑模式下，用户直接输入名称时可能 currentData() 为 None，
        # 此时按显示名回退匹配。
        if not item_config:
            text = self.item_combo.currentText().strip()
            for it in self._items:
                if it["item_name"] == text:
                    item_config = it
                    break
        if not item_config:
            QMessageBox.warning(
                self, "提示",
                "请从下拉中选择一个物品（或在输入框中输入完整名称后回车选择）。")
            return

        # 检查至少有一个平台 ID
        has_buff = bool(item_config.get("buff_goods_id"))
        has_c5 = bool(item_config.get("c5_market_hash_name"))
        if not has_buff and not has_c5:
            QMessageBox.warning(self, "提示", "该物品未配置任何平台ID，请先编辑。")
            return

        wmin = self.wear_min.value() or None
        wmax = self.wear_max.value() or None
        pmin = self.price_min.value() or None
        pmax = self.price_max.value() or None

        plat_idx = self.platform_combo.currentIndex()
        platforms = None
        if plat_idx == 1:
            platforms = ["buff"]
        elif plat_idx == 2:
            platforms = ["c5"]
        elif plat_idx == 3:
            platforms = ["eco"]

        self._set_busy(True, "正在查询...")
        self._clear_results()
        self._worker = QueryWorker(
            item_config, wmin, wmax, pmin, pmax, platforms)
        self._worker.progress.connect(self.status.setText)
        self._worker.finished.connect(self._on_query_done)
        self._worker.error.connect(self._on_query_error)
        self._worker.start()

    def _on_query_done(self, results, errors):
        self._results = results
        self._fill_table(results)
        # 记录查询操作
        item_name = self.item_combo.currentText()
        plat_label = self.platform_combo.currentText()
        if errors:
            remark = f"查询结果 {len(results)} 条；错误: {errors}"
            status = "部分失败"
        else:
            remark = f"查询结果 {len(results)} 条"
            status = "成功"
        if not results and errors:
            status = "失败"
        log_operation(item_name, None, plat_label, None, 0,
                      "查询", status, "", remark)
        self._set_busy(False, f"查询完成，共 {len(results)} 条结果。" +
                       (f" 错误: {errors}" if errors else ""))
        # 记录错误到状态栏供用户查看

    def _on_query_error(self, msg):
        self._set_busy(False, f"查询失败: {msg}")
        QMessageBox.critical(self, "查询失败", msg)

    # ---------- 表格 ----------

    def _fill_table(self, results):
        self.table.setRowCount(len(results))
        for r, order in enumerate(results):
            # 选择复选框
            chk = QTableWidgetItem()
            chk.setFlags(chk.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            chk.setCheckState(Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, order)
            self.table.setItem(r, 0, chk)

            self.table.setItem(r, 1, QTableWidgetItem(order["platform"]))
            self.table.setItem(r, 2, QTableWidgetItem(order["order_id"]))
            # wear=0.0 表示该平台未返回磨损（如 batch 快速路径），显示 "-"
            wv = order.get("wear", 0.0)
            wear_text = f"{wv:.6f}" if wv and wv > 0 else "-"
            self.table.setItem(r, 3, QTableWidgetItem(wear_text))
            self.table.setItem(r, 4, QTableWidgetItem(order.get("wear_name", "") or "-"))
            price_item = QTableWidgetItem(f"¥{order['price']:.2f}")
            self.table.setItem(r, 5, price_item)

        # 高亮最低价（首行，因已按价格升序）
        if results:
            for c in range(len(COLS)):
                item = self.table.item(0, c)
                if item:
                    item.setBackground(Qt.GlobalColor.green)

    def _clear_results(self):
        self._results = []
        self.table.setRowCount(0)

    def _auto_select(self):
        """自动勾选前 N 个最低价（N=购买数量）。"""
        n = self.qty_spin.value()
        count = min(n, self.table.rowCount())
        if count == 0:
            QMessageBox.information(self, "提示", "没有可选择的商品。")
            return
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item:
                item.setCheckState(
                    Qt.CheckState.Checked if r < count
                    else Qt.CheckState.Unchecked)
        self.status.setText(f"已自动勾选前 {count} 个最低价商品。")

    # ---------- 购买 ----------

    def _on_add_to_buy(self):
        """收集勾选的订单，发送到购买列表面板。"""
        selected = []
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                order = item.data(Qt.ItemDataRole.UserRole)
                if order:
                    selected.append(order)
        if not selected:
            QMessageBox.warning(self, "提示", "请先勾选要加入购买列表的商品。")
            return
        item_name = self.item_combo.currentText()
        for order in selected:
            payload = dict(order)
            payload["item_name"] = item_name
            self.request_add_to_buy.emit(payload)
        self.status.setText(f"已加入购买列表 {len(selected)} 件。")

    def _on_buy(self):
        # 收集勾选的订单
        selected = []
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                order = item.data(Qt.ItemDataRole.UserRole)
                if order:
                    selected.append(order)
        if not selected:
            QMessageBox.warning(self, "提示", "请先勾选要购买的商品（或点击“自动选择最低价”）。")
            return

        item_name = self.item_combo.currentText()
        self._set_busy(True, "正在购买...")
        self._buy_worker = BuyWorker(selected, item_name)
        self._buy_worker.progress.connect(self.status.setText)
        self._buy_worker.item_done.connect(self._on_buy_item_done)
        self._buy_worker.finished.connect(self._on_buy_finished)
        self._buy_worker.start()

    def _on_buy_item_done(self, idx, total, success, msg, order_id):
        tag = "✅" if success else "❌"
        self.status.setText(f"{tag} 第 {idx}/{total} 件: {msg}")

    def _on_buy_finished(self, ok, fail):
        self._set_busy(False, f"购买完成: 成功 {ok} 件, 失败 {fail} 件。")
        if fail > 0:
            QMessageBox.warning(self, "购买完成",
                                f"成功 {ok} 件，失败 {fail} 件，详情见历史记录。")
        else:
            QMessageBox.information(self, "购买完成",
                                    f"全部 {ok} 件购买成功！")

    # ---------- 工具 ----------

    def _on_detect_ip(self):
        """调用 C5 API 检测当前请求 IP，并在弹窗中展示结果。"""
        if self._ip_worker and self._ip_worker.isRunning():
            QMessageBox.information(self, "提示", "检测进行中，请稍候。")
            return
        self.btn_detect_ip.setEnabled(False)
        self.status.setText("正在检测 C5 当前请求 IP ...")
        self._ip_worker = DetectIpWorker()
        self._ip_worker.progress.connect(self.status.setText)
        self._ip_worker.finished.connect(self._on_detect_ip_done)
        self._ip_worker.error.connect(self._on_detect_ip_error)
        self._ip_worker.start()

    def _on_detect_ip_done(self, result):
        self.btn_detect_ip.setEnabled(True)
        ip = result.get("ip", "")
        in_wl = result.get("in_whitelist", False)
        raw = result.get("raw_msg", "")
        success = result.get("success", False)

        if success and in_wl:
            title = "C5 IP 检测 - 已在白名单"
            body = ("C5 API 调用成功，当前请求 IP 已在白名单内。\n\n"
                    "可直接使用 C5 查询/购买功能。")
        elif ip:
            title = "C5 IP 检测 - 不在白名单"
            body = (
                f"C5 服务器看到的当前请求 IP：\n\n    {ip}\n\n"
                f"原始返回：{raw}\n\n"
                "请将上述 IP 添加到 C5 开放平台 IP 白名单后重试。"
            )
        else:
            title = "C5 IP 检测 - 失败"
            body = f"未能从 C5 返回中提取 IP。\n\n原始返回：{raw}"

        self.status.setText(f"C5 IP 检测完成: {'在白名单' if (success and in_wl) else (ip or '失败')}")
        QMessageBox.information(self, title, body)

    def _on_detect_ip_error(self, msg):
        self.btn_detect_ip.setEnabled(True)
        self.status.setText(f"C5 IP 检测失败: {msg}")
        QMessageBox.critical(self, "C5 IP 检测失败", msg)

    def _set_busy(self, busy, msg=""):
        self.btn_query.setEnabled(not busy)
        self.btn_buy.setEnabled(not busy)
        self.btn_add_to_buy.setEnabled(not busy)
        self.btn_auto_select.setEnabled(not busy)
        self.btn_add_item.setEnabled(not busy)
        self.btn_refresh.setEnabled(not busy)
        if msg:
            self.status.setText(msg)

    # ---------- Buff 登录态管理 ----------

    def _on_buff_auth_dialog(self):
        dlg = BuffAuthDialog(self)
        dlg.exec()
        # 关闭弹窗后，把状态栏刷新为最新状态摘要
        c = buff_auth.get_session_cookies()
        if c:
            self.status.setText(
                f"Buff 登录态：已就绪（来源 {c.source}，缓存时长 {c.age_seconds/60:.0f} 分钟）")
        else:
            self.status.setText("Buff 登录态：未就绪（请在 Chrome/Edge 登录 Buff 后点「导入」，或扫码登录）。")


class BuffAuthWorker(QThread):
    """后台执行：从 Chrome/Edge 导入 或 Playwright 扫码登录。"""

    progress = Signal(str)
    done = Signal(str)  # 结果人类可读文本
    failed = Signal(str)

    def __init__(self, mode: str, parent=None):
        """mode: 'import_browser' | 'playwright'"""
        super().__init__(parent)
        self.mode = mode

    def run(self):
        try:
            if self.mode == "import_browser":
                self.progress.emit("正在从本机 Chrome / Edge Cookies 读取 Buff 登录态 ...")
                c = buff_auth.get_session_cookies(force_refresh=True, allow_playwright=False)
                if c is None:
                    self.failed.emit(
                        "未从 Chrome/Edge 中找到 Buff 的 session/csrf_token。\n\n"
                        "请先在 Chrome 或 Edge 浏览器中打开 https://buff.163.com 完成登录（保持登录状态），\n"
                        "然后关掉浏览器（让 Cookies 文件刷新到磁盘），再点一次「🔄 从本机浏览器导入」。")
                else:
                    self.done.emit(
                        f"✅ 登录态导入成功（来源：{c.source}）。\n"
                        f"下次启动本软件时会自动复用，无需再手动复制 Cookie。")
            elif self.mode == "playwright":
                self.progress.emit(
                    "正在启动 Playwright 浏览器，请在弹出的浏览器窗口中扫码/账密登录 Buff …\n"
                    "（登录成功后窗口会自动关闭，登录态将永久缓存到本地）")
                c = buff_auth.login_with_playwright(timeout_seconds=600)
                self.done.emit(
                    f"✅ Playwright 扫码登录成功！\n"
                    f"已缓存 session（前缀 {c.session[:20]}…），后续启动自动复用。")
            else:
                self.failed.emit(f"未知 mode={self.mode!r}")
        except RuntimeError as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(f"执行异常：{e!r}")


class BuffAuthDialog(QDialog):
    """Buff 登录态管理弹窗：查看状态 / 导入 / 扫码 / 清除。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("🔐 Buff 登录态管理")
        self.resize(620, 460)
        self._worker: BuffAuthWorker | None = None

        v = QVBoxLayout(self)

        # 状态显示
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        v.addWidget(self.view, 1)
        self._refresh_view()

        # 按钮（手动填写放第一个，用户要求此为主路径）
        btn_row = QHBoxLayout()
        self.btn_manual = QPushButton("✍️ 手动填写")
        self.btn_manual.setToolTip(
            "最稳妥的方式：打开 buff.163.com → F12 → Network → 随便点一个 sell_order 请求 →\n"
            "在 Request Headers -> Cookie 里复制 session=xxx 与 csrf_token=xxx 的值粘贴到这里。")
        self.btn_manual.clicked.connect(self._on_manual)
        btn_row.addWidget(self.btn_manual)

        self.btn_import = QPushButton("🔄 从本机 Chrome/Edge 导入")
        self.btn_import.setToolTip(
            "自动从本机浏览器 Cookie 库解密（需 pywin32 + pycryptodome，且必须完全关闭浏览器）")
        self.btn_import.clicked.connect(self._on_import_browser)
        btn_row.addWidget(self.btn_import)

        self.btn_qr = QPushButton("🆕 扫码登录（Playwright）")
        self.btn_qr.setToolTip(
            "首次使用需执行：pip install playwright && playwright install chromium")
        self.btn_qr.clicked.connect(self._on_playwright)
        btn_row.addWidget(self.btn_qr)

        self.btn_clear = QPushButton("🚫 清除本地缓存")
        self.btn_clear.setToolTip("删除本地保存的 session/csrf_token，下次查询将回退到 found_buff.py 里的旧值。")
        self.btn_clear.clicked.connect(self._on_clear)
        btn_row.addWidget(self.btn_clear)

        btn_row.addStretch()
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.accept)
        btn_row.addWidget(self.btn_close)
        v.addLayout(btn_row)

    # ---------- helpers ----------

    def _refresh_view(self):
        self.view.setPlainText(buff_auth.status_text())

    def _set_buttons_busy(self, busy: bool):
        self.btn_manual.setEnabled(not busy)
        self.btn_import.setEnabled(not busy)
        self.btn_qr.setEnabled(not busy)
        self.btn_clear.setEnabled(not busy)
        self.btn_close.setEnabled(not busy)

    def _on_manual(self):
        dlg = _ManualBuffCookieDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._refresh_view()
            QMessageBox.information(self, "已保存",
                                    "手动填写的 Buff session / csrf_token 已保存到本地缓存。\n"
                                    "现在直接去查询比价即可使用新的登录态。")

    def _start_worker(self, mode: str):
        self._set_buttons_busy(True)
        self.view.appendPlainText("\n── 开始执行 ──\n")
        self._worker = BuffAuthWorker(mode, self)
        self._worker.progress.connect(lambda m: self.view.appendPlainText(m))
        self._worker.done.connect(self._on_worker_done)
        self._worker.failed.connect(self._on_worker_failed)
        self._worker.start()

    def _on_worker_done(self, msg: str):
        self.view.appendPlainText(msg)
        self.view.appendPlainText("\n" + buff_auth.status_text())
        self._set_buttons_busy(False)
        self._worker = None

    def _on_worker_failed(self, msg: str):
        self.view.appendPlainText("❌ 失败：\n" + msg)
        self._set_buttons_busy(False)
        self._worker = None
        QMessageBox.warning(self, "操作失败", msg)

    # ---------- slots ----------

    def _on_import_browser(self):
        self._start_worker("import_browser")

    def _on_playwright(self):
        self._start_worker("playwright")

    def _on_clear(self):
        r = QMessageBox.question(
            self, "确认清除",
            "确定清除本地所有 Buff 登录态缓存？\n\n"
            "清除后下一次查询会回到 found_buff.py 中的硬编码常量，直到你重新导入或扫码。")
        if r != QMessageBox.StandardButton.Yes:
            return
        buff_auth.clear_all()
        self._refresh_view()
        QMessageBox.information(self, "已清除", "本地 Buff 登录态缓存已全部删除。")


class _ManualBuffCookieDialog(QDialog):
    """手动填写 Buff session / csrf_token 对话框。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("✍️ 手动填写 Buff 登录态")
        self.resize(560, 260)

        # 如果已有缓存，回填到输入框方便修改
        current = buff_auth.get_session_cookies()

        form = QFormLayout()
        form.addRow(QLabel(
            "<b>获取步骤（Edge 同样适用）</b>：<br>"
            "1. 打开浏览器登录 https://buff.163.com<br>"
            "2. 按 F12 打开开发者工具 → 切到 <b>Network</b> 面板 → 筛选 Fetch/XHR<br>"
            "3. 在 Buff 页面点一下「在售」触发抓包，点开任意一条以 <code>sell_order?game=csgo</code> 开头的请求<br>"
            "4. 右侧找到 <b>Request Headers → Cookie</b>，复制：<br>"
            "&nbsp;&nbsp;&nbsp;<code>session=</code> 后面直到分号 <code>;</code> 的那段<br>"
            "&nbsp;&nbsp;&nbsp;<code>csrf_token=</code> 后面直到分号 <code>;</code> 的那段<br>"
            "5. 分别粘贴到下面两个框，点「💾 保存」即可长期使用。"
        ))

        self.session_edit = QLineEdit(current.session if current else "")
        self.session_edit.setPlaceholderText(
            "粘贴形如 1-xxxxxxxx...2020212636 的 session 值（勿外传）")
        self.session_edit.setMinimumWidth(480)
        form.addRow("session *", self.session_edit)

        self.csrf_edit = QLineEdit(current.csrf_token if current else "")
        self.csrf_edit.setPlaceholderText(
            "粘贴形如 Ijxxxxxxxx...xxxx.xxxxxx.xxxxxxxx 的 csrf_token 值（勿外传）")
        self.csrf_edit.setMinimumWidth(480)
        form.addRow("csrf_token *", self.csrf_edit)

        v = QVBoxLayout(self)
        v.addLayout(form)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Save).setText("💾 保存并验证")
        btns.accepted.connect(self._on_save)
        btns.rejected.connect(self.reject)
        v.addWidget(btns)

    def _on_save(self):
        session = self.session_edit.text().strip()
        csrf = self.csrf_edit.text().strip()
        if not session or not csrf:
            QMessageBox.warning(self, "提示", "session 和 csrf_token 都不能为空。")
            return
        if ";" in session or "=" in session.split(";")[0]:
            # 用户可能把 Cookie 整行粘进来了，尝试自动解析
            fixed = self._extract_cookie("session", session)
            if fixed:
                session = fixed
        if ";" in csrf or "=" in csrf.split(";")[0]:
            fixed = self._extract_cookie("csrf_token", csrf)
            if fixed:
                csrf = fixed
        try:
            buff_auth.save_manual_cookies(session, csrf)
        except ValueError as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "保存失败", f"{e!r}")
            return
        self.accept()

    @staticmethod
    def _extract_cookie(name: str, raw: str) -> str | None:
        """如果用户整行粘贴 Cookie: 头，自动提取 name=value。"""
        if not raw:
            return None
        # 兼容 "session=xxx; csrf_token=yyy" 整行
        for seg in raw.split(";"):
            if "=" not in seg:
                continue
            k, v = seg.split("=", 1)
            if k.strip() == name:
                return v.strip()
        # 兼容 "Cookie: session=xxx; csrf_token=yyy"
        if raw.lower().startswith("cookie:"):
            rest = raw.split(":", 1)[1]
            return _ManualBuffCookieDialog._extract_cookie(name, rest)
        return None
