# NL2SQL Agent 评测

两层评测，互补：

1. **协议级 golden 回归（CI/本地自动）**
   `api-gateway/tests/test_agent_golden.py`：注入固定 LLM 输出，锁定
   意图协议、sqlguard 拦截 + 反思重试、确认/执行、图表槽位与知识注入
   （术语词典 / 字段释义 / few-shot QA）。改 prompt、guard 或知识注入前
   后必须保持全绿。

2. **实弹评测（人工触发，不进 CI）**
   对真实上游模型回放同一批问题，检查生成 SQL 质量：

   ```bash
   python scripts/agent_eval.py \
       --gateway-url http://127.0.0.1:8092 \
       --token "$AUDIT_API_TOKEN" \
       --cases scripts/agent_eval_cases.json
   ```

   退出码 = 失败用例数。用例放在 `scripts/agent_eval_cases.json`：
   `question` 必填，`expect_sql_contains` 为 SQL 必须包含的片段（子串、
   大小写不敏感），`expect_blocked: true` 表示写操作类问题必须被拦截。

建议时机：更换上游模型、改 `_SYSTEM_PROMPT`、批量调整知识条目后各跑一
次；把线上答错的问题补进 cases 文件形成闭环。
