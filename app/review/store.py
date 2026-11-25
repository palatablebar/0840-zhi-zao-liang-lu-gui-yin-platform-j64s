import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=8)
        connection.row_factory = sqlite3.Row
        return connection

    def init(self):
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS runs(
              id TEXT PRIMARY KEY,
              request_key TEXT UNIQUE NOT NULL,
              created_at TEXT NOT NULL,
              profile TEXT NOT NULL,
              status TEXT NOT NULL,
              revision INTEGER NOT NULL,
              payload TEXT NOT NULL,
              comment TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS rules(
              rule_version TEXT PRIMARY KEY,
              version INTEGER NOT NULL,
              spec TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS batch_versions(
              id TEXT PRIMARY KEY,
              run_id TEXT NOT NULL,
              version INTEGER NOT NULL,
              rule_version TEXT NOT NULL,
              state TEXT NOT NULL,
              request_key TEXT,
              source_sha TEXT,
              digest TEXT,
              payload TEXT NOT NULL,
              created_at TEXT NOT NULL,
              issued_at TEXT,
              UNIQUE(run_id, version)
            );
            CREATE TABLE IF NOT EXISTS revision_requests(
              request_key TEXT PRIMARY KEY,
              id TEXT UNIQUE NOT NULL,
              run_id TEXT NOT NULL,
              rule_version TEXT NOT NULL,
              version INTEGER,
              status TEXT NOT NULL,
              incomplete_reasons TEXT NOT NULL DEFAULT '[]',
              missing TEXT NOT NULL DEFAULT '[]',
              source_sha TEXT,
              payload TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS issuances(
              id TEXT PRIMARY KEY,
              run_id TEXT NOT NULL,
              version INTEGER NOT NULL,
              rule_version TEXT NOT NULL,
              revision INTEGER NOT NULL,
              report_date TEXT,
              digest TEXT,
              record TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS daily_reports(
              id TEXT PRIMARY KEY,
              report_date TEXT NOT NULL,
              request_key TEXT UNIQUE NOT NULL,
              run_id TEXT NOT NULL,
              version INTEGER NOT NULL,
              revision INTEGER,
              rule_version TEXT,
              digest TEXT,
              payload TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            ''')

    # ------------------------------------------------------------------ runs
    def find_by_request(self, request_key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE request_key=?", (request_key,)).fetchone()
        return dict(row) if row else None

    def create(self, request_key, profile, payload):
        existing = self.find_by_request(request_key)
        if existing:
            return existing
        run_id = uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute("INSERT INTO runs(id,request_key,created_at,profile,status,revision,payload) VALUES(?,?,?,?,?,?,?)",
                       (run_id, request_key, _now(), profile, "DRAFT", 0,
                        json.dumps(payload, ensure_ascii=False)))
        return self.get(run_id)

    def get(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, approved_only=False, limit=100):
        sql = "SELECT * FROM runs"
        if approved_only:
            sql += " WHERE status='APPROVED'"
        sql += " ORDER BY created_at DESC LIMIT ?"
        with self.connect() as db:
            rows = db.execute(sql, (limit,)).fetchall()
        return [dict(row) for row in rows]

    def update_payload(self, run_id, payload):
        with self.connect() as db:
            db.execute("UPDATE runs SET payload=? WHERE id=?",
                       (json.dumps(payload, ensure_ascii=False), run_id))
        return self.get(run_id)

    def review(self, run_id, decision, comment, expected_revision, payload=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return None, "missing"
            if row["revision"] != expected_revision:
                return None, "stale"
            if payload is not None:
                db.execute("UPDATE runs SET status=?, revision=revision+1, comment=?, payload=? WHERE id=?",
                           (decision, comment, json.dumps(payload, ensure_ascii=False), run_id))
            else:
                db.execute("UPDATE runs SET status=?, revision=revision+1, comment=? WHERE id=?",
                           (decision, comment, run_id))
        return self.get(run_id), None

    # ----------------------------------------------------------------- rules
    def upsert_rule(self, rule_version, version, spec):
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO rules(rule_version,version,spec,created_at) VALUES(?,?,?,?)",
                (rule_version, version, spec, _now()))

    def list_rules(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM rules ORDER BY version ASC").fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------ batch versions
    def put_version(self, run_id, version, rule_version, state, payload,
                    request_key=None, source_sha=None, digest=None, issued=False):
        version_id = uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO batch_versions
                   (id,run_id,version,rule_version,state,request_key,source_sha,digest,payload,created_at,issued_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (version_id, run_id, version, rule_version, state, request_key, source_sha, digest,
                 json.dumps(payload, ensure_ascii=False), _now(), _now() if issued else None))
        return self.get_version(run_id, version)

    def get_version(self, run_id, version):
        with self.connect() as db:
            row = db.execute("SELECT * FROM batch_versions WHERE run_id=? AND version=?",
                             (run_id, version)).fetchone()
        return dict(row) if row else None

    def get_version_by_id(self, version_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM batch_versions WHERE id=?", (version_id,)).fetchone()
        return dict(row) if row else None

    def list_versions(self, run_id):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM batch_versions WHERE run_id=? ORDER BY version ASC",
                              (run_id,)).fetchall()
        return [dict(row) for row in rows]

    def latest_version(self, run_id):
        rows = self.list_versions(run_id)
        return rows[-1] if rows else None

    def set_version_state(self, run_id, version, state, issued=False):
        with self.connect() as db:
            db.execute("UPDATE batch_versions SET state=?, issued_at=COALESCE(issued_at,?) WHERE run_id=? AND version=?",
                       (state, _now() if issued else None, run_id, version))
        return self.get_version(run_id, version)

    def finalize_version(self, run_id, version, payload, digest=None):
        with self.connect() as db:
            db.execute(
                """UPDATE batch_versions
                   SET payload=?, digest=COALESCE(?, digest), state='ISSUED', issued_at=COALESCE(issued_at,?)
                   WHERE run_id=? AND version=?""",
                (json.dumps(payload, ensure_ascii=False), digest, _now(), run_id, version))
        return self.get_version(run_id, version)

    # ------------------------------------------------- revision requests
    def put_revision_request(self, data):
        with self.connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO revision_requests
                   (request_key,id,run_id,rule_version,version,status,incomplete_reasons,missing,
                    source_sha,payload,created_at,updated_at)
                   VALUES(:request_key,:id,:run_id,:rule_version,:version,:status,
                          :incomplete_reasons,:missing,:source_sha,:payload,:created_at,:updated_at)""",
                data)
        return self.get_revision_request(data["request_key"])

    def get_revision_request(self, request_key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM revision_requests WHERE request_key=?",
                             (request_key,)).fetchone()
        return dict(row) if row else None

    def find_revision(self, revision_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM revision_requests WHERE id=?", (revision_id,)).fetchone()
        return dict(row) if row else None

    def get_revision_by_id(self, revision_id):
        return self.find_revision(revision_id)

    def list_revision_requests(self, run_id):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM revision_requests WHERE run_id=? ORDER BY created_at ASC",
                (run_id,)).fetchall()
        return [dict(row) for row in rows]

    def update_revision_request(self, request_key, **fields):
        if not fields:
            return self.get_revision_request(request_key)
        fields["updated_at"] = _now()
        assignments = ", ".join(f"{key}=:{key}" for key in fields)
        fields["request_key"] = request_key
        with self.connect() as db:
            db.execute(f"UPDATE revision_requests SET {assignments} WHERE request_key=:request_key", fields)
        return self.get_revision_request(request_key)

    # --------------------------------------------------------- issuances
    def add_issuance(self, run_id, version, rule_version, revision, record,
                     digest=None, report_date=None):
        issuance_id = uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute(
                """INSERT INTO issuances
                   (id,run_id,version,rule_version,revision,report_date,digest,record,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (issuance_id, run_id, version, rule_version, revision, report_date, digest,
                 json.dumps(record, ensure_ascii=False), _now()))
        return issuance_id

    def list_issuances(self, run_id):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM issuances WHERE run_id=? ORDER BY created_at ASC",
                              (run_id,)).fetchall()
        return [dict(row) for row in rows]

    # ----------------------------------------------------- daily reports
    def create_report(self, request_key, report_date, run_id, version, payload,
                      revision=None, rule_version=None, digest=None):
        report_id = uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO daily_reports
                   (id,report_date,request_key,run_id,version,revision,rule_version,digest,payload,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (report_id, report_date, request_key, run_id, version, revision, rule_version, digest,
                 json.dumps(payload, ensure_ascii=False), _now()))
        row = self.get_report_by_key(request_key)
        return row

    def get_report_by_key(self, request_key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM daily_reports WHERE request_key=?", (request_key,)).fetchone()
        return dict(row) if row else None

    def get_report(self, report_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM daily_reports WHERE id=?", (report_id,)).fetchone()
        return dict(row) if row else None

    def get_report_by_date(self, report_date):
        with self.connect() as db:
            row = db.execute("SELECT * FROM daily_reports WHERE report_date=? ORDER BY created_at DESC",
                             (report_date,)).fetchone()
        return dict(row) if row else None

    def list_reports(self, report_date=None):
        with self.connect() as db:
            if report_date:
                rows = db.execute("SELECT * FROM daily_reports WHERE report_date=? ORDER BY created_at DESC",
                                  (report_date,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM daily_reports ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]
