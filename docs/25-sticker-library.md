# 表情包库（桌面只读）

> 本文中的账号均为脱敏占位示例，不可用于实际发送。

日期：2026-10-31

## 背景

旧版控制台已有好友收藏表情与含义卡，vNext 桌面之前只有检索/经验页，没有表情包入口。
本次补回一个只读表情包页，并保留现有发送侧的“发送承诺检测”，避免把未发送说成已发送。

## Bridge

新增：

```text
GET /api/stickers?qq=...
```

返回：

```json
{
  "qq": "1000000001",
  "count": 0,
  "items": [
    {
      "key": "sha256...",
      "text": "图上文字",
      "meaning": "含义",
      "emotion": "情绪",
      "usage": "使用场景",
      "by": "user/codex",
      "data_url": "data:image/png;base64,..."
    }
  ]
}
```

- 只读取好友自己的 `metadata` 与 `media/` 原图
- 单图上限 3MB，超过不返回 base64
- 不修改收藏、不发送、不上传

## 桌面 UI

新增导航：

```text
表情包 · 收藏与含义
```

功能：

- 选择好友
- 显示收藏数量
- 网格预览图片
- 显示含义卡：文字、意思、情绪、适合场景
- 明确提示“只读、不会重新上传或伪造发送”

## 测试

```text
tests/test_sticker_service.py   1 passed
tests/test_delivery_claims.py   5 passed
tests/test_bridge.py            通过
```

## 未完成

- 表情收藏/取消收藏
- 含义卡编辑
- 从桌面发送表情
- MEME_Manager 风格的标签、去重、批量导入
