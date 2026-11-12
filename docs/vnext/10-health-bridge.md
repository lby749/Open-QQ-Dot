# Phase 5 — HealthBridge 事件核心

日期：2026-10-30  
状态：**能力检测/授权/去重/撤销核心完成；没有手机健康数据，没有伪造。**

## 实现

`health_bridge.py`：

- 品牌目录：Health Connect、小米、华为、荣耀、三星、Fitbit、Amazfit、Garmin、OPPO、vivo、Apple Watch
- 范围：`sleep`、`steps`、`workout`、`activity`
- 事件：`sleep_start`、`sleep_end`、`wake`、`steps_change`、`workout_end`、`activity_change`、`sync_status`
- 适配器必须先 `report(supported=True)`，否则拒绝事件
- 用户必须显式 `authorize(adapter, scopes)`，否则拒绝 scope
- 事件按 `event_id` 去重
- 上限 500 条
- `purge(adapter)` 删除该品牌事件并清空授权
- JSON 原子写
- 只保存最小规范化字段，不保存原始手机记录

## Bridge

```text
GET /api/health-bridge
```

真实当前状态：

```json
{
  "adapters": [
    {"adapter": "health_connect", "supported": false, "authorized_scopes": []},
    {"adapter": "xiaomi", "supported": false},
    {"adapter": "huawei", "supported": false}
  ],
  "event_count": 0,
  "has_real_events": false,
  "message": "桌面端尚未收到手机能力报告"
}
```

桌面“设备”页已接这个真实状态：

![设备与健康页](<D:\Desktop\New folder (3)\Open-QQ-Dot\build\desktop-health.png>)

所有品牌显示“待手机上报”，没有假睡眠/步数/心率。

## 测试

```text
tests/test_health_bridge.py     5 passed
tests/test_vnext_bridge.py      3 passed
```

覆盖：

- 未上报能力时拒绝 ingest
- 未授权 scope 拒绝
- 去重
- 撤销/删除
- 未知品牌拒绝
- 持久化不包含 raw payload

## 上报闭环

Bridge 新增：

```text
POST /api/health/report
POST /api/health/authorize
POST /api/health/events
```

真实 PowerShell 集成测试已跑通：

```text
report health_connect supported=true
-> supported=true

authorize sleep+steps
-> authorized_scopes=[sleep, steps]

ingest sleep_end
-> accepted=true, event_id=test-event-1

/api/health-bridge
-> event_count=1
```

测试事件与测试授权已清理，当前 `event_count=0`，没有留下伪造健康数据。

## Android 端进度

已加入原生 Health Connect 读取源码：

- `mobile/app/src/main/kotlin/com/openqqdot/mobile/HealthConnectSource.kt`
- READ_SLEEP / READ_STEPS / READ_EXERCISE 权限
- Android 14+ Health Connect 客户端
- 只读取最近 24 小时的步数/睡眠/运动摘要
- 设置页有授权按钮与摘要展示
- 没有把原始记录上传到桌面

实际构建：

```text
app-release.apk  2,503,932 bytes（2.39 MiB）
compileSdk 37 / targetSdk 36 / minSdk 29
```

没有真机/模拟器，所以权限授权和读取仍未实机验证。

## 未实现

- Android 健康摘要上报到桌面
- 手机端真实设备授权与上传验收
- 厂商官方 SDK 适配器
- 手机能力上报接口
- HealthEvent 推送/主动触发
- 时区、延迟、重复记录合并策略
- Apple iOS 适配端
- 真实设备验收

## 下一步

1. Android 端接入 Health Connect 权限，读取最小范围。
2. 手机把能力与授权范围上报到 DeviceGateway。
3. 用真实睡眠/步数事件验证去重与时区。
4. 再让 ProactiveEngine 消费 HealthEvent，并加免打扰/次数限制。
