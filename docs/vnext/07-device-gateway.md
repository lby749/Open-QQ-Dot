# Phase 3 — DeviceGateway 配对核心

日期：2026-10-30  
状态：**配对/令牌/撤销核心完成；TLS/WSS 传输与手机端配对 UI 未完成。**

## 已实现

`device_gateway.py`：

- 8 位、单次使用配对码，默认 5 分钟过期，最长 15 分钟
- 每设备随机 32-byte token
- 磁盘只保存 token 的 SHA-256，不保存明文
- scope 白名单：
  - `chat:read`
  - `tasks:read`
  - `agents:read`
  - `chat:write`
  - `voice:stream`
  - `health:read`
- `authenticate` 常量时间比较
- `list_devices` 不返回 token/hash
- `revoke` 删除设备并立即失效
- 线程锁，原子写 `data/vnext_devices.json`

Bridge 端点：

```text
GET  /api/devices
POST /api/device/pairing-code   (admin/local only)
POST /api/device/pair
POST /api/device/revoke
```

配对流程已真实跑通：

```text
1. POST /api/device/pairing-code -> {"code":"C4M0N849","expires_in":300}
2. POST /api/device/pair {"code":"C4M0N849","name":"PowerShell test phone"}
3. -> device_id + token + scopes
4. GET /api/devices -> 设备存在，但不含 token
```

测试设备已撤销，当前真实响应：

```json
{"items":[]}
```

## 设备令牌有效期

新配对设备带 `expires_at`，默认 30 天。`authenticate()` 拒绝过期令牌；旧记录没有 `expires_at` 时保持兼容。

测试：`tests/test_device_gateway.py` 覆盖过期拒绝。

## 设备令牌轮换

`POST /api/device/refresh` 使用当前设备 token 换取新 token：

- 旧 token 立即失效
- `expires_at` 重置为 30 天
- 返回新 token 只一次

真实测试（Bridge 启用管理员 token）：

```text
旧 token 请求 -> 401
刷新后新 token 请求 -> 200
```

## Android 自动刷新

Android 启动时如果已有设备 token，会调用 `/api/device/refresh`：

- 成功：保存新 token，旧 token 失效
- 失败（如无管理员 token 或网络错误）：保留旧 token，继续加载

真实回归：把刚配对的 token 写入模拟器并重启应用，桌面用旧 token 调用 `/api/device/refresh` 返回 401，说明应用启动时已经轮换；应用仍显示“已连接”。

测试设备随后撤销，`/api/devices` 为空。

## 桌面设备管理

设置页现在列出已配对设备、scope，并提供“撤销”。实际点击撤销后：

```text
已配对设备 1 台 -> 撤销 -> 0 台
GET /api/devices -> {"items":[]}
```

截图：`build/desktop-devices-list.png`

## 测试

```text
tests/test_device_gateway.py     4 passed
tests/test_vnext_bridge.py       3 passed
```

覆盖：

- 配对码单次使用
- 明文 token 不落盘
- 非法 scope 被过滤
- 错误 token 拒绝
- 撤销后立即失效
- 过期码拒绝
- 列表不泄露 token

## 仍未实现

- HTTPS/WSS listener（当前仍是 loopback HTTP）
- POST 配对接口和二维码
- 手机端配对页面
- 设备级速率限制、审计日志、IP 绑定
- 隧道/NAT 穿透
- 真机连接

不能把“配对核心通过单测”说成“手机已经安全配对”。

## 下一步

1. 在 bridge 增加 `POST /api/device/pair`，只允许本机管理员调用，返回一次性配对码。
2. 手机设置页输入配对码，换取 device token，后续请求带 `Authorization: Bearer <device-token>`。
3. 用 TLS 证书或受控隧道替换明文 HTTP；加入设备撤销和审计。
