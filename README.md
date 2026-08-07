# flavor-pkce

基于 **PKCE (RFC 7636) OAuth2 授权服务器 + LLM API 网关** 的双层架构,核心目标是:**让真正的 LLM API Key(如 OpenAI、DeepSeek)永不离开你的服务器**。

客户端(如 flavor-code CLI)只与授权服务器完成 PKCE 登录,拿到 JWT 后把请求发给 API 网关;网关负责校验 JWT、把 `Authorization` 头替换为真实的上游 API Key,再代理到 LLM 提供商。即使客户端被完全逆向,也拿不到任何上游凭据。

```
┌──────────────────┐  PKCE 登录/换 JWT  ┌──────────────────┐
│  客户端           │ ──────────────────▶ │  auth-server     │  :8091
│  (flavor-code 等) │ ◀────────────────── │  (授权服务器)     │
└────────┬─────────┘                      └──────────────────┘
         │  Bearer <JWT> 请求
         ▼
┌──────────────────┐  校验 JWT → 换成真实 API Key   ┌──────────────────┐
│  api-gateway     │ ─────────────────────────────▶ │  上游 LLM 提供商  │
│  (反向代理)       │ ◀───────────────────────────── │  OpenAI/DeepSeek  │
└──────────────────┘                                └──────────────────┘
```

## 功能特性

- **PKCE 授权码流程**(S256),授权码单次使用、短时过期,`redirect_uri` 精确匹配
- **RS256 JWT 签发**:RSA 密钥对首次启动自动生成(`auth-server/keys/`,已 gitignore),私钥不出授权服务器,网关只持有公钥
- **Refresh token 轮换 + RFC 7009 撤销**:网关每次请求向授权服务器校验 jti 撤销状态(可配置短缓存)
- **每用户 LLM 路由**:用户可自行配置上游服务地址与 API Key(Fernet 加密存储),管理员可统一管理模板;`config_version` 随 JWT 下发,网关按用户路由
- **SSRF 出站防护**:用户配置的上游地址仅允许公网 http(s),私网/环回/元数据地址被拒;运维可通过白名单放行内网端点
- **审计与观测**:全事件审计日志(SQLite,留存期自动清理)、审计 API Token 鉴权 + 哈希链完整性校验、用量统计、Prometheus metrics、SSE 审计流
- **安全加固**:强密码策略、登录限流与账号锁定、secure cookie / HSTS、会话持久化、生产环境对不安全默认密钥 **fail-fast**(拒绝启动)
- **生产部署**:Docker Compose + Caddy 自动 HTTPS,服务端口只暴露在内部网络

## 目录结构

```
├── auth-server/            # FastAPI 授权服务器 (:8091)
│   ├── auth_server/        #   config / jwt_utils / llm_config / audit / ratelimit / database
│   └── tests/              #   pytest 测试
├── api-gateway/            # FastAPI API 网关 (:8092)
│   ├── gateway/            #   config / ssrf / metrics / stats / web(report, logs)
│   └── tests/              #   pytest 测试
├── frontend/               # React + Vite SPA(登录/注册/授权确认/LLM 设置)
├── deploy/                 # 生产部署:docker-compose.prod.yml + Caddyfile + .env.prod.example
├── scripts/                # 运维脚本(backup.py 等)
├── docs/                   # 规格与运维文档(specs/、ops/)
├── docker-compose.yml      # 本地开发 compose
├── .env.example            # 环境变量模板(占位符,可提交)
└── PKCE.md                 # 原始设计文档(协议与实现细节)
```

## 快速开始(本地开发)

前置条件:Docker Engine 20.10+ 与 Docker Compose v2(Node.js ≥ 22、Python ≥ 3.10 仅手动方式需要)。

### 方式一:Docker Compose(推荐)

```bash
cp .env.example .env        # 按需填写;不填也能以开发默认值启动
docker compose up --build
```

- 授权服务器:http://localhost:8091/login
- 网关健康检查:http://localhost:8092/health
- 前端页面由 auth-server 直接托管(SPA 已打进镜像)

> 本地 compose 显式设置了 `ALLOW_INSECURE_DEFAULTS=true`(仅限开发)。JWT 密钥、Fernet 密钥、SQLite 数据库均落在命名卷中,首次启动自动生成。

### 方式二:手动启动

```bash
# 1. 授权服务器
cd auth-server && pip install -e ".[dev]"
#    设置环境变量(或参考 .env.example 导出),首次启动自动生成 keys/
uvicorn auth_server.main:app --port 8091

# 2. API 网关(读取 api-gateway/.env 或环境变量)
cd api-gateway && pip install -e ".[dev]"
uvicorn gateway.main:app --port 8092

# 3. 前端(开发服务器,代理到 8091)
cd frontend && npm install && npm run dev   # http://localhost:5174
```

## 环境变量

完整的变量说明见 [`.env.example`](.env.example) 与 [`deploy/.env.prod.example`](deploy/.env.prod.example)。核心变量:

| 变量 | 说明 |
|------|------|
| `PUBLIC_GATEWAY_URL` | token 响应中告知客户端的网关地址 |
| `INTERNAL_SERVICE_TOKEN` | 网关 ↔ 授权服务器内部通信凭据,生产必须为强随机值 |
| `AUTH_SECRET_KEY` | 授权服务器会话密钥,生产必须为强随机值 |
| `AUDIT_API_TOKEN` | 审计/统计 API 的 Bearer Token,生产必填 |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | 管理员初始账号(生产首次启动后建议修改) |
| `UPSTREAM_URL` / `UPSTREAM_API_KEY` | 默认 LLM 上游地址与真实 API Key(仅存于服务端) |
| `UPSTREAM_URL_ALLOWLIST` | SSRF 白名单(逗号分隔),放行内网/特殊 DNS 环境 |
| `COOKIE_SECURE` / `ENABLE_HSTS` | 生产设 `true`(生产 compose 已默认开启) |

## 测试

```bash
# 授权服务器
cd auth-server && pip install -e ".[dev]" && pytest

# API 网关
cd api-gateway && pip install -e ".[dev]" && pytest

# 前端
cd frontend && npm install && npm run lint && npm run build
```

## 生产部署

单机生产部署(Compose + Caddy 自动 HTTPS、密钥/数据命名卷、健康检查)见 **[`deploy/README.md`](deploy/README.md)**:

```bash
cp deploy/.env.prod.example .env.prod   # 全部替换为强随机值
docker compose -f deploy/docker-compose.prod.yml --env-file .env.prod up -d --build
```

生产 compose 强制 `ALLOW_INSECURE_DEFAULTS=false`、`COOKIE_SECURE=true`、`ENABLE_HSTS=true`、`SEED_TEST_USER=false`,并以 `${VAR:?}` 强制要求关键密钥非空。

数据库在线备份/恢复见 [`docs/ops/backup-restore.md`](docs/ops/backup-restore.md)。

## 安全设计要点

- **API Key 永不进客户端**:上游凭据只存在于网关/授权服务器环境变量与加密存储中
- **PKCE 防授权码拦截**、`state` 防 CSRF、`redirect_uri` 精确匹配
- **fail-fast**:`AUTH_SECRET_KEY` / `INTERNAL_SERVICE_TOKEN` 为默认值时拒绝启动(生产)
- **审计链路**:`/api/logs*`、`/api/stats*` 无 Token 一律 401;管理员 JWT(SSO)可访问 `/gw/audit`、`/gw/report`
- **SSRF**:出站请求校验目标地址,仅公网 http(s),私网/元数据被拒
- **撤销**:网关每次请求校验 jti,刷新令牌轮换,登出即撤销

## 安全提醒(提交前必读)

- `.env`、`.env.prod`、`auth-server/keys/`(JWT 私钥与 Fernet 密钥)、`*.db`、`*.log` 均已加入 `.gitignore`;**不要用 `git add -f` 强推**
- 本仓库已做一轮凭据扫描,源码中未发现硬编码的真实 API Key / 密码(测试夹具如 `testpass`、`Secret123` 仅用于测试)。详见本次审计说明
- 若发现历史提交里出现过 `.env` / 密钥文件,需要用 `git filter-repo` 等工具重写历史并轮换受影响凭据(此操作不可逆,会影响远端,请谨慎)
