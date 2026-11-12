# Phase 2 — 原生 Kotlin/Compose 薄客户端

日期：2026-10-30  
状态：**源码完成、Release APK 构建并验签；真机/模拟器未连接，运行验收未完成。**

## 交付

| 项 | 值 |
|---|---|
| 源码 | `mobile/` |
| 包名 | `com.openqqdot.mobile` |
| UI | Kotlin + Jetpack Compose + Material 3 |
| minSdk / targetSdk / compileSdk | 29 / 36 / 37 |
| Debug APK | 29,815,524 bytes |
| Release APK | **2,154,554 bytes（2.05 MiB）** |
| Release 签名 | APK Signature Scheme v2，Android Debug 证书（仅预览） |
| R8 / Resource Shrink | 开启 |
| 原生库 | 仅 Compose 的 `libandroidx.graphics.path.so`，约 0.04 MiB |
| 打包 Python/NapCat/浏览器/模型 | **没有** |

构建命令：

```powershell
$env:JAVA_HOME = "C:\Program Files\Eclipse Adoptium\jdk-25.0.4.101-hotspot"
cd mobile
.\gradlew.bat --no-daemon assembleRelease
```

验签：

```text
apksigner verify --print-certs app-release.apk
V2 Signer: CN=Android Debug
package: com.openqqdot.mobile
versionName: 0.1.0-phase2
```

## 实际功能

- 概览、聊天、Agent、任务、设置 5 个页面
- 读取桌面 `vnext_bridge.py` 的真实 JSON
- Bridge 地址和可选令牌本地保存
- 无桥接时明确显示“无法连接”，不生成假数据
- Release 包体积从旧 APK 223/246 MiB 降至 **2.05 MiB**

## 体积对比

| 版本 | 大小 |
|---|---:|
| 旧 `OpenQQDot-foss-1.0.0.apk` | 223,039,112 bytes（212.7 MiB） |
| 旧 `OpenQQDot-gms-1.0.0.apk` | 246,160,247 bytes（234.8 MiB） |
| 新 Release | **2,154,554 bytes（2.05 MiB）** |
| 新 Debug | 29,815,524 bytes（28.4 MiB） |

新包远低于 25 MB 预算。差异来自不再打包四 ABI ONNX、96MB 地理库、
cloudflared/ngrok、30 个 dex 和 Python 后台。

## 未验证 / 未实现

- `adb devices` 为空：没有真机或模拟器，**没有安装运行截图**
- 没有 DeviceGateway 配对、HTTPS/WSS、设备撤销
- 没有聊天写入、系统通知、语音、Health Connect
- 没有账号/ProviderRouter
- 明文 HTTP 只用于本地开发
- Release 仅用 Debug 证书，不能作为正式发布包

## 下一步

1. 连接 Android 真机，安装 `app-release.apk`，验证局域网访问桌面 Bridge。
2. 将明文 HTTP 替换为 DeviceGateway 的配对令牌 + TLS/WSS。
3. 加聊天读写、通知、语音流和 Health Connect 权限流程。
4. 用真实设备测冷启动、内存、耗电与断线恢复。
