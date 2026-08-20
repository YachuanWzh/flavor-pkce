# 配置档案 / 智能路由 / 角色管理 实施规格

- **日期**: 2026-08-20
- **状态**: approved（用户已确认全部实施，路由切换采用混合策略）
- **关联痛点**: 1) 配置重复填写 2) 网关无智能路由 3) 无超管角色管理

## 1. 痛点 1 —— LLM 配置档案（保存一次，下拉复用）

### 现状

`user_llm_configs` 每用户只有一行激活配置；保存即覆盖并 `config_version+1`，
旧 JWT 失效需 `/login` 重新激活。用户每次换上游都要重填全部字段。

### 设计

新增 `llm_config_profiles` 表，每用户多条命名档案：

```
llm_config_profiles
  id            TEXT PRIMARY KEY
  user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE
  name          TEXT NOT NULL
  provider_id / service_name / api_type / upstream_url
  upstream_api_key_encrypted / upstream_auth_type
  default_model / cheap_model / models / max_output_tokens
  created_at / updated_at
  UNIQUE(user_id, name)
```

激活配置 `user_llm_configs` 保持不变，仍为网关路由的唯一数据源，
新增 `active_profile_id` 列记录当前激活来源（可空）。

### API（全部会话认证，与现有 llm-config 同域）

- `GET /api/me/llm-config-profiles` → `{profiles: [公共字段 + api_key_configured]}`
- `POST /api/me/llm-config-profiles` → 校验同 `LlmConfigUpdate`；
  `upstream_api_key` 省略时为空；同名档案 409
- `PUT /api/me/llm-config-profiles/{profile_id}` → 更新；省略 key 保留原 key；
  传空串清除 key
- `DELETE /api/me/llm-config-profiles/{profile_id}` → 删除档案（不影响激活配置）
- `POST /api/me/llm-config-profiles/{profile_id}/activate` →
  复制档案为激活配置，`config_version+1`，写入 `active_profile_id`；
  档案缺少 API key 且激活配置也无 key 时 400（fail-closed）

密钥可见性策略（2026-08-20 按用户要求调整）：
- **本人会话**（`/api/me/llm-config*`）：返回解密后的 `upstream_api_key`，
  供设置页回显——切换档案时表单携带各自的 key，不再串用上一次保存的 key。
- **管理员 API**（`/api/admin/users*`）：同样返回明文 key（管理员负责凭据轮换）。
- **保持脱敏的通道**：OAuth `/token` 响应、网关内部接口之外的公共面、审计日志。
- 静态存储仍为 Fernet 加密；普通用户读取他人档案/配置一律 404。

### 前端

`/settings/llm` 顶部新增档案下拉：选择档案 → 表单载入 → 保存激活。
另提供"另存为档案"（以新名称保存当前表单）与"删除档案"。
两个页面共用 `ProfilesToolbar` 组件。

### 管理员代管档案 API（2026-08-20 追加）

与本人会话同构，仅 `require_admin` + 目标用户存在性校验：

- `GET/POST /api/admin/users/{user_id}/llm-config-profiles`
- `PUT/DELETE /api/admin/users/{user_id}/llm-config-profiles/{profile_id}`
- `POST /api/admin/users/{user_id}/llm-config-profiles/{profile_id}/activate`
- `PUT /api/admin/users/{user_id}/llm-config/fallback`

激活逻辑共用 `_activate_profile`（档案不存在 404 / 无 key 400），
保证管理员与用户侧行为一致。`/admin/llm-configs` 为每个选中用户
呈现同一套 ProfilesToolbar（下拉/激活/另存/删除/fallback）。

## 2. 痛点 2 —— 网关智能路由（混合策略）

### 数据模型

激活配置新增 `fallback_profile_id` 列（可空，指向备用档案）。

`GET /internal/users/{user_id}/llm-config` 内部接口在返回激活配置的同时
附带 `fallback` 对象（含解密 key 的完整路由字段）或 `null`。

### 网关行为（混合策略）

请求到达后解析 `routes = [primary, fallback?]`，依次尝试：

1. **连接级失败**（DNS/连接拒绝/超时等 `httpx.ConnectError/Timeout`）或
   上游返回 **5xx**：尝试下一路由。
2. **静默切换条件**：下一路由与主路由 `api_type` 相同，且请求模型在下一路由
   `models` 白名单内（`models` 为空视为兼容）。满足则切换继续，响应附
   `X-Gateway-Route: <service_name>` 头与审计字段 `routed_service`。
3. **不满足静默条件**（跨协议或模型不支持）：返回 **409**
   `{"error": "route_switched", "routes": [{service_name, api_type, models}...]}`，
   由 flavor-code 提示用户"网关已切换路由配置，是否继续任务"；用户确认后
   客户端带 `X-Gateway-Preferred-Route: <service_name>` 重发。该头是**同意令牌**：
   只有当其值等于某条候选路由的 service_name（即属于该用户自己的路由集合）
   时才放行对该候选的切换；不匹配时忽略，无法指向他人配置。
4. **已开始流式输出**（SSE 首字节已发）后失败：不重试，直接终止。
5. 全部路由失败：返回 502 `Upstream provider unreachable`。

### 熔断冷却

进程内 `_route_cooldowns: {(user_id, service_name) -> monotonic 截止时间}`
（按 service_name 而非下标，保证档案顺序变化时冷却仍指向正确路由），
路由失败后冷却 30 秒（`FAILOVER_COOLDOWN_SECONDS`，可配），冷却期内直接跳过；
成功请求即清除该路由冷却；写入新冷却时顺带淘汰过期条目，防止长期运行下
字典无限增长。冷却仅影响选择顺序，不改变 fail-closed 语义。
`X-Gateway-Route` 响应头的值经过 CR/LF 与控制字符净化（service_name 用户可控）。

### 安全边界

- 备用路由同样通过 SSRF 校验（`validate_upstream_url`）。
- 备用 key 同样 Fernet 加密存储，任何公共 API 不返回。
- `X-Gateway-Preferred-Route` 只能在该用户**自己的**路由集合内选择，
  不能指向他人配置。

## 3. 痛点 3 —— 角色管理（admin / user）

### API

- `PUT /api/admin/users/{user_id}/role`，body `{"role": "admin"|"user"}`，
  仅 admin（数据库实时校验角色，不信任会话缓存）。
- 防自锁：
  - 不允许修改自己的角色（400 `self_role_change_forbidden`）；
  - 降级为 user 时，若目标是最后一个 admin，拒绝（400 `last_admin_protected`）。
    注意：该分支当前仅在防御纵深意义上生效——最后一个 admin 只能是自己，
    而改自己已被上一条规则拦截；保留它是为了防止将来移除自改禁令时静默失去保护。
- 变更后写审计事件 `role_changed`（actor + target + 新旧角色）。
- `GET /api/admin/users` 已返回 role，无需改动。

### 前端

`/admin/llm-configs` 用户行内加角色切换（admin ⇄ user），调用上述 API，
成功后刷新列表；对自己显示只读。

## 4. 兼容性

- 旧 JWT / 旧配置：`fallback_profile_id` 为空即行为与现状完全一致。
- 数据库迁移幂等：`PRAGMA table_info` 检查后 `ALTER TABLE ADD COLUMN`。
- flavor-code 未升级时：409 `route_switched` 会被视为普通错误重试，
  静默路径不受影响，向后兼容。

## 5. 测试策略（TDD）

- auth-server：角色管理（含自锁保护）、档案 CRUD、激活、版本递增、key 保密
- api-gateway：failover 静默切换 / route_switched 409 / preferred-route / 冷却 / SSE 不重试
- 前端：npm run lint && npm run build
