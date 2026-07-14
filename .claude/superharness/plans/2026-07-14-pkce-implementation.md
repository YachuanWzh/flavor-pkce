# PKCE Authorization Server + API Gateway 实施计划

## 架构概览

```
auth-server/   — FastAPI 授权服务器 (port 8000)
api-gateway/   — FastAPI API 网关 (port 8080)
frontend/      — React 前端 (port 5173)
```

## 任务列表 (TDD 循环)

### Task 1: 项目骨架 + 数据库 (auth-server)
- 初始化 auth-server Python 项目 (pyproject.toml, requirements)
- SQLite 数据库模型 (clients, users, authorization_codes, tokens)
- 种子数据 (默认 client, 测试用户)
- 测试: 验证数据库创建和表结构

### Task 2: 用户认证 API (auth-server)
- POST /register — 用户注册
- POST /login (API) — 用户名密码认证，返回 session cookie
- GET /login (页面) — 返回登录页面 HTML (后续替换为 React)
- 密码 bcrypt 哈希
- 测试: 注册 + 登录流程

### Task 3: PKCE /authorize 端点 (auth-server)
- GET /authorize — 校验参数，若未登录重定向到 /login，已登录显示授权确认页
- GET /consent — 授权确认页面
- POST /consent — 用户确认授权，生成 authorization_code 并 302 重定向
- 测试: 完整 /authorize 流程

### Task 4: PKCE /token 端点 + JWT (auth-server)
- POST /token — PKCE 校验 + JWT(RS256) 签发
- RSA 密钥对生成
- 测试: code_verifier 校验 + token 签发

### Task 5: React 前端 (frontend)
- Vite + React 项目初始化
- 登录页面组件
- 授权确认页面组件
- 与 auth-server API 对接
- auth-server 提供静态文件服务或 CORS 配置

### Task 6: API 网关 (api-gateway)
- FastAPI 项目
- JWT 公钥验证中间件
- 透明代理到上游 LLM API
- 测试: 有效/无效 JWT 测试

### Task 7: 端到端集成测试
- 全链路测试: 前端登录 → 授权 → 获取 JWT → 网关转发
