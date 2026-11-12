# Phase 6 — Android API 35 运行验证

日期：2026-10-30  
状态：**应用在 Android 15 / API 35 上真实安装、启动、连接 Bridge；Health Connect 可见但权限流程未完成。**

## 环境

```text
AVD: qqdot_api35
Image: system-images;android-35;google_apis;x86_64
Android: 15 / API 35
APK: mobile/app/build/outputs/apk/release/app-release.apk
Bridge: http://10.0.2.2:8791
```

## 实际结果

```text
adb install -r app-release.apk  -> Success
am start com.openqqdot.mobile/.MainActivity
状态：已连接
好友：3
Agent：6
任务：19
```

截图：

`build/android-api35.png`

## Health Connect

设置页显示：

```text
Health Connect
Health Connect 可用
```

说明 API 35 的 Health Connect SDK 状态正常。

点击“授权并读取最近 24 小时”后，Health Connect 首次启动展示 onboarding；完成 onboarding 后应用返回“权限未全部授予”。本轮未完成权限页的自动化授予，因此：

- 未读取真实步数/睡眠
- 未向桌面 HealthBridge 写入真实健康事件
- 不把“Health Connect 可用”说成“健康数据已接入”

## 与 API 29 的对比

| 项目 | API 29 | API 35 |
|---|---|---|
| 应用启动 | 通过 | 通过 |
| Bridge 连接 | 通过 | 通过 |
| 手机 TTS/ASR | 通过（模拟器无麦克风） | 未重复 |
| Health Connect | 不可用 | 可用，权限未完成 |

## 权限 rationale 尝试

已增加 `PermissionsRationaleActivity` 和 `androidx.health.ACTION_SHOW_PERMISSIONS_RATIONALE` 声明，并加入 Health Connect 的 `<queries>`。重新安装后点击授权仍未弹出权限页；`getGrantedPermissions()` 仍返回不完整集合。

因此 rationale 兼容声明保留，但**没有**据此声称 Health Connect 已读取。

## 未完成

- Health Connect 权限页自动化/真实授权
- 真实健康记录读取
- 手机健康摘要上报桌面
- 真机验证
