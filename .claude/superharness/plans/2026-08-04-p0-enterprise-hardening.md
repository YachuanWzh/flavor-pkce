# P0 企业级加固实施计划

> **For agentic workers:** Execute this plan task-by-task under the superharness:go workflow, Phase 2 (strict TDD per task). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成差距分析 P0 全部 12 项（安全漏洞 10 项 + 运维 2 项），使 flavor-pkce 可安全上线。

**Architecture:** auth-server（FastAPI + SQLite）负责身份/授权/令牌生命周期；api-gateway（FastAPI + SQLite）负责代理/审计/限流出口。单机 Docker Compose 部署。所有会话/授权状态、限流计数、审计日志落 SQLite；令牌撤销通过 auth 内部接口供 gateway 检查。

**Tech Stack:** Python 3.10+ / FastAPI / SQLite / pytest / Docker Compose / Caddy(TLS)

**约束确认**（来自 2026-08-04 gap-analysis）：内部单组织、自建账号（预留 SSO）、内部审计要求、单机 Compose、MVP 先行。

---

## 关键设计决策（贯穿全部任务）

- **D1 会话持久化**：新增 `sessions` 与 `pending_auths` 表；`_sessions`/`_pending_auth` 内存 dict 全部改为 DB 访问。
- **D2 redirect_uri**：注册值支持两种形态——精确 URI，或 `scheme://host:*`（仅端口通配，供 CLI 随机端口回调）；校验时精确匹配，不再 `startswith`。
- **D3 限流**：SQLite 表 `rate_limits(key, window_start, hits)` 滑动窗口 + `login_failures(key, failures, locked_until)` 失败锁定；纯函数 `is_rate_limited()` / `record_failure()` 可单测。
- **D4 安全头**：统一响应中间件（HSTS/CSP/X-Frame-Options/X-Content-Type-Options/Referrer-Policy）；`COOKIE_SECURE` 配置（生产 compose 设 true）。
- **D5 默认弱配置**：`AUTH_SECRET_KEY`/`INTERNAL_SERVICE_TOKEN` 为默认值时除非 `ALLOW_INSECURE_DEFAULTS=true` 否则启动 fail-fast；`SEED_TEST_USER=true` 才播种 testuser（默认关闭）。
- **D6 Refresh/撤销**：access 仍为 JWT；refresh 为不透明随机串，`tokens` 表存 `sha256` 哈希 + `token_type` 列；`POST /refresh`（轮换）+ `POST /revoke`（RFC 7009）；gateway 通过 `GET /internal/tokens/revoked?jti=...` 检查撤销（短 TTL 缓存）。
- **D7 auth 审计**：`audit_logs` 表（auth 侧）记录注册/登录/授权/换码/刷新/撤销事件（actor/ip/ua/status）；`AUDIT_RETENTION_DAYS`（默认 180）+ 启动清理。
- **D8 网关审计鉴权**：`AUDIT_API_TOKEN`（未设置则审计 API 一律 401）；`audit_logs` 加 `prev_hash`/`hash` 哈希链，`verify_integrity()` 校验。
- **D9 SSRF**：`validate_upstream_url()` 拒绝私网/环回/链路本地/云元数据地址段（含 DNS 解析），代理转发前强制校验。
- **D10 备份**：`scripts/backup.py` 用 sqlite3 backup API 在线备份 + `scripts/restore.py`；备份文件带时间戳。

---

### Task 1: 会话/授权状态持久化（P0-2）

**Files:**
- Modify: `auth-server/auth_server/database.py`（建表 sessions/pending_auths + CRUD helpers）
- Modify: `auth-server/auth_server/main.py`（get_current_user/require_current_user/authorize/consent/register/login 改用 DB）
- Test: `auth-server/tests/test_sessions.py`（新）

- [ ] Step 1: 写失败测试 `tests/test_sessions.py`（sessions 建表、login 落库、重启后 get_current_user 仍有效、consent 的 pending_auth 落库）
- [ ] Step 2: 运行确认 RED
- [ ] Step 3: database.py 建表 + helpers；main.py 替换内存 dict
- [ ] Step 4: 运行确认 GREEN（含全部 auth 测试）
- [ ] Step 5: commit

### Task 2: redirect_uri 精确匹配（P0-5）

**Files:**
- Modify: `auth-server/auth_server/main.py`（validate_redirect_uri 精确匹配 + 端口通配符）
- Modify: `auth-server/auth_server/database.py`（seed 客户端 redirect_uris 保持 `http://127.0.0.1:*` 格式）
- Test: `auth-server/tests/test_authorize.py`（新增用例）

- [ ] Step 1: 写失败测试（`http://127.0.0.1:9999/callback` 通过；`http://127.0.0.1.evil.com/callback` 拒绝；未注册精确 URI 拒绝）
- [ ] Step 2: RED
- [ ] Step 3: 实现精确匹配
- [ ] Step 4: GREEN + commit

### Task 3: 强密码策略（P0-9）

**Files:**
- Modify: `auth-server/auth_server/main.py`（RegisterRequest 校验：min 8，含大小写/数字；注册 400）
- Modify: `auth-server/auth_server/config.py`（`PASSWORD_MIN_LENGTH` 等可配置）
- Test: `auth-server/tests/test_auth.py`（新增用例）

- [ ] Step 1: 写失败测试（弱密码 400；合规密码 201）
- [ ] Step 2: RED
- [ ] Step 3: 实现校验（注意现有测试密码 `secret123`/`mypassword` 等需满足策略）
- [ ] Step 4: GREEN（全量 auth 测试）+ commit

### Task 4: 登录/注册/token 限流 + 失败锁定（P0-4）

**Files:**
- Modify: `auth-server/auth_server/database.py`（rate_limits/login_failures 表）
- Modify: `auth-server/auth_server/ratelimit.py`（新模块，纯函数）
- Modify: `auth-server/auth_server/main.py`（login/register/token 接入）
- Test: `auth-server/tests/test_ratelimit.py`（新）

- [ ] Step 1: 写失败测试（N 次失败后 429/423；窗口内超阈值 429；成功后解锁）
- [ ] Step 2: RED
- [ ] Step 3: 实现
- [ ] Step 4: GREEN + commit

### Task 5: 安全响应头 + secure cookie（P0-6）

**Files:**
- Modify: `auth-server/auth_server/main.py`（中间件加头；set_cookie secure=COOKIE_SECURE）
- Modify: `auth-server/auth_server/config.py`（COOKIE_SECURE/HSTS 配置）
- Test: `auth-server/tests/test_security_headers.py`（新）

- [ ] Step 1: 写失败测试（响应含 X-Frame-Options/CSP/X-Content-Type-Options；cookie 带 Secure when COOKIE_SECURE）
- [ ] Step 2: RED
- [ ] Step 3: 实现
- [ ] Step 4: GREEN + commit

### Task 6: 默认密钥/种子账号清理（P0-8）

**Files:**
- Modify: `auth-server/auth_server/config.py`（fail-fast 校验函数 + SEED_TEST_USER/ALLOW_INSECURE_DEFAULTS）
- Modify: `auth-server/auth_server/main.py`（lifespan 调用校验）
- Modify: `auth-server/auth_server/database.py`（testuser 播种受 SEED_TEST_USER 控制）
- Modify: `auth-server/tests/test_auth.py` + `tests/test_e2e.py`（fixture 开启 SEED_TEST_USER 或改用注册用户）
- Test: `auth-server/tests/test_config.py`（新）

- [ ] Step 1: 写失败测试（默认密钥配置 → 校验抛错；SEED_TEST_USER=false 不播种 testuser；true 播种）
- [ ] Step 2: RED
- [ ] Step 3: 实现 + 更新依赖 seed 的旧测试
- [ ] Step 4: GREEN（全量）+ commit

### Task 7: Refresh token + RFC7009 撤销 + 网关撤销检查（P0-3）

**Files:**
- Modify: `auth-server/auth_server/database.py`（tokens 表加 token_type/refresh_token_hash 列迁移）
- Modify: `auth-server/auth_server/main.py`（/token 返回 refresh_token；/refresh；/revoke；/internal/tokens/revoked）
- Modify: `api-gateway/gateway/main.py`（proxy 前查撤销，短缓存）
- Modify: `api-gateway/gateway/config.py`（REVOCATION_CACHE_TTL）
- Test: `auth-server/tests/test_refresh.py`（新）+ `api-gateway/tests/test_revocation.py`（新）

- [ ] Step 1: 写失败测试（token 响应含 refresh_token；refresh 轮换；旧 refresh 复用拒绝；revoke 后 refresh 失效；gateway 拒已撤销 jti）
- [ ] Step 2: RED
- [ ] Step 3: 实现 auth 侧 + gateway 侧
- [ ] Step 4: GREEN + commit

### Task 8: auth 全事件审计 + 留存策略（P0-10）

**Files:**
- Modify: `auth-server/auth_server/database.py`（audit_logs 表 + insert/query/purge）
- Modify: `auth-server/auth_server/audit.py`（新模块：audit_log helper 取 ip/ua）
- Modify: `auth-server/auth_server/main.py`（各端点接入审计）
- Modify: `auth-server/auth_server/config.py`（AUDIT_RETENTION_DAYS）
- Test: `auth-server/tests/test_audit.py`（新）

- [ ] Step 1: 写失败测试（login 成功/失败、register、token 交换均落审计；purge 删除过期）
- [ ] Step 2: RED
- [ ] Step 3: 实现
- [ ] Step 4: GREEN + commit

### Task 9: 网关审计 API 鉴权 + 哈希链防篡改（P0-1）

**Files:**
- Modify: `api-gateway/gateway/config.py`（AUDIT_API_TOKEN）
- Modify: `api-gateway/gateway/database.py`（prev_hash/hash 列 + insert_log 链 + verify_integrity）
- Modify: `api-gateway/gateway/main.py`（/api/logs* 鉴权 + /api/logs/integrity）
- Test: `api-gateway/tests/test_audit.py`（更新 API 测试带 token）+ `tests/test_integrity.py`（新）

- [ ] Step 1: 写失败测试（无 token 401；带 token 200；篡改行后 verify_integrity 失败）
- [ ] Step 2: RED
- [ ] Step 3: 实现 + 更新旧测试
- [ ] Step 4: GREEN + commit

### Task 10: 网关出站 SSRF 防护（P0-7）

**Files:**
- Modify: `api-gateway/gateway/ssrf.py`（新模块：validate_upstream_url）
- Modify: `api-gateway/gateway/main.py`（proxy 前校验；routing 的 upstream_url 也校验）
- Test: `api-gateway/tests/test_ssrf.py`（新）

- [ ] Step 1: 写失败测试（127.0.0.1/10.0.0.0/169.254.169.254/::1 拒绝；公网通过）
- [ ] Step 2: RED
- [ ] Step 3: 实现
- [ ] Step 4: GREEN + commit

### Task 11: 数据库备份/恢复（P0-11）

**Files:**
- Create: `scripts/backup.py`、`scripts/restore.py`
- Create: `docs/ops/backup-restore.md`
- Test: `scripts/test_backup.py`（或 scripts/tests/）

- [ ] Step 1: 写失败测试（备份生成可打开的 db；恢复后数据一致）
- [ ] Step 2: RED
- [ ] Step 3: 实现 backup/restore + 文档
- [ ] Step 4: GREEN + commit

### Task 12: 生产 docker-compose（P0-12）

**Files:**
- Create: `docker-compose.yml`（auth + gateway + caddy TLS 终结）
- Create: `deploy/README.md`、`.env.prod.example`、`Caddyfile`
- 配置类任务（TDD 例外），以 compose config 验证 + 文档为准

- [ ] Step 1: 编写 compose + Caddyfile + env 模板
- [ ] Step 2: `docker compose config` 校验通过
- [ ] Step 3: commit

---

## 收尾

- [ ] 全量测试：auth-server `python -m pytest -q` + api-gateway `python -m pytest -q` 全绿
- [ ] 代码评审（requesting-code-review）
- [ ] 合并回 main、更新差距分析文档状态
