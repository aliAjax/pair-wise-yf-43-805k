# 实验室仪器校准与方法验证

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8309`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 时效链（instrument → calibration → method → result）

- 结果放行时会**快照**所依据的要素：校准记录 id/版本/有效期、方法版本、放行时间。
- 校准**过期**、校准记录被**取代**、方法被**撤销**、校准到期日被改短而不再覆盖放行日期时，相关已放行结果会在同一事务内**自动重算**并转入 `review_pending`：
  - 原值不删除、不覆盖，保存在 `data.original_release`；重算值写入 `data.recalculated_value`，并记录失效原因。
  - 同时生成持久化复核待办（`review_items`），复核人可 `keep`（放行重算值/原值）或 `requeue`（转复测）。
- 同一仪器两条校准记录时间区间重叠时，批准/改期一律**停下**，生成 `calibration_overlap` 待办，禁止挑一条覆盖；必须由授权人显式裁决 `supersede`（新记录生效、旧记录标记 superseded，不删除）或 `reject_new`（驳回新记录）。
- 并发的晚到提交不会因版本号过期而失败：按**最新版本重新校验**。仍不适用时落库为 `pending_changes`，在每次提交后、手动对账或**下次启动**时继续重试；阻塞解除后自动生效。所有待办与待处理状态都在 SQLite 中持久化，重启后自动对账（重放待处理 + 全链重扫）。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动对账和信号处理；`--date YYYY-MM-DD` 可固定业务时钟（演示/测试时效链）。
- `src/domain.py`：角色、数据结构、领域异常（含 `CalibrationOverlapError`）和基础校验。
- `src/rules.py`：状态机、权限、领域计算、校准区间/重叠判定、时效链评估、结果重算。
- `src/repository.py`：SQLite建表、单事务工作单元（`transaction()`）、乐观锁、复核待办与待处理提交表。
- `src/service.py`：用例编排、晚到提交重放、时效链扫描、重叠裁决、重启对账、审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则、失败场景及时效链测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8309
```

服务启动时会自动建表并重放待处理提交、重扫时效链（启动对账结果打印在日志中）。`--host`可修改监听地址，`--db`可指定其他SQLite文件，`--date 2026-04-01` 固定业务日期。

## 核心对象与状态

- `instrument`：active / calibrating / quarantined。
- `calibration`：requested → passed → approved；可 rejected、superseded；approved 可 `amend_due`（改到期日，重叠即拦截）。
- `method`：draft → validated → revoked。
- `result`：pending → released；时效链失效时 released → review_pending；复核后 released 或 blocked，blocked 可 reanalyze 回 pending。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。`expected_version` 过期时按最新版本重放，不再适用则进入待处理重试队列。
- `GET /api/entities/<id>/chain`：评估某结果当前时效链（是否有效、失效原因、绑定上下文）。
- `GET /api/reviews`：复核待办，`?status=open|resolved|all`、`?kind=result_chain|calibration_overlap`。
- `POST /api/reviews/<id>/resolve`：`{"decision":"keep|requeue|supersede|reject_new","reason":"..."}`（admin/authorizer）。
- `GET /api/pending`：待处理晚到提交，`?status=pending|applied|cancelled`。
- `POST /api/pending/drain`：立即重试全部待处理提交。
- `POST /api/pending/<id>/cancel`：`{"reason":"..."}` 取消一条待处理提交。
- `POST /api/reconcile`：手动对账（重放待处理 + 全链重扫），返回汇总。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

校准周期、误差、放行和重算规则（方法参数中的 `correction_factor` 乘原始测量值）是可演示的业务模型，不替代实验室质量体系或计量认证。
