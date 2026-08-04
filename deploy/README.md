# 生产部署（P0-12）

基于 Docker Compose + Caddy 的**单机生产部署**：Caddy 在 443 端口终结 TLS
（Let's Encrypt 自动证书），auth-server 与 api-gateway 只在内部网络通信，不暴露
公网端口。

## 前置条件

- Docker Engine 20.10+ 与 Docker Compose v2
- 公网域名：`PUBLIC_DOMAIN`（auth）、`PUBLIC_GATEWAY_DOMAIN`（gateway），
  DNS A 记录指向本机公网 IP（Caddy 申请证书需要）
- 或使用内部 CA / 离线环境时，自行替换 Caddyfile 为静态证书（见下文）

## 部署步骤

```bash
# 1. 准备生产环境变量（全部替换为强随机值）
cp deploy/.env.prod.example .env.prod
#   编辑 .env.prod：AUTH_SECRET_KEY / INTERNAL_SERVICE_TOKEN / AUDIT_API_TOKEN
#   必须为强随机值；UPSTREAM_URL 必须是公网 http(s) 地址（SSRF 防护会拒绝私网）

# 2. 启动
docker compose -f deploy/docker-compose.prod.yml --env-file .env.prod up -d --build

# 3. 验证
curl -fsS https://<PUBLIC_DOMAIN>/login          # auth 页面
curl -fsS https://<PUBLIC_GATEWAY_DOMAIN>/health  # {"status":"ok"}

# 4. 查看日志
docker compose -f deploy/docker-compose.prod.yml logs -f
```

## 安全默认值（本次 P0 加固）

| 项 | 生效方式 |
|----|----------|
| 默认密钥 fail-fast | `AUTH_SECRET_KEY`/`INTERNAL_SERVICE_TOKEN` 缺失或为默认值 → auth-server 拒绝启动 |
| secure cookie / HSTS | `COOKIE_SECURE=true`、`ENABLE_HSTS=true`，Caddy 下发 HSTS 头 |
| 无种子账号 | `SEED_TEST_USER=false`（生产绝不创建 testuser） |
| 审计 API 鉴权 | `AUDIT_API_TOKEN` 必填，`/api/logs*` 未带 token 一律 401 |
| SSRF 防护 | 用户配置的上游地址只允许公网 http(s)（私网/元数据/环回被拒） |
| 令牌撤销 | 网关每次请求向 auth 检查 jti 撤销（可配置短缓存） |
| 审计留存 | `AUDIT_RETENTION_DAYS=180`，启动时清理过期日志 |

## 数据持久化与备份

- 命名卷：`keys-volume`（JWT 私钥/Fernet 密钥，**首次启动自动生成**）、
  `auth-db-volume`、`audit-db-volume`
- 备份请使用 `scripts/backup.py`（在线快照，无需停服）：
  ```bash
  docker compose -f deploy/docker-compose.prod.yml exec auth-server \
    python /app/scripts/backup.py backup /app/data/auth.db --out /tmp/auth-$(date +%F).db.bak
  docker compose cp auth-server:/tmp/auth-<date>.db.bak ./backups/
  ```
  详见 `docs/ops/backup-restore.md`。

## 离线/内网（无公网域名）部署

将 Caddyfile 的自动 HTTPS 改为静态证书：

```caddyfile
https://auth.internal.example.com {
    tls /certs/auth.pem /certs/auth.key
    reverse_proxy auth-server:8091
}
```

并挂载证书目录到 caddy 服务。此时 `COOKIE_SECURE` 仍可保持 true（内网也是 HTTPS）。

## 升级

```bash
git pull
docker compose -f deploy/docker-compose.prod.yml --env-file .env.prod up -d --build
# 升级前先备份数据库（见上）
```
