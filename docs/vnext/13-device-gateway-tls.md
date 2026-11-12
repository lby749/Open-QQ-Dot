# Phase 5 — DeviceGateway TLS 基础

日期：2026-10-30  
状态：**桥接器 HTTPS 已真实跑通；Android 仍使用 HTTP，尚未接入证书固定。**

## 实现

新增 `tls_support.py`：

- 首次启动生成本地自签名证书
- RSA 2048 / SHA-256 / 825 天
- SAN：`localhost`、`127.0.0.1`、`::1`
- 私钥权限 0600
- 已存在时复用，不重复生成

Bridge 新增参数：

```powershell
python vnext_bridge.py --port 8792 --tls
```

输出：

```json
{"bridge":"https://127.0.0.1:8792","read_only":true,"auth":false,"tls":true}
```

## 真实 HTTPS 验证

启动 TLS Bridge 后：

```text
curl -k https://127.0.0.1:8792/api/health
-> 200 {"ok":true,"service":"vnext-bridge"}

curl -k https://127.0.0.1:8792/api/overview
-> 200 真实概览
```

使用证书作为 CA 进行校验也成功：

```python
ctx = ssl.create_default_context(cafile='data/vnext_tls/bridge-cert.pem')
urllib.request.urlopen('https://127.0.0.1:8792/api/health', context=ctx)
-> 200
```

生成文件：

```text
data/vnext_tls/bridge-cert.pem  1188 bytes
data/vnext_tls/bridge-key.pem   1704 bytes
```

`data/` 已被 gitignore，证书和私钥不会提交。

## 测试

```text
tests/test_tls_support.py   1 passed
```

## Android HTTPS 实连

已把 `bridge-cert.pem` 打进 APK assets，并使用 **精确证书固定**：

```kotlin
if (chain[0].encoded != pinned) throw CertificateException("bridge certificate pin mismatch")
```

API 35 模拟器设置：

```text
桌面 Bridge 地址：https://10.0.2.2:8792
状态：已连接
```

截图：`build/android-https-settings.png`。

注意：每次重新生成桌面证书后，必须把新 `bridge-cert.pem` 重新打进 APK；否则会按设计拒绝连接。

## 未完成

- 真机 HTTPS 连接（模拟器已通过）
- WSS 语音流
- 证书轮换、指纹展示、配对时绑定
- 端口/防火墙/局域网发现

Android 当前仍明确显示“当前使用明文 HTTP，正式配对与 HTTPS/WSS 属于 DeviceGateway 阶段”，没有把桌面 HTTPS 说成手机已 TLS。
