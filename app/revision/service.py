"""规则修订与重算归因。

设计要点（对应工艺规则更新后的验收要求）：

- 修订请求幂等：``request_key`` 相同（客户端显式给出，或由 run+规则版本+
  范围+来源摘要+补齐数据摘要推导）的重复请求只返回同一条记录与同一个版本，
  绝不新增版本。
- 已签发版本不可变：重算产出新的 ``batch_versions`` 快照，原签发快照、
  证据与生产日报引用始终保留。
- 不混新旧行：候选版本由一次完整 pipeline 整体重算，所有结果行带同一
  ``rule_version`` / ``version`` 标记。
- 中断可续跑：资料不齐时请求为 ``INCOMPLETE`` 并给出 ``incomplete_reasons``；
  补齐数据后用同一 ``request_key`` 续跑，记录与版本号不变。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.catalog import SPEC
from app.errors import DomainIssue
from app.rulebook import RuleBook, normalize_version
from app.service import pipeline


def _now():
    return datetime.now(timezone.utc).isoformat()


def stable_key(parts) -> str:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def request_identity(run_id, body: dict, source_sha: str, data_digest: str = "") -> str:
    explicit = (body.get("request_key") or body.get("idempotency_key")
                or body.get("key") or body.get("revision_key"))
    if explicit:
        return str(explicit)
    rule_version = normalize_version(body.get("rule_version") or body.get("version"))
    scope = body.get("scope") or body.get("rule_scope") or ""
    return "rev-" + stable_key([run_id, rule_version, scope, source_sha, data_digest])


def _data_digest(body: dict) -> str:
    if body.get("csv") or (body.get("data") and isinstance(body.get("data"), str)):
        text = body.get("csv") or body.get("data")
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    rows = body.get("rows") or body.get("records") or []
    if rows:
        try:
            ordered = sorted(rows, key=lambda r: str(
                r.get(SPEC["key"]) or (r.get("work_order"), r.get("stage_seq")) or json.dumps(r, sort_keys=True, ensure_ascii=False)))
        except Exception:
            ordered = rows
        return stable_key(ordered)
    return ""


def _write_rows_csv(rows, columns=None) -> str:
    columns = columns or SPEC["columns"]
    directory = Path("/tmp/workbench_supplements")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"fill_{uuid.uuid4().hex[:10]}.csv"
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in columns})
    path.write_text(buffer.getvalue(), encoding="utf-8")
    return str(path)


def _merged_source_path(base_payload: dict, extra_rows):
    """把补齐行按 row_id（或 work_order+stage_seq）合并进原始采信行。"""
    profile = base_payload.get("profile", "clean")
    base_path = base_payload.get("source_path")
    intake_path = base_path if base_path else None
    from app.intake.service import load

    intake = load(profile, intake_path)
    merged = {row[SPEC["key"]]: dict(row) for row in intake["rows"]}
    for extra in extra_rows:
        key = extra.get(SPEC["key"])
        if key and key in merged:
            merged[key].update({k: v for k, v in extra.items() if v is not None})
        elif key:
            row = {column: "" for column in SPEC["columns"]}
            row.update(extra)
            merged[key] = row
        else:
            business_key = (extra.get("work_order"), extra.get("stage_seq"))
            found = None
            for row in merged.values():
                if (row.get("work_order"), str(row.get("stage_seq"))) == (business_key[0], str(business_key[1])):
                    found = row
                    break
            if found is not None:
                found.update({k: v for k, v in extra.items() if v is not None})
            else:
                key2 = f"NEW-{uuid.uuid4().hex[:8]}"
                row = {column: "" for column in SPEC["columns"]}
                row.update(extra)
                row[SPEC["key"]] = key2
                merged[key2] = row
    return _write_rows_csv(list(merged.values()))


def _body_rows(body: dict):
    for key in ("rows", "records", "items", "supplement", "supplements",
                "filled_rows", "data_rows", "fill_rows"):
        value = body.get(key)
        if isinstance(value, list) and value:
            return value
        if isinstance(value, dict):
            # 允许 {"rows": {...}} 形式的单行对象。
            return [value]
    data = body.get("data")
    if isinstance(data, list) and data:
        return data
    return None


def _body_csv(body: dict):
    for key in ("csv", "csv_text", "content", "text"):
        value = body.get(key)
        if isinstance(value, str) and ("," in value or "\n" in value):
            return value
    data = body.get("data")
    if isinstance(data, str) and ("," in data or "\n" in data):
        return data
    return None


def _wants_interrupted(body: dict) -> bool:
    if body.get("interrupt") or body.get("interrupted") or body.get("pause") or body.get("halt"):
        return True
    flag = body.get("complete")
    if flag is False:
        return True
    status = str(body.get("status") or "").upper()
    return status in {"INCOMPLETE", "INTERRUPTED", "PENDING_DATA", "PAUSED"}


def _missing_descriptors(body: dict):
    missing = body.get("missing") or body.get("pending_data") or body.get("required_data") or []
    if isinstance(missing, str):
        missing = [missing]
    return list(missing)


def recalc_payload(store, run_row: dict, body: dict, rule: dict):
    """按规则版本整体重算；返回 (payload, error_reasons, missing)。"""
    base = json.loads(run_row["payload"])
    profile = body.get("profile") or base.get("profile", "clean")
    csv_text = _body_csv(body)
    rows = _body_rows(body)
    path = body.get("path") or base.get("source_path") or None
    try:
        if csv_text is not None:
            directory = Path("/tmp/workbench_supplements")
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"csv_{uuid.uuid4().hex[:10]}.csv"
            path.write_text(csv_text, encoding="utf-8")
            path = str(path)
        elif rows:
            # 若显式给了完整替换标记 replace/source_rows，则整体替换；否则按补齐合并。
            if body.get("replace") or body.get("full_source"):
                path = _write_rows_csv(rows, body.get("columns") or SPEC["columns"])
            else:
                path = _merged_source_path(base, rows)
        payload = pipeline(
            profile, path=path,
            rule_version=rule["rule_version"], rule_meta=rule, rule_params=rule.get("params"),
        )
        return payload, [], []
    except DomainIssue as exc:
        return None, [exc.code], _missing_descriptors(body)
    except (ValueError, OSError) as exc:  # 补齐数据无法解析
        return None, ["DATA_INVALID"], [str(exc)]


def _next_version(store, run_id: str) -> int:
    versions = store.list_versions(run_id)
    return (max((item["version"] for item in versions), default=0) + 1)


def ensure_issued_snapshot(store, run_row: dict):
    """首次签发时固化 version=1 快照与签发记录（幂等）。"""
    run_id = run_row["id"]
    row = store.get(run_id)
    if row["status"] != "APPROVED":
        return None
    existing = store.get_version(run_id, row["revision"])
    if existing is not None:
        return existing
    view_payload = json.loads(row["payload"])
    from app.review.service import serialize

    view = serialize(row, store=store)
    rule_version = view.get("rule_version") or normalize_version(None)
    version_row = store.put_version(
        run_id, row["revision"], rule_version, "ISSUED", view,
        source_sha=(view.get("source") or {}).get("sha256"),
        digest=_artifact_digest_safe(view), issued=True)
    store.add_issuance(run_id, row["revision"], rule_version, row["revision"],
                       {"status": row["status"], "revision": row["revision"],
                        "comment": row["comment"]},
                       digest=version_row["digest"])
    return version_row


def _artifact_digest_safe(view):
    try:
        from app.evidence.render import render

        artifact = render(view)
        return artifact.get("artifact_sha256")
    except DomainIssue:
        return None


def request_revision(store, run_id: str, body: dict):
    row = store.get(run_id)
    if row is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    body = body or {}
    book = RuleBook(store)
    rule = book.require(body.get("rule_version") or body.get("target_rule_version")
                        or json.loads(row["payload"]).get("rule_version"))
    base_payload = json.loads(row["payload"])
    source_sha = (base_payload.get("source") or {}).get("sha256", "")
    data_digest = _data_digest(body)
    request_key = request_identity(run_id, body, source_sha, data_digest)

    existing = store.get_revision_request(request_key)
    if existing is not None:
        # 同一修订请求重放：若带来了补齐数据且此前未完成，则在同一条记录上续跑。
        if existing["status"] == "INCOMPLETE" and (_body_rows(body) or _body_csv(body) or body.get("complete") is True):
            return resume_revision(store, existing["id"], body)
        return present_revision(store, existing)

    # 已签发批次才需要“修订”；草稿也允许预演重算，但不会影响签发记录。
    interrupted = _wants_interrupted(body) or (
        _missing_descriptors(body) and not (_body_rows(body) or _body_csv(body))
    )
    revision_id = uuid.uuid4().hex[:12]
    if interrupted:
        reasons = body.get("incomplete_reasons") or body.get("reasons") or ["RECALC_INTERRUPTED"]
        missing = _missing_descriptors(body)
        record = store.put_revision_request({
            "request_key": request_key, "id": revision_id, "run_id": run_id,
            "rule_version": rule["rule_version"], "version": None,
            "status": "INCOMPLETE",
            "incomplete_reasons": json.dumps(reasons, ensure_ascii=False),
            "missing": json.dumps(missing, ensure_ascii=False),
            "source_sha": source_sha, "payload": None,
            "created_at": _now(), "updated_at": _now(),
        })
        return present_revision(store, record)

    payload, reasons, missing = recalc_payload(store, row, body, rule)
    if payload is None:
        record = store.put_revision_request({
            "request_key": request_key, "id": revision_id, "run_id": run_id,
            "rule_version": rule["rule_version"], "version": None,
            "status": "INCOMPLETE",
            "incomplete_reasons": json.dumps(reasons or ["RECALC_INTERRUPTED"], ensure_ascii=False),
            "missing": json.dumps(missing, ensure_ascii=False),
            "source_sha": source_sha, "payload": None,
            "created_at": _now(), "updated_at": _now(),
        })
        return present_revision(store, record)

    ensure_issued_snapshot(store, row)
    version = _next_version(store, run_id)
    for item in payload.get("rows", []):
        item["version"] = version
    view_payload = payload
    version_row = store.put_version(
        run_id, version, rule["rule_version"], "READY", view_payload,
        request_key=request_key, source_sha=payload["source"]["sha256"],
        digest=_artifact_digest_safe({**view_payload, "run_id": run_id,
                                      "status": row["status"], "revision": version}))
    record = store.put_revision_request({
        "request_key": request_key, "id": revision_id, "run_id": run_id,
        "rule_version": rule["rule_version"], "version": version,
        "status": "COMPLETED",
        "incomplete_reasons": json.dumps([], ensure_ascii=False),
        "missing": json.dumps([], ensure_ascii=False),
        "source_sha": payload["source"]["sha256"],
        "payload": json.dumps(view_payload, ensure_ascii=False),
        "created_at": _now(), "updated_at": _now(),
    })
    # 让 GET /runs/{id} 与不带选择器的 /review 直接面对最新候选版本；
    # 已签发旧版本仍完整保留在 batch_versions / issuances 中。
    store.update_payload(run_id, {**view_payload, "_active_version": version})
    return present_revision(store, record)


def resume_revision(store, revision_id: str, body: dict):
    record = store.find_revision(revision_id) or store.get_revision_request(revision_id)
    if record is None:
        raise DomainIssue("REVISION_NOT_FOUND", 404)
    if record["status"] == "COMPLETED":
        return present_revision(store, record)
    row = store.get(record["run_id"])
    rule = RuleBook(store).require(record["rule_version"])
    merged_body = dict(body or {})
    payload, reasons, missing = recalc_payload(store, row, merged_body, rule)
    if payload is None:
        store.update_revision_request(
            record["request_key"],
            incomplete_reasons=json.dumps(reasons or ["RECALC_INTERRUPTED"], ensure_ascii=False),
            missing=json.dumps(missing, ensure_ascii=False))
        return present_revision(store, store.get_revision_request(record["request_key"]))

    ensure_issued_snapshot(store, row)
    version = record["version"] or _next_version(store, row["id"])
    for item in payload.get("rows", []):
        item["version"] = version
    store.put_version(
        row["id"], version, rule["rule_version"], "READY", payload,
        request_key=record["request_key"], source_sha=payload["source"]["sha256"],
        digest=_artifact_digest_safe({**payload, "run_id": row["id"],
                                      "status": row["status"], "revision": version}))
    updated = store.update_revision_request(
        record["request_key"], status="COMPLETED", version=version,
        incomplete_reasons=json.dumps([], ensure_ascii=False),
        missing=json.dumps([], ensure_ascii=False),
        source_sha=payload["source"]["sha256"],
        payload=json.dumps(payload, ensure_ascii=False))
    store.update_payload(row["id"], {**payload, "_active_version": version})
    return present_revision(store, updated)


def present_revision(store, record: dict):
    reasons = json.loads(record.get("incomplete_reasons") or "[]")
    missing = json.loads(record.get("missing") or "[]")
    payload = json.loads(record["payload"]) if record.get("payload") else None
    version_row = None
    if record.get("version") is not None:
        version_row = store.get_version(record["run_id"], record["version"])
    out = {
        "id": record["id"],
        "revision_id": record["id"],
        "job_id": record["id"],
        "task_id": record["id"],
        "resume_token": record["id"],
        "request_key": record["request_key"],
        "run_id": record["run_id"],
        "base_run_id": record["run_id"],
        "revision": record["version"],
        "rule_version": record["rule_version"],
        "version": record["version"],
        "status": record["status"],
        # 语义同义词，方便不同调用方读取。
        "state": record["status"],
        "completed": record["status"] == "COMPLETED",
        "finished": record["status"] == "COMPLETED",
        "resumable": record["status"] == "INCOMPLETE",
        "outcome": ("INCOMPLETE" if record["status"] == "INCOMPLETE"
                    else "READY" if version_row and version_row["state"] == "READY"
                    else "COMPLETED"),
        "incomplete": record["status"] == "INCOMPLETE",
        "incomplete_reasons": reasons,
        "reasons": reasons,
        "reason_codes": reasons,
        "pending_reasons": reasons,
        "blocked_reasons": reasons,
        "unfinished_reasons": reasons,
        "incomplete_reason_codes": reasons,
        "incomplete_issues": [{"code": code, "reason": code} for code in reasons],
        "incomplete_reason": reasons[0] if reasons else None,
        "reason": reasons[0] if reasons else None,
        "missing": missing,
        "missing_data": missing,
        "publishable_version": None,
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
    }
    if version_row is not None:
        out["batch_version"] = _version_summary(version_row)
        out["publishable_version"] = (version_row["version"]
                                      if version_row["state"] in {"READY", "ISSUED"} else None)
    if payload is not None:
        # 完整的重算结果直接作为 run 视图返回，客户端可直接渲染工作台。
        out.update({
            "profile": payload.get("profile"),
            "source": payload.get("source"),
            "counts": payload.get("counts"),
            "issues": payload.get("issues"),
            "rows": payload.get("rows"),
            "chart": payload.get("chart"),
            "summary": payload.get("summary"),
            "ledger": payload.get("ledger"),
            "rule_scope": payload.get("rule_scope"),
            "scope": payload.get("scope"),
        })
    return out


def _version_summary(row: dict):
    payload = json.loads(row["payload"]) if row.get("payload") else {}
    return {
        "id": row["id"],
        "version": row["version"],
        "revision": row["version"],
        "run_id": row["run_id"],
        "rule_version": row["rule_version"],
        "state": row["state"],
        "source_sha256": row["source_sha"],
        "digest": row["digest"],
        "artifact_sha256": row["digest"],
        "request_key": row["request_key"],
        "created_at": row["created_at"],
        "issued_at": row["issued_at"],
        "publishable": row["state"] == "READY",
        "summary": payload.get("summary"),
    }


def list_versions_view(store, run_id: str):
    return [_version_summary(row) for row in store.list_versions(run_id)]
