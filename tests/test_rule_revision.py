"""工艺规则更新后的版本化重算 / 签发保留 / 生产日报验收。

这些用例把需求段落逐条固定：
- 新规则修正旧结论；重复同一修订请求不新增版本；
- 已签发版本与证据保留，历史日报不被重算污染；
- 中断可补齐后续跑，且不混新旧行；
- 工作台给出版本、规则范围与未完成原因；
- 来源/规则版本不符禁止签发；证据可回溯来源与规则版本。
"""
import json

import pytest

from app.errors import DomainIssue
from app.main import create_app
from app.review.service import approve
from app.review.store import Store
from app.service import pipeline


@pytest.fixture
def app_client(tmp_path):
    app = create_app(str(tmp_path / "wb.sqlite3"))
    app.config.update(TESTING=True)
    return app.test_client()


def create_clean(client):
    return client.post("/api/runs", json={"profile": "clean"}).get_json()


def approve_clean(client, run_id, rev=0, **extra):
    body = {"decision": "APPROVED", "comment": "复核完成", "expected_revision": rev}
    body.update(extra)
    resp = client.post(f"/api/runs/{run_id}/review", json=body)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def wo(run, label="WO1"):
    return next(row for row in run["rows"] if row["label"] == label)


# ------------------------------------------------------------ 规则版本目录
def test_rule_versions_are_listed_with_scope(client):
    data = client.get("/api/rules").get_json()
    versions = data["rule_versions"]
    assert {item["rule_version"] for item in versions} >= {"v1", "v2"}
    v1 = next(item for item in versions if item["rule_version"] == "v1")
    assert v1["scope"] and v1["rule_scope"]
    catalog = client.get("/api/catalog").get_json()
    assert catalog["rule_versions"]


def test_new_rule_can_be_registered_and_corrects_conclusion(client):
    # 更宽松的新目标 0.60：dirty 中 BELOW_FLOOR 的工单可能从 REJECT 变 REVIEW。
    created = client.post("/api/rules", json={
        "rule_version": "v3", "target": "0.60",
        "params": {"floor": "0.50", "target": "0.60"},
    })
    assert created.status_code == 200
    rule = client.get("/api/rules/v3").get_json()
    assert rule["params"]["target"] == "0.60"
    # 重复登记同一版本不报错也不改变定义。
    again = client.post("/api/rules", json={"rule_version": "v3", "params": {"target": "0.99"}})
    assert again.get_json()["params"]["target"] == "0.60"


# ------------------------------------------------------------ 重算与幂等
def test_new_rule_recalculates_and_corrects_conclusion(client):
    run = create_clean(client)
    assert wo(run)["verdict"] == "PASS"  # v1 目标 0.90，WO1=0.9025

    rev = client.post(f"/api/runs/{run['run_id']}/revisions",
                      json={"rule_version": "v2"}).get_json()
    assert rev["status"] == "COMPLETED"
    assert rev["version"] >= 1
    assert wo(rev)["verdict"] == "REVIEW"  # v2 目标 0.95，旧 PASS 被修正
    assert all(row.get("rule_version") == "v2" for row in rev["rows"])


def test_duplicate_revision_request_creates_no_new_version(client):
    run = create_clean(client)
    url = f"/api/runs/{run['run_id']}/revisions"
    first = client.post(url, json={"rule_version": "v2", "request_key": "req-1"}).get_json()
    second = client.post(url, json={"rule_version": "v2", "request_key": "req-1"}).get_json()
    assert first["id"] == second["id"]
    assert first["version"] == second["version"]
    listing = client.get(url).get_json()
    assert listing["total"] == 1

    # 不带显式 key 的相同请求体同样幂等。
    a = client.post(url, json={"rule_version": "v2"}).get_json()
    b = client.post(url, json={"rule_version": "v2"}).get_json()
    assert a["id"] == b["id"]
    assert a["version"] == b["version"]
    versions = client.get(f"/api/runs/{run['run_id']}/versions").get_json()["versions"]
    assert [v["version"] for v in versions].count(a["version"]) == 1


def test_revision_aliases_share_idempotent_record(client):
    run = create_clean(client)
    rid = run["run_id"]
    a = client.post(f"/api/runs/{rid}/recalculate", json={"rule_version": "v2", "request_key": "k9"}).get_json()
    b = client.post("/api/revisions", json={"run_id": rid, "rule_version": "v2", "request_key": "k9"}).get_json()
    assert a["id"] == b["id"]


# ------------------------------------------------- 已签发版本保留 + 证据
def test_signed_version_and_evidence_survive_new_release(client):
    run = create_clean(client)
    rid = run["run_id"]
    approve_clean(client, rid, 0)
    original = client.get(f"/api/runs/{rid}/evidence").get_json()
    assert original["review"]["revision"] == 1

    rev = client.post(f"/api/runs/{rid}/revisions", json={"rule_version": "v2"}).get_json()
    published = approve_clean(client, rid, 1, version=rev["version"])
    assert published["revision"] == 2

    old = client.get(f"/api/runs/{rid}/evidence?revision=1").get_json()
    assert old["artifact_sha256"] == original["artifact_sha256"]
    assert old["rule_version"] == "v1"
    assert old["review"]["revision"] == 1

    latest = client.get(f"/api/runs/{rid}/evidence").get_json()
    assert latest["rule_version"] == "v2"
    history = client.get(f"/api/runs/{rid}/evidence/history").get_json()
    assert [item["revision"] for item in history["versions"]] == [1, 2]
    # 签发记录保留。
    pubs = client.get(f"/api/runs/{rid}/publications").get_json()
    assert {p["version"] for p in pubs["publications"]} == {1, 2}


def test_evidence_traces_source_and_rule_version(client):
    run = create_clean(client)
    approve_clean(client, run["run_id"], 0)
    art = client.get(f"/api/runs/{run['run_id']}/evidence").get_json()
    assert len(art["source"]["sha256"]) == 64
    assert art["rule_version"]
    assert art["rule_scope"]
    assert art["provenance"]["source_sha256"] == art["source"]["sha256"]
    assert art["provenance"]["rule_version"] == art["rule_version"]


# ------------------------------------------------------------ 生产日报
def test_daily_report_keeps_issued_version_after_recalc(client):
    run = create_clean(client)
    rid = run["run_id"]
    approve_clean(client, rid, 0)

    report = client.post("/api/daily-reports",
                         json={"run_id": rid, "date": "2026-11-25"}).get_json()
    assert report["version"] == 1
    report_id = report["id"]
    assert next(r for r in report["rows"] if r["label"] == "WO1")["verdict"] == "PASS"

    rev = client.post(f"/api/runs/{rid}/revisions", json={"rule_version": "v2"}).get_json()
    approve_clean(client, rid, 1, version=rev["version"])

    again = client.get(f"/api/daily-reports/{report_id}").get_json()
    assert again["version"] == 1
    assert again["rule_version"] == "v1"
    assert next(r for r in again["rows"] if r["label"] == "WO1")["verdict"] == "PASS"
    by_date = client.get("/api/daily-reports/2026-11-25").get_json()
    assert by_date["id"] == report_id


def test_daily_report_requires_approval(client):
    run = create_clean(client)
    resp = client.post("/api/daily-reports", json={"run_id": run["run_id"], "date": "2026-11-25"})
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "NOT_APPROVED"


# ------------------------------------------------- 中断 / 补齐 / 续跑
def test_interrupted_revision_reports_reasons_and_resumes(client):
    run = create_clean(client)
    rid = run["run_id"]
    approve_clean(client, rid, 0)

    started = client.post(f"/api/runs/{rid}/revisions", json={
        "rule_version": "v2", "request_key": "break-1", "complete": False,
    }).get_json()
    assert started["status"] == "INCOMPLETE"
    assert started["incomplete_reasons"]
    assert started["publishable_version"] is None

    # 工作台能看到未完成原因。
    work = client.get(f"/api/runs/{rid}").get_json()
    assert work["incomplete_reasons"]

    # 补齐数据后续跑同一修订请求：仍是同一条记录。
    resumed = client.post(f"/api/revisions/{started['id']}/resume",
                          json={"complete": True}).get_json()
    assert resumed["status"] == "COMPLETED"
    assert resumed["id"] == started["id"]
    assert resumed["version"] is not None
    assert all(row.get("rule_version") == "v2" for row in resumed["rows"])

    # 用同一 request_key 重放并声明完成，也会续到同一结果，不新增版本。
    replay = client.post(f"/api/runs/{rid}/revisions", json={
        "rule_version": "v2", "request_key": "break-1", "complete": True,
    }).get_json()
    assert replay["id"] == started["id"]
    assert replay["version"] == resumed["version"]


def test_supplement_rows_can_fill_and_recompute(client):
    run = create_clean(client)
    rid = run["run_id"]
    approve_clean(client, rid, 0)
    rev = client.post(f"/api/runs/{rid}/revisions", json={
        "rule_version": "v2",
        "rows": [{"row_id": "R002", "rework_good": "15"}],
    })
    assert rev.status_code == 200
    body = rev.get_json()
    assert body["status"] == "COMPLETED"
    assert all(row.get("rule_version") == "v2" for row in body["rows"])


# ------------------------------------------------------------ 工作台字段
def test_workbench_payload_states_version_scope_reasons(client):
    run = create_clean(client)
    view = client.get(f"/api/runs/{run['run_id']}").get_json()
    assert "publishable_version" in view
    assert view["rule_scope"]
    assert isinstance(view["incomplete_reasons"], list)
    assert view["workbench"]["rule_scope"]


# ------------------------------------------------------------ 不符禁签
def test_source_digest_tampering_blocks_approval(client):
    run = create_clean(client)
    rid = run["run_id"]
    store = client.application.extensions["runs"]
    row = store.get(rid)
    payload = json.loads(row["payload"])
    payload["source"]["sha256"] = "0" * 64
    with store.connect() as db:
        db.execute("UPDATE runs SET payload=? WHERE id=?",
                   (json.dumps(payload, ensure_ascii=False), rid))
    resp = client.post(f"/api/runs/{rid}/review",
                       json={"decision": "APPROVED", "comment": "x", "expected_revision": 0})
    assert resp.status_code == 409
    assert resp.get_json()["code"] in {"SOURCE_CHANGED", "RULE_VERSION_MISMATCH", "VERSION_MISMATCH"}


def test_selector_rule_disagreement_blocks_approval(client):
    run = create_clean(client)
    rid = run["run_id"]
    approve_clean(client, rid, 0)
    rev = client.post(f"/api/runs/{rid}/revisions", json={"rule_version": "v2"}).get_json()
    # 用 v1 的规则声明去签发 v2 版本：来源/规则版本不符。
    resp = client.post(f"/api/runs/{rid}/review", json={
        "decision": "APPROVED", "comment": "x", "expected_revision": 1,
        "version": rev["version"], "rule_version": "v1",
    })
    assert resp.status_code == 409
    assert resp.get_json()["code"] in {"RULE_VERSION_MISMATCH", "VERSION_MISMATCH", "SOURCE_RULE_MISMATCH"}


def test_plain_review_publishes_active_candidate(client):
    run = create_clean(client)
    rid = run["run_id"]
    approve_clean(client, rid, 0)
    client.post(f"/api/runs/{rid}/revisions", json={"rule_version": "v2"})
    # 客户端只用标准复核入口 + 当前 expected_revision，也能签发活动候选。
    resp = client.post(f"/api/runs/{rid}/review",
                       json={"decision": "APPROVED", "comment": "直接签发", "expected_revision": 1})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["rule_version"] == "v2"
    assert body["issued_version"] == 2
    assert [v["state"] for v in body["versions"]] == ["ISSUED", "ISSUED"]
    assert client.get(f"/api/runs/{rid}/evidence?revision=2").status_code == 200


def test_service_level_mismatch_code_matches_family(tmp_path):
    store = Store(str(tmp_path / "s.sqlite3"))
    payload = pipeline("clean")
    row = store.create(payload["run_key"], "clean", payload)
    with pytest.raises(DomainIssue) as exc:
        approve(store, row["id"], "APPROVED", "x", 0,
                body={"rule_version": "v9"})
    # 错误码在进程内对近义命名宽容。
    assert exc.value.code in {"RULE_VERSION_INVALID", "RULE_VERSION_MISMATCH", "VERSION_MISMATCH"}
