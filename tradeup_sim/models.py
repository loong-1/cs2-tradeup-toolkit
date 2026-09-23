"""CS2 汰换合同炼金模拟器 — 数据结构定义。

数据结构分三层：
  Skin          —— 皮肤静态元数据（来自 CSV/SQLite，不含实例磨损）
  Material      —— 汰换合同中的一件材料（引用 Skin + 实际磨损 + 单价）
  TradeUpResult —— 一次汰换的产出（产物皮肤 + 磨损 + 概率 + 成本/盈亏）
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any


# ============================================================
# 稀有度（品质）等级
#   消费级(0) → 工业级(1) → 军规级(2) → 受限级(3) → 保密级(4) → 隐秘级(5) → 稀有特殊(6)
#   消费级不可作材料（无下级）；违禁品(contraband)已从游戏移除，禁用。
# ============================================================
QUALITY_ORDER = [
    "消费级", "工业级", "军规级", "受限级", "保密级", "隐秘级", "稀有特殊物品",
]
QUALITY_LEVEL = {q: i for i, q in enumerate(QUALITY_ORDER)}

# 磨损等级阈值（官方精确区间，左闭右开，最后一档到 1.0）
WEAR_GRADES = [
    ("崭新出厂", 0.00, 0.07),
    ("略有磨损", 0.07, 0.15),
    ("久经沙场", 0.15, 0.38),
    ("破损不堪", 0.38, 0.45),
    ("战痕累累", 0.45, 1.00),
]


def get_wear_grade(float_val: float) -> str:
    """根据 float 值返回磨损等级名。

    边界处理：CS2 中 0.07/0.15/0.38/0.45 这些精确边界值实际属于下一档，
    例如 0.07 实际是 0.069999，属于崭新出厂。
    因此上界用闭区间（<=），边界值归入较低档。
    """
    for name, lo, hi in WEAR_GRADES:
        if lo <= float_val <= hi:
            return name
    return "战痕累累"


def next_quality(quality: str) -> Optional[str]:
    """返回高一级稀有度；已是最高级返回 None。"""
    lvl = QUALITY_LEVEL.get(quality)
    if lvl is None or lvl >= len(QUALITY_ORDER) - 1:
        return None
    return QUALITY_ORDER[lvl + 1]


# ============================================================
# Skin —— 皮肤静态元数据
# ============================================================
@dataclass
class Skin:
    collection: str          # 收藏品名称（中文）
    name: str                # 皮肤名称（如 "AWP | 野火"）
    quality: str             # 品质（如 "隐秘级"）
    min_float: float         # 该皮肤的最小磨损
    max_float: float         # 该皮肤的最大磨损
    is_stattrak: bool = False  # 是否 StatTrak 版本（同一皮肤普通/ST 是两行记录）
    market_hash_name: str = ""  # 市场哈希名（C5/ECO 用）
    buff_goods_id: int = 0   # Buff 商品 id（按磨损档区分）
    price: float = 0.0       # 参考价（来自 price_buff，按磨损档）

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ============================================================
# Material —— 汰换合同中的一件材料
# ============================================================
@dataclass
class Material:
    skin: Skin               # 引用的皮肤元数据
    wear: float              # 该件材料的实际磨损值（0~1）
    price: float             # 该件材料的购入单价
    is_souvenir: bool = False  # 是否纪念品（纪念品可作材料，但产出必为普通）
    grade: str = ""          # 磨损档（DP 选中时确定，避免边界浮点误差）

    @property
    def quality(self) -> str:
        return self.skin.quality

    @property
    def collection(self) -> str:
        return self.skin.collection

    @property
    def is_stattrak(self) -> bool:
        return self.skin.is_stattrak

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skin_name": self.skin.name,
            "collection": self.skin.collection,
            "quality": self.skin.quality,
            "wear": round(self.wear, 6),
            "wear_grade": self.grade or get_wear_grade(self.wear),
            "price": round(self.price, 2),
            "is_stattrak": self.is_stattrak,
            "is_souvenir": self.is_souvenir,
        }


# ============================================================
# TradeUpResult —— 一次汰换的产出
# ============================================================
@dataclass
class TradeUpResult:
    output_skin: Skin                 # 产出皮肤（高一级）
    output_wear: float                # 产出磨损值
    probability: float                # 该产出的概率（按收藏品材料数量加权）
    material_cost: float              # 10 件材料总成本
    output_price: float               # 产出皮肤同磨损档参考价
    is_stattrak: bool = False         # 产出是否 StatTrak
    seed: Optional[int] = None        # 随机种子（复现用）

    @property
    def profit(self) -> float:
        return self.output_price - self.material_cost

    @property
    def roi(self) -> float:
        if self.material_cost <= 0:
            return 0.0
        return self.profit / self.material_cost * 100.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "output_skin": self.output_skin.name,
            "output_collection": self.output_skin.collection,
            "output_quality": self.output_skin.quality,
            "output_wear": round(self.output_wear, 6),
            "output_wear_grade": get_wear_grade(self.output_wear),
            "is_stattrak": self.is_stattrak,
            "probability": round(self.probability, 6),
            "material_cost": round(self.material_cost, 2),
            "output_price": round(self.output_price, 2),
            "profit": round(self.profit, 2),
            "roi_pct": round(self.roi, 2),
            "seed": self.seed,
        }
