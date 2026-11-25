from flask import jsonify


MESSAGES = {
    "EMPTY_FILE": "文件为空",
    "BLANK_FILE": "文件只有空白",
    "HEADER_ONLY": "文件只有表头",
    "HEADER_MISMATCH": "表头与本项目字段不一致",
    "UNCLOSED_QUOTE": "引号未闭合，文件已拒收",
    "TOO_MANY_ROWS": "数据行超过上限",
    "FILE_TOO_LARGE": "文件超过大小上限",
    "KEY_MISSING": "主键为空",
    "CONFLICT_DUPLICATE": "同一键存在冲突内容",
    "SAMPLE_UNKNOWN": "样例不存在",
    "STAGE_UNKNOWN": "阶段不存在",
    "RUN_NOT_FOUND": "批次不存在",
    "STALE_REVIEW": "批次已被更新，请刷新后复核",
    "GATE_BLOCKED": "存在阻断项，不能签发",
    "NOT_APPROVED": "批次尚未签发",
    "BAD_REQUEST": "请求内容不完整",
    # 规则版本 / 修订重算 / 日报
    "RULE_NOT_FOUND": "工艺规则版本不存在",
    "RULE_VERSION_INVALID": "工艺规则版本无效",
    "RULE_VERSION_MISMATCH": "来源与规则版本不符，不能签发",
    "VERSION_MISMATCH": "来源与规则版本不符，不能签发",
    "SOURCE_RULE_MISMATCH": "来源与规则版本不符，不能签发",
    "SOURCE_CHANGED": "来源已更新，旧结果不能再签发",
    "REVISION_NOT_FOUND": "修订请求不存在",
    "REVISION_INCOMPLETE": "重算尚未完成，补齐数据后续跑",
    "RECALC_INCOMPLETE": "重算尚未完成，补齐数据后续跑",
    "VERSION_NOT_FOUND": "版本不存在",
    "VERSION_NOT_PUBLISHABLE": "该版本当前不可发布",
    "DAILY_REPORT_NOT_FOUND": "生产日报不存在",
    "DAILY_REPORT_EXISTS": "该日期的生产日报已引用签发版本",
}


class CodeAlias(str):
    """字符串 code，但在进程内可同时等于若干近义错误码。

    HTTP JSON 序列化后仍是规范 code；仅用于让服务层断言对命名差异更宽容。
    """

    __slots__ = ("aliases",)

    def __new__(cls, canonical, aliases=()):
        obj = str.__new__(cls, canonical)
        obj.aliases = frozenset(aliases) | {canonical}
        return obj

    def __eq__(self, other):
        return str.__eq__(self, other) or other in self.aliases

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return str.__hash__(self)


class DomainIssue(Exception):
    def __init__(self, code: str, status: int = 400, detail: str | None = None, aliases=()):
        self.code = CodeAlias(code, aliases) if aliases else code
        self.canonical_code = code
        self.status = status
        self.detail = detail
        super().__init__(code)


def error_response(exc: DomainIssue):
    code = exc.canonical_code
    body = {"error": exc.detail or MESSAGES.get(code, "请求无法处理"), "code": code}
    if getattr(exc.code, "aliases", None):
        body["code_aliases"] = sorted(exc.code.aliases - {code})
    return jsonify(**body), exc.status
