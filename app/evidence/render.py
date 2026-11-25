from app.errors import DomainIssue
from app.evidence.manifest import stable_digest
from app.rulebook import DEFAULT_RULE_VERSION


def render(run):
    if run is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    if run["status"] != "APPROVED":
        raise DomainIssue("NOT_APPROVED", 409)
    rule_version = run.get("rule_version") or DEFAULT_RULE_VERSION
    artifact = {
        "schema": "workbench-evidence/v1",
        "run_id": run["run_id"],
        "project": run["project"],
        "rule": run["rule"],
        # 规则版本与范围：证据可回溯到具体工艺规则。
        "rule_version": rule_version,
        "version": run.get("revision"),
        "rule_scope": run.get("rule_scope"),
        "scope": run.get("scope"),
        "rule_info": run.get("rule_info"),
        "profile": run["profile"],
        # source 内含 sha256（来源版本）与 source.rule_version。
        "source": run["source"],
        "source_version": (run.get("source") or {}).get("sha256"),
        "provenance": {
            "source_sha256": (run.get("source") or {}).get("sha256"),
            "source_filename": (run.get("source") or {}).get("filename"),
            "source_version": (run.get("source") or {}).get("sha256"),
            "rule_version": rule_version,
            "rule_scope": run.get("rule_scope"),
            "scope": run.get("scope"),
            "revision": run["revision"],
        },
        "counts": run["counts"],
        "ledger": run["ledger"],
        "summary": run["summary"],
        # 注意：既有测试要求 review 恰好是这三个键，不能往里加字段。
        "review": {"status": run["status"], "revision": run["revision"],
                   "comment": run.get("comment", "")},
    }
    # 不把易变的 versions 列表放进证据，保证历史快照摘要稳定。
    artifact["artifact_sha256"] = stable_digest(artifact)
    return artifact
