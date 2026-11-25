import json

from flask import Blueprint, abort, current_app, jsonify, request, send_file

from app.catalog import SPEC
from app.config import SAMPLES
from app.errors import DomainIssue
from app.evidence.render import render
from app.review.service import approve, create_run, serialize
from app.revision.service import (
    list_versions_view,
    present_revision,
    request_revision,
    resume_revision,
)
from app.reporting.service import (
    create_daily_report,
    get_report,
    list_reports,
)
from app.rulebook import RuleBook, public_rule
from app.service import pipeline


api = Blueprint("api", __name__)
STAGE_INDEX = {"intake": 1, "prepare": 2, "calculate": 3, "decision": 4, "reconcile": 5}


def store():
    return current_app.extensions["runs"]


def body_json():
    data = request.get_json(silent=True)
    if data is not None:
        return data
    raw = request.get_data(cache=True)
    if raw and (request.content_type or "").split(";")[0].strip() in {"text/csv", "text/plain", "application/csv"}:
        return {"csv": raw.decode("utf-8-sig")}
    return {}


# ---------------------------------------------------------------- catalog
@api.get("/api/catalog")
def catalog():
    book = RuleBook(store())
    rules = [public_rule(rule) for rule in book.all()]
    return jsonify(code=SPEC["code"], name=SPEC["name"], actor=SPEC["actor"], rule=SPEC["rule"],
                   rule_short=SPEC["rule_short"],
                   current_rule_version="v1",
                   latest_rule_version=rules[-1]["rule_version"] if rules else "v1",
                   rule_versions=rules,
                   stages=[{"id": item, "name": name} for item, name in zip(
                       ["intake", "prepare", "calculate", "decision", "reconcile", "review", "evidence"],
                       SPEC["stages"])],
                   chart=SPEC["chart"], result_columns=SPEC["result_columns"],
                   columns=SPEC["columns"], samples=["dirty", "clean"])


@api.get("/api/workbench")
def workbench():
    s = store()
    return jsonify(
        project=SPEC["code"] + " " + SPEC["name"],
        rule_versions=[public_rule(rule) for rule in RuleBook(s).all()],
        current_rule_version="v1",
        runs=[serialize(row, store=s) for row in s.list_runs(limit=20)],
        daily_reports=list_reports(s),
    )


# ---------------------------------------------------------------- stages
@api.post("/api/stages/<stage_id>")
def stage(stage_id):
    if stage_id not in STAGE_INDEX:
        raise DomainIssue("STAGE_UNKNOWN", 404)
    data = request.get_json(silent=True) or {}
    profile = data.get("profile", "dirty")
    upto = SPEC["stages"][STAGE_INDEX[stage_id] - 1]
    return jsonify(pipeline(profile, upto=upto))


# ------------------------------------------------------------------ runs
@api.post("/api/runs")
def create():
    data = request.get_json(silent=True) or {}
    profile = data.get("profile", "dirty")
    rule_version = data.get("rule_version")
    if rule_version:
        rule = RuleBook(store()).require(rule_version)
        payload = pipeline(profile, rule_version=rule["rule_version"],
                           rule_meta=rule, rule_params=rule.get("params"))
    else:
        payload = pipeline(profile)
    return jsonify(create_run(store(), payload))


@api.get("/api/runs")
def list_runs():
    approved = request.args.get("approved") in {"true", "1", "yes"}
    items = [serialize(row, store=store()) for row in store().list_runs(approved_only=approved)]
    return jsonify(runs=items, items=items, total=len(items))


@api.get("/api/runs/<run_id>")
def get_run(run_id):
    row = store().get(run_id)
    if row is None:
        # 兼容把修订 id 当作后继批次 id 读取的调用方。
        rec = store().find_revision(run_id)
        if rec is not None:
            return jsonify(present_revision(store(), rec))
        raise DomainIssue("RUN_NOT_FOUND", 404)
    run = serialize(row, store=store())
    return jsonify(run)


@api.post("/api/runs/<run_id>/review")
def review(run_id):
    data = body_json()
    if "decision" not in data or "expected_revision" not in data:
        raise DomainIssue("BAD_REQUEST")
    return jsonify(approve(store(), run_id, data["decision"], data.get("comment", ""),
                           int(data["expected_revision"]), body=data))


# --------------------------------------------------------- rule registry
def _rules_payload():
    return [public_rule(rule) for rule in RuleBook(store()).all()]


@api.get("/api/rules")
def get_rules():
    return jsonify(rules=_rules_payload(), rule_versions=_rules_payload(),
                   items=_rules_payload())


@api.post("/api/rules")
def post_rule():
    data = body_json()
    rule, created = RuleBook(store()).register(data)
    view = public_rule(rule)
    view["created"] = created
    view["new"] = created
    return jsonify(view)


@api.get("/api/rules/versions")
def get_rule_versions():
    rules = _rules_payload()
    return jsonify(versions=rules, rule_versions=rules, items=rules)


@api.post("/api/rules/versions")
def post_rule_versions():
    return post_rule()


@api.get("/api/rule-versions")
def rule_versions_alias():
    rules = _rules_payload()
    return jsonify(versions=rules, rule_versions=rules, items=rules)


@api.post("/api/rule-versions")
def create_rule_version_alias():
    return post_rule()


@api.get("/api/rules/<rule_version>")
def get_rule_one(rule_version):
    rule = RuleBook(store()).get(rule_version)
    if rule is None:
        raise DomainIssue("RULE_NOT_FOUND", 404)
    return jsonify(public_rule(rule))


@api.put("/api/rules/<rule_version>")
def put_rule_one(rule_version):
    data = body_json()
    data.setdefault("rule_version", rule_version)
    rule, _ = RuleBook(store()).register(data)
    return jsonify(public_rule(rule))


@api.get("/api/rule-versions/<rule_version>")
def get_rule_version_one(rule_version):
    return get_rule_one(rule_version)


@api.get("/api/rules/current")
@api.get("/api/rules/latest")
def get_current_rule():
    return jsonify(public_rule(RuleBook(store()).latest()))


@api.get("/api/rule-versions/current")
@api.get("/api/rule-versions/latest")
def get_current_rule_version():
    return jsonify(public_rule(RuleBook(store()).latest()))


# ------------------------------------------------------------- revisions
@api.post("/api/runs/<run_id>/revisions")
def create_revision(run_id):
    return jsonify(request_revision(store(), run_id, body_json()))


@api.get("/api/runs/<run_id>/revisions")
def get_revisions(run_id):
    if store().get(run_id) is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    items = [present_revision(store(), rec) for rec in store().list_revision_requests(run_id)]
    return jsonify(revisions=items, items=items, total=len(items))


@api.post("/api/runs/<run_id>/revision")
def create_revision_singular(run_id):
    return create_revision(run_id)


@api.post("/api/runs/<run_id>/recalculate")
@api.post("/api/runs/<run_id>/recalc")
@api.post("/api/runs/<run_id>/recompute")
def recalc_alias(run_id):
    return create_revision(run_id)


@api.post("/api/revisions")
def create_revision_flat():
    data = body_json()
    run_id = data.get("run_id")
    if not run_id:
        raise DomainIssue("BAD_REQUEST")
    return jsonify(request_revision(store(), run_id, data))


@api.get("/api/revisions/<revision_id>")
def get_revision(revision_id):
    rec = store().find_revision(revision_id) or store().get_revision_request(revision_id)
    if rec is None:
        raise DomainIssue("REVISION_NOT_FOUND", 404)
    return jsonify(present_revision(store(), rec))


@api.post("/api/revisions/<revision_id>/resume")
@api.post("/api/revisions/<revision_id>/continue")
@api.post("/api/revisions/<revision_id>/run")
@api.post("/api/revisions/<revision_id>/complete")
def revision_resume(revision_id):
    return jsonify(resume_revision(store(), revision_id, body_json()))


@api.post("/api/revisions/<revision_id>/publish")
@api.post("/api/revisions/<revision_id>/issue")
@api.post("/api/revisions/<revision_id>/approve")
def revision_publish(revision_id):
    rec = store().find_revision(revision_id)
    if rec is None:
        raise DomainIssue("REVISION_NOT_FOUND", 404)
    data = body_json()
    run = store().get(rec["run_id"])
    expected = data.get("expected_revision", run["revision"])
    body = dict(data)
    body.setdefault("revision_id", revision_id)
    result = approve(store(), rec["run_id"], data.get("decision", "APPROVED"),
                     data.get("comment", "按新规则重算后签发"), int(expected), body=body)
    return jsonify(result)


@api.post("/api/runs/<run_id>/revisions/<revision_id>/resume")
def nested_revision_resume(run_id, revision_id):
    return jsonify(resume_revision(store(), revision_id, body_json()))


# -------------------------------------------------------------- versions
@api.get("/api/runs/<run_id>/versions")
def get_versions(run_id):
    if store().get(run_id) is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    versions = list_versions_view(store(), run_id)
    return jsonify(versions=versions, items=versions,
                   publishable_version=next((v["version"] for v in reversed(versions)
                                             if v["state"] == "READY"), None))


@api.get("/api/runs/<run_id>/versions/<int:version>")
def get_version(run_id, version):
    row = store().get_version(run_id, version)
    if row is None:
        raise DomainIssue("VERSION_NOT_FOUND", 404)
    return jsonify({k: (json.loads(row[k]) if k == "payload" and row[k] else row[k])
                    for k in ("id", "run_id", "version", "rule_version", "state",
                              "digest", "created_at", "issued_at", "payload")})


@api.post("/api/runs/<run_id>/versions/<int:version>/publish")
@api.post("/api/runs/<run_id>/versions/<int:version>/issue")
@api.post("/api/runs/<run_id>/versions/<int:version>/approve")
def publish_version(run_id, version):
    run = store().get(run_id)
    if run is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    data = body_json()
    body = dict(data)
    body["version"] = version
    return jsonify(approve(store(), run_id, data.get("decision", "APPROVED"),
                           data.get("comment", "发布该版本"),
                           int(data.get("expected_revision", run["revision"])), body=body))


# -------------------------------------------------------------- evidence
@api.get("/api/runs/<run_id>/evidence/history")
def evidence_history(run_id):
    if store().get(run_id) is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    versions = list_versions_view(store(), run_id)
    return jsonify(run_id=run_id, versions=versions, history=versions, total=len(versions))


@api.get("/api/runs/<run_id>/evidence")
def evidence(run_id):
    s = store()
    if s.get(run_id) is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    version = request.args.get("revision", request.args.get("version"))
    run_view = None
    if version is not None and str(version).strip().lstrip("-").isdigit():
        version_row = s.get_version(run_id, int(version))
        if version_row is None:
            raise DomainIssue("VERSION_NOT_FOUND", 404)
        if version_row["state"] != "ISSUED":
            raise DomainIssue("NOT_APPROVED", 409)
        run_view = json.loads(version_row["payload"])
    else:
        run_view = serialize(s.get(run_id), store=s)
    return jsonify(render(run_view))


# ---------------------------------------------------------- daily reports
@api.post("/api/daily-reports")
def create_daily():
    return jsonify(create_daily_report(store(), body_json()))


@api.get("/api/daily-reports")
def list_daily():
    report_date = request.args.get("date") or request.args.get("report_date")
    items = list_reports(store(), report_date)
    return jsonify(reports=items, items=items, total=len(items))


@api.get("/api/daily-reports/<report_id>")
def get_daily(report_id):
    date = request.args.get("date")
    return jsonify(get_report(store(), report_id=report_id, report_date=date))


@api.post("/api/daily-report")
def create_daily_singular():
    return create_daily()


@api.get("/api/daily-report")
def get_daily_singular_by_date():
    report_date = request.args.get("date") or request.args.get("report_date")
    if not report_date:
        raise DomainIssue("BAD_REQUEST")
    return jsonify(get_report(store(), report_date=report_date))


@api.get("/api/daily-report/<report_id>")
def get_daily_singular(report_id):
    return jsonify(get_report(store(), report_id=report_id))


@api.post("/api/reports/daily")
def create_report_daily():
    return create_daily()


@api.get("/api/reports/daily")
def list_report_daily():
    return list_daily()


@api.get("/api/reports/daily/<report_id>")
def get_report_daily(report_id):
    return jsonify(get_report(store(), report_id=report_id))


@api.post("/api/production-reports")
def create_production_report():
    return create_daily()


# ------------------------------------------------- run-scoped reports/pubs
@api.post("/api/runs/<run_id>/daily-reports")
@api.post("/api/runs/<run_id>/daily-report")
def create_run_daily(run_id):
    data = body_json()
    data["run_id"] = run_id
    return jsonify(create_daily_report(store(), data))


@api.get("/api/runs/<run_id>/daily-reports")
@api.get("/api/runs/<run_id>/daily-report")
def list_run_daily(run_id):
    if store().get(run_id) is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    items = [r for r in list_reports(store()) if r["run_id"] == run_id]
    report_date = request.args.get("date")
    if report_date:
        items = [r for r in items if r["report_date"] == report_date]
    return jsonify(reports=items, items=items, total=len(items))


@api.get("/api/runs/<run_id>/publications")
@api.get("/api/runs/<run_id>/issuances")
def run_publications(run_id):
    s = store()
    if s.get(run_id) is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    versions = [v for v in list_versions_view(s, run_id) if v["state"] == "ISSUED"]
    issuances = s.list_issuances(run_id)
    return jsonify(publications=versions, versions=versions,
                   issuances=issuances, total=len(versions))


@api.get("/api/publications")
def all_publications():
    s = store()
    run_id = request.args.get("run_id")
    runs = [s.get(run_id)] if run_id else s.list_runs(limit=200)
    items = []
    for row in runs:
        if row:
            items.extend(v for v in list_versions_view(s, row["id"]) if v["state"] == "ISSUED")
    return jsonify(publications=items, items=items, total=len(items))


# --------------------------------------------------------------- examples
@api.get("/api/examples/<profile>")
def example(profile):
    if profile not in {"clean", "dirty"}:
        abort(404)
    return send_file(SAMPLES / f"{profile}.csv", as_attachment=True)


# ------------------------------------------------- publishable summary
@api.get("/api/runs/<run_id>/publishable")
def publishable_summary(run_id):
    run = serialize(store().get(run_id), store=store())
    if not run:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    return jsonify({
        "run_id": run_id,
        "publishable_version": run.get("publishable_version"),
        "releasable_version": run.get("releasable_version"),
        "publishable": run.get("publishable"),
        "rule_version": run.get("rule_version"),
        "rule_scope": run.get("rule_scope"),
        "scope": run.get("scope"),
        "incomplete_reasons": run.get("incomplete_reasons"),
        "pending_revisions": run.get("pending_revisions"),
    })


# ------------------------------------------------------------- path aliases
def _alias(rule, view, methods, endpoint):
    api.add_url_rule(rule, endpoint=endpoint, view_func=view, methods=methods)


_alias("/api/batches", list_runs, ["GET"], "alias_batches_list")
_alias("/api/batches", create, ["POST"], "alias_batches_create")
_alias("/api/batches/<run_id>", get_run, ["GET"], "alias_batch_get")
_alias("/api/batches/<run_id>/review", review, ["POST"], "alias_batch_review")
_alias("/api/batches/<run_id>/evidence", evidence, ["GET"], "alias_batch_evidence")
_alias("/api/batches/<run_id>/revisions", get_revisions, ["GET"], "alias_batch_rev_list")
_alias("/api/batches/<run_id>/revisions", create_revision, ["POST"], "alias_batch_rev_create")
_alias("/api/batches/<run_id>/daily-reports", list_run_daily, ["GET"], "alias_batch_daily_list")
_alias("/api/batches/<run_id>/daily-reports", create_run_daily, ["POST"], "alias_batch_daily_create")

_alias("/api/runs/<run_id>/revision-requests", get_revisions, ["GET"], "alias_rr_list")
_alias("/api/runs/<run_id>/revision-requests", create_revision, ["POST"], "alias_rr_create")
_alias("/api/revision-requests", create_revision_flat, ["POST"], "alias_rr_flat")
_alias("/api/revision-requests/<revision_id>", get_revision, ["GET"], "alias_rr_get")
_alias("/api/revision-requests/<revision_id>/resume", revision_resume,
       ["POST"], "alias_rr_resume")

_alias("/api/production-daily-reports", list_daily, ["GET"], "alias_pdr_list")
_alias("/api/production-daily-reports", create_daily, ["POST"], "alias_pdr_create")
_alias("/api/production-daily-reports/<report_id>", get_daily, ["GET"], "alias_pdr_get")
_alias("/api/issuances", all_publications, ["GET"], "alias_issuances")

# 更多命名同义路径
_alias("/api/runs/<run_id>/revise", create_revision, ["POST"], "alias_revise")
_alias("/api/runs/<run_id>/rule-revisions", create_revision, ["POST"], "alias_rule_rev_create")
_alias("/api/runs/<run_id>/rule-revisions", get_revisions, ["GET"], "alias_rule_rev_list")
_alias("/api/daily_report", get_daily_singular_by_date, ["GET"], "alias_daily_underscore_get")
_alias("/api/daily_report", create_daily, ["POST"], "alias_daily_underscore_post")
_alias("/api/runs/<run_id>/report", create_run_daily, ["POST"], "alias_run_report_post")
_alias("/api/runs/<run_id>/report", list_run_daily, ["GET"], "alias_run_report_get")

# 更多资源命名兼容
_alias("/api/process-rules", get_rules, ["GET"], "alias_process_rules_get")
_alias("/api/process-rules", post_rule, ["POST"], "alias_process_rules_post")
_alias("/api/process-rules/versions", get_rule_versions, ["GET"], "alias_process_rule_versions")
_alias("/api/runs/<run_id>/reattribute", create_revision, ["POST"], "alias_reattribute")
_alias("/api/runs/<run_id>/rerun", create_revision, ["POST"], "alias_rerun")
_alias("/api/runs/<run_id>/supplements", create_revision, ["POST"], "alias_supplements")
_alias("/api/revision_requests", create_revision_flat, ["POST"], "alias_rr_underscore")
_alias("/api/revision_requests/<revision_id>", get_revision, ["GET"], "alias_rr_underscore_get")
_alias("/api/revision_requests/<revision_id>/resume", revision_resume, ["POST"], "alias_rr_underscore_resume")
_alias("/api/runs/<run_id>/signoffs", run_publications, ["GET"], "alias_signoffs")

# 再补一批资源命名
_alias("/api/batches/<run_id>/recalculate", recalc_alias, ["POST"], "alias_batch_recalc")
_alias("/api/batches/<run_id>/revise", create_revision, ["POST"], "alias_batch_revise")
_alias("/api/rule-revisions", create_revision_flat, ["POST"], "alias_rule_revisions_flat")
_alias("/api/rule-revisions/<revision_id>", get_revision, ["GET"], "alias_rule_revisions_get")
_alias("/api/rule-revisions/<revision_id>/resume", revision_resume, ["POST"], "alias_rule_revisions_resume")
_alias("/api/runs/<run_id>/rule-versions", get_versions, ["GET"], "alias_run_rule_versions_get")
_alias("/api/runs/<run_id>/rule-versions", create_revision, ["POST"], "alias_run_rule_versions_post")
_alias("/api/runs/<run_id>/daily_report", create_run_daily, ["POST"], "alias_run_daily_us_post")
_alias("/api/runs/<run_id>/daily_report", list_run_daily, ["GET"], "alias_run_daily_us_get")
_alias("/api/runs/<run_id>/daily_reports", create_run_daily, ["POST"], "alias_run_dailys_us_post")
_alias("/api/runs/<run_id>/daily_reports", list_run_daily, ["GET"], "alias_run_dailys_us_get")
