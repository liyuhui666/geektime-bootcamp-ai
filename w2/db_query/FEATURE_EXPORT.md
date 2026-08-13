# FEATURE_EXPORT — 数据导出功能设计思路

> 作业：在「智能数据库查询工具」（w2/db_query）基础上新增**数据导出功能模块**。
>
> 本文档记录对现有代码库的理解，以及导出功能的详细设计方案，作为后续实现的蓝本。

---

## 1. 背景与作业要求映射

训练营作业要求在 Cursor 构建的「智能数据库查询工具」之上，新增一个数据导出功能模块，并把 AI 辅助编程从「代码实现」推进到「功能规划与自动化流程设计」。作业三条核心要求与本设计的对应关系：

| 作业要求 | 本设计落点 |
| --- | --- |
| ① 导出格式支持（至少 CSV、JSON） | 后端 `ExportService` + `ExporterRegistry`，内置 CSV / JSON / NDJSON，按 OCP 可平滑扩展 Excel、Markdown 等 |
| ② 自动化流程：用 Claude Code 的 Agent 或自定义 Command，让「执行查询 + 导出」一键完成 | 新增项目级 slash command `/query-export` 与 subagent `data-exporter`；后端提供一键端点 `POST /query/export` |
| ③ 用户交互：自然语言或界面触发 | 界面：`ResultTable` 新增导出按钮；自然语言：由 Command/Agent 解析意图并主动询问格式 |

提交物即：更新后的项目代码 + 本设计文档（`FEATURE_EXPORT.md`）。

---

## 2. 现有代码库分析（设计前提）

导出功能依附于「查询结果」，因此必须先吃透查询链路与数据结构。

### 2.1 查询执行链路

```
前端 execute.tsx
   │  apiClient.post(`/api/v1/dbs/{name}/query`, { sql })
   ▼
API 层  backend/app/api/v1/queries.py :: execute_sql_query
   │  查 DatabaseConnection → execute_query_with_service()
   ▼
桥接层  backend/app/services/query_wrapper.py :: execute_query_with_service
   │  database_service.execute_query(db_type, name, url, sql, limit=1000)
   │  + save_query_history()   ← 注意：会写一条历史（见 §5.5）
   ▼
Service 门面  backend/app/services/database_service.py :: execute_query
   │  validate_and_transform_sql()  ← 只读校验 + 注入 LIMIT 1000 在这一层
   │  adapter_registry.get_adapter(...)
   ▼
Adapter  backend/app/adapters/{postgresql,mysql}.py
   │  执行 SQL，返回 adapter 版 QueryResult
   ▼
QueryResult（API schema）→ 返回前端
```

核实得到的关键事实：

- **校验在 `database_service.execute_query` 内部完成**（[database_service.py:95](backend/app/services/database_service.py) 调 `validate_and_transform_sql`），`SqlValidationError` 经 `query_wrapper` re-raise 冒泡到 API。导出复用 `execute_query_with_service` 即可继承全部只读校验，无需重复。
- SQL 强制只读 + 自动注入 `LIMIT 1000`，**单次结果上限 1000 行**——导出在此量级内，一次性生成字节无内存压力。
- 同时支持 PostgreSQL 与 MySQL，导出逻辑必须**与数据库类型无关**（只依赖 `QueryResult`）。

### 2.2 核心数据结构

后端 `backend/app/models/schemas.py`：

```python
class QueryColumn(BaseModel):
    name: str
    data_type: str = Field(..., alias="dataType")

class QueryResult(BaseModel):
    columns: list[QueryColumn]
    rows: list[dict[str, Any]]          # ← 导出的数据源
    row_count: int = Field(..., alias="rowCount")
    execution_time_ms: int = Field(..., alias="executionTimeMs")
    sql: str
```

前端 `frontend/src/types/query.ts` 镜像同结构。`rows` 是「列名 → 值」的字典数组，值可能是 `int / float / bool / str / datetime / None`，偶有 JSON 字段为 `dict / list`。**导出的本质就是把这些异构值安全地序列化为目标格式**。

### 2.3 架构约束（必须遵循）

- **分层**：API → Service → Adapter，禁止跨层（见 [docs/CLASS_DIAGRAM.md](docs/CLASS_DIAGRAM.md)）。
- **SOLID / OCP**：项目刚完成一次以 OCP 为核心的重构（见 [specs/w2/0001-improvement.md](../specs/w2/0001-improvement.md)），新增格式应「加文件 + 注册一行」，不动既有代码——导出模块需复刻 `DatabaseAdapterRegistry` 的模式。
- **API 约定**：路由前缀 `/api/v1/dbs`，JSON 字段 camelCase（pydantic alias），错误用 `HTTPException(detail=...)`。
- **工具链**：后端 `uv` + `ruff`，前端 antd + axios，测试 `pytest` / REST Client。

---

## 3. 设计目标与原则

1. **数据库无关**：导出只消费 `QueryResult`，不触碰 adapter。
2. **格式可扩展（OCP）**：新增格式 = 新增一个 `Exporter` 类 + 注册一行；**格式清单不在 schema 里硬编码**。
3. **单一职责（SRP）**：「执行查询」「格式化数据」「交付文件」三者分离，便于 Agent 逐步委托。
4. **自动化友好**：所有能力都通过 HTTP 暴露，CLI / curl / Claude Code 均可一键触发。
5. **安全**：复用只读 SQL 校验；文件名 slugify 防路径穿越；副作用可控。
6. **最小侵入**：不改查询链路的既有行为，只做增量；确需改动的位置（`query_wrapper` 签名）以**向后兼容的可选参数**进行。

---

## 4. 整体架构

```
┌──────────────────────────────────────────────────────────────┐
│  触发方                                                       │
│  ① 前端 ResultTable「导出」按钮                               │
│  ② Claude Code: /query-export 命令 / data-exporter subagent   │
│  ③ curl / REST Client                                         │
└──────────────────────────────────────────────────────────────┘
          │ POST /api/v1/dbs/{name}/query/export { sql, format }
          ▼
┌──────────────────────────────────────────────────────────────┐
│  API 层  app/api/v1/exports.py                                │
│   - 参数校验、连接存在性检查、错误映射                         │
└──────────────────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────────────────────────────────────────────────┐
│  查询复用  query_wrapper.execute_query_with_service()         │
│   - 只读校验 + LIMIT 1000 → QueryResult（默认不写历史）        │
└──────────────────────────────────────────────────────────────┘
          │ QueryResult (columns, rows)
          ▼
┌──────────────────────────────────────────────────────────────┐
│  导出服务  app/services/export.py :: ExportService            │
│   - exporter_registry.get(format).export(...)                 │
└──────────────────────────────────────────────────────────────┘
          │  bytes
          ▼
┌──────────────────────────────────────────────────────────────┐
│  交付层  FastAPI Response / StreamingResponse                 │
│   - Content-Type / Content-Disposition: attachment            │
└──────────────────────────────────────────────────────────────┘
```

---

## 5. 后端设计

### 5.1 Exporter 抽象与注册表（复刻 adapter 模式）

新增 `backend/app/services/export.py`，结构对齐项目既有的 `DatabaseAdapter` / `DatabaseAdapterRegistry`：

```python
# app/services/export.py
from typing import Protocol, runtime_checkable, Iterator
from app.models.schemas import QueryResult

@runtime_checkable
class Exporter(Protocol):
    """导出器契约：把 QueryResult 序列化为某种格式的字节流。"""
    format_name: str          # "csv"
    content_type: str         # "text/csv; charset=utf-8"
    file_extension: str       # "csv"

    def export(self, result: QueryResult) -> bytes: ...
    # 可选：真流式（默认实现可 fallback 到一次性 export）
    def export_iter(self, result: QueryResult) -> Iterator[bytes]: ...


class ExporterRegistry:
    """格式注册表 —— 新增格式只需 register()，不改既有代码（OCP）。"""
    def __init__(self) -> None: ...
    def register(self, exporter: Exporter) -> None: ...
    def get(self, format_name: str) -> Exporter:      # 未注册 → KeyError
    def supported(self) -> list[str]: ...              # 动态返回，用于 GET /formats


class ExportService:
    """门面：根据 format 取 exporter 并执行，隔离上层对具体格式的依赖。"""
    def __init__(self, registry: ExporterRegistry): ...
    def export(self, result: QueryResult, fmt: str) -> tuple[bytes, str, str]:
        """returns (payload, content_type, file_extension)；未知 fmt 抛 ValueError"""
    def supported_formats(self) -> list[str]:
        """代理 registry.supported()，供 API 层 / GET /formats 使用"""
```

全局单例（与 `database_service` 风格一致）：

```python
exporter_registry = ExporterRegistry()
for exp in (CsvExporter(), JsonExporter(), NdJsonExporter()):
    exporter_registry.register(exp)
export_service = ExportService(exporter_registry)
```

> 一致性：API 层只调 `export_service`，不直接碰 `registry`；`supported_formats()` 是 `ExportService` 的显式方法（而非让 API 去够 `registry.supported()`）。

### 5.2 各格式实现要点

所有 exporter 共用一个**值规范化函数**，保证类型安全：

```python
def _normalize(value: Any) -> str:
    if value is None: return ""                       # NULL → 空
    if isinstance(value, bool): return "true" if value else "false"  # 必须先于 int 判断
    if isinstance(value, (dict, list)): return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (datetime, date)): return value.isoformat()
    return str(value)
```

| 格式 | content_type | 实现 | 细节 |
| --- | --- | --- | --- |
| **CSV** | `text/csv; charset=utf-8` | 标准库 `csv.DictWriter` | ① 写入前加 UTF-8 BOM，保证 Excel 正确识别中文；② `lineterminator="\r\n"`；③ 表头 = `[c.name for c in columns]`；④ 每个值过 `_normalize` |
| **JSON** | `application/json; charset=utf-8` | `json.dumps(..., ensure_ascii=False, indent=2, default=str)` | 默认 `document` 风格：`{database, sql, exportedAt, rowCount, columns, rows}`；可选 `array` 风格（纯行数组） |
| **NDJSON** | `application/x-ndjson` | 逐行 `json.dumps(row, default=str)` | 每行一个 JSON 对象，便于下游流式处理 |

> **关于「流式」的诚实说明**：`LIMIT 1000` 下结果最多 1000 行，一次性 `export() -> bytes` 完全没有内存压力。因此交付层默认用 `Response(content=payload)` 直接返回即可；`Exporter.export_iter()` 仅作为**协议预留**，等未来放宽行数上限时再让大表 exporter 实现真流式，届时交付层切到 `StreamingResponse(generator)`。本设计**不**为当前的 1000 行过度设计流式管道。

> CSV 的 BOM、布尔先于整数判断、datetime 走 ISO8601，都是基于 `QueryResult.rows` 实际类型分布（见 §2.2）的防坑设计。

### 5.3 API 端点

新增 `backend/app/api/v1/exports.py`，注册到 [main.py](backend/app/main.py)。

**请求 schema**（加入 [schemas.py](backend/app/models/schemas.py)）——`format` 故意用 `str`，**不**用 `Literal`，让格式清单由 `ExporterRegistry` 动态决定（OCP）：

```python
class ExportRequest(BaseModel):
    sql: str = Field(..., min_length=1)
    format: str = "csv"                                # 校验下放到 ExportService
    json_style: Literal["document", "array"] = "document"   # 仅 json 生效
    save_history: bool = False                          # 导出默认不污染查询历史（见 §5.5）
```

**路由**：

```python
# app/api/v1/exports.py
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlmodel import Session, select

from app.database import get_session
from app.models.database import DatabaseConnection
from app.models.schemas import ExportRequest
from app.services.query_wrapper import execute_query_with_service
from app.services.sql_validator import SqlValidationError
from app.services.export import export_service
from app.utils.filename import slugify, utc_timestamp   # 纯工具，置于 app/utils/

router = APIRouter(prefix="/api/v1/dbs", tags=["export"])


@router.get("/export/formats")
async def list_formats() -> dict[str, list[str]]:
    """动态返回支持的导出格式（来源：ExporterRegistry）。"""
    return {"formats": export_service.supported_formats()}


@router.post("/{name}/query/export")
async def export_query(name: str, req: ExportRequest, session: Session = Depends(get_session)):
    # 1) 连接存在性
    conn = session.exec(
        select(DatabaseConnection).where(DatabaseConnection.name == name)
    ).first()
    if not conn:
        raise HTTPException(404, f"Database connection '{name}' not found")

    # 2) 执行查询（复用只读校验 + LIMIT 1000；默认不写历史）
    try:
        result = await execute_query_with_service(
            session, name, conn.db_type, conn.url, req.sql,
            record_history=req.save_history,
        )
    except SqlValidationError as e:
        raise HTTPException(400, str(e))

    # 3) 序列化（格式不支持 → 422）
    try:
        payload, content_type, ext = export_service.export(result, req.format)
    except ValueError as e:                             # ExportService 对未知格式抛 ValueError
        raise HTTPException(422, str(e))

    # 4) 安全文件名 + 返回
    filename = f"{slugify(name)}_{utc_timestamp()}.{ext}"
    return Response(
        content=payload,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
```

`main.py` 增量（同时修 CORS，见 §6.2）：

```python
from app.api.v1 import databases, queries, exports
app.include_router(exports.router)
```

**对 `query_wrapper` 的最小改动**（向后兼容的可选参数，不改变现有查询行为）：

```python
async def execute_query_with_service(
    session, database_name, db_type, url, sql,
    query_source=QuerySource.MANUAL,
    record_history: bool = True,        # ← 新增，默认 True 保持现有行为
) -> QueryResult:
    ...
    if record_history:
        await save_query_history(session, database_name, sql, result.row_count, ...)
    return QueryResult(...)
```

REST Client 用例（追加到 [fixtures/test.rest](fixtures/test.rest)）——格式一律放 **body**：

```http
### 列出支持的导出格式
GET {{baseUrl}}/dbs/export/formats HTTP/1.1

### 导出为 CSV（一键：执行查询 + 导出）
POST {{baseUrl}}/dbs/{{dbName}}/query/export HTTP/1.1
Content-Type: application/json

{ "sql": "SELECT id, name, email FROM users WHERE status = 'active'", "format": "csv" }

### 导出为 JSON（document 风格）
POST {{baseUrl}}/dbs/{{dbName}}/query/export HTTP/1.1
Content-Type: application/json

{ "sql": "SELECT * FROM users LIMIT 100", "format": "json" }

### 导出但不写历史（默认即如此）
POST {{baseUrl}}/dbs/{{dbName}}/query/export HTTP/1.1
Content-Type: application/json

{ "sql": "SELECT * FROM users", "format": "ndjson", "save_history": false }
```

### 5.4 安全与边界

- **只读保证**：导出复用 `execute_query_with_service`，自动继承 `validate_and_transform_sql` 的只读校验，不存在「导出走旁路」的风险。
- **文件名注入**：`slugify` 仅保留 `[a-z0-9-]`，杜绝 `Content-Disposition` 路径穿越。
- **结果上限**：沿用 `LIMIT 1000`，导出量级可控（见 §5.2 关于流式的说明）。
- **错误映射**：`SqlValidationError→400`、连接不存在→`404`、格式不支持→`422`（由 `ExportService` 抛 `ValueError` 触发）、其他→`500`，与现有 queries 路由一致。
- **`format` 校验归属**：刻意不放在 Pydantic `Literal`，而由 `ExportService`/`ExporterRegistry` 在运行时判定 —— 这样新增格式只需注册 exporter，**无需改 schema**，真正落地 OCP。

### 5.5 副作用与权衡（重要）

复用 `execute_query_with_service` 会带来两个副作用，必须显式处理：

| 副作用 | 影响 | 处理 |
| --- | --- | --- |
| **重跑查询** | 导出 = 再执行一次 SQL，而非「导出屏幕上已展示的结果」。若两次执行间数据变化或 SQL 含非确定性（如 `RANDOM()`、无 `ORDER BY` 的分页），结果可能不一致 | 可接受：作业要求「执行查询 + 导出一键完成」，重跑恰好保证导出的是最新数据；前端按钮的 `sql` 取自当前编辑器，语义清晰 |
| **写 QueryHistory** | 旧实现会把导出也记成一条查询历史，挤占 50 条配额、污染真实查询记录 | 已解决：给 `execute_query_with_service` 加 `record_history` 开关，导出默认 `save_history=false`；需要审计时可传 `true` |

> 这是相比初版设计的关键修正：初版默认会污染历史且未说明「重跑」语义，现版本把两个副作用摆到台面并给出开关。

---

## 6. 前端设计

最小改动、贴合 antd 既有交互。

### 6.1 组件改动链路（execute.tsx → ResultTable）

`sql` 与 `databaseName` 都在 [execute.tsx](frontend/src/pages/queries/execute.tsx) 手中，因此导出回调定义在父级并下传，`ResultTable` 只负责触发：

```tsx
// execute.tsx
const handleExport = async (format: ExportFormat) => {
  if (!databaseName || !sql.trim()) return;
  try {
    await exportQuery(databaseName, sql.trim(), format);
    message.success(`已导出为 ${format.toUpperCase()}`);
  } catch (e) {
    message.error("导出失败");
  }
};
// 传给 <ResultTable result={result} loading={loading} onExport={handleExport} />
```

`ResultTable` 的 props 接口相应扩展：

```ts
interface ResultTableProps {
  result: QueryResult | null;
  loading?: boolean;
  onExport?: (format: ExportFormat) => void;   // 新增
}
```

在结果统计区（当前 `Rows:` / `Execution Time:` 两个 `Tag` 旁）追加导出下拉：

```tsx
import { DownloadOutlined } from "@ant-design/icons";
import { Button, Dropdown } from "antd";

<Dropdown
  menu={{
    items: [
      { key: "csv",   label: "导出为 CSV" },
      { key: "json",  label: "导出为 JSON" },
      { key: "ndjson",label: "导出为 NDJSON" },
    ],
    onClick: ({ key }) => onExport?.(key as ExportFormat),
  }}
>
  <Button icon={<DownloadOutlined />} disabled={!result || result.rowCount === 0}>
    导出
  </Button>
</Dropdown>
```

### 6.2 下载流程与 CORS（[api.ts](frontend/src/services/api.ts)）

```ts
export async function exportQuery(
  databaseName: string, sql: string, format: ExportFormat
): Promise<void> {
  const res = await apiClient.post(
    `/api/v1/dbs/${databaseName}/query/export`,
    { sql, format },
    { responseType: "blob" }            // ← 关键：以二进制接收
  );
  // 跨域下需要后端 expose 该头，否则读不到、走 fallback
  const filename = parseFilename(res.headers["content-disposition"]) ?? `export.${format}`;
  const url = URL.createObjectURL(res.data);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}
```

**必须配套修改 CORS**（否则跨域读不到文件名）：[main.py](backend/app/main.py) 的 `CORSMiddleware` 当前**没有** `expose_headers`，跨域请求下 `res.headers["content-disposition"]` 会是 `undefined`。需补：

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],   # ← 新增，前端才能读到文件名
)
```

> 设计取舍：下载走「后端导出 API」而非纯前端拼装，是为了让前端、CLI、Agent **共用同一份序列化逻辑**（DRY），并让 `_normalize` 的类型规则只有一处实现。文件名读取失败时前端有 `export.{format}` fallback，不致阻断下载。

---

## 7. Claude Code 自动化设计（核心练习点）

作业要求用 Claude Code 的 Agent 或自定义 Command，把「执行查询 + 导出」做成一键流程，并体验 Agent 的任务分解。本设计提供**两条互补路径**。

### 7.1 任务分解（Agent 视角）

把「导出数据」拆为可独立委托的子任务，正好对应后端三个组件：

```
导出意图
  ├─① 获取查询结果  → query_wrapper / POST /query
  ├─② 格式化数据    → ExportService / ExporterRegistry
  └─③ 创建/交付文件 → Response attachment → 本地保存（exports/）
```

### 7.2 自定义 Command（路径 A：一键命令）

新增 `.claude/commands/query-export.md`（项目已有 `.claude/commands/` 目录）：

````markdown
---
description: 用自然语言或 SQL 查询数据库并导出为 CSV/JSON，一键完成
argument-hint: <数据库连接名> <自然语言或SQL> [格式:csv|json|ndjson]
allowed-tools: Bash, Read, Write
---

你是「数据库查询导出助手」。用户输入：$ARGUMENTS

工作流：
1. 解析三个槽位：数据库名、查询意图（自然语言或 SQL）、目标格式（缺省 csv）。
2. 若意图是自然语言，先调用：
   curl -s -X POST http://localhost:8000/api/v1/dbs/<db>/query/natural \
     -H 'Content-Type: application/json' \
     -d '{"prompt":"<意图>"}'        # 取返回的 sql 字段
   并把生成的 SQL 给用户确认。
3. 先查支持格式（可选）：GET /api/v1/dbs/export/formats
4. 调用一键导出端点，把结果落盘到 ./exports/ 目录：
   curl -s -X POST 'http://localhost:8000/api/v1/dbs/<db>/query/export' \
     -H 'Content-Type: application/json' \
     -d '{"sql":"<sql>","format":"<格式>"}' \
     -o './exports/<db>_<时间戳>.<ext>'
5. 报告：文件路径、行数、耗时。

约束：
- SQL 必须只读；非 SELECT 拒绝执行并提示。
- 格式不在支持列表时，提示并询问。
- 导出前用一句话向用户确认「库 + SQL + 格式」。
````

用法示例（用户在 Claude Code 中）：

```
/query-export todo 查询所有活跃用户的姓名和邮箱 csv
/query-export interview_db "SELECT * FROM candidates LIMIT 200" json
```

### 7.3 专用 Subagent（路径 B：委托式）

新增 `.claude/agents/data-exporter.md`，封装「查询 + 导出」专长，供主 Agent 在识别到导出意图时委托：

```markdown
---
name: data-exporter
description: 执行数据库查询并把结果导出为 CSV/JSON/NDJSON 文件。当用户想把查询结果保存为文件、导出数据、生成报表时使用。
tools: Bash, Read, Write
---

你是一个专注于「查询并导出」的子代理。

能力边界：
- 仅处理 SELECT 查询的导出；不修改任何数据库数据。
- 复用本项目的 HTTP API（默认 http://localhost:8000）。

标准动作：
1. 明确数据库名、查询、格式三要素；信息不全时先向调用方询问。
2. 自然语言 → POST /query/natural 取 SQL；直接 SQL → 跳过。
3. POST /query/export 下载文件到 ./exports/，文件名含时间戳。
4. 返回结构化结果：{ file, rows, durationMs, sql }。

原则：
- 安全：拒绝写操作，拒绝危险函数。
- 确认：执行前向用户确认 SQL。
- 幂等：同名文件不覆盖，自动追加序号。
```

主 Agent 检测到「导出 / 下载 / 存成文件」等意图时，通过 Task 工具委托 `data-exporter`，实现作业所述「Agent 协调处理多个子任务」。

---

## 8. 用户交互设计（作业要求 ③）

| 场景 | 触发方式 | 体验 |
| --- | --- | --- |
| 界面操作 | `ResultTable`「导出」下拉 | 查询成功后按钮可用，选格式即下载 |
| 自然语言（CLI） | `/query-export` 命令 | 直接输入意图：「导出 users 表为 csv」 |
| AI 主动询问 | Command/Agent 工作流第 2/5 步 | 生成 SQL 后、导出前确认；导出后报告「需要导出为 CSV 或 JSON 吗？」式的二次引导 |

> 作业原文举例「查询后 AI 助手主动询问是否导出」——在 CLI 场景由 Command prompt 的「确认步骤」实现；在 Web 场景可用 antd `message`/`Modal` 在查询成功后轻提示，作为可选增强。

---

## 9. 改动清单（提交物代码概览）

**后端（新增 + 小改）**
- `backend/app/services/export.py`（新）— `Exporter` / `ExporterRegistry` / `ExportService` + Csv/Json/NdJson 实现 + `_normalize`
- `backend/app/api/v1/exports.py`（新）— `POST /{name}/query/export` + `GET /export/formats`
- `backend/app/models/schemas.py`（改）— `ExportRequest`（`format: str`）
- `backend/app/services/query_wrapper.py`（改）— 新增向后兼容的可选参数 `record_history: bool = True`
- `backend/app/utils/filename.py`（新）— `slugify` / `utc_timestamp` 纯工具
- `backend/app/main.py`（改）— 注册 `exports.router` + CORS 补 `expose_headers`
- `backend/tests/unit/test_export.py`（新）— exporter 单测（类型规范化、CSV 表头/BOM、JSON 风格、未知格式）
- `fixtures/test.rest`（改）— 导出用例（格式一律放 body）

**前端（小改）**
- `frontend/src/types/query.ts`（改）— `ExportFormat` 类型
- `frontend/src/services/api.ts`（改）— `exportQuery()` 下载 + `parseFilename` fallback
- `frontend/src/components/ResultTable.tsx`（改）— 新增 `onExport` prop + 导出下拉按钮
- `frontend/src/pages/queries/execute.tsx`（改）— 定义 `handleExport` 并下传

**Claude Code 自动化（新增，已落地于仓库根 `.claude/`）**
- `.claude/commands/query-export.md`（新）— `/query-export` 一键命令
- `.claude/agents/data-exporter.md`（新）— 委托式 subagent

**`.gitignore`**：追加 `exports/`，避免导出文件入库。

> 现有查询链路（queries/database_service/adapter）的既有行为**零改动**；对 `query_wrapper` 仅追加默认值为 `True` 的可选参数，原查询 API 行为完全不变。

---

## 10. 测试计划

| 层级 | 用例 |
| --- | --- |
| 单元（export.py） | NULL→空、bool→"true"/"false"、datetime→ISO、dict/list→JSON 字符串；CSV 含 BOM 与正确表头；JSON document/array 两风格；NDJSON 每行可解析；未知格式 `ExportService.export` 抛 `ValueError`；`supported_formats()` 含全部已注册格式 |
| 单元（filename.py） | `slugify` 剥离特殊字符防注入；`utc_timestamp` 格式合法 |
| API（exports.py） | 正常导出 csv/json/ndjson；非 SELECT → 400；连接不存在 → 404；格式不支持 → 422；`save_history=false` 时历史表无新增；`Content-Disposition` 文件名合法；`GET /export/formats` 返回动态列表 |
| 端到端 | PostgreSQL + MySQL 各跑一次导出，验证与库类型无关 |
| 自动化 | `/query-export` 自然语言路径与纯 SQL 路径各一次；文件落 `exports/` 且行数正确 |

---

## 11. 实现路线图

> **状态：Phase 1–6 全部落地。** 命令 `/query-export` 与 subagent `data-exporter` 已创建于**仓库根** `.claude/`（与既有 `deep-code-review`/`py-arch` 同位，即项目级 command/agent 的发现目录）。

1. ✅ **Phase 1 — 导出内核**：`export.py`（Exporter/Registry/Service + 三格式）+ `utils/filename.py` + 单测（24 例）。
2. ✅ **Phase 2 — 查询侧小改**：`query_wrapper` 加 `record_history` 参数（默认 True）。
3. ✅ **Phase 3 — API 暴露**：`exports.py` + `ExportRequest` + 注册路由 + CORS `expose_headers` + REST 用例 + API 测试（15 例）。
4. ✅ **Phase 4 — 前端下载**：`api.ts`（`exportQuery` 下载）+ `ResultTable` 导出下拉 + `execute.tsx` 下传回调 + **主页面 `Home.tsx` 改为后端驱动**（删除客户端 CSV/JSON 拼装）。
5. ✅ **Phase 5 — 自动化**：仓库根 `.claude/commands/query-export.md`（一键命令，含任务分解与「查询后主动询问」）+ `.claude/agents/data-exporter.md`（委托式 subagent）。
6. ✅ **Phase 6 — 文档与收尾**：`.gitignore` 加 `exports/`、本文件定稿。

每个 Phase 可独立验证，便于用 Cursor 快速迭代、用 Claude Code 做多步骤自动化（呼应作业「工具链整合」练习点）。

---

## 12. 设计决策与初版修正小结

**为什么这样做**

- **后端统一导出而非前端拼装**：序列化逻辑单一来源（DRY），且 CLI/Agent 可复用同一 HTTP 端点，直接满足「自动化」要求。
- **ExporterRegistry 复刻 AdapterRegistry**：沿用项目既定 OCP 风格，新增 Excel/Markdown/INSERT-SQL 等格式零侵入。
- **一键端点 `/query/export`**：对应作业「执行查询 + 导出一键完成」，避免前端回传大数据。
- **Command + Subagent 双路径**：Command 适合固定流程的快速触发，Subagent 适合主 Agent 灵活委托，完整覆盖作业对 Agent/Command 的练习意图。

**相对初版的关键修正（本次评审）**

1. `format` 从 `Literal` 改为 `str`：避免硬编码格式清单违反 OCP，校验下放到 `ExportService`，并新增 `GET /export/formats` 动态返回；删除了原先不可达的 `except KeyError` 死代码。
2. 显式披露并处理「重跑查询 + 写历史」副作用：`query_wrapper` 新增 `record_history` 开关，导出默认 `save_history=false`，不再污染查询历史。
3. 补齐 CORS `expose_headers=["Content-Disposition"]`，否则跨域下前端读不到下载文件名。
4. 修正 REST 示例的 `?format=csv` query param 误用（格式统一放 body）。
5. 诚实标注「流式」：当前 1000 行内一次性生成 `bytes` 足够，`export_iter()` 仅作协议预留，不为小数据过度设计。
6. 补齐 `ExportService.supported_formats()`，消除 API 层对 registry 私有方法的直接依赖；补全 NDJSON 的 `content_type`；明确前端 `onExport` 的 props 传递链路与工具函数归属。