from datetime import date, timedelta
from app.catalog import SPEC
from app.domain.helpers import *  # noqa: F401,F403


def calculate_rows(items, rule_params=None):
    params = rule_params or {}
    # 规则可更新为把返工良品计入分子；默认 False 与初始核心规则一致。
    rework_as_pass = bool(params.get("rework_good_counts_as_pass", False))
    out = []
    rows = items
    orders = {}
    for item in items:
        orders.setdefault(item["work_order"], []).append(item)
    for work_order, stages in orders.items():
        stages.sort(key=lambda row: row["stage_seq"])
        rolling = D("1", "CALC")
        problem = None
        input_total = 0
        lost = 0
        first_pass_good = 0
        rework_good_total = 0
        for index, stage in enumerate(stages):
            if index and stage["input_qty"] != stages[index - 1]["good_qty"] + stages[index - 1]["rework_good"]:
                problem = "STAGE_DISCONTINUITY"
            numerator = stage["good_qty"] + (stage["rework_good"] if rework_as_pass else 0)
            rolling *= D(str(numerator), "CALC") / D(str(stage["input_qty"]), "CALC")
            input_total += stage["input_qty"]
            lost += stage["scrap_qty"] + stage["rework_qty"] - stage["rework_good"]
            first_pass_good += stage["good_qty"]
            rework_good_total += stage["rework_good"]
        out.append({"label": work_order, "rolling": rolling, "stage_count": len(stages),
                    "input_total": input_total, "lost": lost, "problem": problem,
                    "first_pass_good": first_pass_good, "rework_good_total": rework_good_total,
                    "metric": D(str(input_total), "CALC"), "limit": D("0.90", "LIMIT")})
    for item in out:
        item.setdefault("label", str(item.get(SPEC["key"], "")))
    return out
