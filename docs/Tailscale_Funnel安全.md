# Tailscale Funnel 安全说明

## 当前防护（已实现）

1. **Funnel 默认拒绝**
   - 识别请求头 `Tailscale-Funnel-Request`
   - 默认所有路径（包括首页）返回 403。电脑端「连接与会话 → 移动设备与网页访问」点击「一键打开并配置 Funnel」，确认实际转发成功后才保存启用状态；也支持 `QQ_ALLOW_FUNNEL=1`。显式环境变量 `0` 优先拒绝。
   - 不依赖 `client_address`：Funnel 从 tailscaled 到本机 loopback，IP 看起来就是 127.0.0.1

2. **API 必须带好友 Bearer Token**
   - 每个好友独立随机 token（`secrets.token_urlsafe(24)`）
   - 使用 `hmac.compare_digest` 比较
   - 只能访问自己好友的数据；admin 路由另查 `trusted_admin_policy`

3. **输入限制**
   - JSON body 最大 256 KiB
   - 文本字段单独限长
   - 手机工具参数按 schema 校验

4. **响应头**
   - `Cache-Control: no-store`
   - `X-Content-Type-Options: nosniff`
   - `X-Frame-Options: DENY`
   - `Referrer-Policy: no-referrer`
   - `Content-Security-Policy`（只允许同源脚本/样式/连接）
   - Funnel/HTTPS 时加 `Strict-Transport-Security`

5. **静态页不含 token**
   - token 在 URL fragment（`#t=`），不会发给服务器
   - 页面读取后 `history.replaceState` 清掉地址栏
   - 存入 `localStorage`

## 仍然不足

- 没有登录失败限速/锁定
- token 长期有效，电脑端可「重置访问令牌」，重置后旧链接立即失效
- Windows 上 token 由当前系统账户的 DPAPI 加密保存在 `house_web.local.json`；非 Windows 开发环境仍需保护本地文件
- Bearer 链接需要保密；不能将持有链接视为已验证设备身份
- Funnel 一旦显式开启，公网只靠 token 防护
- 没有审计日志（请求日志被故意关闭）

## 一键连接

- 使用 Tailscale 官方 CLI 的 `funnel --bg --yes --https=<端口> http://127.0.0.1:8790`。不需要将应用监听改为 `0.0.0.0`，也不自动打开局域网入口。
- 已安装客户端及匹配的 Funnel 直接复用；Windows 未安装时从官方稳定源下载固定版本 1.104.6 的 MSI，验证官方 SHA-256 后安装，已有有效缓存不重复下载。系统管理员确认和首次 Tailscale 登录需要用户完成。
- 只选空闲的 Funnel 端口 443、8443、10000，不覆盖其他应用的转发，不执行全局 reset。
- 首次授权页面由 Tailscale 提供；打开页面并不代表授权成功。另有不携带好友令牌的 HTTPS 连通检查；规则存在但公网不通时不切换二维码、不声称连接成功。成功后二维码使用当前好友的独立令牌，切换好友重新生成。
- 点击「关闭 Funnel」先关闭应用访问权限，再关闭与本应用端口精确匹配的转发；本机入口和 QQ 登录不受影响。
- 官方说明：[Funnel CLI](https://tailscale.com/docs/reference/tailscale-cli/funnel)。

## 使用建议

- 优先用 Tailscale Serve / 私有 tailnet，不要用 Funnel
- 确实需要 Funnel 时，临时设置：

```powershell
$env:QQ_ALLOW_FUNNEL='1'
```

通过界面启用后，可直接点「关闭 Funnel」；手动环境变量启用的情况请一并移除。
- 不要把带 `#t=` 的链接发到群聊、截图或公开页面
- 定期更换 `house_web.local.json` 中对应好友的 token
- 不要把管理路由暴露给公网

## 验证

```powershell
pytest -q tests/test_house_web_security.py
```
