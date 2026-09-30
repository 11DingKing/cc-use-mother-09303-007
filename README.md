# 国际培训名额配置

联合师资培训名额管理服务端：维护批次容量、院校资格、最低保障、历史参与
修正与候补规则；支持多套试算对比、方案发布冻结、发布后通过额度分录调整、
舍入尾差可解释、并发确认不超发，并为每个机构提供入选/未入选原因。

## 领域流程

```
申报(draft) ──试算(scenario，可多套)──▶ 选定一套发布 ──▶ 分配(published，输入冻结)
                                                          │  confirm 逐校确认
                                                          ├─ relinquish 放弃
                                                          ├─ revoke 资格撤销
                                                          ├─ transfer 机构间转让
                                                          └─ promote 候补递补（释放名额触发）
                                                          ▼
                                                       确认/收尾(confirmed/closed)
```

关键规则：

- **多维配额**：批次总容量 + 国家 / 院校类型 / 专业方向三维容量，每席分配
  与每条额度分录落账时都校验，冲突直接拒绝。
- **最低保障**：保障席位先于竞争分配预留（如“小型学院 ≥ 3 席”）；需求或
  容量不足时记录 `guarantee_shortfalls`，不凭空造名额。
- **历史参与修正**：分配权重 = `1/(1+history)`，历史上参与越多权重越低，
  同分排名靠后，新院校优先。
- **尾差归属**：全部份额用 `fractions.Fraction` 精确计算，最大余额法
  （Hamilton）逐席分发，每个舍入尾差通过 `rounding_traces` 指明判给了谁、
  精确份额是多少、依据什么规则；发不出去的名额在 `unfilled` 中归因。
- **试算隔离**：每套试算绑定自己的不可变输入快照，可任意创建与对比，
  绝不触碰正式额度。
- **发布冻结**：发布后容量、资格、保障、申请输入只读；放弃、撤销、转让、
  递补全部以不可变带符号分录（`LedgerEntry`）表达，余额可审计、总量守恒。
- **并发不超发**：批次级锁 + 写接口乐观版本号（`VERSION_CONFLICT`/409），
  容量校验在锁内完成；任何机构余额不会为负，总持有永不超过容量。
- **逐机构解释**：`institutions/{id}` 视图给出排名、候补位次、保障/竞争/
  尾差原因、分录调整历史，以及“为何暂不能递补”。

## 目录

- `domain/contract.json`：领域角色、状态、不变量及其落地机制。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/quota_service/`：
  - `models.py`：值对象、批次快照、额度分录、错误码。
  - `allocator.py`：保障预留 + 历史修正 + 最大余额法 + 尾差追踪 + 候补排名。
  - `ledger.py`：发布后额度分录账本（守恒、不超发、维度约束）。
  - `service.py`：应用服务（生命周期、试算、发布、调整、并发控制、视图）。
  - `server.py`：零依赖 HTTP/JSON 接口。
- `tools/check_contract.py`：契约摘要检查。
- `tools/demo.py`：端到端情景演示（`python3 tools/demo.py`）。
- `tests/`：算法、账本、服务生命周期、并发不超发、HTTP 端到端回归。

## 验证

```bash
python3 -m unittest discover -s tests -v     # 50 个测试
python3 -m compileall -q src tools tests     # 编译检查
python3 tools/check_contract.py domain/contract.json
python3 tools/demo.py                        # 端到端演示
```

## 启动服务

```bash
python3 -m quota_service.server --host 127.0.0.1 --port 8000
```

## HTTP 接口摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/batches` | 创建批次（`total_capacity`） |
| GET | `/batches` / `/batches/{bid}` | 批次列表 / 批次详情（含正式分录） |
| PUT | `/batches/{bid}/capacity` | 设置总容量（仅 draft） |
| PUT | `/batches/{bid}/dimensions` | 设置维度容量（dimension/value/capacity） |
| PUT/DELETE | `/batches/{bid}/guarantees` | 设置/删除最低保障（DELETE 用 query 参数） |
| PUT | `/batches/{bid}/applications` | 新增/更新院校申请 |
| POST | `/batches/{bid}/eligibility` | 申报阶段设置资格 |
| POST | `/batches/{bid}/history` | 设置历史参与修正系数（如 `"2"`、`"1/2"`） |
| POST | `/batches/{bid}/scenarios` | 新建试算（不影响正式额度） |
| GET | `/batches/{bid}/scenarios/{sid}` | 试算结果（含每校原因与尾差追踪） |
| POST | `/batches/{bid}/compare` | 多套试算对比（`scenario_ids`） |
| POST | `/batches/{bid}/publish` | 选定试算发布，输入冻结 |
| POST | `/batches/{bid}/confirm` | 院校确认名额 |
| POST | `/batches/{bid}/relinquish` | 放弃名额（分录） |
| POST | `/batches/{bid}/revoke` | 发布后资格撤销（收回全部名额） |
| POST | `/batches/{bid}/transfer` | 机构间转让（转出/转入成对、同 ref） |
| GET | `/batches/{bid}/waitlist/next` | 查看当前候补队首 |
| POST | `/batches/{bid}/promote` | 递补候补队首（分录，受容量钳制） |
| POST | `/batches/{bid}/close` | 批次收尾 |
| GET | `/batches/{bid}/institutions/{iid}` | 单机构入选/未入选原因视图 |

所有写接口可在请求体携带上一次响应中的 `batch_version` 做乐观并发控制；
版本过期返回 `409 VERSION_CONFLICT`。
