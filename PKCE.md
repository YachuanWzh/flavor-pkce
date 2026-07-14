# PKCE 授权服务器开发方案

> **背景**: flavor-code 是 TypeScript CLI 编码代理，**不是 LLM 模型提供商**。对于拥有自建授权体系但使用第三方 LLM API（如 OpenAI）的场景，需要"授权服务器 + API 网关"双层架构来保护真正的 API Key 不暴露给客户端。

---

## 1. 整体架构（方案 B：API 网关代理）

```
┌────────────────────┐     ┌──────────────────┐     ┌─────────────────┐     ┌──────────┐
│   flavor-code CLI  │     │  你的授权服务器    │     │   API Gateway   │     │  OpenAI  │
│   (客户端)          │     │  (Auth Server)   │     │   (反向代理)     │     │  (上游)   │
└────────┬───────────┘     └────────┬─────────┘     └────────┬────────┘     └────┬─────┘
         │                          │                        │                   │
         │  1. GET /authorize       │                        │                   │
         │────PKCE params──────────→│                        │                   │
         │                          │  2. 用户登录 + 授权      │                   │
         │                          │  3. 302 redirect        │                   │
         │←────code + state─────────│                        │                   │
         │                          │                        │                   │
         │  4. POST /token          │                        │                   │
         │────code_verifier────────→│                        │                   │
         │                          │  5. PKCE 校验通过        │                   │
         │                          │  6. 签发 JWT            │                   │
         │←──{ access_token: JWT }──│                        │                   │
         │                          │                        │                   │
         │  7. 后续所有 LLM 请求      │                        │                   │
         │  baseURL = gateway       │                        │                   │
         │  Authorization: Bearer   │                        │                   │
         │  <JWT>                   │                        │                   │
         │─────────────────────────────────────────────────→│                   │
         │                          │                        │  8. 校验 JWT       │
         │                          │                        │  9. 替换 header:    │
         │                          │                        │  Authorization:    │
         │                          │                        │  Bearer sk-xxx     │
         │                          │                        │───────────────────→│
         │                          │                        │                    │ 10. OpenAI
         │                          │                        │                    │ 正常响应
         │                          │                        │←───────────────────│
         │←────── LLM response ────────────────────────────│                     │
         │                          │                        │                    │
```

### 核心原则

- **授权服务器只做身份认证**：签发 JWT，不接触任何 LLM API Key
- **API 网关做 token 交换**：校验 JWT → 换成真正的 OpenAI Key → 代理到上游
- **flavor-code 无感知**：它拿到的 `access_token`（JWT）原样塞 `Authorization` header，和传统 API Key 流程完全一致，只是 `baseURL` 指向网关
- **真正的 API Key 永不离开网关**

---

## 2. flavor-code 客户端现状（已实现）

### 2.1 实现文件

| 文件 | 说明 |
|------|------|
| `src/auth/oauth.ts` | `OAuthCallbackAuthProvider` — 完整 PKCE 客户端 |
| `src/auth/store.ts` | `createFileTokenStore()` — token 持久化到 `~/.flavor-code/auth.json` |
| `src/auth/types.ts` | `AuthProvider` / `AuthResult` / `OAuthCallbackOptions` 接口 |
| `src/config/schema.ts` | `ProviderConfigSchema` 扩展了 OAuth 字段 |
| `src/production.ts` | `registerConfiguredAdapters` 支持 `oauth-callback` type |

### 2.2 客户端 PKCE 流程

1. 生成 `code_verifier`（`crypto.randomBytes(32)` → base64url, 43 字符）
2. 计算 `code_challenge = base64url(SHA256(code_verifier))`
3. 生成 `state`（`crypto.randomBytes(32)` → base64url）
4. 在 `127.0.0.1` 随机端口启动临时 HTTP server
5. 用系统默认浏览器打开 `authorizationUrl`
6. 回调到达时校验 `state` → POST `/token` 交换 `code` + `code_verifier`
7. 返回 `AuthResult { headers: { Authorization: "Bearer <JWT>" }, expiresAt }`
8. Token 自动持久化到 `~/.flavor-code/auth.json`，过期前 60s 自动重刷

### 2.3 flavor.json 配置（方案 B）

```json
{
  "providers": {
    "openai": {
      "type": "oauth-callback",
      "apiType": "openai",

      "baseURL": "https://api-gateway.your-company.com/v1",

      "authorizationUrl": "https://auth.your-company.com/authorize",
      "tokenUrl": "https://auth.your-company.com/token",
      "clientId": "flavor-code-cli",
      "scope": "models:read models:use",

      "defaultModel": "gpt-4o",
      "cheapModel": "gpt-4o-mini"
    }
  },
  "agents": {
    "main": { "model": "openai:gpt-4o" },
    "subagent": { "model": "openai:gpt-4o-mini" }
  }
}
```

**关键点**：`baseURL` 指向你的 API 网关，不是 `https://api.openai.com/v1`。

---

## 3. 授权服务器开发指导

### 3.1 两个端点（不变）

#### `GET /authorize`

| 参数 | 必填 | 说明 |
|------|------|------|
| `response_type` | 是 | 固定值 `code` |
| `client_id` | 是 | 客户端标识 |
| `redirect_uri` | 是 | `http://127.0.0.1:{port}/callback` |
| `code_challenge` | 是 | base64url(SHA256(code_verifier)) |
| `code_challenge_method` | 是 | 固定值 `S256` |
| `state` | 是 | 客户端随机串，**必须原样回传** |
| `scope` | 否 | 空格分隔 |

#### `POST /token` — 唯一改动：签发 JWT

Content-Type: `application/x-www-form-urlencoded`

| 参数 | 必填 | 说明 |
|------|------|------|
| `grant_type` | 是 | `authorization_code` |
| `code` | 是 | 上一步的 authorization_code |
| `redirect_uri` | 是 | 必须一致 |
| `client_id` | 是 | 必须一致 |
| `code_verifier` | 是 | PKCE code_verifier |

**与之前方案的唯一区别**：`access_token` 必须是 JWT，由授权服务器用私钥签发。

成功响应不变：

```json
{
  "access_token": "eyJhbGciOiRS256...",
  "token_type": "Bearer",
  "expires_in": 3600
}
```

### 3.2 JWT 签发规范

```json
{
  "sub": "user-123",
  "client_id": "flavor-code-cli",
  "scope": "models:read models:use",
  "iat": 1720000000,
  "exp": 1720003600,
  "jti": "unique-token-id"
}
```

- 算法: **RS256**（非对称，私钥签发 / 公钥验证）或 **HS256**（对称，共享密钥）
- 推荐 RS256：网关只需公钥，私钥不出授权服务器
- `expires_in` 建议 1 小时
- `jti` 用于支持撤销（可选）

### 3.3 数据库设计（与之前一致）

```sql
CREATE TABLE clients (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  redirect_uris TEXT NOT NULL,              -- JSON array: ["http://127.0.0.1:"]
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE users (
  id            TEXT PRIMARY KEY,
  username      TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE authorization_codes (
  code             TEXT PRIMARY KEY,
  client_id        TEXT NOT NULL REFERENCES clients(id),
  redirect_uri     TEXT NOT NULL,
  code_challenge   TEXT NOT NULL,
  user_id          TEXT NOT NULL REFERENCES users(id),
  scope            TEXT,
  expires_at       TEXT NOT NULL,
  used             INTEGER NOT NULL DEFAULT 0,
  created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE tokens (
  id              TEXT PRIMARY KEY,
  jti             TEXT NOT NULL UNIQUE,       -- JWT ID，支持撤销
  client_id       TEXT NOT NULL REFERENCES clients(id),
  user_id         TEXT NOT NULL REFERENCES users(id),
  scope           TEXT,
  expires_at      TEXT NOT NULL,
  revoked         INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_auth_codes_expires ON authorization_codes(expires_at);
CREATE INDEX idx_tokens_jti ON tokens(jti);
```

### 3.4 前端页面（不变）

- 登录页面 `GET /login`
- 授权确认页面（`/authorize` 登录后渲染）

---

## 4. API 网关开发指导（新增）

### 4.1 职责

API 网关是 flavor-code 和真正 LLM 提供商之间的透明代理：

```
Gateway 对 flavor-code: 看起来像 OpenAI API
Gateway 对上游 OpenAI:   用真正的 API Key 鉴权
```

**核心逻辑**：对每个请求，取出 `Authorization: Bearer <JWT>` → 验证 JWT → 替换为 `Authorization: Bearer <REAL_API_KEY>` → 转发到上游。

### 4.2 最小实现（Node.js / Express）

```javascript
// gateway.mjs
import express from "express";
import { createProxyMiddleware } from "http-proxy-middleware";
import jwt from "jsonwebtoken";

const JWT_PUBLIC_KEY = process.env.JWT_PUBLIC_KEY;   // 授权服务器的公钥
const OPENAI_API_KEY = process.env.OPENAI_API_KEY;    // 真正的 OpenAI Key
const UPSTREAM_URL = "https://api.openai.com";

const app = express();

app.use("/v1", async (req, res, next) => {
  const authHeader = req.headers.authorization;
  if (!authHeader?.startsWith("Bearer ")) {
    return res.status(401).json({ error: "Missing authorization" });
  }

  const token = authHeader.slice(7);
  try {
    // 校验 JWT（签名 + 过期时间）
    jwt.verify(token, JWT_PUBLIC_KEY, { algorithms: ["RS256"] });

    // 替换为真实的 OpenAI Key
    req.headers.authorization = `Bearer ${OPENAI_API_KEY}`;
    next();
  } catch {
    res.status(401).json({ error: "Invalid or expired token" });
  }
});

// 透明代理到 OpenAI
app.use("/v1", createProxyMiddleware({
  target: UPSTREAM_URL,
  changeOrigin: true,
}));

app.listen(8080);
```

### 4.3 最小实现（nginx / OpenResty）

```nginx
server {
    listen 443 ssl;
    server_name api-gateway.your-company.com;

    # JWT 验证（需要 nginx-jwt 模块或 lua-resty-jwt）
    location /v1/ {
        access_by_lua_block {
            local jwt = require("resty.jwt")
            local auth_header = ngx.var.http_authorization
            if not auth_header then
                ngx.exit(401)
            end

            local token = string.match(auth_header, "^Bearer%s+(.+)$")
            local jwt_obj = jwt:verify(os.getenv("JWT_PUBLIC_KEY"), token)
            if not jwt_obj.verified then
                ngx.exit(401)
            end

            -- 替换为真实 API Key
            ngx.req.set_header("Authorization", "Bearer " .. os.getenv("OPENAI_API_KEY"))
        }

        proxy_pass https://api.openai.com;
        proxy_set_header Host api.openai.com;
    }
}
```

### 4.4 密钥体系

```
┌─ 授权服务器 ─────────────────┐    ┌─ API 网关 ──────────────────┐
│                              │    │                              │
│  持有: JWT_PRIVATE_KEY       │    │  持有: JWT_PUBLIC_KEY        │
│  (签发 JWT)                  │    │  (验证 JWT)                  │
│                              │    │  持有: OPENAI_API_KEY        │
│                              │    │  (调上游)                    │
└──────────────────────────────┘    └──────────────────────────────┘

生成方式:
  openssl genrsa -out private.pem 2048
  openssl rsa -in private.pem -pubout -out public.pem
```

- `private.pem` → 部署在授权服务器，签发 JWT
- `public.pem` → 部署在网关，验证 JWT
- `OPENAI_API_KEY` → 仅部署在网关

---

## 5. 关键开发节点

### Phase 1: 授权服务器 (Day 1-3)

| 节点 | 内容 | 产出 |
|------|------|------|
| 1.1 | 项目初始化，数据库 schema | 可运行的空服务 |
| 1.2 | `GET /authorize` + `GET /login` + `POST /login` | 登录页面 + Session |
| 1.3 | 授权确认页面 + code 签发 | 授权 UI 可用 |
| 1.4 | `POST /token` + PKCE 校验 | token 端点 |
| 1.5 | JWT 签发（RS256） | 返回 JWT 而非不透明 token |

### Phase 2: API 网关 (Day 3-4)

| 节点 | 内容 | 产出 |
|------|------|------|
| 2.1 | 基础代理框架（Node.js 或 nginx） | 可转发请求 |
| 2.2 | JWT 验证中间件 | 无效 token → 401 |
| 2.3 | Header 替换 + 上游代理 | 完整链路可通 |
| 2.4 | Dockerfile | 一键部署 |

### Phase 3: 联调 (Day 4-5)

| 节点 | 内容 |
|------|------|
| 3.1 | 授权服务器 + 网关本地启动 |
| 3.2 | flavor-code 配置指向本地 |
| 3.3 | 端到端：浏览器授权 → 拿到 JWT → 网关校验 → OpenAI 响应 |
| 3.4 | 异常路径全覆盖 |

---

## 6. 联调约定

### 6.1 PKCE 参数契约（不变）

| 参数 | 生成方 | 格式 |
|------|--------|------|
| `code_verifier` | flavor-code | base64url, 43 字符 |
| `code_challenge` | flavor-code | base64url(SHA256(verifier)), 43 字符 |
| `state` | flavor-code | base64url, 43 字符 |
| `redirect_uri` | flavor-code | `http://127.0.0.1:{随机端口}/callback` |

### 6.2 Token 格式约定（变化）

- **必须是 JWT**，算法推荐 RS256
- JWT payload 至少包含 `sub`, `client_id`, `iat`, `exp`, `jti`
- 网关只校验 `exp` 和签名，不查数据库（性能更好）
- 如需支持撤销，网关检查 `jti` 黑名单（Redis）

### 6.3 错误码

| HTTP | error | 场景 |
|------|-------|------|
| 302 | `access_denied` | 用户拒绝授权 |
| 400 | `invalid_grant` | PKCE 不匹配 / code 过期 / code 已用 |
| 400 | `invalid_client` | client_id 未注册 |
| 401 | — | 网关：JWT 无效或过期 |

---

## 7. E2E 测试方案

### 7.1 正向全链路测试

```
1. 启动授权服务器 (localhost:3000)
2. 启动 API 网关 (localhost:8080)
3. flavor-code 配置 baseURL = http://localhost:8080/v1
4. 启动 flavor → 自动打开浏览器 → 登录 → 授权
5. flavor 发起任意 LLM 请求
6. 断言:
   a. 授权服务器的 /token 被调用，返回 JWT
   b. 网关的 JWT 校验通过
   c. OpenAI 收到请求的 Authorization header 是真实的 sk-xxx
   d. flavor 收到正常的模型响应
```

### 7.2 网关单独测试

```
# 无效 JWT → 401
curl -H "Authorization: Bearer invalid-jwt" http://localhost:8080/v1/models

# 有效 JWT → 200
curl -H "Authorization: Bearer $(get-valid-jwt)" http://localhost:8080/v1/models
```

---

## 8. 附录

### 8.1 为什么不是方案 A（凭证托管）

方案 A 是授权服务器 `/token` 直接返回真正的 OpenAI API Key：

```
优点: 不需要网关
缺点: API Key 经过网络传输到客户端，安全风险高
```

方案 B 通过网关隔离 Key，真正的密钥只在一处，适合企业环境。

### 8.2 base64url 参考

**Node.js**: `buffer.toString("base64url")`
**Go**: `base64.RawURLEncoding.EncodeToString(data)`
**Python**: `base64.urlsafe_b64encode(data).rstrip(b"=").decode()`

### 8.3 RFC

- [RFC 7636 - PKCE](https://datatracker.ietf.org/doc/html/rfc7636)
- [RFC 6749 - OAuth 2.0 Authorization Code Grant](https://datatracker.ietf.org/doc/html/rfc6749#section-4.1)
- [RFC 8252 - OAuth 2.0 for Native Apps](https://datatracker.ietf.org/doc/html/rfc8252)
- [RFC 7519 - JSON Web Token (JWT)](https://datatracker.ietf.org/doc/html/rfc7519)
