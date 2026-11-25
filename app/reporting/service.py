"""生产日报：引用并固化某个已签发版本。

日报一旦生成，其内容（指标、结论、来源摘要、规则版本、证据摘要）以 JSON
快照落库；之后规则修订、新版本签发都不会改变历史日报。签发记录与版本快照
也因此永久保留，可随时回溯。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date as date_cls

from app.errors import DomainIssue


def _today():
    return date_cls.today().isoformat()


def _report_key(run_id, report_date, version):
    return "report-" + hashlib.sha256(
        f"{run_id}|{report_date}|{version}".encode("utf-8")).hexdigest()[:20]


def _issued_version(store, run_id, version=None, revision=None):
    row = store.get(run_id)
    if row is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    wanted = version if version is not None else revision
    if wanted is None:
        # 默认引用当前已签发的 revision。
        if row["status"] != "APPROVED":
            raise DomainIssue("NOT_APPROVED", 409)
        wanted = row["revision"]
    version_row = store.get_version(run_id, int(wanted))
    if version_row is None:
        # 兼容：若没有版本快照但当前 run 正好是该 revision，则即时固化。
        if int(wanted) == row["revision"] and row["status"] == "APPROVED":
            from app.revision.service import ensure_issued_snapshot

            version_row = ensure_issued_snapshot(store, row)
    if version_row is None:
        raise DomainIssue("VERSION_NOT_FOUND", 404)
    if version_row["state"] not in {"ISSUED"}:
        raise DomainIssue("VERSION_NOT_PUBLISHABLE", 409)
    return row, version_row


def create_daily_report(store, body: dict):
    body = body or {}
    run_id = body.get("run_id") or body.get("id")
    if not run_id:
        raise DomainIssue("BAD_REQUEST")
    report_date = body.get("date") or body.get("report_date") or _today()
    version = body.get("version", body.get("issued_version"))
    revision = body.get("revision")
    row, version_row = _issued_version(store, run_id, version, revision)
    key = body.get("request_key") or body.get("idempotency_key") or _report_key(
        run_id, report_date, version_row["version"])
    existing = store.get_report_by_key(key)
    if existing is not None:
        return present_report(existing)

    version_payload = json.loads(version_row["payload"])
    report = {
        "schema": "production-daily-report/v1",
        "report_date": report_date,
        "date": report_date,
        "run_id": run_id,
        # 明确“引用的已签发版本”，同时给出多个同义字段。
        "version": version_row["version"],
        "revision": version_row["version"],
        "issued_version": version_row["version"],
        "signed_version": version_row["version"],
        "publishable_version": version_row["version"],
        "rule_version": version_row["rule_version"],
        "rule_scope": version_payload.get("rule_scope"),
        "scope": version_payload.get("scope"),
        "source": version_payload.get("source"),
        "summary": version_payload.get("summary"),
        "counts": version_payload.get("counts"),
        "ledger_balanced": (version_payload.get("ledger") or {}).get("balanced"),
        "rows": _compact_rows(version_payload.get("rows") or []),
        "artifact_sha256": version_row["digest"],
        "evidence_digest": version_row["digest"],
        "status": "ISSUED",
    }
    report["report_sha256"] = hashlib.sha256(
        json.dumps(report, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    record = store.create_report(
        key, report_date, run_id, version_row["version"], report,
        revision=version_row["version"], rule_version=version_row["rule_version"],
        digest=version_row["digest"])
    return present_report(record)


def _compact_rows(rows):
    compact = []
    for row in rows:
        compact.append({
            "label": row.get("label"),
            "verdict": row.get("verdict"),
            "reason": row.get("reason"),
            "rolling": row.get("rolling"),
            "lost": row.get("lost"),
            "input_total": row.get("input_total"),
            "stage_count": row.get("stage_count"),
            "rule_version": row.get("rule_version"),
            "version": row.get("version"),
        })
    return compact


def present_report(record: dict):
    report = json.loads(record["payload"])
    report.update({
        "id": record["id"],
        "report_id": record["id"],
        "created_at": record["created_at"],
    })
    # 兼容 {"report": {...}} / {"daily_report": {...}} 的包裹式契约。
    nested = {key: value for key, value in report.items() if key not in {"report", "daily_report"}}
    report["report"] = nested
    report["daily_report"] = nested
    return report


def get_report(store, report_id=None, report_date=None):
    record = None
    if report_id:
        record = store.get_report(report_id)
        if record is None:
            # 路径标识也可能直接是日期。
            record = store.get_report_by_date(report_id)
    if record is None and report_date:
        record = store.get_report_by_date(report_date)
    if record is None:
        raise DomainIssue("DAILY_REPORT_NOT_FOUND", 404)
    return present_report(record)


def list_reports(store, report_date=None):
    return [present_report(row) for row in store.list_reports(report_date)]
