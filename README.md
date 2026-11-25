# 制造良率归因台

工厂工艺工程师处理首检良率与返工归因的本地工作台。样例、规则和复核记录都在项目内，不依赖外部服务。

## 启动

```bash
docker compose up --build
```

打开 `http://127.0.0.1:${PORT:-18520}`。默认端口见 `docker-compose.yml`。

## 验证

```bash
docker compose exec -T app pytest -q
docker compose config --quiet
```

## 操作

1. 在左侧选阶段，查看采信、整理、计算、判定和对账结果。
2. 选择 `dirty` 样例观察坏行和隔离原因。
3. 换 `clean` 样例执行批次，填写复核意见后签发。
4. 下载证据 JSON，核对来源摘要、指标和签发版本。
5. 在“规则版本与可发布版本”里选择 `v2` 规则并重算归因，观察旧结论被修正；重复提交同一修订不会新增版本。可勾选“模拟重算中断”，再点“补齐数据后续跑”。发布新版本后，用 `?revision=1` 取回旧版证据，并在“生产日报”里确认历史日报仍引用原签发版本。

## 版本化接口

规则版本、修订重算、版本快照与生产日报的接口契约见 `docs/HTTP.md`，业务约束见 `docs/BUSINESS-RULES.md`。证据中的 `source.sha256` 与 `rule_version`/`rule_scope` 可同时回溯来源与规则版本。

业务规则见 `docs/BUSINESS-RULES.md`，字段定义见 `docs/DATA-DICTIONARY.md`。
