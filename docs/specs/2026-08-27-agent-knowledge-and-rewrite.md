# Data Agent 知识供给与查询改写(参照 DataAgent 第一梯队)

日期:2026-08-27
状态:已实现

## 背景

参考 `spring-ai-alibaba/DataAgent` 项目(企业级 NL2SQL + Python 分析 Agent)的能力清单,
本项目(data agent:NL→SQL + 会话循环)的安全骨架(只读执行、人工确认、审计)已经完备,
短板在**知识供给与生成质量**。本文落地第一梯队四项轻量能力:

1. Q&A 知识对(少样本修正,few-shot correction)
2. 列级语义注释(column glossary,轻量语义模型)
3. 预设问题(preset questions)
4. 查询改写节点(query rewrite)

设计约束:零新服务、仅 SQLite 审计库、管理员自用、与现有会话循环/审计流兼容。

## 1. Q&A 知识对

- 表 `agent_qa_pairs(question UNIQUE, sql_template, tags JSON, enabled, updated_at)`
- API:`GET/POST /api/agent/qa`、`DELETE /api/agent/qa/{id}`(全部 `X-Audit-Token` 门控)
- 匹配:`qa.match_qa_pairs()` —— ASCII 单词 token + CJK 滑动双字 bigram 的集合交集,
  任一方向子串命中 +5 分;仅返回与当前问题有重合的启用项,最多 3 条,按得分降序。
- 注入:`render_qa_prompt(question)` 把命中的条目渲染为 system prompt 末尾的
  `Q: … / SQL: …` few-shot 段落。`agent.py` 与 `agent_loop.py` 的
  `_build_system_prompt(question)` 均接入;loop 侧通过 `_current_question(messages)`
  提取最近一条真实用户问题(跳过 `[feedback]`/`[tool error]` 标记消息)。

## 2. 列级语义注释

- 表 `column_glossary(table_name, column_name, business_name, synonyms JSON, description, enabled, updated_at, UNIQUE(table_name, column_name))`
- API:`GET/POST /api/agent/glossary`、`DELETE /api/agent/glossary/{id}`
- 注入:`render_glossary_prompt()` 把全部启用条目渲染为
  `- audit_logs.status (business name: …; synonyms: …; …)` 段落,追加在 metric terms 之后。

## 3. 预设问题

- 表 `preset_questions(question UNIQUE, enabled, sort_order, updated_at)`
- API:`GET/POST /api/agent/presets`、`DELETE /api/agent/presets/{id}`;
  `GET ?enabled_only=true` 供聊天页读取启用的快捷问题。
- 前端:`AgentChat.jsx` 空会话时展示可点击 chips,直接 `sendText(p.question)` 发送。

## 4. 查询改写

- `agent_loop.rewrite_question()`:仅在**多轮**(session 已有消息)**且传入真实 routing**
  时触发(测试路径以 call_llm 直驱、routing 为 None,天然跳过,不会发起真实上游调用)。
- 输入:最近 8 条消息的紧凑历史 + 当前问题;输出:单一自包含问题。
- 协议:与 agent 传输层一致 —— OpenAI `/chat/completions`(默认)与 Anthropic Messages
  (`/v1/messages`)。任一异常/空输出/超长/与原文相同 → 回退原文,绝不中断本轮。
- 事件:`{"event": "status", "stage": "rewriting"}` 先发,
  改写成功时再发 `{"event": "rewrite", "original, rewritten}`;前端展示
  "Rewrote to: …" 提示条。
- 审计:仍记录原始用户问题(`pending_question`/`agent_queries.question` 不变),
  改写后的文本只进入生成上下文。

## 验证

- 后端:新增 `test_qa.py` / `test_glossary.py` / `test_presets.py` /
  `test_knowledge_api.py`,并在 `test_agent_loop.py` 增加改写与注入测试;
  全量 `python -m pytest -q` → 331 passed。
- 前端:`npx oxlint src` → 0 warnings/0 errors;`npx vite build` → 构建通过。

## 后续(未做)

- Q&A 匹配升级为向量检索(需引入向量库/embedding 服务,违反零新服务约束)。
- SQL 语义一致性校验、Prompt 配置化、多步计划+报告(第二梯队)。
