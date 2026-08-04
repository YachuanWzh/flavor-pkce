# flavor-pkce 企业版演进 TODO

> 来源：2026-08-04 差距分析（见 `.claude/superharness/specs/2026-08-04-enterprise-pkce-gateway-gaps.md`）。
> MVP（P0/P1/P2 清单）先行，本文档记录**完整企业版**持续迭代的演进项。

## 高可用与数据层

- [ ] 数据库从 SQLite 迁移到 Postgres（含 Alembic 迁移）
- [ ] 会话/授权状态迁移 Redis（分布式会话、可水平扩展）
- [ ] auth-server / api-gateway 多副本部署 + 共享存储
- [ ] K8s / Helm 部署清单（替代单机 docker-compose）
- [ ] 多区域/多活部署方案

## 身份与合规

- [ ] OIDC Provider 能力（JWKS 已有 P1-5 基础，演进为完整 IdP）
- [ ] SSO 对接：OIDC/OAuth2 联邦（Okta/Entra/Keycloak）
- [ ] LDAP/AD 目录对接
- [ ] MFA（TOTP/WebAuthn）
- [ ] 对齐外部合规：等保 / SOC2 / GDPR（数据删除、留存、访问控制）

## 网关与业务能力

- [ ] 多租户隔离（若未来对外提供 SaaS）
- [ ] 配额计费 / 成本分摊（按用户/部门/项目）
- [ ] 内容合规检查（请求/响应过滤）
- [ ] 多上游负载均衡、故障转移、灰度发布
- [ ] 请求缓存（模型列表等低变数据）

## 备注

- SSO/LDAP 对接已在差距分析中确认**预留**（P2-2）
- 单机 SQLite 是扩展上限，Postgres 迁移为进入企业版的第一个里程碑
