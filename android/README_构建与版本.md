# Open QQ Dot 蓝色手机版（BETA）

这是蓝色正式手机版的原生源码，入口为 `FriendHouseActivity`。
页面使用电脑端 `house_web.html`，并保留扫码配对、手机权限、手机本机 MCP 服务和主动连接电脑的中继。
项目根目录 `mobile/` 是另一套历史原生预览，不能替代本目录构建正式手机版。

## 来源与升级

本目录保留原蓝色好友入口。上游 Android Remote Control MCP、ngrok 等许可保留在源码中。
本轮移除了对机器本地 `vendor/**/target/*.jar` 的依赖，直接编译随包附带的 ngrok Java 源码。
不需要随源码附带 Rust 编译缓存、预编译的目标目录或签名私钥。

- 包名：`com.danielealbano.androidremotecontrolmcp`
- 入口：`com.danielealbano.androidremotecontrolmcp.ui.FriendHouseActivity`
- 本轮版本：`versionCode=6`、`versionName=1.0.0-beta.4`
- 系统要求：Android 13（API 33）及以上，arm64-v8a。
- `foss` 不包含 Google OCR；`gms` 包含 Google OCR / 相关服务支持。
  两种发行包使用相同包名，不能同时安装。覆盖安装须使用相同证书，不要先卸载。

本机旧发布包与本轮验收包的证书 SHA-256 为
`47577c67a901beefc941a2d793cb71d139bc79ca4ce2b1b48106ea40e93b2852`。
这是用于保持已有测试版升级兼容的 Android Debug 证书；源码不包含私钥。
新维护者应使用自己的发行签名，不能宣称不同证书的 APK 可以直接覆盖已有安装。

## 构建

使用 JDK 21、Android SDK 37 与 Build Tools 37；Gradle 包装器已随源码提供。
在 `local.properties` 配置自己的 `sdk.dir`，该文件不要提交。首次构建需联网解析 Maven 依赖；
依赖已缓存时使用 `--offline`，不需要重复下载 SDK 或依赖。

```powershell
./gradlew.bat '--offline' '--no-daemon' '-PVERSION_CODE=6' '-PVERSION_NAME=1.0.0-beta.4' 'assembleFossRelease' 'assembleGmsRelease'
```

输出位于 `app/build/outputs/apk/{foss,gms}/release/`。
在 Windows PowerShell 中需要引用带点号的版本参数，避免被拆分为任务名。
发行签名配置沿用原工程 `keystore.properties` 机制；未配置时使用本机 Android debug keystore。
不要把 `keystore.properties`、`.android`、私钥、真实配对令牌、构建缓存或个人数据放进发布源码。

更新、项目源码和反馈入口与电脑端共用上一级 `release_source.json` 的 `repository`，本轮为
`lby749/Open-QQ-Dot`。缺失、格式错误或非法仓库名时，不会向上游组件仓库检查更新。
若独立复制本目录构建，可显式传入 `-PRELEASE_SOURCE_FILE=配置文件路径`；此配置仅含公开仓库名。
BETA 构建通过发行列表选取语义版本最高的非草稿记录，正式版仍使用正式发行接口；只接受本项目的发行链接。不同 BETA 的通知保留完整版本号，不折叠为同一个 1.0.0。来源、网络或解析失败时不会冒充检查成功。历史结果见 `../docs/ANDROID_BETA3.md`，本轮范围见发行附件 `Validation.json`。

默认精简包已排除 cloudflared、ngrok 与 ONNX 的可选原生库。
这些可选能力不能因页面存在而被视作默认包已验证；若要构建完整原生功能包，需自行准备对应
JNI 库并显式使用 `-PfullApk`。此轮没有验收该完整变体。
测试模块、基准模块和 test 依赖属于源码与构建工具，不是默认 APK 中的额外应用功能。
ngrok 的可选主机 JNI 测试夹具只在独立构建后存在时加入 test runtime，不能让缺失的机器本地 JAR 阻断普通单元测试。

`privacy-benchmark/src/main/resources/corpus_c.jsonl` 是上游公开的 126 条隐私检测对抗测试语料，
包含用于识别测试的凭据形状文本，不是本项目账号数据。该文件未进入 APK。
已与 [上游固定提交](https://github.com/danielealbano/android-remote-control-mcp/blob/16f39717ce0969aa81a4ec132ba1cad861ba46cc/privacy-benchmark/src/main/resources/corpus_c.jsonl)
逐字节核对（统一 LF 后 SHA-256：`8276f1de054daceef0d9e78fca81fea7e0ebed6bac5ac7e034684377077e6945`）。
发布扫描只可为此路径与固定哈希做明确审核，语料变化后应重新核查，不能放宽其他配置或真实凭据扫描。

## 验收边界

此手机版依赖电脑端服务，不是可脱离电脑独立运行的手机智能体。
本轮使用独立 Android 15 模拟器验收；实体手机、真实摄像头扫码质量、厂商后台保活和 Google OCR
准确率仍需真机验证。原生权限仍由系统及原有工具权限控制，不随电脑配对自动全部授权。
模拟器测试没有重置真实 QQ 登录，没有修改真实好友资料。

感谢您的支持，如果有帮助的话，请点一个 STAR，谢谢。
当前项目仍在测试和完善中，欢迎通过项目反馈入口提出问题和改进建议，非常感谢。
