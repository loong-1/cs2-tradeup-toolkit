"""CS2 汰换合同模拟器 — PySide6 GUI。"""
from __future__ import annotations

import json
import random
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QLineEdit, QPushButton, QSpinBox, QDoubleSpinBox,
    QCheckBox, QComboBox, QTableWidget, QTableWidgetItem, QTabWidget,
    QHeaderView, QMessageBox, QFileDialog, QGroupBox, QSplitter,
    QAbstractItemView, QTextEdit,
)

from . import database as db
from . import engine
from . import history as hist
from .models import Skin, Material, get_wear_grade, QUALITY_LEVEL


MATERIAL_COLS = ["皮肤", "收藏品", "品质", "磨损值", "磨损档", "单价(¥)", "StatTrak", "纪念品"]
RESULT_COLS = ["产物皮肤", "收藏品", "品质", "磨损值", "磨损档", "概率", "产出价(¥)", "StatTrak"]


class TradeUpWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CS2 汰换合同炼金模拟器")
        self.resize(1280, 800)
        db.ensure_data()
        self._materials: List[Material] = []
        self._build_ui()

    # ---------- UI 构建 ----------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        tabs = QTabWidget()
        tabs.addTab(self._build_sim_tab(), "汰换模拟")
        tabs.addTab(self._build_history_tab(), "历史记录")
        root.addWidget(tabs)

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

        # 搜索区
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

        # 搜索结果表
        self.search_table = QTableWidget(0, 4)
        self.search_table.setHorizontalHeaderLabels(
            ["皮肤", "收藏品", "品质", "磨损区间"])
        self.search_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.search_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.search_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        v.addWidget(self.search_table, 1)

        # 添加材料参数
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

        # 合同材料表
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

        # 控制区
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

        # 摘要区
        self.summary = QTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setMaximumHeight(140)
        v.addWidget(self.summary)

        # 结果表
        self.result_table = QTableWidget(0, len(RESULT_COLS))
        self.result_table.setHorizontalHeaderLabels(RESULT_COLS)
        self.result_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.result_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        v.addWidget(self.result_table, 1)

        # 导出按钮
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
        # 校验磨损在皮肤区间内
        if not (skin.min_float <= wear <= skin.max_float):
            QMessageBox.warning(
                self, "磨损超界",
                f"{skin.name} 的磨损区间是 "
                f"[{skin.min_float:.4f}, {skin.max_float:.4f}]，"
                f"输入 {wear:.6f} 超出范围。\n"
                "（仍可添加，但模拟时会按区间边界归一化）")
        price = self.mat_price.value()
        is_st = self.chk_mat_st.isChecked() or skin.is_stattrak
        is_souv = self.chk_mat_souv.isChecked()
        # 如果选的是 ST 皮肤，强制 is_st=True
        mat = Material(skin=skin, wear=wear, price=price, is_souvenir=is_souv)
        if skin.is_stattrak:
            mat.skin = Skin(**{**skin.to_dict(), "is_stattrak": True})
        else:
            mat.skin = Skin(**{**skin.to_dict(), "is_stattrak": is_st})
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
        # 左键删除材料
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
        # 保存历史
        mat_dicts = [m.to_dict() for m in self._materials]
        hist.save_history(mat_dicts, res.to_dict(), seed=res.seed, batch_size=1)

    def _show_single_result(self, res):
        self.summary.setHtml(self._summary_html([res], batch=False))
        self.result_table.setRowCount(1)
        self._fill_result_row(0, res)
        self._last_result_rows = [res.to_dict()]

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
        # 结果表：按产物聚合（去重显示，附次数）
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
        # 保存历史（存第一条作代表 + 摘要）
        mat_dicts = [m.to_dict() for m in self._materials]
        first = results[0].to_dict()
        first["_batch_summary"] = {
            "times": times,
            "ev": summary["ev"],
            "profit": summary["profit"],
            "roi_pct": summary["roi_pct"],
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
                f"材料成本: ¥{cost:.2f} | "
                f"EV: ¥{ev:.2f} | "
                f"<span style='color:{color}'>盈亏: ¥{profit:.2f} (ROI {roi:.2f}%)</span>")
        r = results[0]
        color = "green" if r.profit >= 0 else "red"
        return (
            f"<b>单次模拟结果</b>（种子 {r.seed}）<br>"
            f"产物: {r.output_skin.name} ({r.output_skin.quality}) "
            f"磨损 {r.output_wear:.6f} [{get_wear_grade(r.output_wear)}] "
            f"{'StatTrak' if r.is_stattrak else '普通'}<br>"
            f"概率: {r.probability*100:.2f}% | "
            f"成本: ¥{r.material_cost:.2f} | "
            f"产出价: ¥{r.output_price:.2f} | "
            f"<span style='color:{color}'>盈亏: ¥{r.profit:.2f} (ROI {r.roi:.2f}%)</span>")

    # ---------- 导出 ----------
    def _export_csv(self):
        if not getattr(self, "_last_result_rows", None) and not getattr(self, "_last_dist", None):
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
        if not getattr(self, "_last_result_rows", None) and not getattr(self, "_last_dist", None):
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
            self.hist_table.setItem(r, 5, QTableWidgetItem(
                str(res.get("output_wear", ""))))
            self.hist_table.setItem(r, 6, QTableWidgetItem(
                str(res.get("material_cost", ""))))
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


def main():
    import sys
    app = QApplication(sys.argv)
    win = TradeUpWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
