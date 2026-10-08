# pg-mcp 架构及流程说明文档

> PostgreSQL MCP Server —— 基于自然语言的只读数据库查询服务
>
> 本文对应版本：v0.3（HTTP 远程传输 + 多数据库 + 弹性与可观测体系）

---

## 目录

1. [系统定位与总体架构](#1-系统定位与总体架构)
2. [模块结构](#2-模块结构)
3. [启动与生命周期](#3-启动与生命周期)
4. [请求处理全流程](#4-请求处理全流程)
5. [SQL 生成与重试机制](#5-sql-生成与重试机制)
6. [多数据库与安全策略模型](#6-多数据库与安全策略模型)
7. [安全纵深体系](#7-安全纵深体系)
8. [弹性机制](#8-弹性机制)
9. [可观测性](#9-可观测性)
10. [错误模型](#10-错误模型)
11. [配置参考](#11-配置参考)
12. [部署形态](#12-部署形态)

---

## 1. 系统定位与总体架构

### 1.1 一句话定位

pg-mcp 是一个 **MCP（Model Context Protocol）服务器**：MCP 客户端（Claude Desktop / IDE 等）通过它，用自然语言提问，服务端借助 LLM 生成 SQL、经过多重安全校验后，在 PostgreSQL 上**只读**执行，并把结构化结果连同置信度评估返回给客户端。

### 1.2 总体架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│                          MCP 客户端                                  │
│          (Claude Desktop / IDE / 任何 MCP 协议客户端)                │
└──────────────┬──────────────────────────────┬───────────────────────┘
               │ stdio (本地)                  │ streamable-HTTP (远程)
               │                              │ + Bearer Token 鉴权
═══════════════╪══════════════════════════════╪═══════════ 协议边界 ══
               ▼                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  传输层   stdio / streamable_http_app + BearerTokenMiddleware       │
│           FastMCP（官方 mcp SDK）+ TransportSecuritySettings         │
├─────────────────────────────────────────────────────────────────────┤
│  工具层   query tool（唯一入口）                                      │
│           → QueryRequest 模型 → request_context(request_id)          │
├─────────────────────────────────────────────────────────────────────┤
│  编排层   QueryOrchestrator                                          │
│           ├─ 查询限流槽（并发 10，覆盖全管线）                        │
│           ├─ 数据库路由（单库 / 多库 / default_database）             │
│           ├─ SchemaCache（TTL 缓存 + 后台刷新）                       │
│           ├─ SQL 生成重试循环（熔断 + LLM 限流槽 + 反馈通道）          │
│           └─ 结果验证（非阻塞）                                        │
├───────────────────────┬─────────────────────────────────────────────┤
│  服务层                │                                             │
│  ├─ SQLGenerator      │  AsyncOpenAI ──────────► LLM 网关 / API     │
│  │   (chat, temp=0)   │                                             │
│  ├─ ResultValidator   │  AsyncOpenAI (json_object 模式)              │
│  └─ (LLM 槽并发 5)    │                                             │
├───────────────────────┴─────────────────────────────────────────────┤
│  数据层   DatabaseManager → 每库一个 DatabaseRuntime                 │
│           ┌──────────────────────────────────────────────┐          │
│           │ DatabaseRuntime (pool + executor +            │          │
│           │   validator + EffectivePolicy)                │          │
│           │ ├─ SchemaIntrospector (集合式目录查询)         │          │
│           │ ├─ SQLValidator (SQLGlot, 按策略驱动)          │          │
│           │ └─ SQLExecutor (只读事务 + 会话参数 + 超时)     │          │
│           └──────────────┬───────────────────────────────┘          │
├───────────────────────────┼─────────────────────────────────────────┤
│  横切层   Prometheus 指标 (9090) / 结构化日志 (stderr) / request_id   │
└───────────────────────────┼─────────────────────────────────────────┘
                            ▼
                     PostgreSQL（一个或多个）
```

### 1.3 技术栈

| 层次 | 选型 | 说明 |
|------|------|------|
| MCP 协议 | 官方 `mcp` SDK 的 `FastMCP` | stdio 与 streamable-HTTP 双传输 |
| 数据库驱动 | `asyncpg` | 异步连接池、只读事务、SQLSTATE 错误 |
| SQL 解析 | `SQLGlot` | 跨库方言解析，用于白名单校验 |
| LLM 客户端 | `openai` SDK（`AsyncOpenAI`） | 兼容任意 OpenAI 协议网关（`OPENAI_BASE_URL`） |
| 配置 | `pydantic-settings` v2 | 8 个配置段，环境变量驱动，fail-fast 校验 |
| 可观测 | `prometheus_client` + stdlib logging | 14 个指标 + JSON 结构化日志 |
| 运行时 | Python 3.12+ / anyio / uvicorn | uv 管理依赖 |

---

## 2. 模块结构

```
src/pg_mcp/
├── __main__.py            # 入口：load_dotenv → 选择传输 → 启动
├── server.py              # FastMCP 实例、lifespan、query tool 定义
├── config/
│   ├── settings.py        # 8 个配置段 + 多库解析（model_post_init fail-fast）
│   └── policy.py          # EffectivePolicy：全局策略与每库覆盖的合并结果
├── db/
│   ├── pool.py            # create_pool 封装
│   ├── introspection.py   # SchemaIntrospector：集合式目录查询
│   └── runtime.py         # DatabaseRuntime + DatabaseManager（每库 bundle）
├── services/
│   ├── orchestrator.py    # QueryOrchestrator：请求编排核心（七步管线）
│   ├── sql_generator.py   # SQLGenerator：自然语言 → SQL（LLM 调用）
│   ├── sql_validator.py   # SQLValidator：SQLGlot 白名单校验
│   ├── sql_executor.py    # SQLExecutor：只读事务执行 + 序列化
│   └── result_validator.py# ResultValidator：LLM 结果置信度评估
├── models/
│   ├── schema.py          # DatabaseSchema / TableInfo / ColumnInfo 等
│   ├── query.py           # QueryRequest / QueryResponse / QueryResult
│   └── errors.py          # PgMcpError 层次 + ErrorCode
├── cache/
│   └── schema_cache.py    # SchemaCache：TTL + 后台自动刷新
├── resilience/
│   ├── circuit_breaker.py # CircuitBreaker（closed/half_open/open）
│   ├── rate_limiter.py    # MultiRateLimiter（query 槽 + llm 槽）
│   └── backoff.py         # 指数退避计算
├── observability/
│   ├── metrics.py         # MetricsCollector（单例，14 个 Prometheus 指标）
│   ├── logging.py         # 结构化日志（输出到 stderr）
│   └── tracing.py         # request_id 上下文传播
└── prompts/
    ├── sql_generation.py  # 生成 prompt（系统 + 用户模板）
    └── result_validation.py # 验证 prompt
```

分层职责与依赖方向：`server.py` → `services/orchestrator.py` →（`sql_generator` / `sql_validator` / `sql_executor` / `result_validator` / `cache` / `resilience`）→ `db` / `models` / `config`。横切的 `observability` 被各层按需引用。

---

## 3. 启动与生命周期

### 3.1 入口选择（`__main__.py`）

```text
python -m pg_mcp
  ├─ load_dotenv(.env)          # 嵌套 BaseSettings 不继承 env_file，需显式加载
  ├─ MCP_TRANSPORT=stdio（默认）
  │    └─ anyio.run(mcp.run_stdio_async)          # 本地：Claude Desktop 等
  └─ MCP_TRANSPORT=http
       ├─ mcp.streamable_http_app()               # ASGI 应用
       ├─ MCP_HTTP_TOKEN 已配置 → 挂 BearerTokenMiddleware
       └─ uvicorn.Server.serve(host, port)        # 默认 127.0.0.1:8000
```

两个部署细节：

- **DNS 重绑定防护**：官方 SDK 的 HTTP 模式默认开启 loopback DNS 重绑定防护，非 localhost 访问会收到 421。`server.py` 构造 `FastMCP` 时显式传入 `TransportSecuritySettings(enable_dns_rebinding_protection=False)`，允许绑定 `0.0.0.0` 供远程访问。
- **日志走 stderr**：stdio 模式下 stdout 是协议通道，任何日志/指标输出写 stdout 都会破坏 JSON-RPC 帧，因此日志统一输出 stderr。

### 3.2 lifespan 与初始化守卫

官方 `mcp` SDK 在 HTTP 模式下，**每个客户端会话都会进入一次 lifespan**。如果每次都重建连接池，进程内的池数量会随会话数线性增长，最终触发 PostgreSQL 的 `TooManyConnections`。为此 `server.py` 使用**进程级守卫**：

```python
_lifespan_initialized = False   # 模块级标志

async def lifespan(_app):
    global _lifespan_initialized
    if _lifespan_initialized:          # 第二个及以后的会话：
        logger.info("Reusing existing initialization")
        yield                          # 直接复用，什么都不建
        return

    _lifespan_initialized = True
    try:
        ... 全量初始化（见下）...
        yield
    finally:
        _lifespan_initialized = False  # 进程真正退出时复位
        ... 清理 ...
```

初始化内容（首个会话触发，全进程只执行一次）：

```text
lifespan
  ├─ get_settings()                    # 已由 load_dotenv 注入环境
  ├─ MetricsCollector() + start_metrics_server(9090)
  ├─ configure_logging(log_level, log_format)
  ├─ DatabaseManager.build(settings, metrics)
  │     对 settings.databases 中每个条目：
  │       EffectivePolicy.merge(全局 SecurityConfig, 每库 override)
  │       create_pool(dsn)             # 任一库连不上 → 拒绝启动（fail-fast）
  │       SQLValidator(policy) + SQLExecutor(pool, policy)
  │       → DatabaseRuntime(name, pool, executor, validator, policy)
  ├─ SchemaCache(cache_config)
  ├─ SQLGenerator(openai_config)
  ├─ ResultValidator(openai_config, validation_config)
  ├─ QueryOrchestrator(runtimes, ...)
  └─ 对每个 runtime：asyncio.create_task(_load_schema_in_background)
        # 后台预热 schema，不阻塞 initialize 握手
```

退出清理（`finally`）：取消所有 schema 后台加载任务 → 停止缓存自动刷新 → 关闭全部连接池 → 复位守卫标志。

### 3.3 Schema 预热与按需回源

Schema 内省采用**集合式目录查询**：每类元数据一条 SQL（表、视图、枚举、列、主键、外键、索引、行数估计），一次覆盖**所有**用户表，与表数量无关。

> ⚠️ 性能约束（见 `introspection.py` 模块 docstring）：**严禁**回退到逐表/逐列轮询。在宽库（实测 130 表 / 2036 列）上，串行往返从集合式的 ~1s 恶化到 ~63s，会拖垮启动并超出 MCP 客户端的 initialize 超时。

三级兜底关系：

1. **启动后台预热**：lifespan 里 `create_task` 加载，不阻塞握手；
2. **请求路径缓存命中**：`schema_cache.get(db)` 命中即零 IO；
3. **按需回源**：缓存未命中/过期时，orchestrator 用该库 runtime 的 pool 现场执行 `SchemaCache.load`（失败包装为 `SchemaLoadError`），同时后台任务按 TTL 自动刷新。

---

## 4. 请求处理全流程

### 4.1 端到端时序

```text
MCP 客户端          query tool        Orchestrator            组件
    │   query(question, database?,      │                     │
    │          return_type)             │                     │
    │ ─────────────────────────────►    │                     │
    │                        校验 return_type ∈ {sql, result} │
    │                        QueryRequest 模型化               │
    │                        request_context(request_id)      │
    │                                  │ ── execute_query ──►│
    │                                  │  ① 查询限流槽 acquire（并发 10）
    │                                  │     超时 5s → RATE_LIMIT_EXCEEDED（直接返回）
    │                                  │  ② Step 0 问题长度检查
    │                                  │     > VALIDATION_MAX_QUESTION_LENGTH → 报错返回
    │                                  │  ③ Step 1 数据库路由 _resolve_database
    │                                  │     显式指定→须存在；单库→自动选；
    │                                  │     多库→default_database；无默认→报错并列出可用库
    │                                  │  ④ Step 2 schema_cache.get
    │                                  │     未命中 → SchemaCache.load（回源内省）
    │                                  │  ⑤ Step 3 _generate_sql_with_retry ──► 见 §5
    │                                  │  ⑥ Step 4 return_type==sql？
    │                                  │     是 → 提前返回（confidence=100）
    │                                  │  ⑦ Step 5 executor.execute ──────► PostgreSQL
    │                                  │     只读事务 + 会话参数 + 超时 + max_rows
    │                                  │  ⑧ Step 6 _validate_results_safely ─► LLM（可选）
    │                                  │     非阻塞：任何失败 → confidence=100 继续
    │                                  │  ⑨ Step 7 组装 QueryResponse
    │                                  │  finally: 记 query_requests / query_duration
    │ ◄───────────────────────────────  │
    │   to_dict()（成功或结构化错误）    │
```

### 4.2 关键设计点

**查询限流槽的窄契约**。`execute_query` 中限流判断使用 `acquire()` 返回 bool 的窄契约，而不是宽 `except TimeoutError`：管线内部（`asyncio.wait_for`、executor）本身会抛 `TimeoutError`，宽捕获会把真实的执行超时误报成限流拒绝。

**限流的作用域**。query 槽（默认 10 并发）**包裹整条管线**——从数据库路由到结果验证，槽在手即占用一个查询并发额度；llm 槽（默认 5 并发）只包裹对 LLM 的调用（生成与结果验证两处），两个槽独立计数。

**request_id 贯穿**。`server.py` 的 query tool 通过 `request_context(request_id)` 从 MCP SDK 拿到上游请求 id 注入上下文；orchestrator 优先复用它（无则生成 uuid4），此后每条日志、每个指标都携带同一 request_id，可与 MCP 客户端侧日志对账。

**指标标签无 PII**。`query_requests` 的标签只有 `status`（错误码或 `success`）与 `database`（库名或 `-`），不含问题文本、SQL、结果内容。

---

## 5. SQL 生成与重试机制

`_generate_sql_with_retry` 是弹性设计的核心，位于 [orchestrator.py](src/pg_mcp/services/orchestrator.py)。

### 5.1 重试循环结构

```text
进入前：熔断器检查（open → 直接拒绝，half_open → 放行试探）
循环：最多 max_retries + 1 次 LLM 调用（默认 3+1=4 次预算）
  │
  ├─ async with llm 槽（并发 5，acquire 超时 5s）:
  │     sql_generator.generate(question, schema,
  │                             previous_attempt?,  # 反馈通道
  │                             error_feedback?)    # 反馈通道
  │
  ├─ validator.validate_or_raise(sql)   # 在目标库的 EffectivePolicy 下
  │
  └─ 成功 → circuit_breaker.record_success()
            + 构建 SQLValidationResult → 返回
```

### 5.2 两类失败、两条通道

重试预算（`max_retries + 1` 次 LLM 调用）被两类失败**共享**，但走不同的重试通道：

| 失败类别 | 典型异常 | 重试通道 | 理由 |
|----------|----------|----------|------|
| **瞬时 API 故障** | 网络抖动、网关 5xx（`LLMError`, retryable=True） | **指数退避**（`retry_delay` 起步，`backoff_factor` 倍增）后原样重试 | 同一请求稍后重发大概率恢复 |
| **内容级缺陷** | 空响应/无法提取 SQL（`LLMResponseError`）；SQL 被安全校验拒绝（`SecurityViolationError` / `SQLParseError`） | **反馈通道**：把 `previous_attempt` + `error_feedback` 注入下一次 prompt | 生成温度为 0，**盲重试会逐字复现同样的错误输出**，必须让模型"看到"上一次错在哪 |
| **不可重试** | 认证失败（`LLMUnavailableError`, retryable=False） | **fail-fast**，立即抛出 | 重试无意义，且大概率是配置问题 |
| **限流** | `RateLimitExceededError`（llm 槽 acquire 超时） | **直接 raise，不耗预算** | 不是 LLM 的问题，不该由重试预算买单 |
| **意外异常** | 未归类错误 | 记入熔断器并包装为 `LLMError` | 保守处理，纳入熔断统计 |

### 5.3 反馈通道的意义

`sql_generator.generate` 的签名里有 `previous_attempt` 与 `error_feedback` 两个可选参数。重试时 orchestrator 把上一次的 SQL 和失败原因（例如 `relation "user" does not exist`）传入，prompt 模板将其渲染为"你上一次生成了 X，报错 Y，请修正"。这等价于一个**受控的 Agent 自我纠错循环**——用错误信息引导模型修正，而不是期望随机性带来不同结果。

### 5.4 SQL 提取的鲁棒性

`SQLGenerator._extract_sql` 按优先级四策略提取 SQL：` ```sql ` 代码块 → 泛化代码块 → 文本中的 SELECT/WITH 语句 → 整段就是 SQL。所有策略统一规范化结尾分号。提取失败抛 `LLMResponseError` 走反馈通道。

---

## 6. 多数据库与安全策略模型

### 6.1 DatabaseRuntime：每库一个 bundle

```python
@dataclass
class DatabaseRuntime:
    name: str            # 库的显示名（请求里的 database 参数）
    pool: Pool           # asyncpg 连接池
    executor: SQLExecutor
    validator: SQLValidator
    policy: EffectivePolicy
```

**策略跟随数据库**是核心不变量：`SQLValidator` 与 `SQLExecutor` 都在构造时绑定该库的 `EffectivePolicy`。请求路由到哪个 runtime，就用哪个库的策略校验和执行——**不存在**"换个库就能绕过某库黑名单"的路径。

`DatabaseManager.build` 遍历 `settings.databases`，逐库 `EffectivePolicy.merge(全局, 每库override)` → `create_pool` → 构造 validator/executor。任一库连不上则整个启动失败（fail-fast），避免"半可用"状态。

### 6.2 EffectivePolicy 合并语义

[config/policy.py](src/pg_mcp/config/policy.py)，`@dataclass(frozen=True)`，构造后不可变：

| 字段 | 可否每库覆盖 | 说明 |
|------|:---:|------|
| `blocked_tables` / `blocked_columns` | ✅ | 覆盖式（None → 回退全局）；支持裸名（`password`）与限定名（`users.password`） |
| `allow_explain` / `allow_explain_analyze` | ✅ | EXPLAIN 放行开关，可按库收紧/放宽 |
| `readonly_role` / `safe_search_path` | ✅ | 执行会话的角色与 search_path |
| `blocked_functions` | ❌ 全局专属 | 函数黑名单不允许任何库放宽 |
| `max_rows` / `max_execution_time` | ❌ 全局专属 | 资源上限不允许任何库放宽 |

### 6.3 数据库路由（`_resolve_database`）

```text
请求显式指定 database?
 ├─ 是 → 必须命中已配置的库，否则 DATABASE_NOT_FOUND（报错列出可用库）
 └─ 否 → 只配了一个库？ → 自动选它
         └─ 多库 → settings.multidb.default_database 有值？ → 用默认库
                   └─ 无默认 → 报错，响应中列出所有可用库名
```

### 6.4 多库配置解析（`Settings.model_post_init`）

- `MULTIDB_DATABASES_JSON` 未设置 → **单库模式**：`DATABASE_*` 变量成为唯一条目，行为与单库版本完全一致；
- 已设置 → 解析 JSON 数组为 `DatabaseEntry` 列表：**重名报错**、`DATABASE_*` 被忽略并记 warning、`MULTIDB_DEFAULT_DATABASE` 必须命中列表内条目；
- 任何不一致（JSON 非法、条目缺 `name`、默认库不存在）都在 `model_post_init` 抛错 → **启动即失败**。

一个刻意的类型细节：`DatabaseConnectionParams` 用**普通 `BaseModel`** 而非 `BaseSettings`。嵌套 BaseSettings 在 JSON 条目缺字段时会静默回读 `DATABASE_*` 环境变量，导致两种配置模式互相污染；普通 BaseModel 强制 JSON 条目自包含。

---

## 7. 安全纵深体系

只读是**硬约束**，全链路共六层防线，任何一层被绕过都有下一层兜底：

```text
① 传输鉴权    HTTP 模式：BearerTokenMiddleware
                Authorization: Bearer <token> 头，或 ?access_token=<token> 查询参数
                （兼容无法自定义请求头的 MCP 客户端）
                hmac.compare_digest 恒定时间比较 → 401 + WWW-Authenticate
                stdio 模式天然本地信任，无鉴权层

② 输入校验    QueryRequest 模型：question 非空且 ≤ max_question_length（默认 10000）
                return_type ∈ {sql, result} 白名单
                （server.py 层先于 orchestrator 校验）

③ 静态 SQL 校验  SQLValidator（SQLGlot 解析，在 EffectivePolicy 驱动下）：
                - 白名单语句类型：Select / Union / Intersect / Except
                  顶层另允许 With / Subquery
                - 黑名单语句：Insert / Update / Delete / Drop 等一律拒绝
                - 函数黑名单（全局不可放宽）：pg_sleep、pg_read_file、
                  pg_write_file、lo_import、lo_export …
                - 表/列黑名单（每库可覆盖）：裸名或 schema.table 限定名
                - EXPLAIN：默认拒绝；allow_explain 放行纯计划；
                  allow_explain_analyze 才放行 ANALYZE（会真实执行语句）
                  兼容 "EXPLAIN (ANALYZE, BUFFERS) …" 前缀与括号选项两种形式
                - pg_catalog / information_schema 引用可按需封锁
                  （block_system_catalogs，默认关闭）
                校验器无状态，同一策略对相同 SQL 结果确定

④ 只读事务    SQLExecutor：asyncpg connection.transaction(readonly=True)
                即使恶意 SQL 逃过了静态校验，数据库层面也会拒绝写入

⑤ 会话参数加固  每次执行前 SET：
                statement_timeout = max_execution_time（默认 30s）
                search_path = safe_search_path（默认 public，防 schema 劫持）
                readonly_role 有值时 SET ROLE 到只读角色

⑥ 资源限制    asyncio.wait_for 执行超时兜底
                max_rows（默认 10000）结果截断，total_count 保留真实总数
                连接池上限（max_pool_size，默认 20/库）
                限流器并发上限（query 10 / llm 5）
```

补充：日志与指标全程脱敏——`safe_dsn` 属性把密码打码为 `***`；API key 存 `SecretStr`；指标标签不含问题文本与 SQL 内容。

---

## 8. 弹性机制

### 8.1 熔断器（CircuitBreaker）

保护对象是 **LLM 依赖**（生成 + 结果验证），状态机：

```text
        失败连续达 threshold（默认 5 次）
closed ─────────────────────────────────► open
   ▲                                        │ 冷却 recovery_timeout（默认 60s）
   │ 半开试探成功                            ▼
   └───────────────────── half_open ◄──── 放行单个试探请求
```

- `open` 状态下新的生成请求**直接快速失败**，不再消耗 LLM 网关配额与用户等待时间；
- `half_open` 放行一个试探请求：成功 → 恢复 `closed` 并清零计数；失败 → 回到 `open` 重新计时；
- 状态变化同步到 `pg_mcp_circuit_breaker_state` 指标（0=closed, 1=half_open, 2=open）。

### 8.2 双通道限流（MultiRateLimiter）

| 槽 | 默认并发 | 覆盖范围 | acquire 超时 |
|----|:---:|----------|:---:|
| query | 10 | **整条查询管线**（路由→生成→执行→验证） | 5s |
| llm | 5 | 对 LLM 的调用点（SQL 生成、结果验证） | 5s |

语义要点：

- 超时**不排队挂死**：等不到槽就返回结构化 `RATE_LIMIT_EXCEEDED`（含 `retry_after_seconds` 提示），客户端可择机重试；
- query 槽的判断用 bool 契约而非宽 `except TimeoutError`（原因见 §4.2）；
- llm 槽用 `asynccontextmanager` 封装（`_llm_slot`），槽超时抛 `RateLimitExceededError`，避免裸 `TimeoutError` 泄漏到上层被误分类；
- 活跃槽位数实时发布到 `pg_mcp_rate_limiter_active` 指标。

### 8.3 指数退避

瞬时类失败的重试间隔：`retry_delay × backoff_factor^n`（默认 1s 起步、2 倍增：1s → 2s → 4s）。与反馈通道重试（内容类失败，立即带反馈重试，不等待）互不干扰，共享同一个总预算。

### 8.4 结果验证的非阻塞原则

Step 6 的结果置信度评估是**尽力而为**的增值服务：`validation.enabled=False` → 直接 100 分；LLM 调用失败、超时、被限流、返回非法 JSON → 记 warning 日志后返回 100 分。**验证组件的任何故障都不影响已成功执行的查询结果返回**。唯一的副作用是响应里 `validation.explanation` 会说明验证未完成。

---

## 9. 可观测性

### 9.1 Prometheus 指标（默认端口 9090，`/metrics`）

共 14 个指标，按域分组：

**查询域**

| 指标 | 类型 | 标签 | 含义 |
|------|------|------|------|
| `pg_mcp_query_requests_total` | Counter | status, database | 请求总数；status 为错误码或 success |
| `pg_mcp_query_duration_seconds` | Histogram | – | 全管线耗时（含生成+执行） |

**LLM 域**

| 指标 | 类型 | 标签 | 含义 |
|------|------|------|------|
| `pg_mcp_llm_calls_total` | Counter | operation, status | 生成/验证两类调用的成败计数 |
| `pg_mcp_llm_latency_seconds` | Histogram | operation | LLM 调用延迟 |
| `pg_mcp_llm_tokens_used` | Counter | operation | token 消耗（含验证调用） |

**安全域**

| 指标 | 类型 | 标签 | 含义 |
|------|------|------|------|
| `pg_mcp_sql_rejected_total` | Counter | reason | 静态校验拒绝数（ddl_detected、blocked_function…） |

**弹性域**

| 指标 | 类型 | 标签 | 含义 |
|------|------|------|------|
| `pg_mcp_sql_generation_duration_seconds` | Histogram | attempt | 重试循环内单次生成耗时（attempt=第几次） |
| `pg_mcp_sql_validation_failures_total` | Counter | reason | 重试循环中校验失败次数 |
| `pg_mcp_database_errors_total` | Counter | database, sqlstate_class | 数据库错误按 SQLSTATE 前两位分类 |
| `pg_mcp_circuit_breaker_state` | Gauge | – | 熔断器状态（0/1/2） |
| `pg_mcp_rate_limiter_active` | Gauge | type | 当前持有的限流槽数 |

**数据库与缓存域**

| 指标 | 类型 | 标签 | 含义 |
|------|------|------|------|
| `pg_mcp_db_connections_active` | Gauge | database | 活跃连接数 |
| `pg_mcp_db_query_duration_seconds` | Histogram | – | 纯 SQL 执行耗时 |
| `pg_mcp_schema_cache_age_seconds` | Gauge | database | schema 缓存年龄 |

### 9.2 结构化日志与 request_id

- 日志输出到 **stderr**（stdout 在 stdio 模式是协议通道）；
- 格式可选 `json`（默认，机器可解析）或 `text`；
- `request_id` 从 `request_context` 注入每条日志的 `extra` 字段，与 MCP 客户端侧可对账；
- 问题文本只记前 100 字符（`question[:100]`），SQL 只记长度不记内容，杜绝 PII 入日志。

---

## 10. 错误模型

[models/errors.py](src/pg_mcp/models/errors.py) 定义统一异常层次，全部继承 `PgMcpError` 并携带 `ErrorCode`：

```text
PgMcpError
├─ QuestionTooLongError        # Step 0 输入检查
├─ DatabaseNotFoundError       # Step 1 路由失败
├─ SchemaLoadError             # Step 2 回源内省失败
├─ LLMError                    # 生成调用失败（retryable 区分退避/放弃）
│  ├─ LLMTimeoutError
│  ├─ LLMUnavailableError      # 认证失败等，retryable=False → fail-fast
│  └─ LLMResponseError         # 内容缺陷 → 反馈通道
├─ SecurityViolationError      # 静态校验拒绝 → 反馈通道
├─ SQLParseError               # SQLGlot 解析失败 → 反馈通道
├─ QueryExecutionError         # 执行期数据库错误（SQLSTATE 分类）
└─ RateLimitExceededError      # 槽超时（不耗重试预算）
```

响应侧约定：**已知错误**（`PgMcpError` 子类）→ 结构化 `QueryResponse`，`error.code` 为对应 `ErrorCode`，`details` 带上下文（长度、库名、超时值等）；**未知异常** → `INTERNAL_ERROR`，详情不外泄（记入日志），避免内部实现泄露。

---

## 11. 配置参考

### 11.1 八个配置段（环境变量前缀 → 配置类）

| 前缀 | 配置类 | 关键项（默认值） |
|------|--------|------------------|
| `DATABASE_` | `DatabaseConfig` | host(localhost) / port(5432) / name(postgres) / user / password / min_pool_size(5) / max_pool_size(20) / pool_timeout(30) / command_timeout(30) |
| `OPENAI_` | `OpenAIConfig` | api_key（必填，须 `sk-` 开头）/ base_url（None=官方端点；可指向内部网关）/ model(gpt-4o-mini) / max_tokens(2000) / temperature(0.0) / timeout(30) |
| `SECURITY_` | `SecurityConfig` | blocked_functions(pg_sleep 等) / blocked_tables([]) / blocked_columns([]) / allow_explain(False) / allow_explain_analyze(False) / block_system_catalogs(False) / max_rows(10000) / max_execution_time(30) / readonly_role(None) / safe_search_path(public) |
| `VALIDATION_` | `ValidationConfig` | max_question_length(10000) / enabled(True) / sample_rows(5) / timeout_seconds(10) / confidence_threshold(70) |
| `CACHE_` | `CacheConfig` | schema_ttl(3600) / max_size(100) / enabled(True) |
| `RESILIENCE_` | `ResilienceConfig` | max_retries(3) / retry_delay(1.0) / backoff_factor(2.0) / circuit_breaker_threshold(5) / circuit_breaker_timeout(60) / rate_limit_query(10) / rate_limit_llm(5) / rate_limit_acquire_timeout(5.0) |
| `OBSERVABILITY_` | `ObservabilityConfig` | metrics_enabled(True) / metrics_port(9090) / log_level(INFO) / log_format(json) |
| `MULTIDB_` | `MultiDatabaseConfig` | databases_json(None) / default_database(None) |

另有传输相关变量（`__main__.py` 消费）：`MCP_TRANSPORT`(stdio|http)、`MCP_HTTP_HOST`(127.0.0.1)、`MCP_HTTP_PORT`(8000)、`MCP_HTTP_TOKEN`（HTTP 鉴权令牌，不设则不挂鉴权中间件）。

### 11.2 配置加载机制的两个细节

1. **嵌套 BaseSettings 不继承 env_file**：各配置段只认自己的 env_prefix，`.env` 文件由 `__main__.py` 显式 `load_dotenv` 注入环境（路径锚定在包根上一级的 `.env`）。这也是 `DatabaseConnectionParams` 刻意用普通 BaseModel 的同一逻辑（见 §6.4）。
2. **列表变量兼容两种格式**：`SECURITY_BLOCKED_FUNCTIONS` 既可写 JSON 数组也可写逗号分隔字符串（`NoDecode` + before 校验器），与 `.env.example` 的文档格式一致。

### 11.3 多库配置示例

```json
MULTIDB_DATABASES_JSON='[
  {"connection": {"name": "oltp", "host": "db1.internal", "port": 5432}},
  {"connection": {"name": "analytics", "host": "db2.internal"},
   "security": {"blocked_tables": ["secrets", "audit.logs"]}}
]'
MULTIDB_DEFAULT_DATABASE=oltp
```

每个条目：`connection` 必填（`name` 缺失即校验失败），`security` 可选（字段级覆盖，None 回退全局）。

---

## 12. 部署形态

| 形态 | 传输 | 适用场景 | 要点 |
|------|------|----------|------|
| **本地桌面** | stdio | Claude Desktop / 本地 IDE | `MCP_TRANSPORT=stdio`（默认）；stdout 为协议通道，日志走 stderr；无鉴权（本地信任） |
| **远程服务** | streamable-HTTP | 团队共享、服务器部署 | `MCP_TRANSPORT=http` + `MCP_HTTP_HOST=0.0.0.0`；务必配置 `MCP_HTTP_TOKEN`（Bearer/查询参数双通道鉴权）；DNS 重绑定防护已显式关闭以允许非 loopback 绑定 |
| **容器** | HTTP（docker-compose） | 云原生部署 | 镜像内置依赖；compose 注入环境变量/密钥；健康检查可打 metrics 端口 |

## 附：一次典型请求的生命周期（缩略版）

> 用户问："每个部门的员工数是多少？"

1. MCP 客户端调用 `query(question="每个部门的员工数是多少？", return_type="result")`；
2. HTTP 中间件校验 Bearer token（恒定时间比较）；
3. query tool 校验入参，生成/继承 `request_id`；
4. orchestrator 拿查询槽（第 1/10 并发）；
5. 路由到默认库，schema 缓存命中（启动时已后台预热）；
6. llm 槽内调用 LLM（温度 0）：`SELECT d.name, COUNT(*) FROM departments d JOIN employees e ON e.dept_id = d.id GROUP BY d.name;`
7. SQLValidator 解析：Select ✓、无黑名单函数/表/列 ✓；
8. 只读事务执行，`statement_timeout=30s`、`search_path=public` 已 SET，结果 ≤ max_rows；
9. ResultValidator 抽 5 行样本问 LLM："结果是否回答了问题？" → confidence 85 ≥ 70 ✓（此步失败也不影响返回）；
10. 组装 `QueryResponse`（SQL、列名、行数据、行数、耗时、置信度、token 数）返回；
11. 指标落盘：`query_requests{status="success"}` +1，`query_duration` 记录耗时，全链路日志以同一 `request_id` 串联。

---

*本文档基于源码实测撰写；各组件的精确行为以对应模块 docstring 与代码为准。*
