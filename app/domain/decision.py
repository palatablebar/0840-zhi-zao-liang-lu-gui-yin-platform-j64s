from app.domain.helpers import D


def decide_rows(items, rule_params=None):
    params = rule_params or {}
    # 默认阈值与初始规则 v1 严格一致，保证既有结论不变。
    floor = D(str(params.get("floor", "0.70")), "LIMIT")
    target = D(str(params.get("target", "0.90")), "LIMIT")
    loss_ratio = D(str(params.get("loss_ratio", "0.10")), "LIMIT")
    for item in items:
        loss_over = D(str(item["lost"]), "LIMIT") > D(str(item["input_total"]), "LIMIT") * loss_ratio
        if item.get("problem"):
            item["verdict"] = "REJECT"
            item["reason"] = item["problem"]
        elif item["rolling"] < floor:
            item["verdict"] = "REJECT"
            item["reason"] = "BELOW_FLOOR"
        elif item["rolling"] < target:
            item["verdict"] = "REVIEW"
            item["reason"] = "BELOW_TARGET"
        elif loss_over:
            item["verdict"] = "REVIEW"
            item["reason"] = "LOSS_OVER_10"
        else:
            item["verdict"] = "PASS"
            item["reason"] = "TARGET_MET"
        item.setdefault("verdict", "PASS")
        item.setdefault("reason", "OK")
    return items
