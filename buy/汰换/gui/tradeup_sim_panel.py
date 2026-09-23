"""本地汰换合同炼金模拟器面板（集成到主软件标签页）。

复用 f:\\steamdt-project\\tradeup_sim 包的核心算法（models/database/engine/history），
仅重写 GUI 层为 QWidget 面板以适配主窗口的 QTabWidget。
"""
from __future__ import annotations

import json
import os
import random
import sys
from typing import List, Optional

# tradeup_sim 包位于项目根目录 f:\steamdt-project\tradeup_sim，
# 主软件运行目录是 f:\steamdt-project\buy\汰换，需把项目根加入 sys.path。
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QProgressBar,
    QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
    QTextEdit, QVBoxLayout, QWidget, QAbstractItemView,
)

from tradeup_sim import database as db
from tradeup_sim import engine
from tradeup_sim import history as hist
from tradeup_sim.models import Skin, Material, get_wear_grade


MATERIAL_COLS = ["皮肤", "收藏品", "品质", "磨损值", "磨损档", "单价(¥)", "StatTrak", "纪念品"]
RESULT_COLS = ["产物皮肤", "收藏品", "品质", "磨损值", "磨损档", "概率", "产出价(¥)", "StatTrak"]


class PriceUpdateWorker(QThread):
    """后台线程：一键更新所有皮肤价格（SteamDT + C5 + ECO）。"""
    progress = Signal(int, int, str)  # current, total, message
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self, use_steamdt=True, use_c5=True, use_eco=True,
                 clear_first=True, sync_to_csv=True, mode="update"):
        super().__init__()
        self.use_steamdt = use_steamdt
        self.use_c5 = use_c5
        self.use_eco = use_eco
        self.clear_first = clear_first
        self.sync_to_csv = sync_to_csv
        self.mode = mode  # "update" = 全量更新, "backfill" = 补查缺失

    def run(self):
        try:
            from tradeup_sim import price_updater
            if self.mode == "backfill":
                result = price_updater.backfill_prices(
                    progress_cb=lambda c, t, m: self.progress.emit(c, t, m),
                    use_steamdt=self.use_steamdt,
                    use_c5=self.use_c5,
                    use_eco=self.use_eco,
                    sync_to_csv=self.sync_to_csv,
                )
            else:
                result = price_updater.update_all_prices(
                    progress_cb=lambda c, t, m: self.progress.emit(c, t, m),
                    use_steamdt=self.use_steamdt,
                    use_c5=self.use_c5,
                    use_eco=self.use_eco,
                    clear_first=self.clear_first,
                    sync_to_csv=self.sync_to_csv,
                )
            self.finished.emit(result)
        except Exception as e:
            self.failed.emit(str(e))


class RecipeOptimizeWorker(QThread):
    """后台线程：查询候选材料实时价格 → 运行配方优化 DP。

    信号：
      progress(int, int)  — 当前进度 / 总数
      status(str)         — 状态文本
      finished(dict)      — 优化结果
      failed(str)         — 错误信息
    """
    progress = Signal(int, int)
    status = Signal(str)
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self, target_skin, target_wear, is_stattrak,
                 use_live_price, platforms, max_candidates=60,
                 strategy="max_profit", target_count=None):
        super().__init__()
        self.target_skin = target_skin
        self.target_wear = target_wear
        self.is_stattrak = is_stattrak
        self.use_live_price = use_live_price
        self.platforms = platforms
        self.max_candidates = max_candidates
        self.strategy = strategy
        self.target_count = target_count

    def run(self):
        try:
            from core import price_fetcher as pf
            import tradeup_sim.steamdt_price as sdp

            # 1. 获取候选（材料皮肤, 磨损档）列表
            self.status.emit("枚举候选材料...")
            options, target_colls, all_colls = engine.prepare_options(
                self.target_skin, self.is_stattrak)
            if not options:
                self.failed.emit("无可用材料候选")
                return

            # ---- 收集需要查询价格的 (皮肤, 磨损档) ----
            # 材料候选
            mat_skin_grades = []
            seen = set()
            for opt in options:
                s = opt["skin"]
                mhash = (opt.get("market_ids") or {}).get(
                    "c5_market_hash_name", "") or s.market_hash_name
                key = (s.name, opt["grade"], self.is_stattrak)
                if key not in seen:
                    seen.add(key)
                    mat_skin_grades.append((s.name, mhash, opt["grade"],
                                            self.is_stattrak))

            # 潜在产物皮肤（目标稀有度，来自所有可能的收藏品）
            # 先确定目标稀有度
            from tradeup_sim.models import QUALITY_LEVEL, QUALITY_ORDER, next_quality
            tgt_q = self.target_skin.quality
            lvl = QUALITY_LEVEL[tgt_q]
            out_quality = next_quality(tgt_q) if lvl > 0 else None
            out_skins = []
            if out_quality:
                out_skins = db.get_skins_by_collections_quality(
                    all_colls, out_quality, self.is_stattrak)

            live_prices = None
            output_prices = None
            if self.use_live_price:
                live_prices = {}
                output_prices = {}

                # ---- SteamDT 批量查询（材料 + 产物一起查） ----
                all_skin_grades = list(mat_skin_grades)
                for s in out_skins:
                    for grade, _, _ in [(g, 0, 0) for g in
                                        ["崭新出厂", "略有磨损", "久经沙场",
                                         "破损不堪", "战痕累累"]]:
                        # 只查该皮肤实际有的磨损档
                        mids = db.get_skin_market_ids(s.name, grade,
                                                      self.is_stattrak)
                        if mids.get("c5_market_hash_name"):
                            all_skin_grades.append((
                                s.name, mids["c5_market_hash_name"],
                                grade, self.is_stattrak))

                self.status.emit("SteamDT 批量查询价格...")
                self.progress.emit(0, 1)
                steamdt_prices = sdp.query_skin_prices(all_skin_grades)
                self.progress.emit(1, 1)

                # 分离材料价和产物价
                mat_keys = set((n, g) for n, _, g, _ in mat_skin_grades)
                for (name, grade), price in steamdt_prices.items():
                    if (name, grade) in mat_keys:
                        live_prices[(name, grade)] = price
                    else:
                        output_prices[(name, grade)] = price

                # ---- eco/c5 回退：SteamDT 未命中的用 eco/c5 补查 ----
                missing_mat = [(n, m, g, st) for n, m, g, st in mat_skin_grades
                               if (n, g) not in live_prices]
                missing_out = [(s, g) for s in out_skins
                               for g in ["崭新出厂", "略有磨损", "久经沙场",
                                         "破损不堪", "战痕累累"]
                               if (s.name, g) not in output_prices
                               and db.get_skin_market_ids(s.name, g,
                                                          self.is_stattrak).get(
                                   "c5_market_hash_name")]

                total_missing = len(missing_mat) + len(missing_out)
                if total_missing > 0:
                    self.status.emit(
                        f"SteamDT 未命中 {total_missing} 个，eco/c5 补查...")
                    done = 0
                    # 补查材料
                    for name, mhash, grade, is_st in missing_mat:
                        item_cfg = {
                            "buff_goods_id": 0,
                            "c5_market_hash_name": mhash,
                            "c5_app_id": 730,
                            "item_name": name,
                        }
                        try:
                            results, _ = pf.query_all(
                                item_cfg, platforms=["c5", "eco"])
                            if results:
                                best = min(results,
                                           key=lambda x: float(x.get("price", 1e9)))
                                live_prices[(name, grade)] = float(best["price"])
                        except Exception:
                            pass
                        done += 1
                        self.progress.emit(done, total_missing)
                    # 补查产物
                    for s, grade in missing_out:
                        mids = db.get_skin_market_ids(s.name, grade,
                                                      self.is_stattrak)
                        item_cfg = {
                            "buff_goods_id": 0,
                            "c5_market_hash_name": mids.get("c5_market_hash_name", ""),
                            "c5_app_id": 730,
                            "item_name": s.name,
                        }
                        try:
                            results, _ = pf.query_all(
                                item_cfg, platforms=["c5", "eco"])
                            if results:
                                best = min(results,
                                           key=lambda x: float(x.get("price", 1e9)))
                                output_prices[(s.name, grade)] = float(
                                    best["price"])
                        except Exception:
                            pass
                        done += 1
                        self.progress.emit(done, total_missing)

                self.status.emit(
                    f"价格查询完成：材料 {len(live_prices)}/{len(mat_skin_grades)}, "
                    f"产物 {len(output_prices)}")

            # 3. 运行优化
            self.status.emit("计算最优配方（DP + EV）...")
            result = engine.optimize_recipe(
                self.target_skin, self.target_wear,
                is_stattrak=self.is_stattrak,
                max_candidates=self.max_candidates,
                live_prices=live_prices,
                output_prices=output_prices,
                strategy=self.strategy,
                target_count=self.target_count)
            result["used_live_price"] = self.use_live_price
            self.finished.emit(result)
        except ValueError as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(f"内部错误: {e}")


class TradeupSimPanel(QWidget):
    """汰换合同模拟器面板（QWidget，可直接加入 QTabWidget）。"""

    def __init__(self):
        super().__init__()
        db.ensure_data()
        self._materials: List[Material] = []
        self._last_result_rows: Optional[List[dict]] = None
        self._last_dist: Optional[dict] = None
        self._opt_worker: Optional[RecipeOptimizeWorker] = None
        self._build_ui()

    # ---------- UI ----------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        # 顶部工具栏：一键更新价格 + 补查
        toolbar = QWidget()
        toolbar.setMinimumHeight(40)
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(0, 0, 0, 0)
        self.btn_update_prices = QPushButton("🔄 一键更新价格")
        self.btn_update_prices.setToolTip(
            "清空旧价格，用 SteamDT+C5+ECO 重新查询所有皮肤价格")
        self.btn_update_prices.setMinimumHeight(32)
        self.btn_update_prices.clicked.connect(self._on_update_prices)
        tb_layout.addWidget(self.btn_update_prices)

        self.btn_backfill_prices = QPushButton("🔁 补查缺失价格")
        self.btn_backfill_prices.setToolTip(
            "只查询当前没有价格的皮肤，不清空已有价格")
        self.btn_backfill_prices.setMinimumHeight(32)
        self.btn_backfill_prices.clicked.connect(self._on_backfill_prices)
        tb_layout.addWidget(self.btn_backfill_prices)

        self.lbl_price_status = QLabel("")
        self.lbl_price_status.setStyleSheet("color: #666;")
        tb_layout.addWidget(self.lbl_price_status, 1)
        self.progress_prices = QProgressBar()
        self.progress_prices.setVisible(False)
        self.progress_prices.setMaximumWidth(300)
        self.progress_prices.setMinimumHeight(20)
        tb_layout.addWidget(self.progress_prices)
        root.addWidget(toolbar)

        tabs = QTabWidget()
        tabs.addTab(self._build_sim_tab(), "汰换模拟")
        tabs.addTab(self._build_optimizer_tab(), "配方优化")
        tabs.addTab(self._build_history_tab(), "历史记录")
        root.addWidget(tabs, 1)

    def _build_sim_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._build_material_panel())
        split.addWidget(self._build_result_panel())
        split.setSizes([640, 640])
        layout.addWidget(split, 1)
        return w

    def _build_material_panel(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        grp = QGroupBox("选择材料皮肤")
        g = QGridLayout(grp)
        g.addWidget(QLabel("搜索:"), 0, 0)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("输入皮肤名关键词...")
        self.search_edit.returnPressed.connect(self._do_search)
        g.addWidget(self.search_edit, 0, 1)
        g.addWidget(QLabel("品质:"), 0, 2)
        self.quality_combo = QComboBox()
        self.quality_combo.addItem("全部", None)
        for q in db.list_qualities():
            self.quality_combo.addItem(q, q)
        g.addWidget(self.quality_combo, 0, 3)
        self.chk_st = QCheckBox("StatTrak")
        g.addWidget(self.chk_st, 0, 4)
        self.btn_search = QPushButton("搜索")
        self.btn_search.clicked.connect(self._do_search)
        g.addWidget(self.btn_search, 0, 5)
        v.addWidget(grp)

        self.search_table = QTableWidget(0, 4)
        self.search_table.setHorizontalHeaderLabels(
            ["皮肤", "收藏品", "品质", "磨损区间"])
        self.search_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.search_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.search_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        v.addWidget(self.search_table, 1)

        add_grp = QGroupBox("添加为材料")
        ag = QGridLayout(add_grp)
        ag.addWidget(QLabel("磨损值:"), 0, 0)
        self.mat_wear = QDoubleSpinBox()
        self.mat_wear.setRange(0.0, 1.0)
        self.mat_wear.setDecimals(6)
        self.mat_wear.setSingleStep(0.001)
        self.mat_wear.setValue(0.15)
        ag.addWidget(self.mat_wear, 0, 1)
        ag.addWidget(QLabel("单价(¥):"), 0, 2)
        self.mat_price = QDoubleSpinBox()
        self.mat_price.setRange(0.0, 1_000_000.0)
        self.mat_price.setDecimals(2)
        self.mat_price.setSingleStep(0.5)
        self.mat_price.setValue(1.0)
        ag.addWidget(self.mat_price, 0, 3)
        self.chk_mat_st = QCheckBox("StatTrak")
        ag.addWidget(self.chk_mat_st, 0, 4)
        self.chk_mat_souv = QCheckBox("纪念品")
        ag.addWidget(self.chk_mat_souv, 0, 5)
        self.btn_add_mat = QPushButton("➕ 添加到合同")
        self.btn_add_mat.clicked.connect(self._add_material)
        ag.addWidget(self.btn_add_mat, 0, 6)
        v.addWidget(add_grp)

        mat_grp = QGroupBox("合同材料（点击行删除）")
        mg = QVBoxLayout(mat_grp)
        self.mat_table = QTableWidget(0, len(MATERIAL_COLS))
        self.mat_table.setHorizontalHeaderLabels(MATERIAL_COLS)
        self.mat_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.mat_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.mat_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.mat_table.cellClicked.connect(self._on_mat_clicked)
        mg.addWidget(self.mat_table)
        self.mat_count_label = QLabel("材料数: 0 / 10")
        mg.addWidget(self.mat_count_label)
        v.addWidget(mat_grp, 1)
        return w

    def _build_result_panel(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        ctrl = QGroupBox("模拟控制")
        g = QGridLayout(ctrl)
        g.addWidget(QLabel("随机种子:"), 0, 0)
        self.seed_spin = QSpinBox()
        self.seed_spin.setRange(0, 2_000_000_000)
        self.seed_spin.setValue(42)
        self.chk_random_seed = QCheckBox("随机")
        self.chk_random_seed.setChecked(True)
        g.addWidget(self.seed_spin, 0, 1)
        g.addWidget(self.chk_random_seed, 0, 2)

        g.addWidget(QLabel("批量次数:"), 0, 3)
        self.batch_spin = QSpinBox()
        self.batch_spin.setRange(1, 1_000_000)
        self.batch_spin.setValue(1000)
        g.addWidget(self.batch_spin, 0, 4)

        self.chk_knife = QCheckBox("隐秘升刀（5件隐秘→刀/手套）")
        self.chk_knife.setChecked(True)
        g.addWidget(self.chk_knife, 1, 0, 1, 3)

        self.btn_single = QPushButton("🎲 单次模拟")
        self.btn_single.clicked.connect(self._on_single)
        g.addWidget(self.btn_single, 1, 3)
        self.btn_batch = QPushButton("📊 批量模拟")
        self.btn_batch.clicked.connect(self._on_batch)
        g.addWidget(self.btn_batch, 1, 4)
        self.btn_dist = QPushButton("📋 概率分布")
        self.btn_dist.clicked.connect(self._on_dist)
        g.addWidget(self.btn_dist, 1, 5)
        v.addWidget(ctrl)

        self.summary = QTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setMaximumHeight(140)
        v.addWidget(self.summary)

        self.result_table = QTableWidget(0, len(RESULT_COLS))
        self.result_table.setHorizontalHeaderLabels(RESULT_COLS)
        self.result_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.result_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        v.addWidget(self.result_table, 1)

        exp_row = QHBoxLayout()
        self.btn_export_csv = QPushButton("导出 CSV")
        self.btn_export_csv.clicked.connect(self._export_csv)
        self.btn_export_json = QPushButton("导出 JSON")
        self.btn_export_json.clicked.connect(self._export_json)
        exp_row.addStretch()
        exp_row.addWidget(self.btn_export_csv)
        exp_row.addWidget(self.btn_export_json)
        v.addLayout(exp_row)
        return w

    # ---------- 配方优化 ----------
    def _build_optimizer_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        # 目标选择区
        grp = QGroupBox("目标产物")
        g = QGridLayout(grp)
        g.addWidget(QLabel("搜索目标皮肤:"), 0, 0)
        self.opt_search = QLineEdit()
        self.opt_search.setPlaceholderText("输入皮肤名...")
        self.opt_search.returnPressed.connect(self._opt_do_search)
        g.addWidget(self.opt_search, 0, 1)
        g.addWidget(QLabel("品质:"), 0, 2)
        self.opt_quality = QComboBox()
        self.opt_quality.addItem("全部", None)
        for q in db.list_qualities():
            self.opt_quality.addItem(q, q)
        g.addWidget(self.opt_quality, 0, 3)
        self.opt_chk_st = QCheckBox("StatTrak")
        g.addWidget(self.opt_chk_st, 0, 4)
        self.opt_btn_search = QPushButton("搜索")
        self.opt_btn_search.clicked.connect(self._opt_do_search)
        g.addWidget(self.opt_btn_search, 0, 5)

        g.addWidget(QLabel("目标磨损值:"), 1, 0)
        self.opt_wear = QDoubleSpinBox()
        self.opt_wear.setRange(0.0, 1.0)
        self.opt_wear.setDecimals(6)
        self.opt_wear.setSingleStep(0.001)
        self.opt_wear.setValue(0.15)
        g.addWidget(self.opt_wear, 1, 1)
        self.opt_chk_mid = QCheckBox("使用区间中点")
        self.opt_chk_mid.setChecked(True)
        g.addWidget(self.opt_chk_mid, 1, 2)
        self.opt_chk_live = QCheckBox("实时价格")
        self.opt_chk_live.setChecked(True)
        g.addWidget(self.opt_chk_live, 1, 3)
        g.addWidget(QLabel("平台:"), 1, 4)
        self.opt_platforms = QComboBox()
        self.opt_platforms.addItem("SteamDT（推荐）", "steamdt")
        self.opt_platforms.addItem("C5+ECO", ["c5", "eco"])
        self.opt_platforms.addItem("仅 C5", ["c5"])
        self.opt_platforms.addItem("仅 ECO", ["eco"])
        self.opt_platforms.addItem("全部", ["buff", "c5", "eco"])
        g.addWidget(self.opt_platforms, 1, 5)
        g.addWidget(QLabel("策略:"), 2, 0)
        self.opt_strategy = QComboBox()
        self.opt_strategy.addItem("最大利润 (EV-成本)", "max_profit")
        self.opt_strategy.addItem("最低成本", "min_cost")
        self.opt_strategy.addItem("最大 EV/成本", "max_ev_ratio")
        g.addWidget(self.opt_strategy, 2, 1, 1, 2)
        g.addWidget(QLabel("主料数:"), 2, 3)
        self.opt_target_count = QComboBox()
        self.opt_target_count.addItem("不限", None)
        for x in range(1, 10):
            self.opt_target_count.addItem(f"{x}打{10 - x}", x)
        g.addWidget(self.opt_target_count, 2, 4)
        self.opt_btn_optimize = QPushButton("🎯 计算最优配方")
        self.opt_btn_optimize.clicked.connect(self._on_optimize)
        g.addWidget(self.opt_btn_optimize, 2, 5)
        self.opt_progress = QProgressBar()
        self.opt_progress.setVisible(False)
        g.addWidget(self.opt_progress, 3, 0, 1, 6)
        v.addWidget(grp)

        # 目标搜索结果
        self.opt_search_table = QTableWidget(0, 4)
        self.opt_search_table.setHorizontalHeaderLabels(
            ["皮肤", "收藏品", "品质", "磨损区间"])
        self.opt_search_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.opt_search_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.opt_search_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.opt_search_table.setMaximumHeight(160)
        v.addWidget(self.opt_search_table)

        # 优化结果摘要
        self.opt_summary = QTextEdit()
        self.opt_summary.setReadOnly(True)
        self.opt_summary.setMaximumHeight(120)
        v.addWidget(self.opt_summary)

        # 操作按钮行
        btn_row = QHBoxLayout()
        self.opt_btn_export = QPushButton("💾 导出到汰换库")
        self.opt_btn_export.setEnabled(False)
        self.opt_btn_export.clicked.connect(self._on_export_recipe)
        btn_row.addWidget(self.opt_btn_export)
        btn_row.addStretch()
        v.addLayout(btn_row)

        # 配方材料表
        self.opt_mat_table = QTableWidget(0, 6)
        self.opt_mat_table.setHorizontalHeaderLabels(
            ["皮肤", "收藏品", "磨损档", "磨损值", "单价(¥)", "小计(¥)"])
        self.opt_mat_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.opt_mat_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.opt_mat_table.setMaximumHeight(200)
        v.addWidget(self.opt_mat_table)

        # 输出池表
        v.addWidget(QLabel("📋 输出池（所有可能产物）"))
        self.opt_out_table = QTableWidget(0, 5)
        self.opt_out_table.setHorizontalHeaderLabels(
            ["产物皮肤", "收藏品", "预估磨损档", "价格(¥)", "概率"])
        self.opt_out_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.opt_out_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        v.addWidget(self.opt_out_table, 1)
        return w

    def _opt_do_search(self):
        kw = self.opt_search.text().strip()
        if not kw:
            QMessageBox.information(self, "提示", "请输入目标皮肤关键词")
            return
        q = self.opt_quality.currentData()
        skins = db.search_skins(kw, quality=q,
                                is_stattrak=self.opt_chk_st.isChecked())
        self.opt_search_table.setRowCount(len(skins))
        for r, s in enumerate(skins):
            self.opt_search_table.setItem(r, 0, QTableWidgetItem(s.name))
            self.opt_search_table.setItem(r, 1, QTableWidgetItem(s.collection))
            self.opt_search_table.setItem(r, 2, QTableWidgetItem(s.quality))
            self.opt_search_table.setItem(r, 3, QTableWidgetItem(
                f"{s.min_float:.4f} ~ {s.max_float:.4f}"))
            self.opt_search_table.item(r, 0).setData(Qt.ItemDataRole.UserRole, s)

    # ---------- 一键更新价格 ----------
    def _on_update_prices(self):
        reply = QMessageBox.question(
            self, "确认",
            "将清空所有旧价格，然后用 SteamDT + C5 + ECO 重新查询。\n"
            "查询完成后会自动同步到主 CSV 的 price_buff 列（自动备份原文件）。\n"
            "此过程可能需要 2-3 分钟，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.btn_update_prices.setEnabled(False)
        self.progress_prices.setVisible(True)
        self.progress_prices.setValue(0)
        self.lbl_price_status.setText("正在更新价格...")

        self._price_worker = PriceUpdateWorker(
            use_steamdt=True, use_c5=True, use_eco=True,
            clear_first=True, sync_to_csv=True)
        self._price_worker.progress.connect(self._on_price_progress)
        self._price_worker.finished.connect(self._on_price_finished)
        self._price_worker.failed.connect(self._on_price_failed)
        self._price_worker.start()

    def _on_price_progress(self, current, total, message):
        self.lbl_price_status.setText(message)
        if total > 0:
            self.progress_prices.setMaximum(total)
            self.progress_prices.setValue(current)

    def _on_price_finished(self, result):
        self.btn_update_prices.setEnabled(True)
        self.btn_backfill_prices.setEnabled(True)
        self.progress_prices.setVisible(False)
        self.lbl_price_status.setText(
            f"价格更新完成：共 {result['total']} 条，"
            f"C5 {result['updated_c5']}，"
            f"ECO {result['updated_eco']}，"
            f"SteamDT {result['updated_steamdt']}，"
            f"CSV同步 {result.get('csv_synced', 0)}，"
            f"失败 {result['failed']}，"
            f"耗时 {result['elapsed']}s")

    def _on_price_failed(self, msg):
        self.btn_update_prices.setEnabled(True)
        self.btn_backfill_prices.setEnabled(True)
        self.progress_prices.setVisible(False)
        self.lbl_price_status.setText(f"更新失败：{msg}")

    def _on_backfill_prices(self):
        self.btn_update_prices.setEnabled(False)
        self.btn_backfill_prices.setEnabled(False)
        self.progress_prices.setVisible(True)
        self.progress_prices.setValue(0)
        self.lbl_price_status.setText("正在补查缺失价格...")

        self._price_worker = PriceUpdateWorker(
            use_steamdt=True, use_c5=True, use_eco=True,
            clear_first=False, sync_to_csv=True, mode="backfill")
        self._price_worker.progress.connect(self._on_price_progress)
        self._price_worker.finished.connect(self._on_backfill_finished)
        self._price_worker.failed.connect(self._on_price_failed)
        self._price_worker.start()

    def _on_backfill_finished(self, result):
        self.btn_update_prices.setEnabled(True)
        self.btn_backfill_prices.setEnabled(True)
        self.progress_prices.setVisible(False)
        self.lbl_price_status.setText(
            f"补查完成：共 {result['total']} 条，"
            f"C5 {result['updated_c5']}，"
            f"ECO {result['updated_eco']}，"
            f"SteamDT {result['updated_steamdt']}，"
            f"CSV同步 {result.get('csv_synced', 0)}，"
            f"失败 {result['failed']}，"
            f"耗时 {result['elapsed']}s")

    def _on_optimize(self):
        row = self.opt_search_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "提示", "请先搜索并选择一个目标皮肤")
            return
        target: Skin = self.opt_search_table.item(row, 0).data(
            Qt.ItemDataRole.UserRole)
        if target is None:
            return
        if self.opt_chk_mid.isChecked():
            target_wear = (target.min_float + target.max_float) / 2.0
        else:
            target_wear = self.opt_wear.value()
        is_st = self.opt_chk_st.isChecked() or target.is_stattrak
        target.is_stattrak = is_st

        use_live = self.opt_chk_live.isChecked()
        platforms = self.opt_platforms.currentData()
        strategy = self.opt_strategy.currentData()
        target_count = self.opt_target_count.currentData()

        # 启动后台 worker
        self.opt_btn_optimize.setEnabled(False)
        self.opt_progress.setVisible(use_live)
        self.opt_progress.setValue(0)
        self.opt_summary.setHtml("<b>正在计算...</b>")

        self._opt_worker = RecipeOptimizeWorker(
            target, target_wear, is_st, use_live, platforms,
            strategy=strategy, target_count=target_count)
        self._opt_worker.progress.connect(self._on_opt_progress)
        self._opt_worker.status.connect(
            lambda s: self.opt_summary.setHtml(f"<b>{s}</b>"))
        self._opt_worker.finished.connect(self._on_opt_finished)
        self._opt_worker.failed.connect(self._on_opt_failed)
        self._opt_worker.start()

    def _on_opt_progress(self, cur, total):
        self.opt_progress.setMaximum(total)
        self.opt_progress.setValue(cur)
        self.opt_summary.setHtml(
            f"<b>查询实时价格... {cur}/{total}</b>")

    def _on_opt_finished(self, result):
        self.opt_btn_optimize.setEnabled(True)
        self.opt_progress.setVisible(False)
        self._last_opt_result = result
        self.opt_btn_export.setEnabled(True)
        target = result["target_skin"]
        used = "实时价" if result.get("used_live_price") else "静态价"
        ev = result.get("theoretical_ev", 0)
        profit = result.get("profit", 0)
        roi = result.get("roi_pct", 0)
        ber = result.get("break_even_rate", 0)
        profit_color = "green" if profit >= 0 else "red"
        self.opt_summary.setHtml(
            f"<b>最优配方（{used}，策略: {result.get('strategy', '')}）</b><br>"
            f"目标: {target.name} ({target.quality}) | "
            f"目标磨损: {result['target_wear']:.6f} "
            f"({get_wear_grade(result['target_wear'])})<br>"
            f"预测产出磨损: {result['predicted_wear']:.6f} "
            f"({result['predicted_wear_grade']}) | "
            f"误差: {result['wear_error']:.6f}<br>"
            f"<b>材料总成本: ¥{result['material_cost']:.2f}</b> | "
            f"目标概率: {result['probability']*100:.2f}% "
            f"(输出池 {result['output_pool_size']} 个)<br>"
            f"<b>期望价值 EV: ¥{ev:.2f}</b> | "
            f"<b style='color:{profit_color}'>利润: ¥{profit:.2f} "
            f"(ROI {roi:+.1f}%)</b> | "
            f"保本率: {ber*100:.1f}%")
        mats = result["materials"]
        agg = {}
        for m in mats:
            key = (m.skin.name, m.skin.collection, get_wear_grade(m.wear))
            if key not in agg:
                agg[key] = {"count": 0, "wear": m.wear, "price": m.price}
            agg[key]["count"] += 1
        rows = sorted(agg.items(), key=lambda x: x[1]["count"], reverse=True)
        self.opt_mat_table.setRowCount(len(rows))
        for r, ((name, coll, grade), v) in enumerate(rows):
            self.opt_mat_table.setItem(r, 0, QTableWidgetItem(name))
            self.opt_mat_table.setItem(r, 1, QTableWidgetItem(coll))
            self.opt_mat_table.setItem(r, 2, QTableWidgetItem(grade))
            self.opt_mat_table.setItem(r, 3, QTableWidgetItem(f"{v['wear']:.6f}"))
            self.opt_mat_table.setItem(r, 4, QTableWidgetItem(f"¥{v['price']:.2f}"))
            self.opt_mat_table.setItem(r, 5, QTableWidgetItem(
                f"¥{v['price']*v['count']:.2f} (x{v['count']})"))

        # 输出池表
        out_rows = result.get("output_rows", [])
        self.opt_out_table.setRowCount(len(out_rows))
        for r, row in enumerate(out_rows):
            s = row["skin"]
            is_tgt = s.name == target.name
            item_name = QTableWidgetItem(
                f"{'★ ' if is_tgt else ''}{s.name}")
            if is_tgt:
                from PyQt6.QtGui import QColor, QBrush
                item_name.setBackground(QBrush(QColor(255, 255, 200)))
            self.opt_out_table.setItem(r, 0, item_name)
            self.opt_out_table.setItem(r, 1, QTableWidgetItem(s.collection))
            self.opt_out_table.setItem(r, 2, QTableWidgetItem(row["wear_grade"]))
            self.opt_out_table.setItem(r, 3, QTableWidgetItem(
                f"¥{row['price']:.2f}"))
            self.opt_out_table.setItem(r, 4, QTableWidgetItem(
                f"{row['probability']*100:.2f}%"))

    def _on_opt_failed(self, msg):
        self.opt_btn_optimize.setEnabled(True)
        self.opt_progress.setVisible(False)
        self.opt_summary.setHtml(f"<b style='color:red'>失败: {msg}</b>")
        QMessageBox.warning(self, "优化失败", msg)

    def _on_export_recipe(self):
        result = getattr(self, "_last_opt_result", None)
        if not result:
            QMessageBox.information(self, "提示", "没有可导出的配方")
            return
        target = result["target_skin"]
        mats = result["materials"]
        from tradeup_sim.engine import export_recipe_to_taihuan
        try:
            plan_id = export_recipe_to_taihuan(mats, target)
            QMessageBox.information(
                self, "导出成功",
                f"配方已导出到自动汰换库！\n配方编号: #{plan_id}\n"
                f"请前往「自动汰换」标签页查看。")
        except Exception as e:
            QMessageBox.warning(self, "导出失败", f"{e}")

    def _build_history_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        top = QHBoxLayout()
        self.btn_refresh_hist = QPushButton("刷新")
        self.btn_refresh_hist.clicked.connect(self._refresh_history)
        self.btn_clear_hist = QPushButton("清空")
        self.btn_clear_hist.clicked.connect(self._clear_history)
        self.btn_export_hist_csv = QPushButton("导出 CSV")
        self.btn_export_hist_csv.clicked.connect(self._export_hist_csv)
        top.addWidget(self.btn_refresh_hist)
        top.addWidget(self.btn_clear_hist)
        top.addStretch()
        top.addWidget(self.btn_export_hist_csv)
        v.addLayout(top)

        self.hist_table = QTableWidget(0, 8)
        self.hist_table.setHorizontalHeaderLabels(
            ["ID", "时间", "种子", "次数", "产物", "磨损", "成本(¥)", "盈亏(¥)"])
        self.hist_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.hist_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        v.addWidget(self.hist_table, 1)
        self._refresh_history()
        return w

    # ---------- 搜索 ----------
    def _do_search(self):
        kw = self.search_edit.text().strip()
        if not kw:
            QMessageBox.information(self, "提示", "请输入搜索关键词")
            return
        q = self.quality_combo.currentData()
        skins = db.search_skins(kw, quality=q,
                                is_stattrak=self.chk_st.isChecked())
        self.search_table.setRowCount(len(skins))
        for r, s in enumerate(skins):
            self.search_table.setItem(r, 0, QTableWidgetItem(s.name))
            self.search_table.setItem(r, 1, QTableWidgetItem(s.collection))
            self.search_table.setItem(r, 2, QTableWidgetItem(s.quality))
            self.search_table.setItem(r, 3, QTableWidgetItem(
                f"{s.min_float:.4f} ~ {s.max_float:.4f}"))
            self.search_table.item(r, 0).setData(Qt.ItemDataRole.UserRole, s)

    # ---------- 材料管理 ----------
    def _add_material(self):
        row = self.search_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "提示", "请先在搜索结果中选择一个皮肤")
            return
        skin: Skin = self.search_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        if skin is None:
            return
        wear = self.mat_wear.value()
        if not (skin.min_float <= wear <= skin.max_float):
            QMessageBox.warning(
                self, "磨损超界",
                f"{skin.name} 的磨损区间是 "
                f"[{skin.min_float:.4f}, {skin.max_float:.4f}]，"
                f"输入 {wear:.6f} 超出范围。\n"
                "（仍可添加，模拟时按区间边界归一化）")
        price = self.mat_price.value()
        is_st = self.chk_mat_st.isChecked() or skin.is_stattrak
        is_souv = self.chk_mat_souv.isChecked()
        new_skin = Skin(**{**skin.to_dict(), "is_stattrak": is_st})
        mat = Material(skin=new_skin, wear=wear, price=price, is_souvenir=is_souv)
        self._materials.append(mat)
        self._refresh_mat_table()

    def _refresh_mat_table(self):
        self.mat_table.setRowCount(len(self._materials))
        for r, m in enumerate(self._materials):
            self.mat_table.setItem(r, 0, QTableWidgetItem(m.skin.name))
            self.mat_table.setItem(r, 1, QTableWidgetItem(m.skin.collection))
            self.mat_table.setItem(r, 2, QTableWidgetItem(m.skin.quality))
            self.mat_table.setItem(r, 3, QTableWidgetItem(f"{m.wear:.6f}"))
            self.mat_table.setItem(r, 4, QTableWidgetItem(get_wear_grade(m.wear)))
            self.mat_table.setItem(r, 5, QTableWidgetItem(f"{m.price:.2f}"))
            self.mat_table.setItem(r, 6, QTableWidgetItem("是" if m.is_stattrak else "否"))
            self.mat_table.setItem(r, 7, QTableWidgetItem("是" if m.is_souvenir else "否"))
        n = len(self._materials)
        need = 5 if (n > 0 and self._materials[0].skin.quality == "隐秘级"
                     and self.chk_knife.isChecked()) else 10
        self.mat_count_label.setText(f"材料数: {n} / {need}")

    def _on_mat_clicked(self, row, _col):
        if 0 <= row < len(self._materials):
            del self._materials[row]
            self._refresh_mat_table()

    # ---------- 模拟 ----------
    def _get_seed(self) -> Optional[int]:
        if self.chk_random_seed.isChecked():
            return random.randint(0, 2_000_000_000)
        return self.seed_spin.value()

    def _on_single(self):
        try:
            res = engine.simulate(
                self._materials, seed=self._get_seed(),
                allow_covert_knife=self.chk_knife.isChecked())
        except ValueError as e:
            QMessageBox.warning(self, "材料错误", str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "模拟失败", str(e))
            return
        self._show_single_result(res)
        mat_dicts = [m.to_dict() for m in self._materials]
        hist.save_history(mat_dicts, res.to_dict(), seed=res.seed, batch_size=1)

    def _show_single_result(self, res):
        self.summary.setHtml(self._summary_html([res], batch=False))
        self.result_table.setRowCount(1)
        self._fill_result_row(0, res)
        self._last_result_rows = [res.to_dict()]
        self._last_dist = None

    def _on_batch(self):
        times = self.batch_spin.value()
        try:
            out = engine.simulate_batch(
                self._materials, times=times, base_seed=self._get_seed(),
                allow_covert_knife=self.chk_knife.isChecked())
        except ValueError as e:
            QMessageBox.warning(self, "材料错误", str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "模拟失败", str(e))
            return
        results = out["results"]
        summary = out["summary"]
        self.summary.setHtml(self._summary_html(results, batch=True, summary=summary))
        agg = summary["per_output"]
        rows = sorted(agg.values(), key=lambda x: x["count"], reverse=True)
        self.result_table.setRowCount(len(rows))
        for r, v in enumerate(rows):
            s = v["skin"]
            self.result_table.setItem(r, 0, QTableWidgetItem(s.name))
            self.result_table.setItem(r, 1, QTableWidgetItem(s.collection))
            self.result_table.setItem(r, 2, QTableWidgetItem(s.quality))
            self.result_table.setItem(r, 3, QTableWidgetItem(f"{v['avg_wear']:.6f}"))
            self.result_table.setItem(r, 4, QTableWidgetItem(
                get_wear_grade(v["avg_wear"])))
            self.result_table.setItem(r, 5, QTableWidgetItem(
                f"{v['prob']*100:.2f}% ({v['count']}次)"))
            self.result_table.setItem(r, 6, QTableWidgetItem(f"{v['avg_price']:.2f}"))
            self.result_table.setItem(r, 7, QTableWidgetItem(
                "是" if v["is_stattrak"] else "否"))
        self._last_result_rows = [r.to_dict() for r in results]
        self._last_dist = None
        mat_dicts = [m.to_dict() for m in self._materials]
        first = results[0].to_dict()
        first["_batch_summary"] = {
            "times": times, "ev": summary["ev"],
            "profit": summary["profit"], "roi_pct": summary["roi_pct"],
        }
        hist.save_history(mat_dicts, first, seed=results[0].seed, batch_size=times)

    def _on_dist(self):
        try:
            dist = engine.get_probability_distribution(
                self._materials, allow_covert_knife=self.chk_knife.isChecked())
        except ValueError as e:
            QMessageBox.warning(self, "材料错误", str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "失败", str(e))
            return
        self.summary.setHtml(
            f"<b>精确概率分布</b><br>"
            f"产出稀有度: {dist['output_quality']} | "
            f"输出池大小: {dist['pool_size']} | "
            f"材料成本: ¥{dist['material_cost']:.2f}<br>"
            f"理论 EV: ¥{dist['theoretical_ev']:.2f} | "
            f"盈亏: ¥{dist['profit']:.2f} | "
            f"ROI: {dist['roi_pct']:.2f}%")
        rows = dist["rows"]
        self.result_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            s = row["skin"]
            self.result_table.setItem(r, 0, QTableWidgetItem(s.name))
            self.result_table.setItem(r, 1, QTableWidgetItem(s.collection))
            self.result_table.setItem(r, 2, QTableWidgetItem(s.quality))
            self.result_table.setItem(r, 3, QTableWidgetItem(f"{row['est_wear']:.6f}"))
            self.result_table.setItem(r, 4, QTableWidgetItem(row["wear_grade"]))
            self.result_table.setItem(r, 5, QTableWidgetItem(
                f"{row['probability']*100:.2f}%"))
            self.result_table.setItem(r, 6, QTableWidgetItem(f"{row['price']:.2f}"))
            self.result_table.setItem(r, 7, QTableWidgetItem(
                "是" if row["is_stattrak"] else "否"))
        self._last_dist = dist
        self._last_result_rows = None

    def _fill_result_row(self, r, res):
        s = res.output_skin
        self.result_table.setItem(r, 0, QTableWidgetItem(s.name))
        self.result_table.setItem(r, 1, QTableWidgetItem(s.collection))
        self.result_table.setItem(r, 2, QTableWidgetItem(s.quality))
        self.result_table.setItem(r, 3, QTableWidgetItem(f"{res.output_wear:.6f}"))
        self.result_table.setItem(r, 4, QTableWidgetItem(
            get_wear_grade(res.output_wear)))
        self.result_table.setItem(r, 5, QTableWidgetItem(
            f"{res.probability*100:.2f}%"))
        self.result_table.setItem(r, 6, QTableWidgetItem(f"{res.output_price:.2f}"))
        self.result_table.setItem(r, 7, QTableWidgetItem(
            "是" if res.is_stattrak else "否"))

    def _summary_html(self, results, batch=False, summary=None) -> str:
        if not results:
            return "无结果"
        cost = results[0].material_cost
        if batch and summary:
            ev = summary["ev"]
            profit = summary["profit"]
            roi = summary["roi_pct"]
            times = summary["times"]
            color = "green" if profit >= 0 else "red"
            return (
                f"<b>批量模拟结果（{times} 次）</b><br>"
                f"材料成本: ¥{cost:.2f} | EV: ¥{ev:.2f} | "
                f"<span style='color:{color}'>盈亏: ¥{profit:.2f} (ROI {roi:.2f}%)</span>")
        r = results[0]
        color = "green" if r.profit >= 0 else "red"
        return (
            f"<b>单次模拟结果</b>（种子 {r.seed}）<br>"
            f"产物: {r.output_skin.name} ({r.output_skin.quality}) "
            f"磨损 {r.output_wear:.6f} [{get_wear_grade(r.output_wear)}] "
            f"{'StatTrak' if r.is_stattrak else '普通'}<br>"
            f"概率: {r.probability*100:.2f}% | 成本: ¥{r.material_cost:.2f} | "
            f"产出价: ¥{r.output_price:.2f} | "
            f"<span style='color:{color}'>盈亏: ¥{r.profit:.2f} (ROI {r.roi:.2f}%)</span>")

    # ---------- 导出 ----------
    def _export_csv(self):
        if not self._last_result_rows and not self._last_dist:
            QMessageBox.information(self, "提示", "没有可导出的结果")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 CSV", "tradeup_result.csv", "CSV (*.csv)")
        if not path:
            return
        if self._last_dist:
            hist.export_distribution_csv(self._last_dist, path)
        else:
            import csv as _csv
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                if not self._last_result_rows:
                    return
                w = _csv.DictWriter(f, fieldnames=list(self._last_result_rows[0].keys()))
                w.writeheader()
                w.writerows(self._last_result_rows)
        QMessageBox.information(self, "导出成功", f"已导出到 {path}")

    def _export_json(self):
        if not self._last_result_rows and not self._last_dist:
            QMessageBox.information(self, "提示", "没有可导出的结果")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 JSON", "tradeup_result.json", "JSON (*.json)")
        if not path:
            return
        data = (self._last_dist["rows"] if self._last_dist
                else self._last_result_rows)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        QMessageBox.information(self, "导出成功", f"已导出到 {path}")

    # ---------- 历史 ----------
    def _refresh_history(self):
        rows = hist.list_history()
        self.hist_table.setRowCount(len(rows))
        for r, h in enumerate(rows):
            res = h.get("result", {})
            self.hist_table.setItem(r, 0, QTableWidgetItem(str(h["id"])))
            self.hist_table.setItem(r, 1, QTableWidgetItem(h["ts"]))
            self.hist_table.setItem(r, 2, QTableWidgetItem(str(h.get("seed") or "")))
            self.hist_table.setItem(r, 3, QTableWidgetItem(str(h.get("batch_size", 1))))
            self.hist_table.setItem(r, 4, QTableWidgetItem(res.get("output_skin", "")))
            self.hist_table.setItem(r, 5, QTableWidgetItem(str(res.get("output_wear", ""))))
            self.hist_table.setItem(r, 6, QTableWidgetItem(str(res.get("material_cost", ""))))
            profit = res.get("profit", "")
            item = QTableWidgetItem(str(profit))
            if isinstance(profit, (int, float)):
                item.setForeground(Qt.GlobalColor.green if profit >= 0
                                   else Qt.GlobalColor.red)
            self.hist_table.setItem(r, 7, item)

    def _clear_history(self):
        if QMessageBox.question(self, "确认", "确定清空所有历史记录？") != \
                QMessageBox.StandardButton.Yes:
            return
        n = hist.clear_history()
        QMessageBox.information(self, "已清空", f"删除 {n} 条记录")
        self._refresh_history()

    def _export_hist_csv(self):
        rows = hist.list_history(limit=10000)
        if not rows:
            QMessageBox.information(self, "提示", "无历史记录")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出历史 CSV", "tradeup_history.csv", "CSV (*.csv)")
        if not path:
            return
        hist.export_history_csv(rows, path)
        QMessageBox.information(self, "导出成功", f"已导出 {len(rows)} 条到 {path}")

    def stop_if_running(self):
        """无后台线程，占位保持接口一致。"""
        pass
