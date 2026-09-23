# 0009 - pg-mcp 缺失功能详细设计（多数据库安全 / 弹性可观测整合 / 模型缺陷修复）

| 项 | 内容 |
|---|---|
| 文档编号 | specs/w5/0009 |
| 关联文档 | 0001-prd、0002-design、0006-code-review（codex review 缺失项来源）、0007-test-plan |
| 状态 | 已实现（P0–P3 全部落地，实施记录见附录 D） |
| 目标版本 | v0.3.0 |
| 读者 | 实现工程师、代码评审者、课程作业评审 |

---

## 1. 背景与问题陈述

codex review（0006）指出三类"设计中承诺、实现中缺失"的问题。经代码核实，逐条对应如下事实（文件:行号以当前 master 为准）：

### 问题 A：多数据库与安全控制未启用

| # | 事实 | 证据 |
|---|---|---|
| A1 | 服务实际只用单库单执行器：lifespan 只为主库创建 executor，orchestrator 只拿到主库那一个，即使有多库连接池也全部走同一执行器 | `server.py:161-168,197` |
| A2 | SQLValidator 支持表/列黑名单与 EXPLAIN 开关，但 lifespan 传 `blocked_tables=None, blocked_columns=None, allow_explain=False`，且无任何环境变量可配置 | `server.py:153-158`、`sql_validator.py:77-100` |
| A3 | `SecurityConfig` 缺少 `blocked_tables / blocked_columns / allow_explain` 字段，敏感对象保护停留在 validator 能力层面 | `config/settings.py:73-109` |
| A4 | `.env.example` 已暗示 `SECONDARY_DATABASE_*` 多库扩展，但代码无对应实现 | `.env.example:228-239` |
| A5 | EXPLAIN 校验存在漏洞：`allow_explain=True` 时不校验内层 SQL，且未区分 `EXPLAIN ANALYZE`（会真实执行语句） | `sql_validator.py:152-168` |

### 问题 B：弹性与可观测性未接入请求链路

| # | 事实 | 证据 |
|---|---|---|
| B1 | `MultiRateLimiter` 在 lifespan 中实例化但 `for_queries/for_llm` 从未被调用，限流零生效 | `server.py:187-190`（全局无调用点） |
| B2 | 重试循环无退避：`RESILIENCE_RETRY_DELAY / BACKOFF_FACTOR` 配置存在但循环内无任何 sleep | `orchestrator.py:377-478` |
| B3 | LLM 瞬时错误（超时/网络抖动）直接终止，不重试；仅校验失败有反馈重试 | `orchestrator.py:458-471` |
| B4 | `MetricsCollector` 定义全套指标并在 9090 暴露，但流水线零埋点，所有计数恒为 0 | `metrics.py` 全文 vs `orchestrator.py`（无引用） |
| B5 | `tracing.py` 的 request_context/TracingLogger 未使用；orchestrator 自造 uuid 且日志 formatter 不输出 request_id | `tracing.py` 全文 vs `orchestrator.py:130` |
| B6 | 熔断器在 lifespan 与 orchestrator 各建一个，前者为死代码 | `server.py:181-184`、`orchestrator.py:99-102` |

### 问题 C：响应/模型缺陷与入口错误

| # | 事实 | 证据 |
|---|---|---|
| C1 | `QueryResponse` 定义了两个 `to_dict`（后者覆盖前者，语义互相矛盾：一个 include-none 一个 exclude-none） | `models/query.py:160-173` 与 `214-220` |
| C2 | `tokens_used` 恒为 0：从未读取 OpenAI 响应的 `usage` 字段 | `orchestrator.py:396-398`、`sql_generator.py:99-152` |
| C3 | 未使用/自相矛盾的配置字段：`allow_write_operations`（校验器不读）、`validation.min_confidence_score`（与 confidence_threshold 重复）、`cache.max_size`（缓存无上限）、`validation.max_question_length`（QueryRequest 硬编码 10000） | `settings.py:78,119,144,116` |
| C4 | `OPENAI_MAX_TOKENS` pydantic 上限 4096，而 `.env.example`/docker-compose 默认 32000 → 按示例配置启动即失败 | `settings.py:53` vs `.env.example:79` |
| C5 | 根目录 `main.py` 是遗留 demo（加法工具服务器）；README 快速开始与 Dockerfile `CMD ["python","main.py"]` 均指向它 → **Docker 部署启动的是错误服务** | `main.py`、`Dockerfile:96`、README:98 |
| C6 | Dockerfile 健康检查 `import psutil`，psutil 不在依赖中 → 容器必然 unhealthy | `Dockerfile:91-92` |
| C7 | server.py 工具层重复补 `tokens_used`、校验结果硬编码（is_select=True 等），校验细节未透传 | `server.py:359-361`、`orchestrator.py:448-454` |

---

## 2. 目标与非目标

### 2.1 目标

1. **G1 多数据库**：支持一次部署配置多个 PostgreSQL 库，请求按 `database` 参数路由到对应执行器；未指定时按默认库解析。
2. **G2 精细安全控制**：表/列黑名单、EXPLAIN 策略可配置（全局 + 按库覆盖），在 SQL 校验层强制生效；修复 EXPLAIN ANALYZE 漏洞。
3. **G3 弹性生效**：限流（查询级 + LLM 级）真实拦截并发；LLM 瞬时错误带指数退避重试；校验重试间插入退避。
4. **G4 可观测生效**：全部已定义 Prometheus 指标在流水线真实埋点；request_id 贯穿日志；新增熔断/限流状态 gauge。
5. **G5 模型治理**：消除重复 `to_dict`；`tokens_used` 真实统计；未用配置字段"启用或删除"逐一定案；清理死代码；修复 Docker 入口与健康检查。
6. **G6 测试补强**：新增单元/集成测试覆盖上述能力，安全模块覆盖率 ≥95%，整体 ≥80%。

### 2.2 非目标

- 不支持写操作（`allow_write_operations` 将被删除而非实现，见 5.3）。
- 不引入 OpenTelemetry 等外部 tracing 后端（沿用 contextvars + 日志方案）。
- 不做多租户鉴权/每调用方限流（限流按进程全局维度）。
- 不改动 MCP 工具签名（`query(question, database, return_type)` 保持兼容）。

---

## 3. 总体方案

架构变化集中在三处（其余模块对外行为不变）：

```
                     ┌────────────────────────────────────────────────┐
                     │ server.py (lifespan)                           │
                     │  Settings → DatabaseManager.build_runtimes()   │
                     │  → dict[str, DatabaseRuntime]                  │
                     └───────────────┬────────────────────────────────┘
                                     │ 注入
┌────────────────────────────────────▼───────────────────────────────────┐
│ QueryOrchestrator                                                      │
│  runtimes: dict[str, DatabaseRuntime]   ← 取代 单 executor+单 validator │
│  rate_limiter: MultiRateLimiter         ← B1 接入                      │
│  metrics: MetricsCollector              ← B4 埋点                      │
│                                                                        │
│  execute_query:                                                        │
│    request_context(request_id)          ← B5 追踪                      │
│    rate_limiter.for_queries() → 整条流水线                              │
│      ├─ 校验问题长度(question_to_long)                                  │
│      ├─ runtime[db].validator  ← 按库策略校验（含表/列黑名单、EXPLAIN）   │
│      ├─ rate_limiter.for_llm() → LLM 生成（带退避重试 + token 统计）     │
│      ├─ runtime[db].executor   ← 按库执行器                             │
│      └─ 结果验证（for_llm + token 统计）                                │
└────────────────────────────────────────────────────────────────────────┘
```

三个功能组按依赖顺序分阶段实施：**Phase 0 入口修复 → Phase 1 模型缺陷(F3) → Phase 2 弹性可观测(F2) → Phase 3 多数据库安全(F1)**。理由：F3 的 `GenerationResult.tokens_used` 是 F2 指标埋点的数据来源；F1 改造 orchestrator 构造参数，放在最后避免与 F2 改动冲突。

---

## 4. 详细设计 F1：多数据库与按库安全控制

### 4.1 配置模型

**决策：新增 `DATABASES_JSON` 环境变量承载多库配置；保留现有 `DATABASE_*` 前缀作为单库模式（向后兼容）。**

被否决的备选：编号前缀（`DATABASE_2_HOST`...）——遍历需扫描环境变量、按库策略字段无法自然嵌套、与 `.env.example` 中 `SECONDARY_DATABASE_*` 的暗示也不一致；JSON 方案 pydantic 原生支持、策略字段可嵌套。

```python
# config/settings.py 新增

class DatabaseSecurityOverride(BaseModel):
    """按库覆盖的安全策略，缺省字段回落到全局 SecurityConfig。"""
    blocked_tables: list[str] | None = None       # ["internal", "audit.logs"]
    blocked_columns: list[str] | None = None      # ["users.password", "ssn"]
    allow_explain: bool | None = None
    allow_explain_analyze: bool | None = None
    readonly_role: str | None = None
    safe_search_path: str | None = None

class DatabaseConnectionParams(BaseModel):
    """连接参数纯模型 —— 刻意不继承 DatabaseConfig(BaseSettings)。

    若嵌套 BaseSettings，字段缺失时会静默回退读取 DATABASE_* 环境变量，
    与规则 2（DATABASES_JSON 模式下忽略 DATABASE_*）矛盾。
    JSON 条目必须自包含，缺失字段取与 DatabaseConfig 相同的默认值。"""
    host: str = "localhost"
    port: int = 5432
    name: str = ""
    user: str = "postgres"
    password: str = ""
    min_pool_size: int = 5
    max_pool_size: int = 20
    pool_timeout: float = 30.0
    command_timeout: float = 30.0


class DatabaseEntry(BaseModel):
    """DATABASES_JSON 数组元素 = 连接配置 + 可选策略覆盖。"""
    connection: DatabaseConnectionParams
    security: DatabaseSecurityOverride | None = None

class MultiDatabaseConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MULTIDB_")
    databases_json: str | None = Field(
        default=None,
        description='JSON 数组，如 [{"connection":{...},"security":{...}}, ...]',
    )
    default_database: str | None = Field(
        default=None,
        description="多库模式下 request.database 未指定时的默认库；未配置则单库自动选择、多库报错",
    )
```

解析规则（`Settings` 组合时执行，失败 **fail-fast** 抛 `ConfigValidationError`，服务器拒绝启动）：

1. `databases_json is None` → 单库模式：`entries = [DatabaseEntry(connection=现有 database 配置)]`，行为与 v0.2 完全一致。
2. `databases_json` 非空 → 解析 JSON 数组；库 `name` 重复时报错；此时 `DATABASE_*` 前缀变量**被忽略**并在启动日志 warning 提示（避免两套配置静默混用）。
3. `default_database` 必须出现在 entries 中，否则启动报错。

环境变量示例：

```bash
# 单库模式（现状不变）
DATABASE_NAME=blog_small ...

# 多库模式
MULTIDB_DATABASES_JSON=[{"connection":{"name":"blog_small","host":"localhost","user":"ro_user","password":"***"}},{"connection":{"name":"saas_crm_large","host":"10.0.0.8"},"security":{"blocked_tables":["tenant_secrets"],"allow_explain":true}}]
MULTIDB_DEFAULT_DATABASE=blog_small
```

### 4.2 EffectivePolicy 与按库 Validator

```python
# config/policy.py 新增
@dataclass(frozen=True)
class EffectivePolicy:
    """全局 SecurityConfig 与按库 override 合并后的最终策略。"""
    blocked_functions: frozenset[str]
    blocked_tables: frozenset[str]        # 小写；裸名或 schema.table
    blocked_columns: frozenset[str]       # 小写；裸名或 table.column
    allow_explain: bool
    allow_explain_analyze: bool
    block_system_catalogs: bool
    readonly_role: str | None
    safe_search_path: str
    max_rows: int
    max_execution_time: float

    @classmethod
    def merge(cls, base: SecurityConfig, override: DatabaseSecurityOverride | None) -> "EffectivePolicy":
        ...  # None 字段逐项回落 base
```

**SecurityConfig 同步扩展**（全局默认值，env 前缀 `SECURITY_`）：

| 新增字段 | env | 默认 | 说明 |
|---|---|---|---|
| `blocked_tables` | `SECURITY_BLOCKED_TABLES` | `[]` | 逗号分隔，支持 `schema.table` |
| `blocked_columns` | `SECURITY_BLOCKED_COLUMNS` | `[]` | 逗号分隔，支持 `table.column` |
| `allow_explain` | `SECURITY_ALLOW_EXPLAIN` | `false` | 是否允许 EXPLAIN |
| `allow_explain_analyze` | `SECURITY_ALLOW_EXPLAIN_ANALYZE` | `false` | ANALYZE 会真实执行，默认禁用 |
| `block_system_catalogs` | `SECURITY_BLOCK_SYSTEM_CATALOGS` | `false` | 拦截 pg_catalog/information_schema 表引用；默认关闭以兼容 README 示例（见 4.5 第 3 点） |

### 4.3 DatabaseRuntime 与 DatabaseManager

```python
# db/runtime.py 新增
@dataclass
class DatabaseRuntime:
    """一个库的全部运行期组件：请求路由到的所有能力都从这里取。"""
    name: str
    pool: Pool
    executor: SQLExecutor          # 构造时注入该库 effective policy
    validator: SQLValidator        # 构造时注入该库 effective policy
    policy: EffectivePolicy

class DatabaseManager:
    @staticmethod
    async def build(settings: Settings) -> dict[str, DatabaseRuntime]:
        """按 4.1 解析规则构建所有库的 runtime；任一库连接失败则整体启动失败。"""
```

lifespan 装配改为：

```python
runtimes = await DatabaseManager.build(_settings)
for rt in runtimes.values():
    await _schema_cache.load(rt.name, rt.pool)      # SchemaCache 已按库名 key，无需改动
_orchestrator = QueryOrchestrator(runtimes=runtimes, ...)
```

同时删除 lifespan 中的死代码熔断器与限流器实例（B6，迁移到 orchestrator，见 F2）。

### 4.4 Orchestrator 路由改动

```python
class QueryOrchestrator:
    def __init__(self, runtimes: dict[str, DatabaseRuntime], ...): ...

    def _resolve_database(self, database: str | None) -> str:
        if database is not None:
            if database not in self.runtimes:
                raise DatabaseError("Database ... not found",
                    details={"requested": database, "available": list(self.runtimes)})
            return database
        if len(self.runtimes) == 1:
            return next(iter(self.runtimes))          # 单库自动选择（现状兼容）
        if self._default_database:
            return self._default_database             # 多库 + 配置了默认
        raise DatabaseError("Multiple databases available, please specify which to query",
            details={"available_databases": list(self.runtimes)})
```

`execute_query` 内两处取用改为按库：

- 生成重试循环中的 `self.sql_validator.validate_or_raise(...)` → `runtime.validator.validate_or_raise(...)`
- 执行阶段 `self.sql_executor.execute(...)` → `runtime.executor.execute(...)`

**接口清理（配套，实施 P3 时一并完成）**：

- `QueryOrchestrator.__init__` 的 `pools: dict[str, Pool]` 参数删除（runtime 已持有 pool），schema 回填改走 `runtime.pool`
- `SQLValidator` 构造签名改为接收 `EffectivePolicy`（替换现有 SecurityConfig + 散参形式）
- `SQLExecutor` 构造参数改从 EffectivePolicy 取 max_rows / max_execution_time / safe_search_path / readonly_role，并**新增 `database_name` 字段**（§5.3 `database_errors_total` 的 label 数据源）

**安全语义**：策略跟着库走——即使请求方指定了库 B，也只会用 B 的策略校验，无法通过切换库绕过 A 的黑名单；同一请求从头到尾只接触一个 runtime。

### 4.5 SQLValidator 增强（表/列黑名单 + EXPLAIN 策略）

现有 `_check_blocked_tables/_check_blocked_columns` 的 `exp.Table/exp.Column` 遍历已覆盖 CTE 与子查询（sqlglot AST 全树遍历），需补强三点：

1. **schema 限定匹配**：`blocked_tables` 含 `.` 时匹配 `schema_name.table_name`；纯名单独匹配 `table_name`。列同理（`table.column` 用 `column.table`）。
2. **EXPLAIN 内层校验**（修复 A5）：

```python
if isinstance(statement, exp.Command) and cmd_name == "EXPLAIN":
    if not self.policy.allow_explain:
        raise SecurityViolationError("EXPLAIN statements are not allowed")
    inner = _extract_explain_body(statement)
    # EXPLAIN ANALYZE 会真实执行语句，单独开关控制。
    # 注意两种语法：前缀形式 "EXPLAIN ANALYZE SELECT ..." 与括号选项形式
    # "EXPLAIN (ANALYZE, BUFFERS) SELECT ..." —— 仅 startswith("ANALYZE") 会漏检后者。
    options, body = _split_explain_options(inner)
    # _split_explain_options：括号形式取配对 "()" 内逗号分隔项集合，
    # 前缀形式取语句关键字之前的单词集合；返回 (选项集合, 内层语句文本)
    if "ANALYZE" in options:
        if not self.policy.allow_explain_analyze:
            raise SecurityViolationError("EXPLAIN ANALYZE is not allowed")
    inner = body.strip()
    if not inner:
        raise SQLParseError("EXPLAIN has no inner query")
    self.validate_or_raise(inner)   # 递归：内层语句必须完整通过全部规则
    return None
```

> 实现注意：sqlglot 28.5 将 EXPLAIN 解析为 `exp.Command`，内层文本的存放位置随版本有差异；`_extract_explain_body` 以"原始 SQL 去掉 EXPLAIN 关键字前缀"为兜底方案，并用单元测试锁定当前依赖版本的行为。

3. **系统目录保护（opt-in，默认关闭）**：`SECURITY_BLOCK_SYSTEM_CATALOGS=true` 时拒绝 `pg_catalog.*`、`information_schema.*` 表引用。**不默认开启**：README 的示例查询（"How many tables are in the database?" → 查 `information_schema.tables`）依赖元数据读取，默认拒绝会破坏文档化用例；且搜索路径已固定 + 只读事务下，元数据读取风险可控。

### 4.6 对外行为变化

| 场景 | v0.2 行为 | v0.3 行为 |
|---|---|---|
| 单库 + `database=None` | 自动选择 | 不变 |
| 多库 + `database=None` | 报错 | 有 `default_database` 则用之，否则报错（错误信息含可用库列表） |
| `database` 指向不存在的库 | `database_error` | 不变（details 增加 available 列表） |
| 命中黑名单表/列 | 无此能力 | `security_violation`，message 指明对象（不回显完整 SQL） |

---

## 5. 详细设计 F2：弹性与可观测性整合

### 5.1 限流接入（B1）

**决策：限流器注入 orchestrator（而非在 server.py 工具层包裹），查询级包裹整条流水线，LLM 级包裹两次 LLM 调用。**

```python
# orchestrator.__init__ 新增参数
rate_limiter: MultiRateLimiter | None = None

async def execute_query(self, request) -> QueryResponse:
    limiter = self.rate_limiter
    if limiter is None:
        return await self._execute_query_impl(request)
    # 窄化判断：用 acquire() 的 bool 返回值，而非 except TimeoutError 包裹 impl。
    # impl 内部（asyncio.wait_for 等）同样会抛 TimeoutError，宽泛 except
    # 会把数据库/其他超时误报成 rate_limit_exceeded。现有 RateLimiter.acquire
    # 契约为"超时返回 False 不抛异常"，正好支撑这种写法。
    if not await limiter.query_limiter.acquire(timeout=self._acquire_timeout):
        return self._error_response(
            ErrorCode.RATE_LIMIT_EXCEEDED,
            "Too many concurrent queries, please retry later",
            details={"retry_after_seconds": 1.0, "limiter": "queries"},
        )
    try:
        return await self._execute_query_impl(request)
    finally:
        limiter.query_limiter.release()
```

LLM 级在 `_generate_sql_with_retry` 与 `_validate_results_safely` 的调用点，通过专用 helper 统一处理槽位超时：

```python
@asynccontextmanager
async def _llm_slot(self):
    """获取 LLM 槽位；超时抛 RateLimitExceededError（errors.py 已有，当前无人使用）。
    不能直接用 for_llm()：其 TimeoutError 不会被生成循环的 `except LLMError`
    捕获，会一路冒泡被顶层当成 INTERNAL_ERROR。"""
    if self.rate_limiter is None:
        yield
        return
    if not await self.rate_limiter.llm_limiter.acquire(timeout=self._acquire_timeout):
        raise RateLimitExceededError(
            "LLM concurrency limit exceeded",
            details={"retry_after_seconds": 1.0, "limiter": "llm"},
        )
    try:
        yield
    finally:
        self.rate_limiter.llm_limiter.release()

# 调用点
async with self._llm_slot():
    result = await self.sql_generator.generate(...)
```

orchestrator 顶层异常处理新增 `except RateLimitExceededError` 分支（排在 `except PgMcpError` 之前或复用其 code 映射）返回 `rate_limit_exceeded` 响应。

新配置（消除 server.py 中的硬编码 10/5）：

| env | 默认 | 说明 |
|---|---|---|
| `RESILIENCE_RATE_LIMIT_QUERY` | `10` | 并发查询上限 |
| `RESILIENCE_RATE_LIMIT_LLM` | `5` | 并发 LLM 调用上限 |
| `RESILIENCE_RATE_LIMIT_ACQUIRE_TIMEOUT` | `5.0` | 获取槽位等待秒数，超时拒绝 |

设计取舍说明：查询槽位**覆盖全流水线**（含 LLM 重试等待期）是有意为之——慢 LLM 会占住查询并发额度，正是需要保护的场景；代价是高峰期新请求 5 秒后收到 `rate_limit_exceeded`，该行为写入 README 故障排查。

### 5.2 重试与指数退避（B2/B3）

**统一的退避函数与可重试错误分类：**

```python
# resilience/backoff.py 新增
def backoff_delay(base: float, factor: float, attempt: int, *,
                  cap: float = 30.0, jitter: float = 0.2) -> float:
    delay = min(base * (factor ** attempt), cap)
    return delay * random.uniform(1 - jitter, 1 + jitter)   # 防惊群
```

错误可重试性分类（LLM 异常体系小幅扩展；新增 `LLMResponseError(LLMError)` 子类承载"响应为空 / SQL 提取失败"，generator 中对应的两处 raise 点改抛该子类）：

| 异常 | 可重试 | 理由 |
|---|---|---|
| `LLMTimeoutError` | ✅ | 瞬时抖动 |
| `LLMError`（网络/5xx/空响应以外的 API 错误） | ✅ | 瞬时抖动 |
| `LLMResponseError`（SQL 提取失败，新增子类） | ❌ 走反馈重试 | temperature=0 时无反馈重试输出基本不变，纯退避是白烧预算；把"响应无法解析"作为 error_feedback 进入反馈通道 |
| `LLMUnavailableError`(认证失败) | ❌ | 换 key 前重试无意义 |
| `LLMUnavailableError`(限流 429) | ✅ | 正是退避的典型场景 |
| `SecurityViolationError` / `SQLParseError` | 走既有反馈重试 | 不属于本节 |

实现方式：`LLMUnavailableError.__init__` 增加 `retryable: bool = False`，generator 的错误映射处对 `rate_limit` 关键字设 `retryable=True`。

**重试预算合并（关键决策）**：LLM 瞬时错误重试与"校验失败反馈重试"**共享同一个 `max_retries` 预算**，避免两者相乘导致 4×4=16 次调用：

```python
async def _generate_sql_with_retry(self, ...):
    for attempt in range(max_retries + 1):
        try:
            async with self._llm_slot():
                result = await self._call_generator(...)
        except RateLimitExceededError:
            raise                                  # 顶层统一返回限流响应，不消耗重试预算
        except LLMResponseError as e:
            # 内容级失败：转反馈重试（temp=0 下无反馈重试输出基本不变）
            previous_sql, error_feedback = None, f"Response unparseable: {e}"
            await asyncio.sleep(backoff_delay(cfg.retry_delay, cfg.backoff_factor, attempt))
            continue
        except LLMError as e:
            if not _is_retryable(e) or attempt >= max_retries:
                self.circuit_breaker.record_failure()
                raise
            await asyncio.sleep(backoff_delay(cfg.retry_delay, cfg.backoff_factor, attempt))
            continue                               # ← B2：API 级瞬时错误真实退避
        try:
            runtime.validator.validate_or_raise(result.sql)
        except (SecurityViolationError, SQLParseError) as ve:
            if attempt < max_retries:
                previous_sql, error_feedback = result.sql, str(ve)
                await asyncio.sleep(backoff_delay(cfg.retry_delay, cfg.backoff_factor, attempt))
                continue
            self.circuit_breaker.record_failure()
            raise
        ...
```

配套调整：`build_user_prompt` 反馈段的条件从 `if previous_attempt and error_feedback` 放宽为 `if error_feedback`（提取失败场景没有上一次 SQL 文本可带）。

单元测试用 `monkeypatch(asyncio.sleep)` 捕获 delay 序列断言 `≈ base*factor^attempt`（含 jitter 边界）。

### 5.3 指标埋点（B4）

注入方式：`MetricsCollector` 为既有单例，orchestrator/generator/validator/executor 构造函数增加 `metrics: MetricsCollector | None = None`（None 安全，兼容现有单测）。埋点清单：

| 指标 | 埋点位置 | labels |
|---|---|---|
| `pg_mcp_query_requests_total` (Counter) | `execute_query` 所有出口（成功 + 每个 error code） | `status`(success/error code), `database` |
| `pg_mcp_query_duration_seconds` (Histogram) | `execute_query` 全程 `time()` | — |
| `pg_mcp_sql_generation_duration_seconds` (Histogram, 新增) | 每次 `generator.generate` 前后 | `attempt` |
| `pg_mcp_sql_validation_failures_total` (Counter, 新增) | 校验失败 except 分支 | `reason`(parse/security) |
| `pg_mcp_llm_calls_total` (Counter) | generate / validate 调用出口 | `operation`(generation/validation), `status`(success/error) |
| `pg_mcp_llm_latency_seconds` (Histogram) | 同上 | `operation` |
| `pg_mcp_llm_tokens_used_total` (Counter) | 依赖 F3 的 tokens 统计（5.4） | `operation` |
| `pg_mcp_database_errors_total` (Counter, 新增) | executor `PostgresError` 分支 | `database`, `sqlstate_class`(如 `28`→权限) |
| `pg_mcp_circuit_breaker_state` (Gauge, 新增) | 熔断器状态迁移时 `set(0/1/2)` | — |
| `pg_mcp_rate_limiter_active` (Gauge, 新增) | limiter acquire/release | `type`(queries/llm) |

约定：
- 限流拒绝同样计入 `pg_mcp_query_requests_total{status="rate_limit_exceeded"}`（§5.1 查询拒绝路径与 `_llm_slot` 异常路径都要埋点）。
- `database_errors_total` 的 `database` label 由 SQLExecutor 提供 —— 其构造函数新增 `database_name` 字段（见 §4.4 接口清理）。
- 指标 label 中 **不含** question/SQL 原文（防 PII 泄漏，与日志脱敏要求一致）。
- `MetricsCollector` 增加 `@classmethod reset()` 供测试隔离（单例在 pytest 间残留会串计数）。
- README「可用指标」一节按上表重写（当前 README 列名与代码不一致，如 `pg_mcp_queries_total` 实际叫 `pg_mcp_query_requests_total`）。

### 5.4 token 统计（依赖 F3）

`SQLGenerator.generate` / `ResultValidator.validate` 返回值携带 `response.usage.total_tokens`（API 不返回 usage 时容错为 0），orchestrator 聚合后写入 `QueryResponse.tokens_used`，并 `llm_tokens_used_total.inc(tokens, operation=...)`。详细模型变更见 6.2。

### 5.5 追踪接入（B5）

最小侵入方案（不重写 orchestrator 日志调用）：

1. `server.py` 工具入口包裹：`async with request_context() as request_id:`。
2. `orchestrator.execute_query` 首行改为：`request_id = get_request_id() or str(uuid.uuid4())`——复用上游 id，不再自造。
3. `observability/logging.py` 的 JSON formatter 字段列表增加 `request_id`（record 上存在才输出）；text formatter 追加 `[req_id]` 前缀。
4. `tracing.py` 的 `trace_async` 装饰器**标记废弃**（其 `setLogRecordFactory` 全局换工厂的实现在并发下会互相覆盖，属隐患），文档注明用 request_context 替代，v0.4 删除。

---

## 6. 详细设计 F3：模型与响应缺陷修复

### 6.1 QueryResponse.to_dict 去重（C1/C7）

删除 `models/query.py:214-220` 的第二个定义（当前它实际生效，导致 `tokens_used: null` 被丢弃、server.py 又手动补 0）。保留**唯一**实现，语义定为：

```python
def to_dict(self) -> dict[str, Any]:
    """None 字段不输出；tokens_used 恒输出（None→0），保证客户端契约稳定。"""
    result = self.model_dump(exclude_none=True)
    if result.get("tokens_used") is None:
        result["tokens_used"] = 0
    return result
```

server.py 工具层的补 0 逻辑随之删除（C7）。校验结果透传（定案）：`SQLValidator` 新增 `validate_detail(sql) -> ValidationResult` —— 通过时按解析结果填充 `is_select`，失败时由 orchestrator 捕获异常构造 `ValidationResult(is_valid=False, error_message=...)` 并填入真实命中的 `uses_blocked_functions`；`_generate_sql_with_retry` 返回该对象，删除现有硬编码 `is_select=True` 的构造。

### 6.2 生成结果携带 tokens（C2）

```python
# services/sql_generator.py
@dataclass(frozen=True)
class GenerationResult:
    sql: str
    tokens_used: int          # response.usage.total_tokens，缺省 0
    model: str
    latency_ms: float

async def generate(...) -> GenerationResult:   # 签名变更，调用方仅 orchestrator
```

`ResultValidationResult` 模型新增 `tokens_used: int = 0` 字段，validate() 同样从 usage 提取。orchestrator 汇总：`tokens_used = generation.tokens_used + validation.tokens_used`。

### 6.3 配置字段逐项定案（C3/C4）

| 字段 | 决策 | 动作 |
|---|---|---|
| `security.allow_write_operations` | **删除** | 只读是本服务的硬安全约束（validator 白名单不依赖此开关），保留只会误导运维以为能开写。删字段 + 删 `.env.example:96-99` + compose 对应行 + README 表格。兼容性：settings `extra="ignore"`，残留环境变量不会导致启动失败 |
| `validation.min_confidence_score` | **删除** | 与 `confidence_threshold` 完全重复，保留后者 |
| `validation.max_question_length` | **启用** | orchestrator 入口校验 `len(request.question) > config.max_question_length` → 返回 `question_to_long`（ErrorCode 已存在，当前从未使用） |
| `cache.max_size` | **启用** | `SchemaCache.load` 写入前若 `len(self._cache) >= max_size`，驱逐 `_cache_timestamps` 最旧的条目 |
| `openai.max_tokens` 上限 | **上调** | `le=4096 → le=32768`，对齐 `.env.example`/compose 的 32000（C4：当前按示例配置启动即 ValidationError） |
| `resilience.retry_delay/backoff_factor` | **启用** | 见 5.2 |
| `security.safe_search_path/readonly_role`、`openai.timeout` 等 | 保持 | 已在用 |

### 6.4 死代码与入口修复（C5/C6/B6）

| 项 | 动作 |
|---|---|
| 根目录 `main.py` | **删除**（遗留 demo）。全仓 grep 确认无引用 |
| Dockerfile | `COPY main.py ./` 删除；`CMD ["python","main.py"]` → `CMD ["python","-m","pg_mcp"]`；健康检查改为不依赖 psutil 的存活检查：`CMD python -c "import pg_mcp" \|\| exit 1` |
| docker-compose | 健康检查 `curl -f http://localhost:9090/metrics` 依赖 curl，`python:3.14-slim` 镜像**没有 curl** → 改为 `python -c "import urllib.request; urllib.request.urlopen('http://localhost:9090/metrics')"`。另注意：stdio 型 MCP 服务在无客户端接入的容器中会因 stdin EOF 退出，compose 部署模式本身需另议（§11 Q4、§9 R7） |
| README | 快速开始 `uv run python main.py` → `uv run python -m pg_mcp`；Claude Desktop 配置同步 |
| `server.py` 模块级 `_circuit_breaker`/`_rate_limiter` 全局 | 删除（熔断器归 orchestrator 自建，限流器按 5.1 注入） |
| `orchestrator._get_current_time_ms` | 改用 `time.perf_counter()`（当前 `time.time()*1000` 受系统对时影响，测出的 duration 可能倒退） |

---

## 7. 测试设计（G6）

### 7.1 新增单元测试

| 文件 | 覆盖点 |
|---|---|
| `tests/unit/test_multi_db_config.py`（新） | DATABASES_JSON 解析/非法 JSON fail-fast/库名重复/单库兼容模式/default_database 校验；EffectivePolicy.merge 全回落路径 |
| `tests/unit/test_sql_validator.py`（增） | 表黑名单：裸名、schema.table、CTE 内引用、子查询内引用均拦截；列黑名单：`table.column` 限定匹配；EXPLAIN：禁用拒绝 / 允许时内层非法 SQL 拒绝 / `EXPLAIN ANALYZE` 前缀形式与 `EXPLAIN (ANALYZE, BUFFERS)` 括号形式**均**默认拒绝 / 开关打开放行；系统目录表：默认放行、`block_system_catalogs=true` 后拒绝 |
| `tests/unit/test_orchestrator.py`（增） | 多库路由：按参数选 runtime；default_database 回退；限流拒绝路径返回 `rate_limit_exceeded`；退避 delay 序列（mock asyncio.sleep）；共享重试预算（LLM 错误重试 n 次后校验失败不再重试）；token 聚合；question_to_long |
| `tests/unit/test_query_models.py`（增） | to_dict 唯一实现契约：None 不输出、tokens_used 恒存在 |
| `tests/unit/test_backoff.py`（新） | backoff_delay 上限 cap、jitter 边界、attempt=0 |
| `tests/unit/test_rate_limit_flow.py`（新） | 查询槽超时返回 rate_limit_exceeded；**impl 内部超时不会被误报为限流**（窄化判断回归用例）；`_llm_slot` 超时抛 RateLimitExceededError 且不消耗重试预算；LLMResponseError 走反馈重试而非纯退避 |
| `tests/unit/test_metrics.py`（新） | 各埋点在 mock 流水线中的计数/histogram 样本；reset() 隔离 |

### 7.2 集成测试（复用 fixtures 三库）

| 场景 | 断言 |
|---|---|
| 双库模式（blog_small + ecommerce_medium） | 同一 question 指定不同 database，返回各自库的数据（如各自表数不同） |
| 跨库黑名单 | ecommerce_medium 配置 `blocked_tables=["payments"]`，问"查询支付记录" → `security_violation` |
| EXPLAIN 策略 | 默认拒绝；`allow_explain=true` 时 `EXPLAIN SELECT ...` 成功；`EXPLAIN ANALYZE SELECT` 默认拒绝 |
| 限流 | `RESILIENCE_RATE_LIMIT_QUERY=2` 并发 5 请求 → ≥3 个 `rate_limit_exceeded` |
| 指标端点 | `curl :9090/metrics` 断言 `pg_mcp_query_requests_total` 计数随请求增长 |
| LLM 瞬时错误重试 | mock OpenAI 前 2 次抛 timeout，第 3 次成功 → 请求成功且产生退避 |

### 7.3 覆盖率门禁

- `sql_validator.py` / 新增 policy 模块：**≥95%**（安全模块红线，含分支覆盖）
- 整体：≥80%（沿用现有 `--cov-fail-under=80`）

---

## 8. 实施计划

| 阶段 | 内容 | 涉及文件 | 工作量 | 验收标准 |
|---|---|---|---|---|
| **P0 入口修复** | 删 main.py；Dockerfile CMD/健康检查/COPY；compose 健康检查；README 快速开始 | main.py, Dockerfile, docker-compose.yml, README | 0.5d | `docker build` 成功；`docker run -i`（保留 stdin）下手动发送 MCP initialize 握手能返回 pg-mcp 工具列表；单机 `uv run python -m pg_mcp` 日志出现 "PostgreSQL MCP Server initialization complete"。（stdio 服务在无 stdin 的容器中会立即退出，healthy 验收必须带 `-i`，见 R7） |
| **P1 模型缺陷** | to_dict 去重；GenerationResult/tokens；配置定案（6.3 全表）；perf_counter；server.py 补 0 删除 | models/query.py, sql_generator.py, result_validator.py, settings.py, schema_cache.py, server.py | 1d | 现有单测全绿 + 新契约单测通过；`.env.example` 原样复制可正常启动（C4 消除） |
| **P2 弹性可观测** | 限流注入；退避重试+错误分类；全套指标埋点；request_context 贯穿 | orchestrator.py, resilience/*, observability/*, settings.py, server.py | 2d | /metrics 计数随请求增长；并发限流实测拦截；mock LLM 超时重试成功 |
| **P3 多数据库安全** | DATABASES_JSON/EffectivePolicy/DatabaseManager/DatabaseRuntime；validator 增强（黑名单匹配+EXPLAIN+系统目录）；orchestrator 路由；多库集成测试 | config/*, db/runtime.py(新), sql_validator.py, orchestrator.py, server.py, tests | 2.5d | 双库 fixture 集成测试全绿；黑名单/EXPLAIN 用例全绿；单库模式行为与 v0.2 完全一致 |

总计约 6 人日。每阶段独立可交付、独立提交（git 规范沿用 CLAUDE.md：`feat/fix/refactor/security` 前缀）。

---

## 9. 风险与对策

| # | 风险 | 等级 | 对策 |
|---|---|---|---|
| R1 | `allow_write_operations` 删除是配置 breaking change | 低 | settings `extra="ignore"` 使残留 env 无害；CHANGELOG + README 迁移说明 |
| R2 | 限流引入新错误面，客户端可能不识别 `rate_limit_exceeded` | 中 | 错误 details 带 `retry_after_seconds`；README 故障排查补充；LLM 槽位等待 5s 可配置放大缓冲 |
| R3 | LLM 错误重试 + 退避拉长单请求尾延迟（最坏 4 次×30s 超时+退避） | 中 | 共享重试预算封顶 4 次尝试；熔断器兜底（连续失败快速失败）；`RESILIENCE_MAX_RETRIES` 可调 0 |
| R4 | DATABASES_JSON 中密码以明文环境变量存在 | 中 | 文档强制 secret 管理（沿用 README 安全章节）；后续版本可支持 `${ENV:VAR}` 引用展开（本期不做，记入 open questions） |
| R5 | tokens 字段依赖 API 返回 usage，部分兼容网关不返回 | 低 | 容错为 0，不影响主流程 |
| R6 | per-db validator 实例化与多库建池拉长启动时间 | 低 | 池并发创建（asyncio.gather）；启动失败 fail-fast 明确报哪个库 |
| R7 | stdio 型 MCP 服务与容器化部署天然冲突（无客户端 stdin 即退出，healthy 验收失效） | 中 | P0 验收改用 `docker run -i` + 手动 MCP 握手；容器长期运行方案（streamable-http transport）列为 §11 Q4，本期不实现 |

---

## 10. 附录 A：codex review 条目 → 设计章节映射

| codex review 原文 | 证据（附录 1 的问题编号） | 设计章节 | 阶段 |
|---|---|---|---|
| 多数据库未启用、单一执行器 | A1/A4 | 4.1–4.4 | P3 |
| 表/列访问限制无法强制 | A2/A3 | 4.2/4.5 | P3 |
| EXPLAIN 策略 | A2/A5 | 4.5 | P3 |
| 请求访问错误数据库 | A1 | 4.4（按库路由 + default_database） | P3 |
| 速率限制未整合 | B1 | 5.1 | P2 |
| 重试/退避未整合 | B2/B3 | 5.2 | P2 |
| 指标/追踪未整合 | B4/B5 | 5.3/5.5 | P2 |
| 重复 to_dict | C1 | 6.1 | P1 |
| 未使用的配置字段 | C3/C4 | 6.3 | P1 |
| 测试覆盖不足 | C7 及全篇 | 7 | 各阶段伴随 |

## 11. 附录 B：遗留 Open Questions（评审时定）

1. `DATABASES_JSON` 是否需要支持 `${ENV_VAR}` 间接引用密码？（建议 v0.4）
2. 多库模式下 `query` 工具的 docstring 是否需要动态注入可用库列表帮助 LLM 客户端？（建议 P3 实现时顺带）
3. `RESULT_VALIDATION` 的采样行是否需要脱敏（blocked_columns 命中列的值打码）？（建议后续版本，本期校验期拦截已足够）
4. stdio 之外的 transport（streamable-http）以支持真正的容器化/远程部署？（建议 v0.4 单独立项，P0 仅保证 stdio 正确性，见 R7）
5. `DATABASES_JSON` 环境变量在 Windows 下受环境块约 32KB 限制，库数量极多时是否改为配置文件挂载？（≤10 库无风险，暂不处理）

---

## 12. 附录 C：自查修订记录（v2）

评审自查发现并已在本文修正的问题清单：

| # | 类别 | 原设计缺陷 | 影响 | 修正位置 |
|---|---|---|---|---|
| 1 | 流程 | 查询限流用 `except TimeoutError` 包裹整条 impl，依赖"内部调用恰好都包装异常"的隐式契约 | 数据库/其他超时可能被误报为 rate_limit_exceeded | §5.1 改 acquire() 布尔窄化判断 |
| 2 | 流程 | `for_llm()` 的 TimeoutError 不被生成循环 `except LLMError` 捕获 | LLM 槽位超时冒泡成 INTERNAL_ERROR，根因被掩盖 | §5.1 `_llm_slot` 抛 RateLimitExceededError + 顶层专项 except |
| 3 | 流程 | 提取失败归入可重试 LLMError，纯退避重试无反馈 | temperature=0 下重试输出基本不变，白烧重试预算 | §5.2 新增 LLMResponseError 子类转反馈通道；build_user_prompt 条件放宽 |
| 4 | 安全 | EXPLAIN ANALYZE 仅用 startswith 检测 | `EXPLAIN (ANALYZE, ...)` 括号形式绕过开关真实执行语句 | §4.5 选项区解析 + 单测锁定两种形式 |
| 5 | 用例兼容 | 系统目录表默认一律拒绝 | 破坏 README 文档化示例（information_schema 查询） | §4.5 改 opt-in `block_system_catalogs`，默认 false |
| 6 | 验收 | P0 验收"docker run 后 healthy"不可达 | stdio 服务无 stdin 即退出，验收永不通过 | §8 改 `docker run -i` + 手动 MCP 握手；新增 R7、Q4 |
| 7 | 一致性 | DatabaseEntry.connection 复用 BaseSettings | 字段缺失时静默回读 DATABASE_*，与"忽略"规则矛盾 | §4.1 改纯 BaseModel |
| 8 | 一致性 | 接口残留未列全（orchestrator pools 参数 / executor 缺 database_name / validator 构造签名） | 实施时遗漏会产生死参数或缺 label 数据源 | §4.4 接口清理清单 |

**需求覆盖结论**：作业三项要求（多数据库与安全控制 / 弹性与可观测性整合 / 响应模型缺陷与测试覆盖）均已由 F1（§4）、F2（§5）、F3（§6）+ 测试设计（§7）覆盖，追溯映射见附录 A。

---

## 13. 附录 D：实施记录（P0–P3 完成情况与偏差）

四个阶段均已实现并分阶段提交（P0/P1：`edcd667`；P2：`f7913e0`；P3：见 git log）。以下为与设计的偏差及实现中额外发现的问题，供评审对照。

### 13.1 实现中额外发现并修复的安全问题

| # | 问题 | 处理 |
|---|---|---|
| D1 | 数据修改型 CTE 被放行：`WITH d AS (DELETE FROM users) SELECT * FROM d` 通过原校验（子查询检查只扫 `exp.Subquery`，CTE 体不在其中） | `_check_subquery_safety` 改为整棵语法树扫描 FORBIDDEN 类型，任意位置出现写操作/DDL/命令即拒绝 |
| D2 | UNION 子查询被误拒：`(SELECT 1 UNION SELECT 2)` 的内层是 `exp.Union`，不是 `exp.Select`，原"子查询必须是 SELECT"检查把合法只读 SQL 拒绝 | 内层类型判断改用 `exp.Query`（Select/Union 共同基类） |
| D3 | `DatabaseConnectionParams.name` 原设计为 `default=""` + `min_length=1`：pydantic v2 默认值不走字段校验，JSON 条目缺 `name` 会静默变成空字符串 | `name` 改为必填字段（无默认值），缺失即启动失败 |

### 13.2 与设计文本的偏差

| # | 偏差 | 理由 |
|---|---|---|
| D4 | `blocked_columns` 表限定条目（如 `users.password`）的匹配在解析后按语句内表名/别名集合解析后再比对；无歧义时精确匹配，有歧义时宁可多拦（over-blocking） | 黑名单方向的安全选择：误拒好于漏放；已用单测锁定行为（别名、JOIN、WHERE 场景） |
| D5 | 顶层 `exp.With` 特判被删除 | sqlglot 28.5 顶层 `WITH ... SELECT` 解析为 `exp.Select`（with arg），原分支不可达（死代码） |
| D6 | `db/runtime.py` 对 services 层的导入延迟到 `DatabaseManager.build()` 函数体内 | `services.orchestrator` 需要反向导入 `DatabaseRuntime`，模块级导入成环 |
| D7 | `EXPLAIN` 内层语句采用完整递归校验（`validate_or_raise(inner)`），比 §4.5 "内层需过全部规则"更进一步 | 复用同一套规则天然覆盖黑名单/目录/子查询检查，避免两套判断漂移 |

### 13.3 质量门实测值（P3 提交前）

| 指标 | 要求 | 实测 |
|---|---|---|
| 单元测试 | 全部通过 | 396 passed（P2 结束时 292） |
| sql_validator 覆盖率 | ≥ 95% | 95% |
| 总体覆盖率 | ≥ 80% | 83% |
| 新增安全模块（policy / runtime / pool / tracing） | — | 96%–100% |
| ruff check / format | 无告警 | 通过 |

### 13.4 遗留事项

- `MULTIDB_DATABASES_JSON` 环境变量在 Windows 下受环境块 32KB 限制（见 §11 R4），库数量极多时需改为配置文件挂载（v0.4 候选）
- `trace_async` / `trace_sync` 装饰器已标记 deprecated（改用 `request_context()`），计划 v0.4 移除
- server lifespan 的集成路径（真实 PostgreSQL 连接）由 `tests/integration/` 覆盖，需要真实数据库，不在单元测试门内
