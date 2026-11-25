"""命名兼容入口：规则版本登记。"""
from app.rulebook import (  # noqa: F401
    BASE_SCOPE,
    BUILTIN_RULES,
    DEFAULT_RULE_VERSION,
    RuleBook,
    normalize_version,
    public_rule,
    version_number,
)

RULES = BUILTIN_RULES
RULE_VERSIONS = BUILTIN_RULES
CURRENT_RULE_VERSION = DEFAULT_RULE_VERSION


def get_rule(rule_version):
    return RuleBook().get(rule_version)


def list_rules(store=None):
    return RuleBook(store).all()
