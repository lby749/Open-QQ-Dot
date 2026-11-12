# Phase 4 — ProviderRouter 基础层

日期：2026-10-30  
状态：**只读路由基础完成；多账号写配置、额度/成本统计尚未实现。**

## 已实现

`provider_router.py`：

- 读取每位好友现有 `friend.json` 的 provider/model/api_key_id/effort
- 读取 per-provider approval；回退到旧 `computer_tools.approval_mode`
- 识别 Agent provider 与 API provider
- `route(root, qq, purpose)` 做显式路由：
  - 未知 provider 报错
  - Agent 未注册/未安装报错
  - 返回 transport 与原因
- `provider_summary(root)` 返回 provider 聚合和账号摘要
- **不读取或返回任何 API Key 值**

Bridge：

```text
GET /api/providers
```

真实当前结果：

```json
{
  "providers": [
    {"provider": "unknown", "friends": 2},
    {"provider": "webchat_gemini", "friends": 1}
  ]
}
```

桌面“账号与模型”页已改为读取该端点，显示：

- provider
- 使用好友数
- Agent 是否安装
- 每好友模型与审批

## 测试

```text
tests/test_provider_router.py    4 passed
tests/test_vnext_bridge.py       3 passed
```

覆盖：

- 非敏感字段读取
- per-provider approval 覆盖全局
- 未知 provider 拒绝
- 未安装 Agent 不报告为可用
- bridge `/api/providers`

## AccountService 基础

新增 `account_service.py`：

- 非敏感账号目录 `data/vnext_accounts.json`
- 仅保存 label/provider/base_url/model/key_id/effort/enabled
- 明确拒绝 `api_key`/`secret`/`token`/`password` 字段
- `key_id` 只引用现有 DPAPI 凭据，不复制密钥
- `discover()` 只读现有好友 profile
- 原子写、进程内锁

Bridge：

```text
GET  /api/accounts
POST /api/accounts/add
POST /api/accounts/remove
```

测试：`tests/test_account_service.py 3 passed`。

## 桌面账号与 DPAPI 密钥

账号表单支持可选 API Key：

- 明文不进入 `vnext_accounts.json`
- 使用现有 `api_backend.save_key` 写入 Windows DPAPI
- 密钥目录 `data/vnext_accounts/<id>/`
- 删除账号时同时删除该目录

真实 UI 测试：

```text
添加 Secure UI Account + sk-ui-secret
json 中明文：False
GET /api/accounts -> key_id = acct_...
删除账号 -> 密钥目录不存在
```

## 账号连通性测试

`POST /api/accounts/test` 使用 DPAPI 中保存的 Key 请求 `/v1/models`，只读探测，不发送聊天内容。

真实本地 mock 测试：

```text
账号 base_url=http://127.0.0.1:8899
POST /api/accounts/test
-> {"ok":true,"model_count":1,"models":["mock-model"]}
```

桌面账号列表新增“测试”按钮，显示成功模型数或失败原因。

## 好友账号绑定

新增非敏感绑定：

```text
POST /api/accounts/bind  {qq, account_id}
GET  /api/accounts/route?qq=...
```

闲置绑定存储在 `data/vnext_account_bindings.json`，不写好友 profile。真实测试：

```text
绑定 123456789 -> acct_...
route -> bound=true
删除账号 -> route -> bound=false（绑定同时清理）
```

## 桌面绑定

账号页新增：

```text
好友 QQ + 选择账号 + 绑定好友
```

实际 Playwright 操作后页面显示：

```text
已绑定：Bind UI
```

API 路由返回 `bound=true`；删除测试账号后绑定同步清理，`bound=false`。

## 绑定优先级

`ProviderRouter.route()` 先检查账号目录绑定：

```text
绑定存在且 enabled -> 使用账号 catalog
否则 -> 回退到原 friend.json profile
```

新增只读端点：

```text
GET /api/providers/route?qq=...
```

真实测试：

```json
{"route":{"qq":"123456789","provider":"codex","model":"m","transport":"cli","reason":"chat: account_catalog binding acct_..."}}
```

## 账号推理调用

`POST /api/accounts/chat` 使用账号 DPAPI Key 调用 OpenAI-compatible `/v1/chat/completions`，只在显式请求时发送 prompt。

本地 mock 真实测试：

```text
POST /api/accounts/chat {id, prompt:'你好'}
-> {"ok":true,"model":"mock","content":"mock chat reply"}
```

未对真实外部 Provider 发送测试消息。

## 桌面账号推理

账号页新增：

```text
选择账号 + 测试消息 + 调用模型
```

实际 Playwright 操作返回：

```text
模型返回：desktop mock reply
```

链路为桌面 UI -> Bridge -> DPAPI Key -> 本地 mock `/v1/chat/completions`。未向真实外部模型发送测试消息。

## 未实现

- 凭据轮换/测试连通性
- 账号与好友绑定
- 凭据写入与轮换（沿用现有 DPAPI，不在新 UI 重做）
- 额度、Token、成本统计
- 模型自动路由/负载均衡
- 跨账号 A/B
- 账号登录状态探测

## 下一步

1. 独立 `AccountService`，把凭据引用与 profile 分离。
2. Provider health probe（不泄露密钥）。
3. 用量统计接入 `usage_calls`，做真实成本表。
4. 再让桌面/手机只依赖 AccountService，不直接读 friend.json。
