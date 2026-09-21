# 潮位通航窗口 API（FastAPI + SciPy + SQLite）

根据按 UTC 递增的潮位样本，使用 **PCHIP（单调保形三次 Hermite）插值** 求出潮高
不低于安全水深（`吃水 + 最小富余`）的全部连续通航窗口；阈值交点用 Brent 法精确
求根并**保守取整到秒**。相邻样本间隔超过 `max_gap_seconds` 的区段**禁止插值**，
作为 `unknown` 返回，通航窗口不会跨越该区段。

可选的**航程占用约束**：创建任务时传入 `transit_duration_seconds`（航程占用秒数）
与 `maintenance_blocks`（维护封航区间，UTC 起止）后，结果额外包含
`departure_windows` —— 仅保留**从出发到结束全程落在同一安全通航窗口**、且不与任何
维护封航区间相交的出发时段（占用区间按 `[出发时刻, 出发时刻+航程)` 处理）。

计算以**异步任务**方式执行：先创建任务拿到 ID，再按 ID 轮询结果；任务状态与
结果持久化在 SQLite 中（容器启动时自动建库建表）。

另提供**同步**接口 `POST /sample-quality`：在计算通航窗口前审计潮位样本，
返回断链区段、查询范围覆盖缺口与突变异常样本（只报告、不修正），详见下文。

## 样本质量审计（`POST /sample-quality`）

在创建计算任务之前，可以先对潮位样本做一次**同步审计**（不建任务、不落库，
直接返回结果），提前发现会导致 `unknown` 区段或结果失真的数据问题：

- **断链识别**：按时间顺序找出相邻间隔超过 `max_gap_seconds` 的样本对——与
  窗口计算完全相同的切链规则；`gaps[]` 裁剪到查询范围、按时间排序，
  **首尾相接且原因相同**的段合并为一段；
- **覆盖缺口**：`query_start` 早于首个样本 / `query_end` 晚于末个样本的部分
  以 `reason = no_sample_coverage` 列入 `gaps[]`，并在 `coverage` 与 `notes`
  中明确说明（不做外推）；
- **突变异常**：在每条连续链内计算相邻斜率（米/秒），以链内**中位数与
  MAD**（中位数绝对偏差）构造稳健 Z 分数 `0.6745 × |斜率 − 中位数| / MAD`，
  超过 `spike_z_threshold`（默认 3.5）的斜率记为离群；某样本**两侧**斜率均
  离群时才标记为突变异常——单侧离群只是台阶式水位变化，不标记；链端点只有
  单侧斜率，永不标记。MAD 为 0 时任何偏离中位数的斜率都算离群，此时
  `robust_z` 为 `null`。链内样本少于 3 个时不做突变判定。
- **异常点只报告、绝不修正**：`anomalies[]` 原样带回入参的 `time` 与
  `height_m`，不做插值替换、平滑或剔除；是否剔除/重测由调用方决定。

`overall_status`：无断链、无覆盖缺口且无突变异常时为 `ok`，否则为 `warn`。

请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `samples` | 数组（≥2） | 潮位样本，按 UTC 时间**严格递增**：`{"time", "height_m"}` |
| `query_start` / `query_end` | datetime | 查询起止时间（UTC）；断链段裁剪到该范围，范围外的样本端点形成覆盖缺口 |
| `max_gap_seconds` | number > 0 | 相邻样本最大允许间隔（秒），超过则判为断链 |
| `spike_z_threshold` | number > 0，可选 | 突变判定的稳健 Z 分数阈值，默认 `3.5` |

```bash
curl -X POST http://localhost:8000/sample-quality \
  -H 'Content-Type: application/json' \
  -d '{
    "samples": [
      {"time": "2026-09-21T00:00:00Z", "height_m": 2.0},
      {"time": "2026-09-21T01:00:00Z", "height_m": 2.8},
      {"time": "2026-09-21T02:00:00Z", "height_m": 3.4},
      {"time": "2026-09-21T03:00:00Z", "height_m": 3.8},
      {"time": "2026-09-21T04:00:00Z", "height_m": 9.5},
      {"time": "2026-09-21T05:00:00Z", "height_m": 4.0},
      {"time": "2026-09-21T06:00:00Z", "height_m": 3.6},
      {"time": "2026-09-21T07:00:00Z", "height_m": 3.0},
      {"time": "2026-09-21T08:00:00Z", "height_m": 2.4},
      {"time": "2026-09-21T09:00:00Z", "height_m": 2.0}
    ],
    "query_start": "2026-09-21T00:00:00Z",
    "query_end":   "2026-09-21T09:00:00Z",
    "max_gap_seconds": 3700
  }'
```

上面请求中 04:00 的 9.5 m 是孤立突点（两侧斜率 ±5.7/−5.5 m·h⁻¹ 均显著偏离
链内中位数），响应（真实结果）：

```json
{
  "overall_status": "warn",
  "sample_count": 10,
  "chain_count": 1,
  "max_gap_seconds": 3700.0,
  "spike_z_threshold": 3.5,
  "gaps": [],
  "anomalous_indices": [4],
  "anomalies": [
    {
      "index": 4,
      "time": "2026-09-21T04:00:00Z",
      "height_m": 9.5,
      "chain_index": 0,
      "robust_z": 4.3,
      "slope_in_m_per_s": 0.0015833333333333333,
      "slope_out_m_per_s": -0.0015277777777777779,
      "median_slope_m_per_s": -0.00011111111111111109,
      "mad_slope_m_per_s": 0.00022222222222222218
    }
  ],
  "coverage": {
    "first_sample_time": "2026-09-21T00:00:00Z",
    "last_sample_time": "2026-09-21T09:00:00Z",
    "query_start_covered": true,
    "query_end_covered": true,
    "leading_gap_seconds": 0.0,
    "trailing_gap_seconds": 0.0
  },
  "notes": ["标记 1 个突变异常样本（索引见 anomalous_indices）；异常点仅报告，不做任何修正"]
}
```

响应字段：

| 字段 | 说明 |
| --- | --- |
| `overall_status` | `ok` / `warn` |
| `sample_count` / `chain_count` | 样本总数 / 按 `max_gap_seconds` 切出的连续链数量 |
| `gaps[]` | 查询范围内的断链/覆盖缺口：`start`、`end`、`reason`（`sample_gap_exceeds_max_gap` / `no_sample_coverage`）、`duration_seconds`；按时间排序，首尾相接且同因的段已合并 |
| `anomalous_indices` | 突变异常样本在请求 `samples` 中的下标（升序） |
| `anomalies[]` | 异常明细：下标、原始时间/潮高、所在链编号、两侧斜率、链内中位数与 MAD、`robust_z`（两侧稳健 Z 分数的较小者，MAD 为 0 时为 `null`） |
| `coverage` | 首末样本时间、`query_start`/`query_end` 是否被样本覆盖、两端缺口秒数 |
| `notes` | 人类可读的结论说明（含 query_start/query_end 覆盖缺口） |

校验失败（样本未严格递增、`query_end` 不晚于 `query_start`、
`max_gap_seconds`/`spike_z_threshold` 非正数或非有限值、潮高为 NaN 等）
统一返回 `422` 并指明字段与原因。

## 船队协同任务（`POST /fleet-tasks`）

单船之外，还可以一次性提交**一队船舶**（各自的名称、吃水、富余水深、航程时长），
共享同一份潮位样本、查询区间、最大采样间隔与封航区间。后台为每艘船分别计算安全
通航窗口与出发窗口，再取**全队公共出发区间**——在该区间内任意时刻出发，每艘船的
占用区间 `[t, t+各自航程)` 都能全程落在**各自的**安全窗口内。

- 各船结果见 `result.ships[]`（`name`、`threshold_m`、`windows`、`unknown`、
  `departure_windows`，字段含义与单船结果相同）；
- 公共出发区间见 `result.common_departure_windows[]`，即各船出发窗口的交集；
- 导致公共区间被排除的区段见 `result.exclusions[]`，每段给出 `start` / `end` /
  `duration_seconds`、`reason` 与 `ships`：
  - `reason = ship_constraint`：`ships` 列出在该区段无法出发的船名（按请求顺序）；
  - `reason = maintenance_closure` 或 `unknown_sample_segment`：全队共享的封航 /
    未知采样段约束，`ships` 为空数组。封航与未知段按**全队约束**处理：以队中
    **最长航程**整体向前平移后扣除（最慢的船决定全队能走的时刻），端点相接的
    整点出发仍允许。

`POST /fleet-tasks` → `201` 立即返回任务 ID，`GET /fleet-tasks/{task_id}` 轮询
（`pending` / `done` / `failed`，不存在返回 `404`），模式与单船任务完全一致，
结果持久化在 SQLite 的 `fleet_tasks` 表中。

请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ships` | 数组（≥1） | 船舶参数 `[{"name", "draft_m", "clearance_m", "transit_duration_seconds"}]`；船名非空白且队内需唯一，航程必填且 > 0 |
| `samples` | 数组（≥2） | 全队共享的潮位样本，按 UTC 时间**严格递增** |
| `query_start` / `query_end` | datetime | 查询起止时间（UTC） |
| `max_gap_seconds` | number > 0 | 相邻样本最大允许间隔（秒），全队约束 |
| `maintenance_blocks` | 数组，可选 | 全队共享的封航区间 `[{"start", "end"}]`，裁剪到查询范围并合并 |

```bash
curl -X POST http://localhost:8000/fleet-tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "ships": [
      {"name": "甲轮", "draft_m": 4.0, "clearance_m": 1.0, "transit_duration_seconds": 1800},
      {"name": "乙轮", "draft_m": 5.0, "clearance_m": 1.0, "transit_duration_seconds": 1800}
    ],
    "samples": [
      {"time": "2026-09-21T00:00:00Z", "height_m": 1.0},
      {"time": "2026-09-21T01:00:00Z", "height_m": 3.0},
      {"time": "2026-09-21T02:00:00Z", "height_m": 6.0},
      {"time": "2026-09-21T03:00:00Z", "height_m": 8.0},
      {"time": "2026-09-21T04:00:00Z", "height_m": 8.0},
      {"time": "2026-09-21T05:00:00Z", "height_m": 6.0},
      {"time": "2026-09-21T06:00:00Z", "height_m": 3.0},
      {"time": "2026-09-21T07:00:00Z", "height_m": 1.0}
    ],
    "query_start": "2026-09-21T00:00:00Z",
    "query_end":   "2026-09-21T07:00:00Z",
    "max_gap_seconds": 3700,
    "maintenance_blocks": [
      {"start": "2026-09-21T03:00:00Z", "end": "2026-09-21T04:00:00Z"}
    ]
  }'
```

上面的请求中，甲轮安全阈值 5 m（安全窗口约 01:39–05:20），乙轮吃水更大、
阈值 6 m（安全窗口 02:00–05:00）；公共出发区间为 `02:00–02:30` 与 `04:00–04:30`，
`exclusions` 依次给出：早高峰前仅乙轮水深不足（`ship_constraint`，`ships=["乙轮"]`）、
封航平移 30 分钟航程后挡出的 `02:30:01–03:59:59`（`maintenance_closure`，`ships=[]`）、
以及傍晚仅乙轮水深不足的收尾段。

校验失败（船队为空、船名重复或为空白、缺少航程、吃水为负、样本未严格递增、
封航区间 end 不晚于 start 等）统一返回 `422` 并指明字段与原因。

## 一条命令启动

前置条件：已安装 Docker 与 Docker Compose 插件（v2）。

```bash
docker compose up --build -d
```

启动后访问：

- Swagger 交互文档：<http://localhost:8000/docs>
- ReDoc 文档：<http://localhost:8000/redoc>
- OpenAPI 描述：<http://localhost:8000/openapi.json>
- 健康检查：<http://localhost:8000/health>

停止 / 查看日志：

```bash
docker compose down        # 停止（数据保留在命名卷 tide-data 中）
docker compose logs -f
```

## 必要配置

| 配置 | 位置 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `TIDE_DB_PATH` | 环境变量（compose 中） | `/data/tide.db` | SQLite 数据库文件路径，启动时自动创建目录、文件与表结构 |
| 端口映射 | `docker-compose.yml` | `8000:8000` | 左侧为宿主机端口，如被占用可改为 `8080:8000` |
| 数据卷 | `docker-compose.yml` | `tide-data:/data` | 数据库持久化卷，`docker compose down` 后数据仍保留 |

本地（非容器）运行：

```bash
pip install -r requirements.txt
TIDE_DB_PATH=./data/tide.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API

### 1. 创建计算任务

`POST /tasks` → `201 Created`，立即返回任务 ID（`status` 初始为 `pending`），
计算在后台进行。

请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `samples` | 数组（≥2） | 潮位样本，按 UTC 时间**严格递增**：`{"time", "height_m"}` |
| `query_start` / `query_end` | datetime | 查询起止时间（ISO-8601，建议带 `Z`；无时区按 UTC 处理） |
| `draft_m` | number ≥ 0 | 船舶吃水（米） |
| `clearance_m` | number ≥ 0 | 船底最小富余水深（米） |
| `max_gap_seconds` | number > 0 | 相邻样本最大允许间隔（秒），超过则该区段禁止插值 |
| `transit_duration_seconds` | number > 0，可选 | 航程占用时长（秒）；提供后结果附带 `departure_windows` |
| `maintenance_blocks` | 数组，可选 | 维护封航区间 `[{"start", "end"}]`（UTC 起止）；需配合 `transit_duration_seconds` 使用，计算前会裁剪到查询范围并合并重叠/首尾相接的区间 |

> 潮高、吃水、富余必须使用**同一垂直基准面**，单位统一为米。
> 安全水深阈值 = `draft_m + clearance_m`。
> 不传 `transit_duration_seconds` / `maintenance_blocks` 时，行为与之前完全一致
> （`departure_windows` 为 `null`）。

```bash
curl -X POST http://localhost:8000/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "samples": [
      {"time": "2026-09-21T00:00:00Z", "height_m": 1.0},
      {"time": "2026-09-21T01:00:00Z", "height_m": 3.0},
      {"time": "2026-09-21T02:00:00Z", "height_m": 6.0},
      {"time": "2026-09-21T03:00:00Z", "height_m": 8.0},
      {"time": "2026-09-21T04:00:00Z", "height_m": 8.0},
      {"time": "2026-09-21T05:00:00Z", "height_m": 6.0},
      {"time": "2026-09-21T06:00:00Z", "height_m": 3.0},
      {"time": "2026-09-21T07:00:00Z", "height_m": 1.0}
    ],
    "query_start": "2026-09-21T00:00:00Z",
    "query_end":   "2026-09-21T07:00:00Z",
    "draft_m": 4.0,
    "clearance_m": 1.0,
    "max_gap_seconds": 3700,
    "transit_duration_seconds": 1800,
    "maintenance_blocks": [
      {"start": "2026-09-21T03:00:00Z", "end": "2026-09-21T04:00:00Z"}
    ]
  }'
```

### 2. 按 ID 查询任务

`GET /tasks/{task_id}` → `200`（不存在返回 `404`）。

`status`：`pending`（计算中）/ `done`（结果见 `result`）/ `failed`（原因见 `error`）。

`result.windows[]`（**全部**连续安全窗口，起止时间为整秒、UTC `Z` 结尾）：

| 字段 | 说明 |
| --- | --- |
| `start` / `end` | 窗口起止（保守取整：交点向上/向下取整秒，保证窗口内任意时刻满足水深） |
| `start_height_m` / `end_height_m` | 窗口边界处插值潮高（米） |
| `min_height_m` | 窗口内插值潮高最小值（米，由内部样本点与边界取小者） |
| `duration_seconds` | 窗口时长（秒） |

`result.unknown[]`（无法判定的区段，窗口不会跨越）：

| `reason` | 含义 |
| --- | --- |
| `sample_gap_exceeds_max_gap` | 相邻样本间隔超过 `max_gap_seconds`，按要求禁止插值 |
| `no_sample_coverage` | 查询范围超出全部样本覆盖区间（不做外推） |

`result.departure_windows[]`（仅当请求提供 `transit_duration_seconds` 时返回，否则为 `null`）：

| 字段 | 说明 |
| --- | --- |
| `start` / `end` | 可出发时段起止（整秒、UTC `Z` 结尾）：在该时段内任意时刻 `t` 出发，占用区间 `[t, t+航程)` 全程落在**同一**安全通航窗口内，且不与任何维护封航区间或 `unknown` 区段相交 |
| `duration_seconds` | 可出发时段时长（秒） |

> 边界规则：占用区间为半开区间 `[t, t+航程)`，因此在封航开始时刻减去航程的
> 整点、或封航结束时刻整点出发是**允许**的（占用区间与封航区间仅端点相接、
> 不相交）。封航区间先裁剪到查询范围、合并重叠或首尾相接的区间后，再整体
> 向前平移航程时长，从各安全窗口的候选出发区间中扣除。

示例响应（上面请求的真实结果，节选）：

```json
{
  "task_id": "51341849cf6c44cc96b48dc19fb0f4f0",
  "status": "done",
  "created_at": "2026-09-21T13:50:03.600093Z",
  "result": {
    "threshold_m": 5.0,
    "max_gap_seconds": 3700.0,
    "windows": [
      {
        "start": "2026-09-21T01:39:11Z",
        "end": "2026-09-21T05:20:49Z",
        "start_height_m": 5.001,
        "end_height_m": 5.001,
        "min_height_m": 5.001,
        "duration_seconds": 13298
      }
    ],
    "unknown": [],
    "departure_windows": [
      {
        "start": "2026-09-21T01:39:11Z",
        "end": "2026-09-21T02:30:00Z",
        "duration_seconds": 3049
      },
      {
        "start": "2026-09-21T04:00:00Z",
        "end": "2026-09-21T04:50:49Z",
        "duration_seconds": 3049
      }
    ]
  },
  "error": null
}
```

其中安全窗口为 01:39:11–05:20:49；封航 03:00–04:00 向前平移 30 分钟航程后
扣除出发区间 (02:30, 04:00)，故可出发时段被切成两段（02:30 与 04:00 整点
出发均允许），且每段最晚出发时刻都比窗口结束早 30 分钟。

校验失败（如样本未严格递增、吃水为负、封航区间 end 不晚于 start、只传
`maintenance_blocks` 而未传 `transit_duration_seconds` 等）统一返回 `422`
并指明字段与原因。

## 算法说明

1. **时间数值化**：所有时间转 POSIX 秒；无时区输入按 UTC 处理。
2. **链分割**：相邻样本间隔 `> max_gap_seconds` 处切分为若干独立样本链。
   每条链单独构造 PCHIP，**任何三次分段都不会跨越该间隙**；孤立样本点
   （链长为 1）不产生有效覆盖。
3. **PCHIP 插值 + 内部极值**：PCHIP 保形（不会超过数据点极值），但单个三次
   分段在非单调数据附近仍可能出现内部极值。因此对每条链用
   `derivative().roots()` 取全部驻点，与样本节点共同构成事件网格——相邻事件
   之间曲线单调，是否跨阈值只需比较两端符号。
4. **阈值求根**：对跨阈值的相邻事件用 `scipy.optimize.brentq`（容差 `1e-6` 秒，
   远高于秒级要求）精确求出交点。
5. **保守取整到秒**：窗口起点 `ceil`、终点 `floor`，舍去不足整秒的窗口，
   保证报告的每一秒潮高都不低于安全水深。
6. **unknown 互补集**：查询区间内未被任何有效链覆盖的部分即为 `unknown`，
   并区分“超间隔（禁止插值）”与“无样本覆盖（不外推）”两种原因；
   窗口严格落在覆盖区内，绝不跨越未知段。
7. **出发窗口（可选）**：提供 `transit_duration_seconds` 时，在每个安全窗口
   `[ws, we]` 内取候选出发区间 `[ws, we - 航程]`（保证占用区间
   `[t, t+航程)` 全程落在同一窗口内）；维护封航区间裁剪到查询范围、合并
   重叠/首尾相接区间后，与 `unknown` 区段一并向前平移航程时长，从候选区间
   中扣除（扣除区间为开区间，端点时刻出发仍允许），最后同样保守取整到秒。

## 项目结构与测试

```
app/
  main.py            # FastAPI 路由、样本质量审计、单船/船队任务创建/查询、后台计算
  schemas.py         # 请求/响应模型与输入校验
  config.py          # 配置（TIDE_DB_PATH 等）
  db.py              # SQLite 连接与启动建表（tasks / fleet_tasks）
  services/tides.py  # PCHIP 插值、求根、窗口与 unknown 计算
  services/fleet.py  # 船队协同：各船窗口、公共出发区间与排除归因
  services/quality.py# 样本质量审计：断链识别、覆盖缺口、中位数/MAD 突变标记
  tests/             # pytest：算法单测 + 接口端到端测试
Dockerfile           # 依赖安装于构建阶段
docker-compose.yml   # 一条命令启动，含卷持久化与健康检查
requirements.txt
```

运行测试（本地）：

```bash
python -m pytest app/tests -q
```
