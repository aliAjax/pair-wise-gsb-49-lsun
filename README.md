# 再保险合约与巨灾暴露管理

纯Python标准库实现的再保险合约与巨灾暴露管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

支持海外巨灾赔案多币种处理：外币报损、按报损日汇率核定占用人民币合约额度、按结算日汇率结算并冻结账单、额度不足时列示原币/折人民币/缺口、批次按币种汇总。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/currency.py`：币种资料（币种目录、精度、汇率快照类型），人民币为基准币种。
- `src/fx.py`：换算规则（取汇率、原币/人民币换算、核定占用与结算冻结计算），纯计算不访问存储。
- `src/rules.py`：状态转换、分层摊回、赔偿限额和恢复保费和冲突检查。
- `src/repository.py`：SQLite建表（记录、审计、汇率、账单、批次）、事务和查询。
- `src/service.py`：用例编排、权限检查、额度占用校验、账单冻结、批次汇总和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `static/claim.html`：赔案详情页（汇率、占用、冻结账单、批次汇总、审计时间线）。
- `tests/`：完整流程、规则计算、多币种流程和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8325
```

默认端口为`8325`，默认数据库位于项目目录。服务启动时自动建表。

## 多币种赔案流程

1. 财务登记汇率：`POST /api/fx-rates`，请求体`{"currency":"USD","date":"2026-08-01","rate":7.10,"source":"央行中间价"}`。汇率为1单位外币折合人民币数量，同一币种同一日期重复登记会覆盖；已冻结的账单和占用不受影响。
2. 提交赔案：`submit_claim`时传`currency`（如`USD`）、`reported_loss`（原币报损）和`loss_report_date`（报损日，外币必填）。
3. 核定：`calculate`按**报损日汇率**把原币摊回折成人民币冻结为额度占用（`recoverable_amount`），同时保留原币摊回（`recoverable_fc`）与报损日汇率快照（`report_rate`）。合约额度不足时返回409 `capacity_shortfall`，明细含原币金额、折人民币金额与缺口。
4. 结算：`settle`按**结算日汇率**（`settle_date`，默认当天UTC）换算应付人民币，汇率与账单一并冻结（`bills`表），并记录与核定占用的汇兑差异（`fx_variance_cny`）。已结算账单不随后续汇率变动修改。
5. 批次：财务创建结算批次并加入账单，批次汇总按币种展示原币合计、折人民币合计与汇兑差异合计。

人民币赔案无需登记汇率（汇率恒为1），原有流程不变。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面；`GET /claim?id={id}`：赔案详情页。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情，已结算记录附带冻结账单`bill`。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `POST /api/fx-rates`：登记/更新汇率（finance/admin）；`GET /api/fx-rates?currency=USD`：查询汇率。
- `POST /api/batches`：创建结算批次（finance/admin），请求体`{"reference":"...","title":"..."}`。
- `GET /api/batches` / `GET /api/batches/{id}`：批次列表/详情，详情含账单与`summary_by_currency`分币种汇总。
- `POST /api/batches/{id}/items`：把账单加入批次，请求体`{"bill_id":1}`或`{"record_id":1}`；同一账单只能进入一个批次。

除`/health`和页面外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、多币种核定/结算/冻结、额度不足明细、批次分币种汇总、服务重启后核对、重复引用、权限拒绝和版本冲突。
