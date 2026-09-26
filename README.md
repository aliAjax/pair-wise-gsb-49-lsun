# 再保险合约与巨灾暴露管理

纯Python标准库实现的再保险合约与巨灾暴露管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

合约额度以**人民币（CNY）**计价；海外巨灾赔案可用**外币原币报损**，按业务日汇率折算：

- **报损（submit_claim）**：记录原币金额、报损日与报损日汇率，并折人民币备查。
- **核定（calculate）**：按**报损日汇率**折算后计算层内摊回，占用人民币合约额度。
- **结算（settle）**：以核定冻结的原币摊回，按**结算日汇率**重新换算人民币付款，冻结当天汇率与账单；已结算账单不再随之后录入的新汇率变化。
- **额度不足**：核定/结算任一阶段人民币口径超出事件合约容量时返回409，错误体列出每笔的原币金额、折人民币金额及缺口（`gap_cny`）。
- **批次汇总**：结算批次按币种建档，汇总同时展示原币合计与折人民币合计。

汇率口径：`rate`为1单位外币兑人民币；取数规则为“不晚于业务日的最近一条”，因此服务重开后历史案件仍能按原业务日复算。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误（携带结构化details）和基础校验。
- `src/fx.py`：币种资料、汇率快照（FxContext）、Decimal换算与按币种汇总（换算规则，纯函数）。
- `src/rules.py`：状态转换、分层摊回、外币占用与结算账单冻结。
- `src/repository.py`：SQLite建表（records/audit_events/fx_rates/settlement_batches/batch_items）、事务和查询。
- `src/service.py`：用例编排、汇率接入、额度缺口、批次与重开后核对、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：列表、汇率资料、批次与按事件核对页。
- `static/claim.html`：案件详情页（合约额度、原币报损与占用、冻结账单、批次归属、操作、审计）。
- `tests/`：完整流程、规则计算、失败场景、多币种流程、缺口、批次与HTTP接口测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8325
```

默认端口为`8325`，默认数据库位于项目目录。服务启动时自动建表（新增表对已有库幂等）。

## 主要接口

案件与统计：

- `GET /health`：健康检查。
- `GET /`：列表/核对演示页；`GET /claim.html?id=1`：案件详情页。
- `GET /api/records`：记录列表，可带`state`、`event_id`、`limit`。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线与该案件所属批次。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：业务动作，`{"expected_version":1,"data":{...}}`。
  - `submit_claim`的data支持`loss_currency`、`loss_amount`（原币）、`loss_date`（YYYY-MM-DD）。
  - `settle`的data支持`settle_date`（缺省为当天）。
- `GET /api/reconcile/event?event_id=...`：按巨灾事件逐案核对报损/占用/结算口径，含按币种汇总与容量缺口。

汇率资料（finance/admin维护，其余角色可查询）：

- `POST /api/fx/rates`：录入或覆盖某日汇率，`{"data":{"currency":"USD","rate_date":"2026-03-01","rate":"7.10","source":"..."}}`。
- `GET /api/fx/rates?currency=USD&limit=200`：汇率列表。
- `GET /api/fx/rates/USD/2026-03-05`：取不晚于该日期的最近汇率；不存在返回422 `fx_rate_missing`。

结算批次（finance/admin）：

- `POST /api/batches`：`{"data":{"reference":"B-USD-2026-09","currency":"USD","note":""}}`，批次单一币种。
- `GET /api/batches` / `GET /api/batches/{id}`：批次列表/详情（含按币种汇总）。
- `POST /api/batches/{id}/records/{record_id}`：把已结算案件加入批次（存入账单快照）。
- `GET /api/batches/{id}/reconcile`：用快照与案件当前冻结账单逐项核对，服务重开后可用。

除`/health`和静态页外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

额度不足响应示例：

```json
{
  "error": "conflict",
  "message": "人民币额度不足：本币折人民币4053873.24，缺口53873.24",
  "details": {
    "reason": "capacity_shortfall",
    "stage": "settlement",
    "currency": "USD",
    "foreign_amount": 559154.93,
    "claimed_cny": 4053873.24,
    "capacity_cny": 4000000.0,
    "used_cny": 0.0,
    "available_cny": 0.0,
    "gap_cny": 53873.24,
    "lines": [{"reference": "RI-...", "currency": "USD", "foreign_amount": 559154.93, "cny_amount": 4053873.24, "state": "calculated", "current": true}]
  }
}
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖人民币完整流程、规则计算、重复引用、权限、版本冲突，以及汇率取数与权限、外币报损/核定/结算、核定与结算缺口、账单冻结不随新汇率变动、JPY零位小数、批次按币种汇总、重开后按批次和按事件核对、HTTP接口与缺口错误体。
