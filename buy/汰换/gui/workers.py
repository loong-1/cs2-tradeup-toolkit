"""后台工作线程：查询与购买，避免阻塞 GUI 事件循环。"""
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from PySide6.QtCore import QThread, Signal

from vendor import found_c5

from core.price_fetcher import query_all
from core.buyer import buy_item
from core import price_monitor as pm
from core import query_queue_manager as qqm
from core.data_manager import log_price_monitor
from utils.config import C5_APP_KEY

# 全局购买互斥锁：Buff 网页爬取与 C5/ECO API 两条检测通道可能并发触发
# 同一材料的自动购买，锁内逐件重查剩余需求量，防止两通道合计超买
_AUTO_BUY_LOCK = __import__("threading").Lock()


class QueryWorker(QThread):
    """比价查询线程。"""

    progress = Signal(str)          # 进度文本
    finished = Signal(list, dict)   # (results, errors)
    error = Signal(str)             # 致命错误

    def __init__(self, item_config, wear_min, wear_max,
                 price_min, price_max, platforms):
        super().__init__()
        self.item_config = item_config
        self.wear_min = wear_min
        self.wear_max = wear_max
        self.price_min = price_min
        self.price_max = price_max
        self.platforms = platforms

    def run(self):
        try:
            self.progress.emit("正在并行查询 Buff / C5 ...")
            results, errors = query_all(
                self.item_config,
                wear_min=self.wear_min,
                wear_max=self.wear_max,
                price_min=self.price_min,
                price_max=self.price_max,
                platforms=self.platforms,
                c5_full_list=True,   # GUI 查询购买需要带磨损的完整列表
            )
            self.finished.emit(results, errors)
        except Exception as e:
            self.error.emit(f"查询异常: {e}")


class BuyWorker(QThread):
    """购买线程，逐个购买选中的商品。"""

    progress = Signal(str)                          # 进度文本
    item_done = Signal(int, int, bool, str, str)   # (idx, total, success, msg, order_id)
    finished = Signal(int, int)                    # (success_count, fail_count)

    def __init__(self, orders, item_name):
        super().__init__()
        self.orders = orders          # list of order dict
        self.item_name = item_name

    def run(self):
        total = len(self.orders)
        ok = 0
        fail = 0
        for i, order in enumerate(self.orders, 1):
            self.progress.emit(
                f"正在购买第 {i}/{total} 个 "
                f"({order['platform']} ¥{order['price']:.2f}) ...")
            try:
                success, msg, order_id = buy_item(order, self.item_name)
            except Exception as e:
                success, msg, order_id = False, f"异常: {e}", ""
            if success:
                ok += 1
            else:
                fail += 1
            self.item_done.emit(i, total, success, msg, order_id)
        self.finished.emit(ok, fail)


class DetectIpWorker(QThread):
    """检测 C5 当前请求 IP 的线程（调用 C5 API 并从返回中提取 IP）。"""

    progress = Signal(str)          # 进度文本
    finished = Signal(dict)         # 检测结果 dict
    error = Signal(str)             # 致命错误

    def __init__(self, app_key: str = None):
        super().__init__()
        self.app_key = app_key or C5_APP_KEY

    def run(self):
        try:
            self.progress.emit("正在调用 C5 API 检测当前请求 IP ...")
            result = found_c5.detect_current_ip(self.app_key)
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(f"检测异常: {e}")


class FavoritesQueryWorker(QThread):
    """批量查询收藏夹物品最低价的线程。

    并行查询多个物品的 Buff/C5，对每个物品取最低价 & 最低磨损值。
    """

    progress = Signal(str)                          # 进度文本
    item_done = Signal(dict)                        # 单个物品结果
    finished = Signal(int, int)                     # (成功数, 失败数)

    def __init__(self, items, platforms=None):
        """
        :param items: list of dict，每项含 item_name / buff_goods_id /
                      c5_market_hash_name / c5_app_id，可附带 id（收藏夹记录id）
        :param platforms: list 如 ['buff','c5']，None 表示全部
        """
        super().__init__()
        self.items = items
        self.platforms = platforms

    def run(self):
        total = len(self.items)
        ok = 0
        fail = 0
        if total == 0:
            self.finished.emit(0, 0)
            return

        def _query_one(item):
            try:
                results, errors = query_all(
                    item, platforms=self.platforms)
                if not results:
                    return item, None, (errors or "无在售")
                # 最低价（query_all 已按价格升序排序）
                min_price_item = results[0]
                # 最低磨损
                min_wear_item = min(results, key=lambda x: x.get("wear", 0))
                return item, {
                    "min_price": min_price_item["price"],
                    "min_price_platform": min_price_item["platform"],
                    "min_wear": min_wear_item["wear"],
                    "min_wear_platform": min_wear_item["platform"],
                    "count": len(results),
                    "best": min_price_item,
                }, None
            except Exception as e:
                return item, None, str(e)

        with ThreadPoolExecutor(max_workers=min(5, total)) as pool:
            futures = {pool.submit(_query_one, it): it for it in self.items}
            done_count = 0
            for fut in as_completed(futures):
                item, info, err = fut.result()
                done_count += 1
                if info is not None:
                    ok += 1
                    self.progress.emit(
                        f"已查询 {done_count}/{total} - {item['item_name']}: "
                        f"最低 ¥{info['min_price']:.2f}")
                    self.item_done.emit({
                        "item": item, "info": info, "error": None})
                else:
                    fail += 1
                    self.progress.emit(
                        f"已查询 {done_count}/{total} - {item['item_name']}: {err}")
                    self.item_done.emit({
                        "item": item, "info": None, "error": err})
        self.finished.emit(ok, fail)


class PriceMonitorWorker(QThread):
    """持续价格检测线程：按设定间隔循环查询所有监控目标。

    【2026-09-12 阈值囤货模式】每个目标带磨损区间/价格阈值/需求件数：
    检测到 ≤阈值 的在售时，一轮内把所有达标件（最多补齐到剩余需求量）
    全部买下；已购件数从 monitor_auto_buy_log 统计，防重复购买。
    """

    progress = Signal(str)                          # 进度文本
    item_done = Signal(dict)                        # 单个目标结果
    round_finished = Signal(int, int, str)          # (轮次序号, 耗时秒, 时间戳)
    stopped = Signal()                              # 已停止

    def __init__(self, interval_seconds: float = 150.0,
                 platforms=None, parent=None):
        """
        :param interval_seconds: 每轮检测之间的间隔秒数
        :param platforms: list 如 ['buff','c5']，None 表示全部
        """
        super().__init__(parent)
        self.interval_seconds = max(5.0, float(interval_seconds))
        self.platforms = platforms
        self._stop_flag = False

    def stop(self):
        """请求停止（在下一次 sleep 或本轮结束后生效）。"""
        self._stop_flag = True

    def run(self):
        round_idx = 0
        while not self._stop_flag:
            round_idx += 1
            # 每轮从数据库读取一次最新配置
            auto_buy_cfg = pm.get_auto_buy_config()
            targets = [t for t in pm.list_targets() if t.get("enabled", True)]
            # 已购满需求的目标跳过（省请求）
            active = []
            for t in targets:
                need = t.get("need_count") or 0
                if need <= 0:
                    active.append(t)      # 无需求量：仅监控不购买
                elif pm.remaining_need(t) > 0:
                    active.append(t)
            if not active:
                self.progress.emit(
                    f"[第 {round_idx} 轮] 无待检测目标（全部购满或无目标），"
                    f"等待下一轮...")
                self._sleep_with_check(self.interval_seconds)
                continue

            self.progress.emit(
                f"[第 {round_idx} 轮] 开始检测 {len(active)} 个目标 ...")
            start_ts = time.time()
            ok = 0
            fail = 0

            def _check_one(t):
                return pm.run_single_check(t, platforms=self.platforms)

            with ThreadPoolExecutor(max_workers=min(5, len(active))) as pool:
                futures = {pool.submit(_check_one, t): t for t in active}
                for fut in as_completed(futures):
                    if self._stop_flag:
                        break
                    result = fut.result()
                    target = result["target"]
                    # 计算 ≤阈值 件数（供 GUI 显示 + 购买，截取至剩余需求量）
                    afford = pm.get_affordable_orders(
                        result, target, auto_buy_cfg,
                        limit=pm.remaining_need(target))
                    result["affordable_count"] = len(afford)
                    if result["success"]:
                        ok += 1
                        log_price_monitor(
                            target["item_name"],
                            result["min_price_platform"],
                            result["min_price"],
                            result["min_wear"],
                            sample_count=result["count"],
                            remark=f"自动检测 第{round_idx}轮")
                    else:
                        fail += 1
                        log_price_monitor(
                            target["item_name"], "",
                            None, None,
                            sample_count=0,
                            remark=f"检测失败: {result['error']}")
                    self.item_done.emit(result)
                    # 阈值囤货自动购买（一轮买齐所有 ≤阈值 的，至剩余需求量）
                    if auto_buy_cfg.get("enabled") and result.get("success") \
                            and afford:
                        self._do_auto_buy_batch(target, afford)

            elapsed = time.time() - start_ts
            ts = datetime.now().isoformat(timespec="seconds")
            self.round_finished.emit(round_idx, int(elapsed), ts)
            self.progress.emit(
                f"[第 {round_idx} 轮] 完成: 成功 {ok}, 失败 {fail}, "
                f"耗时 {int(elapsed)}s。等待 {int(self.interval_seconds)}s ...")
            # 间隔等待（可被打断）
            self._sleep_with_check(self.interval_seconds)

        self.stopped.emit()

    def _do_auto_buy_batch(self, target, orders):
        """阈值囤货批量购买：逐件下单（随机间隔防连击），记录日志。

        :param orders: ≤阈值的在售件列表（已按价格升序、截取至剩余需求量）
        """
        target_id = target.get("id")
        if target_id is None:
            return
        item_name = target.get("item_name", "")
        threshold = pm.target_threshold(target, pm.get_auto_buy_config())
        self.progress.emit(
            f"🤖 AUTO-BUY 触发: {item_name} "
            f"{len(orders)} 件 ≤¥{threshold:.2f}，开始批量下单 ...")
        bought_now = 0
        with _AUTO_BUY_LOCK:   # 双通道并发购买互斥
            for order in orders:
                if self._stop_flag:
                    break
                # 逐件重查剩余需求（另一通道可能已买走额度）
                if pm.remaining_need(target) <= 0:
                    self.progress.emit(
                        f"🎯 {item_name} 需求已购满，停止本轮购买。")
                    break
                order = dict(order)
                platform = order.get("platform", "")
                price = float(order.get("price", 0) or 0)
                wear = float(order.get("wear", 0) or 0)
                order_id = order.get("order_id", "")
                try:
                    success, msg, out_order_id = buy_item(order, item_name)
                except Exception as e:
                    success, msg, out_order_id = False, f"异常: {e}", ""
                pm.log_auto_buy(
                    target_id, item_name, platform, out_order_id or order_id,
                    price, wear, success, msg)
                if success:
                    bought_now += 1
                    self.progress.emit(
                        f"✅ AUTO-BUY: {item_name} #{bought_now} "
                        f"¥{price:.2f} ({platform}) {msg}")
                else:
                    self.progress.emit(
                        f"❌ AUTO-BUY: {item_name} ¥{price:.2f} "
                        f"({platform}) {msg}")
                # 购买间随机间隔 1.0-1.5s（防连击风控）
                time.sleep(1.0 + random.random() * 0.5)
        if bought_now:
            total = pm.count_bought(target_id)
            self.progress.emit(
                f"🎯 {item_name} 本轮购入 {bought_now} 件，"
                f"累计 {total}/{target.get('need_count')}。")
            # 重发 item_done 让 GUI 刷新已购列
            self.item_done.emit({
                "target": target, "success": True,
                "affordable_count": 0, "checked_at": "",
            })

    def _sleep_with_check(self, seconds: float):
        """分段 sleep，便于快速响应 stop 请求。"""
        interval = 0.5
        elapsed = 0.0
        while elapsed < seconds and not self._stop_flag:
            self.msleep(int(interval * 1000))
            elapsed += interval


class QueryQueueWorker(QThread):
    """查询列表串行查询线程。

    持续从 query_queue 表中取 pending 状态的物品，串行查询，
    每个物品查完所有在售商品后写入 result_json，再处理下一个。
    队列空时短暂 sleep 后重新检查；stop() 后在当前物品查询结束后退出。
    """

    progress = Signal(str)                       # 进度文本
    item_started = Signal(int, str)              # (id, item_name)
    item_finished = Signal(int, str, int, bool, str)
    # (id, item_name, count, success, msg)
    queue_empty = Signal()                       # 队列为空
    stopped = Signal()                           # 已停止

    IDLE_SLEEP_MS = 2000  # 队列空时检查间隔

    def __init__(self, platforms=None, parent=None):
        """
        :param platforms: list 如 ['buff','c5']，None 表示全部
        """
        super().__init__(parent)
        self.platforms = platforms
        self._stop_flag = False

    def stop(self):
        self._stop_flag = True

    def run(self):
        while not self._stop_flag:
            pending = qqm.list_pending()
            if not pending:
                self.queue_empty.emit()
                # 分段 sleep 以便快速响应 stop
                slept = 0
                while slept < self.IDLE_SLEEP_MS and not self._stop_flag:
                    self.msleep(100)
                    slept += 100
                continue

            item = pending[0]
            item_id = item["id"]
            item_name = item["item_name"]
            qqm.mark_running(item_id)
            self.progress.emit(f"正在查询: {item_name} ...")
            self.item_started.emit(item_id, item_name)

            try:
                results, errors = query_all(item, platforms=self.platforms)
                if errors and not results:
                    err_msg = "; ".join(f"{k}: {v}" for k, v in errors.items())
                    qqm.mark_error(item_id, err_msg)
                    self.item_finished.emit(
                        item_id, item_name, 0, False, err_msg)
                    self.progress.emit(f"❌ {item_name}: {err_msg}")
                else:
                    qqm.save_result(item_id, results)
                    err_remark = ""
                    if errors:
                        err_remark = " (部分失败: " + "; ".join(
                            f"{k}:{v}" for k, v in errors.items()) + ")"
                    self.item_finished.emit(
                        item_id, item_name, len(results), True,
                        f"查询到 {len(results)} 条{err_remark}")
                    self.progress.emit(
                        f"✅ {item_name}: 查询到 {len(results)} 条")
            except Exception as e:
                qqm.mark_error(item_id, str(e))
                self.item_finished.emit(
                    item_id, item_name, 0, False, f"异常: {e}")
                self.progress.emit(f"❌ {item_name}: 异常 {e}")

        self.stopped.emit()
