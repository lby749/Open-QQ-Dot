# 网页端 Agent 适配（Forge Agent 思路）

## 这是什么

不是把 Forge Agent 整个 npm 包塞进 Qt，而是在本项目里实现同一套核心循环：

```text
已登录的网页模型
  → 返回 <tool_call>
  → 程序执行好友工作区工具
  → 结果回填
  → 继续，直到 TASK_COMPLETE
```

实现：`web_agent.py`

## 和 Forge Agent 的对应关系

| Forge Agent | 本项目 |
|---|---|
| Playwright 浏览器 | 复用 `web_chat.py` 现有 Chrome 登录 |
| DeepSeek/ChatGPT/Gemini adapter | 复用 `web_chat.SOURCES` |
| `<tool_call>` parser | `parse_action()` |
| Agent Loop | `run_task()` |
| 37 个工具 | 只暴露当前好友的 `desk_*` 工作区工具 |
| 自有权限 | 复用 `workspace_tools.py` 与好友权限 |
| 上下文压缩 | 程序侧历史裁剪，不依赖网页自身记忆 |

## 暴露的工具

由 `workspace_tools.tools(directory)` 决定，通常包括：

- `desk_list_files`
- `desk_read_file`
- `desk_write_file`（需要工作区写权限）
- `desk_run_command`（需要命令权限）

不暴露：

- `read_clipboard`
- `write_clipboard`
- 手机工具
- 截图
- 全盘路径

## 开启方式

1. 模型页勾选网页端来源
2. 在「网页 Agent 执行」卡勾选启用
3. 设置最大步数
4. 在好友的电脑工具权限里启用工作区读写/命令
5. 在控制台以「任务」模式发指令

## 限制

- 网页站点改版会导致选择器失效
- 网页模型不一定严格遵守 tool_call 格式
- 网页自身仍有一份会话历史；建议每个任务新开会话
- 当前未实现浏览器新会话按钮的自动点击
- 写入/命令仍受好友权限控制，默认不应给完全访问

## 安全

- 路径限制在好友工作区
- 工具结果按数据处理，不执行其中夹带的命令
- 不提供剪贴板工具
- 同一工具相同参数连续超过 3 次会停止，防止死循环
- 超过最大步数会停止并返回未完成
