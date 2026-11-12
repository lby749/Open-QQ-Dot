# Open QQ Dot vNext — APK 体积基线（Phase 0）

日期：2026-10-30  
对象：`D:\Desktop\openqqdots-最终发布\APK\`  
方法：Python `zipfile` 只读读取每个 `ZipInfo` 的 `file_size`，按路径分类；**未反编译、未签名验证、未在真机安装**。

## 1. 产物

| 文件 | 字节 | MiB | 条目数 | 修改时间 |
|---|---:|---:|---:|---|
| `OpenQQDot-foss-1.0.0.apk` | 223,039,112 | 212.7 | 1,784 | 2026-10-26 23:16:26 |
| `OpenQQDot-gms-1.0.0.apk` | 246,160,247 | 234.8 | 1,875 | 2026-10-26 23:17:16 |

没有 AAB、没有 mapping 文件、没有 APK 签名证书副本、没有构建脚本或源码归档。

## 2. 分类体积（未压缩的 `file_size` 求和）

| 类别 | FOSS | GMS |
|---|---:|---:|
| `lib/` | 193.1 MB（28 文件） | 232.2 MB（32 文件） |
| `classes*.dex` | 105.8 MB（30 文件） | 108.4 MB（30 文件） |
| `assets/` | 96.3 MB（2 文件） | 98.6 MB（27 文件） |
| `res/` | 1.1 MB（998 文件） | 1.2 MB（1,041 文件） |
| `META-INF/` | 0.2 MB（166 文件） | 0.2 MB（166 文件） |
| 其他 | 3.5 MB（560 文件） | 3.6 MB（579 文件） |

## 3. ABI 拆分

| ABI | FOSS | GMS |
|---|---:|---:|
| `x86_64` | 71.4 MB | 82.5 MB |
| `arm64-v8a` | 63.1 MB | 73.6 MB |
| `x86` | 36.8 MB | 47.8 MB |
| `armeabi-v7a` | 21.8 MB | 28.3 MB |

两个 APK 都是**四 ABI 通用包**，不是按 ABI 拆分的 App Bundle。单独 arm64 设备仍会携带 x86/x86_64/armeabi-v7a 的本地库。

## 4. 最大条目

### FOSS

```text
96.1 MB  assets/geo/location-db.bin
36.7 MB  lib/x86_64/libonnxruntime.so
36.7 MB  lib/x86/libonnxruntime.so
36.1 MB  classes25.dex
30.6 MB  lib/arm64-v8a/libonnxruntime.so
28.4 MB  lib/x86_64/libcloudflared.so
26.8 MB  lib/arm64-v8a/libcloudflared.so
21.7 MB  lib/armeabi-v7a/libonnxruntime.so
20.9 MB  classes.dex
11.3 MB  classes29.dex
6.1 MB   lib/x86_64/libngrok_java.so
```

### GMS

```text
96.1 MB  assets/geo/location-db.bin
36.7 MB  lib/x86_64/libonnxruntime.so
36.7 MB  lib/x86/libonnxruntime.so
36.0 MB  classes25.dex
30.6 MB  lib/arm64-v8a/libonnxruntime.so
28.4 MB  lib/x86_64/libcloudflared.so
26.8 MB  lib/arm64-v8a/libcloudflared.so
21.7 MB  lib/armeabi-v7a/libonnxruntime.so
20.9 MB  classes.dex
11.7 MB  classes29.dex
11.1 MB  lib/x86_64/libmlkit_google_ocr_pipeline.so
```

## 5. 膨胀来源判断（基于条目，不是猜测）

1. **ONNX Runtime 四 ABI**：约 125.7 MB（FOSS）未压缩合计，是最大单一库族。
2. **地理数据库**：`assets/geo/location-db.bin` 96.1 MB，几乎等于全部 assets。
3. **Cloudflared / ngrok 本地库**：FOSS 中 cloudflared + ngrok 合计约 70 MB（含多 ABI）。
4. **DEX 30 个**：105.8 MB，说明依赖/代码量非常大；没有 R8/资源压缩后的体积证据。
5. **GMS** 额外引入 ML Kit OCR，四个 ABI 共约 39.4 MB。

这与指挥书“手机端是 Kotlin 原生薄客户端，不能把整个 Python/Node/FFmpeg/模型权重带入 APK”的约束直接冲突。

## 6. 缺失证据

- APK 的 `applicationId`、versionCode、minSdk/targetSdk：未验证
- APK 签名方案与证书：未验证
- 是否可从当前仓库重建：**没有 Android 源码/Gradle，无法重建**
- 与 `OpenQQDot-gms` 的差异：仅从条目名推断包含 ML Kit；未做 dex 级比较
- 真机启动、登录、配对、崩溃率：未验证

## 7. Phase 0 结论

```text
APK 文件存在，但只是发布产物，不是可审计源码。
体积基线：FOSS 212.7 MiB / GMS 234.8 MiB。
最大问题：四 ABI ONNX + 96MB 地理库 + 多 ABI 隧道库 + 30 dex。
```

Phase 1 不得直接“优化 APK”而不先定位 Android 源码；在源码缺失前，任何“薄客户端已完成”的说法都是造假。
