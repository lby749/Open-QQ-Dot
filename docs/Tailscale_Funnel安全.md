# Tailscale Funnel 安全说明

## 当前防护（已实现）

1. **Funnel 默认拒绝**
   - 识别请求头 `Tailscale-Funnel-Request`
   - 未设置 `QQ_ALLOW_FUNNEL=1` 时，所有路径（包括首页）返回 403
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
- token 长期有效，没有自动过期和轮换入口
- token 明文保存在 `house_web.local.json`
- 没有设备绑定，知道 token 就能用
- Funnel 一旦显式开启，公网只靠 token 防护
- 没有审计日志（请求日志被故意关闭）

## 使用建议

- 优先用 Tailscale Serve / 私有 tailnet，不要用 Funnel
- 确实需要 Funnel 时，临时设置：

```powershell
$env:QQ_ALLOW_FUNNEL='1'
```

用完删除该环境变量并重启。
- 不要把带 `#t=` 的链接发到群聊、截图或公开页面
- 定期更换 `house_web.local.json` 中对应好友的 token
- 不要把管理路由暴露给公网

## 验证

```powershell
pytest -q tests/test_house_web_security.py
```
