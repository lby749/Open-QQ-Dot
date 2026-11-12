# 最终版 Fluent 界面恢复记录（2026-11-04）

## 问题

先前工作区混用了 2026-10-28 06:00 与 10-28 07:38 两批文件，导致部分界面不是最终发布版：

- `pages_friend.py`：163,180 字节（旧） vs 190,873 字节（最终）
- `pages_library.py`：28,454 vs 30,913
- `window.py`：31,753 vs 36,048
- `common.py`、`adapter_settings.py`、`quick_setup.py` 也偏旧

## 恢复来源

`OpenQQDots-原代码.zip` 与 `OpenQQDots-测试版-绿色包.zip` 的 `house_qt/`、`house_settings.py`、`house_status.py`、`codex_settings.py` 完全一致，均以 2026-10-28 07:38 为最终版本。

## 已恢复

- `pages_friend.py`：最终版
- `pages_library.py`：最终版
- `window.py`：最终版，并重新接入账号切换托盘、设备/账号/健康/经验标签
- `common.py`、`adapter_settings.py`、`quick_setup.py`：最终版
- `house_settings.py`、`house_status.py`：最终版；补回 `romance_enabled` 开关
- `codex_settings.py`：保留最终版 1M 上下文窗口，同时保留 256k 默认压缩阈值

## 界面整合（不再单独堆 vNext）

vNext 侧栏已移除，功能补进原有页面：

- `智能体与工具 → 账号与路由`
- `QQ 与会话 → 设备与通话`（配对码 + 邀请通话）
- `系统洞察 → 健康来源 / 经验库`
- `智能体协作 → 群聊协作`（最终版遗漏，已补回）

## 验证

- `tests/test_house_qt.py`：36 passed
- `tests/test_agent_group_ui.py`：4 passed
- `tests/test_release_ui.py`：相关 2 项通过
- Fluent 实际启动截图：最终版侧栏（使用教程/主页/好友/智能体/任务/协作/QQ/系统洞察/设置），QQ 页有“设备与通话”，智能体页有“账号与路由”和“模型来源/API 与检索”分段。


## 全仓回归

```text
2016 passed
9 skipped
2 failed（系统 Python 缺 sherpa_onnx）
449 subtests passed
```

项目 `.venv` 的 TTS 测试通过。
