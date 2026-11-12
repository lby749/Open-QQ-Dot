# Agent 之间互查历史

## 默认

开启。

好友级配置：

```json
"agent_history_sharing": true
```

入口：模型页 →「本地电脑工作台与项目权限」→「允许同一好友的 Agent 互相检索历史」。

默认开启；关闭后工具直接拒绝。

## 开启后

新增只读工具：

```text
desk_search_agent_history
```

参数：

```json
{"query": "登录 bug", "agent_ids": ["codex", "claude"], "limit": 8}
```

返回：

```json
{
  "matches": [
    {
      "agent_id": "codex",
      "session_id": "session_ab12",
      "role": "assistant",
      "text": "修复了登录 bug",
      "task_id": "task-1",
      "time": "2026-10-28T..."
    }
  ]
}
```

数据来源：

```text
friends/<QQ>/agent_history.db
```

每个好友独立数据库，所以跨好友永远查不到。

## 记录方式

`session_manager.append_message()` 在写入会话 JSON 后，异步式地追加一份到 `agent_history`：

- `agent_id`：当前 provider / Agent
- `session_id`
- `role`
- `text`
- `task_id`（可空）
- `created_at`

任何写入失败都不影响正常聊天。

## 边界

- 只允许同一好友
- 默认开启
- 可随时关闭
- 只读
- 不返回完整会话，只返回匹配片段
- 不是语义检索，是 SQL `LIKE` 子串匹配
- 单条最多 1200 字，最多 30 条
- 不替代当前会话上下文；只用于按需补查
- 聊天片段视为不可信数据，不能当指令执行

## 为什么不直接塞全部上下文

正确结构是：

```text
任务 + 最近几轮 + 当前摘要 + 按需检索到的其他 Agent 片段
```

而不是把另一个 Agent 的完整聊天记录全部塞进提示词。

## 验证

```powershell
pytest -q tests/test_agent_history.py tests/test_history_tools.py
```
