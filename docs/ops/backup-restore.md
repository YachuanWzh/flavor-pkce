# 数据库备份与恢复（P0-11）

flavor-pkce 使用两个 SQLite 数据库：

| 服务 | 默认路径 | 内容 |
|------|----------|------|
| auth-server | `data/auth.db` | 用户、客户端、会话、授权码、令牌、审计日志 |
| api-gateway | `data/gateway-audit.db` | 网关审计日志（请求/响应体、令牌用量） |

## 备份

使用 `scripts/backup.py`（基于 SQLite 在线备份 API，**无需停服**，生成一致快照）：

```bash
# auth-server 数据库
python scripts/backup.py backup data/auth.db --out backups/auth-$(date +%F).db.bak

# 网关审计数据库
python scripts/backup.py backup data/gateway-audit.db --out backups/audit-$(date +%F).db.bak
```

不指定 `--out` 时，默认生成 `<数据库名>-<UTC时间戳>.db.bak`。

### 定时备份（cron 示例）

```cron
# 每天 02:00 备份，保留 30 天
0 2 * * * cd /opt/flavor-pkce && python scripts/backup.py backup data/auth.db --out backups/auth-$(date +\%F).db.bak
0 2 * * * find /opt/flavor-pkce/backups -name '*.db.bak' -mtime +30 -delete
```

生产 docker-compose 中建议将 `data/` 与 `backups/` 挂载到宿主机卷或独立磁盘。

## 恢复

```bash
# 将备份恢复到目标数据库（会覆盖目标文件；建议先停服或确认无写入）
python scripts/restore.py backups/auth-2026-08-01.db.bak data/auth.db
python scripts/restore.py backups/audit-2026-08-01.db.bak data/gateway-audit.db
```

### 恢复演练

1. 在临时目录恢复备份：`python scripts/backup.py restore <备份> /tmp/test.db`
2. 用 `sqlite3 /tmp/test.db 'PRAGMA integrity_check;'` 校验完整性
3. 网关审计日志额外校验哈希链：`GET /api/logs/integrity`（需 `X-Audit-Token`）

## 注意事项

- 备份文件包含**明文请求/响应体**（网关审计），请像保管生产密钥一样保管备份（加密磁盘/受限权限）。
- 恢复前确认目标库无活跃写入，否则可能覆盖新数据。
- 定期做一次恢复演练（建议每季度），确保备份可用。
