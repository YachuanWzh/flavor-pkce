# flavor-pkce 企业级 PKCE+网关基座差距分析

- **日期**: 2026-08-04
- **状态**: approved（用户已确认）
- **来源**: superharness brainstorm 会话（脑图快照 rev 7）

## 1. 背景与约束（已确认的决策）

| 决策点 | 结论 |
|--------|------|
| 目标定位 | 内部自用（单组织私有部署），非多租户 SaaS、非开源分发 |
| 身份源 | 自建账号体系即可，**预留 SSO/LDAP 对接 TODO** |
| 合规要求 | 有内部审计要求（审计日志可查、防篡改、留存管理） |
| 部署形态 | 单机 Docker Compose |
| 实施策略 | MVP 先行（security-first），完整企业版演进项记入 `todo.md` 持续迭代 |

## 2. 现状盘点

- **auth-server**（FastAPI + SQLite）：PKCE `/authorize` `/consent` `/token`（仅 authorization_code）、注册/登录（bcrypt）、RS256 JWT（3 天）、每用户 LLM 上游配置（Fernet 加密存储）、admin 接口、内部服务接口（X-Internal-Service-Token）
- **api-gateway**（FastAPI）：JWT 校验 → 按用户路由上游（config_version 一致性检查）→ 透明代理（含 SSE 流式）、模型白名单、Prometheus 指标、SQLite 审计日志（含请求/响应体，截断 50k）、`/audit` 查看页
- **frontend**（React/Vite SPA，由 auth-server 托管）：登录/注册/授权确认/LLM 设置/Admin
- **测试**：auth-server + api-gateway 共 35+ 测试（含全链路 PKCE E2E）

## 3. P0 — 上线前必须修（12 项）

### 安全漏洞（10 项）

| # | 缺口 | 现状 | 目标 |
|---|------|------|------|
| P0-1 | 审计 API 无鉴权且可清空 | `GET/DELETE /api/logs` 裸奔，任何人可读全量请求/响应体（含提示词）、可删光审计 | 管理员鉴权 + append-only/哈希链防篡改；删除/清空仅限受控管理员操作 |
| P0-2 | 会话与授权状态在内存 | `_sessions`/`_pending_auth` 为进程内存 dict，重启即丢、无法水平扩展 | 迁移到 SQLite（单机场景）或 Redis；重启不丢会话 |
| P0-3 | 无 refresh token、撤销未生效 | `/token` 仅 authorization_code；`tokens.revoked` 无消费方；JWT 3 天有效期内无法撤销 | 补 refresh_token grant（轮换+复用检测）+ RFC 7009 撤销端点 + 网关撤销检查 |
| P0-4 | 登录/注册/token 无限流 | 暴力破解零防护 | 限流（IP/账号维度）+ 失败锁定 + 告警 |
| P0-5 | redirect_uri 前缀匹配 | `startswith("http://127.0.0.1:")`，违反 RFC 6749，开放重定向风险 | 客户端注册时白名单，授权/换码时精确匹配 |
| P0-6 | 无 HTTPS 强制、安全头缺失 | cookie `secure=False`；无 HSTS/CSP/X-Frame-Options；consent 页可被点击劫持 | TLS 终结 + Secure/SameSite cookie + HSTS + CSP + X-Frame-Options |
| P0-7 | 网关出站 SSRF | 用户可配 `upstream_url` 指向内网/云元数据地址 | 出站私网/元数据地址段黑名单 + 上游白名单审批 |
| P0-8 | 默认弱配置 | `dev-secret-change-in-production`、`testuser/testpass` 种子账号、`dev-internal-token-change-me` | 首启强制生成强密钥、去除种子账号、弱默认值直接拒绝启动 |
| P0-9 | 密码策略缺失 | 注册密码 `min_length=1` | 强密码策略（长度+复杂度），管理员可配置 |
| P0-10 | auth 侧无审计、日志无留存策略 | 登录/注册/授权/令牌事件未审计；审计日志无限增长 | 统一审计事件源（auth+gateway），含 IP/UA；留存期（如 180 天）+ 清理任务 |

### 运维（2 项）

| # | 缺口 | 目标 |
|---|------|------|
| P0-11 | 数据库无备份/恢复 | 定时备份（sqlite3 backup/文件快照）+ 恢复演练 |
| P0-12 | 无生产部署清单 | 生产级 docker-compose：TLS 终结、健康检查、重启策略、密钥挂载、日志卷 |

## 4. P1 — 三个月内补（9 项）

| # | 项 | 说明 |
|---|----|------|
| P1-1 | 配额/限流/计量 | 按用户/客户端聚合 token 用量（审计已有基础数据）+ 阈值告警 |
| P1-2 | 用户生命周期管理 | 禁用/删除/重置密码/角色，admin API + UI |
| P1-3 | 密码重置/邀请制 | 管理员发起一次性链接 |
| P1-4 | auth 可观测性 | Prometheus 指标 + 结构化日志；trace_id 贯穿 auth/gateway |
| P1-5 | JWKS / OIDC discovery | `/.well-known/openid-configuration` + `jwks.json`（含 kid/iss/aud），客户端免手工配公钥；SSO 前置 |
| P1-6 | 密钥轮换 | JWT 多 kid 宽限期轮换；Fernet 主密钥轮换 |
| P1-7 | 数据库迁移工具 | Alembic 替代手写 ALTER TABLE |
| P1-8 | 上游供应商统一管理 | 管理员配置供应商，用户只选不自定义 URL/Key（彻底消解 SSRF） |
| P1-9 | 客户端管理 | clients 表管理 API/UI：注册 redirect_uri、吊销客户端 |

## 5. P2 — 增强项（6 项）

| # | 项 | 说明 |
|---|----|------|
| P2-1 | MFA | TOTP / WebAuthn |
| P2-2 | SSO / LDAP 对接 | OIDC/OAuth2 联邦、LDAP/AD（已确认预留） |
| P2-3 | 高级网关策略 | 内容合规检查、请求缓存、多上游负载均衡/故障转移、灰度 |
| P2-4 | 成本/用量报表 | 按用户/部门聚合 token 用量 |
| P2-5 | 前端体验增强 | 可访问性、i18n、审计页导出 CSV |
| P2-6 | 完整企业版演进 | 多租户、Postgres/Redis HA、K8s/Helm、OIDC 提供方 → 记入 `todo.md` |

## 6. 风险

- **R1**：审计日志落库含提示词全文 → 需脱敏、权限分级、按留存期删除
- **R2**：单机 SQLite 是明确扩展上限 → 演进路线中迁 Postgres（P2-6）
- **R3**：JWT 3 天有效期 + 无撤销 = 泄露窗口大 → P0-3 优先落地短期 token + refresh 轮换

## 7. 下一步

- 用 `/superharness:go <目标>` 按 TDD 逐个实施 P0 项
- P0 完成后更新本文档状态；完整企业版演进项见仓库根目录 `todo.md`
