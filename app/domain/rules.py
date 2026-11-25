"""领域层规则版本兼容入口。"""
from app.rules import *  # noqa: F401,F403
from app.rules import RULES, RULE_VERSIONS, RuleBook, get_rule, list_rules  # noqa: F401
