# 接口

- `GET /`：工作台。
- `GET /api/catalog`：阶段、字段、样例与规则版本目录。
- `POST /api/stages/{stage}`：按阶段返回处理结果。
- `POST /api/runs`：执行完整批次并保存草稿（可带 `rule_version`）。
- `GET /api/runs`：列出批次（可加 `?approved=true`）。
- `GET /api/runs/{id}`：读取批次，含 `rule_version`、`rule_scope`、`scope`、`versions`、`publishable_version`、`issued_version`、`incomplete_reasons` 与 `workbench` 摘要。
- `POST /api/runs/{id}/review`：签发或暂缓，提交 `decision`、`comment`、`expected_revision`；签发候选版本可带 `version`（批次版本号）或 `rule_version`。来源摘要或来源/规则版本不符返回 `409`（`SOURCE_CHANGED` / `RULE_VERSION_MISMATCH` 等），旧版本号返回 `STALE_REVIEW`。
- `GET /api/rules`、`GET /api/rule-versions`：规则版本列表（含 `params`、`scope`、`rule_scope`）。
- `POST /api/rules`：登记规则版本；同一 `rule_version` 重复登记幂等不新增。
- `GET /api/rules/current`、`GET /api/rules/{v}`：当前/指定规则。
- `POST /api/runs/{id}/revisions`：按规则版本重算归因（同义路径 `/revision`、`/recalculate`、`/recalc`、`/recompute`，扁平入口 `POST /api/revisions`、`/api/revision-requests`）。
  - JSON：`{"rule_version":"v2","request_key":"可选幂等键","complete":false,"rows":[...]}`。
  - 相同 `request_key`（或相同请求内容）重复执行只返回同一修订与同一版本，不新增版本。
  - 资料不齐时 `status=INCOMPLETE`，并给出 `incomplete_reasons` / `missing`；补齐 `rows`/`csv`/`complete=true` 后用同一 `request_key` 或 `POST /api/revisions/{rid}/resume` 续跑。
- `GET /api/runs/{id}/revisions`、`GET /api/revisions/{rid}`：修订记录。
- `GET /api/runs/{id}/versions`、`GET /api/runs/{id}/versions/{v}`：批次版本。
- `POST /api/revisions/{rid}/publish|issue|approve`、`POST /api/runs/{id}/versions/{v}/publish`：签发候选版本。
- `GET /api/runs/{id}/evidence`：下载已签发证据；`?revision=N`（或 `?version=N`）取回指定已签发版本的固化快照，摘要稳定。
- `GET /api/runs/{id}/evidence/history`：证据版本历史。
- `POST /api/daily-reports`、`POST /api/runs/{id}/daily-reports`：生成生产日报，固定引用某个已签发版本（默认当前已签发版本）；同日期/批次/版本幂等。
- `GET /api/daily-reports`、`GET /api/daily-reports/{id-or-date}`：列出/读取日报；规则重算与新版本签发不改变已生成日报。
- `GET /api/runs/{id}/publications`（同义 `/issuances`）、`GET /api/publications`、`GET /api/issuances`：保留的签发记录。
- `GET /api/examples/{profile}`：下载样例。
- `GET /healthz`：健康检查。

`/api/batches/...` 是 `/api/runs/...` 的同义前缀；`/api/production-daily-reports`、`/api/reports/daily`、`/api/daily-report(s)` 均为日报资源的同义路径。

## 证据字段

证据 schema 仍为 `workbench-evidence/v1`，`review` 固定为 `{status, revision, comment}`；
顶层与 `provenance` 同时给出来源摘要（`source.sha256` / `source_version`）和规则版本
（`rule_version`、`rule_scope`、`scope`），保证来源与规则版本均可回溯。
