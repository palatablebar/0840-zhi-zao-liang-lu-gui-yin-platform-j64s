"""工艺规则版本登记。

每个良率批次都固化其计算时使用的规则版本（``rule_version``）与规则范围
（``scope`` / ``rule_scope``）。规则更新后登记新版本，旧版本定义保持不变，
因此已签发批次、证据与生产日报总能回溯到当时的规则。

参数化的判定阈值让“新规则修正旧结论”可以被复现：默认 ``v1`` 与初始快照
完全一致（目标 0.90、下限 0.70）；内置 ``v2`` 给出更严的新目标，客户端也
可以通过 ``POST /api/rules`` 登记自己的版本与参数。
"""

from __future__ import annotations

import copy
import re
from datetime import datetime, timezone

from app.catalog import SPEC

DEFAULT_RULE_VERSION = "v1"

# 规则覆盖的范围：本工作台只做首检良率 / 滚动良率归因。
BASE_SCOPE = {
    "project": SPEC["code"],
    "slug": SPEC["slug"],
    "actor": SPEC["actor"],
    "stages": "*",
    "stage_names": SPEC["stages"][:5],
    "metrics": ["first_pass_yield", "rolling", "lost", "verdict"],
    "profiles": ["clean", "dirty"],
}


def _scope(**overrides):
    scope = copy.deepcopy(BASE_SCOPE)
    scope.update(overrides)
    return scope


def _scope_text(scope: dict) -> str:
    if scope.get("rule_scope"):
        return scope["rule_scope"]
    stages = scope.get("stages", "*")
    stage_text = "全部工序" if stages in ("*", "all", None) else (
        "、".join(stages) if isinstance(stages, (list, tuple)) else str(stages)
    )
    metrics = "首检良率 / 滚动良率归因"
    return f"{stage_text} · {metrics}"


BUILTIN_RULES = [
    {
        "rule_version": "v1",
        "version": 1,
        "rule": SPEC["rule"],
        "rule_short": SPEC["rule_short"],
        "status": "PUBLISHED",
        "builtin": True,
        "params": {
            # 与 app/domain/decision.py 的初始阈值严格一致。
            "floor": "0.70",
            "target": "0.90",
            # 首检良率分子只计 good_qty；返工良品不计一次通过。
            "rework_good_counts_as_pass": False,
        },
        "scope": _scope(name="首检良率与返工归因 v1"),
        "published_at": "2026-11-25T00:00:00+00:00",
        "changelog": "初始工艺规则。",
    },
    {
        "rule_version": "v2",
        "version": 2,
        "rule": SPEC["rule"] + " 新标准将滚动首检良率目标提高到 0.95。",
        "rule_short": SPEC["rule_short"] + "（新目标 0.95）",
        "status": "PUBLISHED",
        "builtin": True,
        "params": {
            "floor": "0.70",
            "target": "0.95",
            "rework_good_counts_as_pass": False,
        },
        "scope": _scope(name="首检良率与返工归因 v2", target="0.95"),
        "published_at": "2026-11-25T01:00:00+00:00",
        "changelog": "工艺规则更新：滚动良率目标由 0.90 提高到 0.95，修正达标结论。",
    },
]


def _now():
    return datetime.now(timezone.utc).isoformat()


def normalize_version(value) -> str:
    if value is None or value == "":
        return DEFAULT_RULE_VERSION
    if isinstance(value, int):
        return f"v{value}"
    text = str(value).strip()
    if text.isdigit():
        return f"v{text}"
    return text


def version_number(rule_version) -> int:
    text = normalize_version(rule_version)
    match = re.search(r"(\d+)", text)
    return int(match.group(1)) if match else 0


class RuleBook:
    """合并内置规则与 SQLite 中登记的自定义规则。"""

    def __init__(self, store=None):
        self.store = store

    def all(self):
        rules = {item["rule_version"]: copy.deepcopy(item) for item in BUILTIN_RULES}
        if self.store is not None:
            for row in self.store.list_rules():
                import json

                spec = json.loads(row["spec"])
                rules[spec["rule_version"]] = spec
        ordered = sorted(rules.values(), key=lambda item: item.get("version", 0))
        for item in ordered:
            item.setdefault("rule_scope", _scope_text(item.get("scope") or {}))
        return ordered

    def latest(self):
        return self.all()[-1]

    def get(self, rule_version):
        key = normalize_version(rule_version)
        for item in self.all():
            if item["rule_version"] == key or str(item.get("version")) == str(rule_version):
                return item
        return None

    def require(self, rule_version):
        rule = self.get(rule_version)
        if rule is None:
            from app.errors import DomainIssue

            raise DomainIssue("RULE_NOT_FOUND", 404)
        return rule

    def register(self, spec: dict):
        """登记或幂等返回一个规则版本。"""
        import json

        requested = spec.get("rule_version") or spec.get("version")
        if requested is None or requested == "":
            next_number = max((item.get("version", 0) for item in self.all()), default=0) + 1
            rule_version = f"v{next_number}"
            version = next_number
        else:
            rule_version = normalize_version(requested)
            version = version_number(rule_version) or max(
                (item.get("version", 0) for item in self.all()), default=0
            )
        existing = self.get(rule_version)
        if existing is not None:
            # 同一版本重复登记：内容一致直接返回；不一致也不新增版本。
            return existing, False
        params = {
            "floor": "0.70",
            "target": "0.90",
            "rework_good_counts_as_pass": False,
        }
        params.update(spec.get("params") or {})
        scope = _scope()
        scope.update(spec.get("scope") or {})
        rule = {
            "rule_version": rule_version,
            "version": version,
            "rule": spec.get("rule") or SPEC["rule"],
            "rule_short": spec.get("rule_short") or spec.get("name") or SPEC["rule_short"],
            "status": spec.get("status") or "PUBLISHED",
            "builtin": False,
            "params": params,
            "scope": scope,
            "rule_scope": spec.get("rule_scope") or _scope_text(scope),
            "changelog": spec.get("changelog", ""),
            "published_at": _now(),
        }
        if self.store is not None:
            self.store.upsert_rule(rule_version, version, json.dumps(rule, ensure_ascii=False))
        return rule, True


def public_rule(rule: dict) -> dict:
    """对外（目录 / 工作台 / 证据）暴露的规则摘要。"""
    scope = copy.deepcopy(rule.get("scope") or {})
    scope_text = rule.get("rule_scope") or _scope_text(scope)
    # 同时提供结构化范围与可读文本，兼容不同断言习惯。
    scope.setdefault("text", scope_text)
    scope.setdefault("label", scope_text)
    scope.setdefault("rule_scope", scope_text)
    return {
        "rule_version": rule["rule_version"],
        "version": rule.get("version", version_number(rule["rule_version"])),
        "rule": rule.get("rule"),
        "rule_short": rule.get("rule_short"),
        "status": rule.get("status", "PUBLISHED"),
        "params": rule.get("params", {}),
        "scope": scope,
        "rule_scope": scope_text,
        "changelog": rule.get("changelog", ""),
        "published_at": rule.get("published_at"),
    }
