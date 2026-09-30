# 国际培训名额配置

本项目维护国际培训名额配置的领域约定、角色边界、样例数据，以及一套完整的名额管理服务端。服务端覆盖批次容量、资格、最低保障、历史参与修正、多套试算对比、方案冻结、额度分录、候补递补与可解释尾差，供后端服务、接口和自动化验证统一使用。

## 领域约束

契约见 `domain/contract.json`，五个状态与四个关键不变量：

| 状态 | 含义 |
| --- | --- |
| 申报 | 维护院校、申报、批次容量（国家/院校类型/专业方向三维）与最低保障 |
| 试算 | 可创建任意多套**不可变**试算互相对比，不产生正式额度 |
| 分配 | 选定一套试算发布，**输入冻结**，初始额度以分录入账 |
| 确认 | 院校确认/放弃名额，放弃或少确认的名额通过分录释放 |
| 递补 | 按候补顺位与多维容量约束递补，无人承接的名额落入可解释的机动名额池 |

四个不变量如何落实：

- **多维配额**：`country / type / major` 三维容量在每个名额派发、每次机构间转让时同时硬校验。
- **最低保障**：匹配保障规则的申报先预留保底名额；保障无法落实时试算判定为不可行（`feasible=false` 并给出逐校缺口），**禁止发布**。
- **候补递补**：综合分 = 基础分 − 历史参与分 × 修正权重；未足额申报按综合分入候补，放弃/撤销释放名额后顺位递补，资格撤销者跳过。
- **尾差归属**：余量按**最大余数法（Hamilton，`Fraction` 精确计算）**分配，每个舍入尾差名额都带余数与顺位的 `SeatTrace`；派不出去的名额始终落到具名对象「机动名额池」并附原因。

正式额度的一切变化（发布、放弃、资格撤销、机构间转让、候补递补、尾差回收）都只表达为**额度分录（ledger entry）**，某申报余额恒等于其分录金额之和。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/quota_service/`：名额管理服务端。
  - `models.py`：批次、院校、申报、容量、保障、试算结果、额度分录等领域模型。
  - `engine.py`：纯函数分配引擎（资格过滤、历史修正、保障预留、最大余数、多维容量、尾差解释）。
  - `service.py`：状态机、试算快照与对比、发布冻结、确认/放弃/撤销/转让、候补递补、解释视图。
  - `store.py`：JSON 原子持久化 + 进程内锁与 `flock` 跨进程锁，杜绝并发超发。
  - `api.py` / `__main__.py`：标准库 HTTP 服务（无第三方依赖）。
- `tools/check_contract.py`：命令行契约摘要检查。
- `tools/e2e_check.py`：对运行中服务做端到端业务断言。
- `tests/`：契约、引擎、服务层、HTTP API 的回归测试。

## 运行

```bash
PYTHONPATH=src python3 -m quota_service --host 127.0.0.1 --port 8080 --db data/quota.json
```

主要接口（均为 JSON）：

- `POST /batches`、`GET /batches`、`GET /batches/{id}`
- `POST /batches/{id}/institutions`、`.../institutions/{iid}/eligibility`、`.../history`
- `POST /batches/{id}/applications`
- `POST /batches/{id}/capacities`、`POST /batches/{id}/guarantees`
- `POST /batches/{id}/trials`、`GET /batches/{id}/trials`、`GET /batches/{id}/compare?ids=t1,t2`
- `POST /batches/{id}/publish`
- `POST /batches/{id}/confirmations`、`/declines`、`/revocations`、`/transfers`
- `POST /batches/{id}/backfill-start`、`/backfill-run`、`/close`
- `GET /batches/{id}/ledger`、`GET /batches/{id}/institutions/{iid}/view`

写接口可用 `If-Match: <version>` 头（或请求体 `expected_version`）做乐观并发控制；并发写在服务端被串行化，冲突返回 `409`。

### 端到端验证

```bash
PYTHONPATH=src python3 -m quota_service --port 8099 --db /tmp/quota_e2e.json &
python3 tools/e2e_check.py
```

## 测试

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
