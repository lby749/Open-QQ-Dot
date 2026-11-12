# 发送承诺检测（语音 / 图片 / 表情 / 文件 / 视频）

日期：2026-10-31

## 问题

模型可能在回复里写“语音发你了”“图片发了”“文件传过去了”，但实际没有对应的
OneBot 发送动作，或者语音合成失败后退回了文字。接收方会收到一个未兑现的承诺。

## 解决

新增 `delivery_claims.py`：

- 检测语音、图片、表情、文件、视频的发送承诺
- 检测“发给你了 / 刚发了 / 传过去了”等泛化承诺
- 只允许在以下情况保留承诺：
  1. 当前这条出站消息本身包含对应媒体段
  2. 最近 5 分钟内有该类型工具发送成功的回执（`delivery:` 元数据）
- 不满足时把承诺改写成“语音还没发出去 / 图片还没发出去 / 还没发出去”，并改为纯文字发送

## 接入点

1. `main.py` 的 `Bridge.send_text`
   - 在语音合成、表情/图片段确定之后检查
   - 拦截后改为文字段，并清除引用
   - 日志：`已拦截未实际发送的媒体承诺`
2. `agent_service.send_friend_message`
   - 手动/模型文本发送前同样检查
3. `qq_media.send`
   - 真实附件发送成功后写入 `delivery:<message_id>` 元数据，供后续承诺校验

## 已验证

真实发送路径（FakeNapCat）测试：

```text
输入 bubble：语音发你了，听听看
实际出站：语音还没发出去，听听看
断言：不再包含“发你了”，且没有 record 段
```

测试：

```text
tests/test_delivery_claims.py      5 passed
tests/test_bridge.py               包含 false_voice_claim 集成测试
tests/test_qq_media.py             通过
tests/test_send_validator.py       通过
tests/test_send_state_consistency.py 通过

合计：61 passed, 28 subtests passed
```

## 边界

- 只处理“发送承诺”，不对普通聊天里的“发送消息”做误判
- 不伪造回执
- 回执只来自真实 OneBot 成功响应或当前出站媒体段
- 语音合成失败后退回文字时，不会保留“语音已发”的句子
