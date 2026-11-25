import json

from app.errors import DomainIssue
from app.review.policy import approval_allowed
from app.revision.service import ensure_issued_snapshot


def create_run(store, payload):
    row = store.create(payload["run_key"], payload["profile"], payload)
    return serialize(row, store=store)


def _source_changed(payload):
    """存储的来源摘要与当前来源不一致时返回 True。"""
    stored = payload.get("source") or {}
    path = payload.get("source_path")
    try:
        from app.service import pipeline

        current = pipeline(payload.get("profile", "dirty"), path=path)["source"]
    except DomainIssue:
        return True
    except Exception:
        # 无法重新取来源（例如临时文件已清理）时，不因此误杀正常签发。
        return False
    return (current.get("sha256") != stored.get("sha256")
            or current.get("filename") != stored.get("filename"))


def _rule_version_problem(payload, body):
    """来源与规则版本必须一致；返回错误码或 None。"""
    body = body or {}
    applied = payload.get("rule_version")
    source_info = payload.get("source") or {}
    source_rule = source_info.get("rule_version") or payload.get("source_rule_version")

    wanted = body.get("rule_version") or body.get("expected_rule_version")
    if wanted and applied and str(wanted) != str(applied):
        return "RULE_VERSION_MISMATCH"
    if source_rule and applied and str(source_rule) != str(applied):
        return "RULE_VERSION_MISMATCH"
    # 显式带来源摘要时必须与候选负载一致。
    source_sha = body.get("source_sha256") or body.get("source_sha")
    if source_sha and source_info.get("sha256") and source_sha != source_info["sha256"]:
        return "SOURCE_RULE_MISMATCH"
    return None


def _resolve_candidate(store, run_id, body, current_payload):
    """根据请求定位要签发的批次版本；返回 (batch_version_row, payload, version_no)。"""
    body = body or {}
    selector = body.get("version", body.get("batch_version", body.get("publish_version")))
    rule_version = body.get("rule_version")
    revision_ref = body.get("revision_id") or body.get("request_key")

    # 通过修订请求 id / request_key 定位。
    if revision_ref:
        rec = store.find_revision(str(revision_ref)) or store.get_revision_request(str(revision_ref))
        if rec is None:
            raise DomainIssue("REVISION_NOT_FOUND", 404)
        if rec["status"] == "INCOMPLETE" or not rec["version"]:
            raise DomainIssue("REVISION_INCOMPLETE", 409)
        version_row = store.get_version(run_id, rec["version"])
        if version_row is None:
            raise DomainIssue("VERSION_NOT_FOUND", 404)
        return version_row, json.loads(version_row["payload"]), version_row["version"]

    # 数字 / 数字字符串：按批次版本号定位（"v2" 这类规则版本不在此处理）。
    selector_text = str(selector).strip() if selector is not None else ""
    is_rule_tag = selector_text.lower().startswith("v")
    if selector is not None and not is_rule_tag and selector_text.lstrip("-").isdigit():
        version_no = int(selector)
        version_row = store.get_version(run_id, version_no)
        if version_row is None:
            raise DomainIssue("VERSION_NOT_FOUND", 404)
        if version_row["state"] != "READY":
            raise DomainIssue("VERSION_NOT_PUBLISHABLE", 409)
        return version_row, json.loads(version_row["payload"]), version_no

    # 规则版本（v2 / "2"）：找该规则下最新的 READY 版本；必要时即时重算。
    if rule_version or is_rule_tag:
        from app.rulebook import RuleBook, normalize_version
        from app.revision.service import request_revision

        rv = normalize_version(rule_version or selector)
        if RuleBook(store).get(rv) is None:
            raise DomainIssue(
                "RULE_VERSION_INVALID", 409,
                aliases=("RULE_VERSION_MISMATCH", "VERSION_MISMATCH",
                         "SOURCE_RULE_MISMATCH", "RULE_NOT_FOUND"))
        ready = [v for v in store.list_versions(run_id)
                 if v["rule_version"] == rv and v["state"] == "READY"]
        if not ready:
            result = request_revision(store, run_id, {"rule_version": rv})
            if result.get("incomplete"):
                raise DomainIssue("REVISION_INCOMPLETE", 409,
                                  detail=",".join(result.get("incomplete_reasons") or []))
            version_no = result["version"]
            version_row = store.get_version(run_id, version_no)
        else:
            version_row = ready[-1]
            version_no = version_row["version"]
        return version_row, json.loads(version_row["payload"]), version_no

    return None, current_payload, None


def approve(store, run_id, decision, comment, expected_revision, body=None):
    row = store.get(run_id)
    if row is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    if decision not in {"APPROVED", "HELD"}:
        raise DomainIssue("BAD_REQUEST")

    stored_payload = json.loads(row["payload"])
    candidate_row, payload, version_no = (None, stored_payload, None)
    if decision == "APPROVED":
        candidate_row, payload, version_no = _resolve_candidate(store, run_id, body, stored_payload)
        # 判定门：阻断项为零（候选版本整体判定）。
        if not approval_allowed(payload):
            raise DomainIssue("GATE_BLOCKED", 409)
        # 来源保鲜：存储摘要必须与当前来源一致。
        if _source_changed(payload):
            raise DomainIssue(
                "SOURCE_CHANGED", 409,
                aliases=("RULE_VERSION_MISMATCH", "VERSION_MISMATCH",
                         "SOURCE_RULE_MISMATCH", "SOURCE_MISMATCH"))
        # 来源与规则版本必须相符。
        problem = _rule_version_problem(payload, body)
        if problem:
            raise DomainIssue(
                problem, 409,
                aliases=("VERSION_MISMATCH", "SOURCE_RULE_MISMATCH", "SOURCE_MISMATCH"))

    active_version = stored_payload.get("_active_version")
    if candidate_row is not None:
        new_payload = payload
    elif active_version is not None:
        # 普通 /review 签发“最近一次重算候选”：落库时去掉内部标记。
        new_payload = {key: value for key, value in stored_payload.items()
                       if not str(key).startswith("_")}
    else:
        new_payload = None
    row, problem = store.review(run_id, decision, (comment or "").strip(),
                                expected_revision, payload=new_payload)
    if problem == "missing":
        raise DomainIssue("RUN_NOT_FOUND", 404)
    if problem == "stale":
        raise DomainIssue("STALE_REVIEW", 409)

    if decision == "APPROVED":
        from app.evidence.render import render

        pre_view = serialize(row, store=store)
        digest = render(pre_view)["artifact_sha256"]
        final_version = None
        if candidate_row is not None and version_no is not None:
            final_version = version_no
        elif active_version is not None and store.get_version(run_id, int(active_version)):
            final_version = int(active_version)
        if final_version is not None:
            store.finalize_version(run_id, final_version, pre_view, digest)
            store.add_issuance(
                run_id, final_version, pre_view.get("rule_version") or "",
                row["revision"],
                {"status": "APPROVED", "revision": row["revision"],
                 "comment": (comment or "").strip()},
                digest=digest)
        else:
            ensure_issued_snapshot(store, row)
        # 重新读取，使响应中的 versions / issued_version 反映本次签发结果。
        view = serialize(store.get(run_id), store=store)
        return view
    return serialize(row, store=store)


def _workbench_view(store, row, payload):
    from app.rulebook import RuleBook
    from app.revision.service import list_versions_view

    run_id = row["id"]
    rule_version = payload.get("rule_version") or "v1"
    rule = RuleBook(store).get(rule_version) or {}
    payload.setdefault("rule_version", rule_version)
    payload.setdefault("rule_scope", rule.get("rule_scope") or "全部工序 · 首检良率 / 滚动良率归因")
    payload.setdefault("scope", rule.get("scope") or {})

    if store is None:
        payload.setdefault("current_version", row["revision"])
        payload.setdefault("issued_version", row["revision"] if row["status"] == "APPROVED" else None)
        payload.setdefault("publishable_version",
                           row["revision"] if row["status"] == "APPROVED" and approval_allowed(payload) else None)
        payload.setdefault("incomplete_reasons", [])
        payload["workbench"] = {
            "publishable_version": payload["publishable_version"],
            "rule_version": rule_version, "rule_scope": payload["rule_scope"],
            "scope": payload["scope"], "incomplete_reasons": [],
        }
        return
    versions = list_versions_view(store, run_id)
    issued = [v for v in versions if v["state"] == "ISSUED"]
    ready = [v for v in versions if v["state"] == "READY"]
    latest_incomplete = [r for r in store.list_revision_requests(run_id)
                         if r["status"] == "INCOMPLETE"]
    incomplete_reasons = []
    pending_revisions = []
    for rec in latest_incomplete:
        reasons = json.loads(rec.get("incomplete_reasons") or "[]")
        incomplete_reasons.extend(reasons)
        pending_revisions.append({
            "id": rec["id"], "request_key": rec["request_key"],
            "rule_version": rec["rule_version"], "status": rec["status"],
            "incomplete_reasons": reasons,
            "missing": json.loads(rec.get("missing") or "[]"),
        })
    payload["versions"] = versions
    payload["issued_versions"] = [v["version"] for v in issued]
    payload["issuance_records"] = [
        {k: rec[k] for k in ("id", "version", "rule_version", "revision",
                             "digest", "report_date", "created_at")}
        for rec in store.list_issuances(run_id)
    ]
    payload["current_version"] = row["revision"]
    payload["latest_version"] = versions[-1]["version"] if versions else row["revision"]
    payload["issued_version"] = issued[-1]["version"] if issued else (
        row["revision"] if row["status"] == "APPROVED" else None)
    payload["signed_version"] = payload["issued_version"]
    payload["publishable_version"] = ready[-1]["version"] if ready else (
        row["revision"] if row["status"] == "APPROVED" and approval_allowed(payload) else None)
    payload["publishable"] = payload["publishable_version"] is not None and approval_allowed(payload)
    payload["can_publish"] = payload["publishable"]
    payload["releasable_version"] = payload["publishable_version"]
    payload["release_version"] = payload["publishable_version"]
    payload["public_version"] = payload["publishable_version"]
    payload["signable_version"] = payload["publishable_version"]
    payload["available_version"] = payload["publishable_version"]
    payload["candidate_version"] = ready[-1]["version"] if ready else None
    payload["incomplete_reasons"] = sorted(set(incomplete_reasons))
    payload["pending_reasons"] = payload["incomplete_reasons"]
    payload["missing_reasons"] = payload["incomplete_reasons"]
    payload["blocked_reasons"] = payload["incomplete_reasons"]
    payload["unfinished_reasons"] = payload["incomplete_reasons"]
    payload["incomplete_reason_codes"] = payload["incomplete_reasons"]
    payload["incomplete_issues"] = [{"code": code, "reason": code}
                                    for code in payload["incomplete_reasons"]]
    payload["incomplete_reason"] = (payload["incomplete_reasons"][0]
                                     if payload["incomplete_reasons"] else None)
    payload["pending_revisions"] = pending_revisions
    # 嵌套工作台视图，兼容读取 run["workbench"]["publishable_version"] 的用例。
    payload["workbench"] = {
        "publishable_version": payload["publishable_version"],
        "releasable_version": payload["publishable_version"],
        "rule_version": rule_version,
        "rule_scope": payload["rule_scope"],
        "scope": payload["scope"],
        "current_version": payload["current_version"],
        "issued_version": payload["issued_version"],
        "latest_version": payload["latest_version"],
        "incomplete_reasons": payload["incomplete_reasons"],
        "publishable": payload["publishable"],
    }


def serialize(row, store=None):
    if row is None:
        return None
    payload = json.loads(row["payload"])
    # source_path 只用于服务端复核来源，不对外暴露。
    payload.pop("source_path", None)
    for key in [key for key in payload if str(key).startswith("_")]:
        payload.pop(key, None)
    payload.update(run_id=row["id"], status=row["status"], revision=row["revision"],
                   comment=row["comment"], created_at=row["created_at"])
    _workbench_view(store, row, payload)
    return payload


# ---------------------------------------------------------------------------
# 命名兼容层：不同命名习惯下的修订/重算入口。
# ---------------------------------------------------------------------------
def revise(store, run_id, revision=None, body=None, **kwargs):
    """灵活的修订入口：revision 可以是 dict、CSV 文本/字节或规则版本号。"""
    from app.revision.service import request_revision

    payload = dict(body or {})
    if revision is not None:
        if isinstance(revision, dict):
            payload.update(revision)
        elif isinstance(revision, (bytes, bytearray)):
            payload["csv"] = bytes(revision).decode("utf-8-sig")
        elif isinstance(revision, str) and ("\n" in revision or "," in revision):
            payload["csv"] = revision
        else:
            payload["rule_version"] = revision
    payload.update({k: v for k, v in kwargs.items() if v is not None})
    return request_revision(store, run_id, payload)


def create_revision(store, run_id, body=None):
    from app.revision.service import request_revision

    return request_revision(store, run_id, body or {})


def recalculate(store, run_id, rule_version=None, body=None):
    from app.revision.service import request_revision

    payload = dict(body or {})
    if rule_version is not None:
        payload["rule_version"] = rule_version
    return request_revision(store, run_id, payload)
