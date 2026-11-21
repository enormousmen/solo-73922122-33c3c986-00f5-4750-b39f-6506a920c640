# Canary Rollout API

灰度发布控制服务：运维员上报副本状态与窗口指标，服务根据**目标版本、每批副本上限、
最短稳定窗口、错误率阈值**自动决定 **放量(promote) / 暂停(hold) / 回滚(rollback) /
完成(complete)**，全部决策与执行结果持久化在 SQLite，重启后可继续发布。

发布途中值班员可修改**错误率阈值**和**最短稳定窗口**：配置按版本受理
（乐观并发），无待执行决定时立即生效，放量等待回执时先存为待生效配置、
回执入账时启用。详见第 3 节。

## 1. 启动（Docker Compose）

```bash
docker compose up -d --build
curl -s http://127.0.0.1:8000/health      # {"status":"ok"}
# 交互式文档：http://127.0.0.1:8000/docs
```

SQLite 数据库位于命名卷 `canary-data`（容器内 `/data/canary.db`，WAL 模式）。
容器重启后状态不丢失（含待生效配置与待执行决定）；`docker compose down`
不删卷，`docker compose down -v` 才清空。旧版本数据库启动时自动就地迁移
（`releases` 增加 `config_version`，新增 `pending_configs` 表），升级前创建的
发布和待执行决定可继续处理。

本地无 Docker 时也可直接运行：

```bash
pip install -r requirements.txt
CANARY_DB_PATH=./data/canary.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 2. 工作模型与代次(generation)语义

- 创建发布时给定 `total_replicas`、`max_replicas_per_batch`、
  `min_stable_window_seconds`、`error_rate_threshold`，初始代次为 `0`，
  初始配置版本为 `0`。
- 运维员持续上报**带代次**的副本状态和指标窗口。
- `POST /evaluate` 按当前代次的观测做决策，优先级：
  1. **回滚**：错误率 `error_count / request_count` **严格大于**阈值（无流量不触发）；
  2. **完成**：最后一批已生效、全部副本 ready、且有满足最短窗口的观测；
  3. **放量**：窗口达标、无 unready 副本、未到总副本数，放量步长为
     `min(总副本数, 当前副本数 + 每批上限)`；
  4. **暂停(hold)**：条件不满足时保持当前规模，并在 `reason` 中说明阻塞项。
- `promote` / `rollback` 决策产生后进入 `pending`，由平台执行后通过
  `.../result` 回报 `succeeded` 或 `failed`：
  - 成功：放量推进副本数，回滚进入终态 `rolled_back`，**代次 +1**；
  - 失败：**保留上一个可执行状态**（副本数与代次均不变），决策标记 `failed`，
    放量失败转 `paused`、回滚失败转终态 `failed` 等待人工介入。
- `pause/hold` 决策不占用代次，可在同代次继续观测后重新评估。
- **旧代次/迟到观测**（`generation < current_generation`）会入库但标记
  `ignored=1`，绝不参与聚合、不能改写已提交状态；未来代次（`> current`）直接 422。
- 执行结果只接受一次，重复回报返回 `409`。

## 3. 发布途中修改配置（版本化）

可修改字段：`error_rate_threshold`、`min_stable_window_seconds`
（`total_replicas` 与 `max_replicas_per_batch` 创建后不可变）。

- **详情展示**：`GET /rollouts/{id}` 中 `error_rate_threshold` /
  `min_stable_window_seconds` / `config_version` 为**生效配置及版本**；
  存在待生效配置时 `pending_config` 列出其**值与版本**（否则为 `null`）。
- **提交修改**：`PUT /rollouts/{id}/config`，请求体必须带 `base_version`
  —— 最近受理的配置版本（有待生效配置时取其版本，否则取生效版本）。
  两个字段可只给其一，未给的字段从所基于的版本继承。
- **版本过期即拒绝**：`base_version` 不等于最近受理版本时返回 `409`；
  同一版本的并发提交由写锁串行化，**只接受第一份**，其余因版本过期被拒。
- **生效时机**：
  - 无待执行决定 → 新配置**立即生效**（`applied=true`）；
  - 放量决定等待回执 → 先存为**待生效配置**（`applied=false`），后续受理的
    修改可**替换**它；放量回执（成功或失败）仍按原决定处理，记录回执时
    **同时启用**最新待生效配置，供下一次评估使用；
  - **回滚待执行**或**发布已结束**（终态）→ 拒绝修改（`409`）。
- 待生效配置存于 SQLite，**重启后保留**。

典型响应（放量待回执时改配置）：

```json
{
  "applied": false,
  "message": "config v1 stored as pending; it activates when the execution result of decision 'dec_...' is recorded",
  "rollout": {
    "config_version": 0,
    "error_rate_threshold": 0.05,
    "min_stable_window_seconds": 30,
    "pending_config": { "version": 1, "error_rate_threshold": 0.2, "min_stable_window_seconds": 30, "created_at": "..." }
  }
}
```

## 4. API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/rollouts` | 创建灰度发布 |
| GET | `/rollouts` / `/rollouts/{id}` | 列表 / 详情（含生效与待生效配置及版本） |
| PUT | `/rollouts/{id}/config` | 修改阈值/最短窗口（带 `base_version`） |
| POST | `/rollouts/{id}/replicas` | 上报副本状态（带代次） |
| POST | `/rollouts/{id}/metrics` | 上报指标窗口（带代次） |
| POST | `/rollouts/{id}/evaluate` | 评估并产生决策 |
| POST | `/rollouts/{id}/decisions/{did}/result` | 回报决策执行结果 |
| POST | `/rollouts/{id}/pause` / `/resume` | 人工暂停 / 恢复 |
| GET | `/rollouts/{id}/decisions` | 全部决策（SQLite 持久化） |
| GET | `/rollouts/{id}/observations?kind=replicas\|metrics` | 原始观测（含 ignored 行） |

## 5. 在 /docs 演示：改配置 + 回报放量结果

`docker compose up -d --build` 后打开 <http://127.0.0.1:8000/docs>，依次展开
各端点 **Try it out → Execute**：

1. **POST /rollouts**：`{"service":"checkout-service","target_version":"v2.3.1",
   "total_replicas":4,"max_replicas_per_batch":2,"min_stable_window_seconds":30,
   "error_rate_threshold":0.05}`。记下返回的 `id`（下称 `$RID`），
   此时 `config_version=0`、`pending_config=null`。
2. **POST /rollouts/{id}/replicas**（两次，`replica_id` 分别 `canary-1`/`canary-2`）：
   `{"generation":0,"replica_id":"canary-1","state":"ready","observed_version":"v2.3.1"}`。
3. **POST /rollouts/{id}/metrics**：`{"generation":0,
   "window_started_at":"2026-11-21T00:00:00Z","window_seconds":30,
   "request_count":1000,"error_count":4}`。
4. **POST /rollouts/{id}/evaluate** → 产生 `promote 0 -> 2`，
   `pending_decision_id` 指向该决定（记下 `decision.id`，下称 `$DID`）。
5. **PUT /rollouts/{id}/config**：`{"base_version":0,"error_rate_threshold":0.2}`
   → `applied=false`，响应中 `pending_config` 列出待生效值与版本 `1`，
   生效配置仍是 `0.05 / v0`。
   - 再发一次相同 `base_version:0` → `409`（版本过期）；
   - 改发 `{"base_version":1,"error_rate_threshold":0.3}` → 替换待生效配置为 `v2`。
6. **GET /rollouts/{id}**：可见生效配置（`config_version:0`）与
   待生效配置（`pending_config.version:2`）并列。
7. **POST /rollouts/{id}/decisions/{did}/result**：`{"status":"succeeded",
   "detail":"scaled to 2"}` → 原决定照常入账（副本 0→2、代次 +1），响应
   `message` 含 `pending config v2 activated`，`config_version` 变为 `2`，
   `error_rate_threshold` 变为 `0.3`，`pending_config` 清空。
8. 继续上报代次 1 的观测后再 **POST /evaluate**，即按新阈值/窗口评估。

等价脚本：`bash examples/api_examples.sh`（curl+jq）或
`python3 examples/client_demo.py`（纯标准库）。

## 6. 可直接调用的示例

```bash
# 依赖 curl + jq；覆盖 hold→放量→改配置→回执激活→旧观测忽略→第二批→阈值回滚全流程
bash examples/api_examples.sh

# 仅依赖 Python 标准库的等价示例
BASE=http://127.0.0.1:8000 python3 examples/client_demo.py
```

典型放量决策响应：

```json
{
  "evaluated": true,
  "message": "stable for 30s (>= 30s) with error rate 0.0040 <= 0.0500; promote 0 -> 2 replicas",
  "decision": {
    "id": "dec_...", "generation": 0, "action": "promote",
    "from_replicas": 0, "to_replicas": 2,
    "execution_status": "pending",
    "detail": { "ready_replicas": 2, "metrics": { "error_rate": 0.004, "max_window_seconds": 30.0 } }
  },
  "rollout": { "status": "pending", "current_generation": 0, "current_replicas": 0, "pending_decision_id": "dec_...", "config_version": 0, "pending_config": null }
}
```

## 7. 自测

```bash
pip install -r requirements-dev.txt
python3 -m pytest -q        # 31 个用例：放量/完成、阈值回滚、旧观测、执行失败、
                            # 重启恢复、校验、配置版本化（过期/并发/待生效/激活/迁移）
```

## 8. 目录结构

```
app/            FastAPI 应用（main 路由 / rollout 状态机+配置版本 / db SQLite / models 模型）
tests/          pytest 自测
examples/       curl+jq 与纯标准库 Python 两套可调用示例
Dockerfile, docker-compose.yml, requirements.txt
```
