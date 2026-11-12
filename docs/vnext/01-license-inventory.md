# Open QQ Dot vNext — 许可证与依赖清单（Phase 0）

日期：2026-10-30  
范围：仓库自有代码、Python 核心运行依赖、Windows 打包依赖、模型资产、发布 APK 中可识别的第三方库。  
方法：`requirements*.txt` + 当前 `.venv` 的 `importlib.metadata` + `licenses/` + `THIRD_PARTY_NOTICES.md` + APK 条目名。  
**这不是法律意见；缺失 SPDX/许可证文本的条目明确标记为未知，不作推断。**

## 1. 项目自身许可证

- 当前文件：`LICENSE`
- 实际内容：**MIT License**
- Copyright：`2026 Open QQ Dot contributors`

指挥书第 0.8 条称“目标许可证 Apache 2.0 只是拟议目标”。当前仓库**没有**切换到 Apache-2.0，也没有 `NOTICE` 文件。

结论：

```text
当前自有代码：MIT
vNext 目标：Apache-2.0（拟议，未执行）
```

切换前必须完成版权归属审计、第三方兼容性审计和 `NOTICE` 迁移；Phase 0 不修改。

## 2. 核心 Python 依赖

版本取当前 `.venv` 实测；许可证取包元数据/仓库 `licenses/`。

| 依赖 | 版本 | 许可证/状态 | 是否随 Windows 包分发 | 证据 |
|---|---|---|---|---|
| Python | 3.13（当前解释器） | PSF License | 是（嵌入式/运行时） | `licenses/Python-LICENSE.txt` |
| PySide6 / shiboken6 | 6.12.0 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | 是（PyInstaller） | 包元数据；`licenses/third-party/PySide6_Essentials/` |
| PySide6-Fluent-Widgets | 1.11.3 | **GPLv3** | 是 | `licenses/third-party/PySide6-Fluent-Widgets-LICENSE`；`THIRD_PARTY_NOTICES.md` |
| PySideSix-Frameless-Window | 0.8.2 | LGPLv3 | 是 | 包元数据 |
| pystray | 0.19.5 | LGPLv3 | 是 | `licenses/third-party/pystray-COPYING` |
| Pillow | 12.3.0 | 未在元数据声明；仓库有许可证文本 | 是 | `licenses/third-party/Pillow-LICENSE` |
| psutil | 7.0.0 | BSD-3-Clause | 是 | `licenses/third-party/psutil-LICENSE` |
| numpy | 2.2.6 | BSD-3-Clause（含 bundled notices） | 是 | `licenses/third-party/numpy/` |
| websockets | 16.1.1 | 未在元数据声明；仓库有许可证文本 | 是 | `licenses/third-party/websockets-LICENSE` |
| qrcode | 8.2 | BSD（元数据另含 Other/Proprietary 分类，需复核） | 是 | `licenses/third-party/qrcode-LICENSE` |
| jsonschema | 4.26.0 | 未在元数据声明；仓库有 COPYING | 是 | `licenses/third-party/jsonschema-COPYING` |
| beautifulsoup4 | 4.15.0 | MIT | 是 | 包元数据 |
| pypdf | 6.21.2 | 未在元数据声明；需补许可证文本 | 是 | `pip list` |
| sympy | 1.14.0 | BSD | 是（如打包） | 包元数据 |
| ijson | 3.5.1 | 未在元数据声明；需补文本 | 是（可选） | `requirements-persona.txt` |
| torch | 2.7.1+cu128 | BSD-3-Clause | 否（本地模型可选，不进干净源码包） | 包元数据 |
| transformers | 4.51.3 | Apache-2.0 | 否（同上） | 包元数据；`requirements-embedding-*.txt` |
| sentencepiece / safetensors / tokenizers | 见 requirements | Apache-2.0 / Apache-2.0 / Apache-2.0 | 否 | `requirements-embedding-*.txt` |
| sherpa-onnx | 1.13.8 | Apache-2.0 | 可选 | `requirements-voice.txt`；通知文件 |
| onnx | 1.23.1 | 未在元数据声明；通知文件称 Apache-2.0 | 可选 | `requirements-voice.txt`；`THIRD_PARTY_NOTICES.md` |
| playwright | 1.64.1 | 未在元数据声明；需补文本 | 可选（浏览器自动化） | 当前 `.venv` |
| PyInstaller | 6.22.0 | GPLv2-or-later + special exception | 构建工具，不作为运行库 | `licenses/PyInstaller-COPYING.txt` |

## 3. 模型与模型资产

| 资产 | 许可证 | 是否在源码包 | 备注 |
|---|---|---|---|
| BAAI/bge-m3 | MIT | 否 | 用户下载到 `data/embedding_models` |
| BAAI/bge-reranker-v2-m3 | Apache-2.0 | 否 | 同上 |
| MeloTTS zh_en | MIT（模型随附 LICENSE） | 否 | `data/tts_models` |
| Kokoro-82M v1.1-zh | Apache-2.0 | 否 | 随附 espeak-ng 数据为 GPLv3 |
| VoxCPM2 | Apache-2.0 | 否 | 独立 Python 环境，仅克隆场景 |
| 本地模型的 cppjieba 词典 | MIT | 否 | 随模型获取 |

模型权重、TTS 数据和本地 Python 环境**不在干净源码包**；发布时如携带需保留上游许可证与署名。

## 4. Windows 构建/发布

- `Open QQ Dot.spec` 使用 PyInstaller。
- `tools/build_windows_installer.py`、`dotlink/` 生成安装器。
- PyInstaller 的 GPLv2+exception 文本已存在：`licenses/PyInstaller-COPYING.txt`。
- Qt/PySide6 的 LGPL/GPL 文本存在于 `licenses/third-party/PySide6_Essentials/`、`shiboken6/`。
- **GPL 冲突风险**：`PySide6-Fluent-Widgets` 为 GPLv3，而项目当前 MIT；分发组合二进制时必须满足 GPLv3 的相应义务，现有 `THIRD_PARTY_NOTICES.md` 已提示，但没有完整源码提供/GPL 合规方案。Phase 0 不擅自改总许可证。

## 5. 发布 APK 中识别的第三方库（仅条目名证据）

两个 APK：`OpenQQDot-foss-1.0.0.apk`、`OpenQQDot-gms-1.0.0.apk`。从 `lib/` 可见：

- `libonnxruntime.so` / `libonnxruntime4j_jni.so`
- `libcloudflared.so`
- `libngrok_java.so`
- `libmlkit_google_ocr_pipeline.so`（仅 GMS 包）
- AndroidX / Kotlin 运行时库
- `assets/geo/location-db.bin`（96.1 MB）

当前仓库**没有** Android 源码、Gradle 文件、依赖锁定或 APK 内许可证清单，因此：

```text
APK 内各库的具体版本、SPDX、源码提供义务：未验证
APK 与当前仓库的构建对应关系：未验证
```

## 6. NapCat / QQ 客户端

- 仓库包含 `napcat/`（运行时、QQ 数据目录、启动脚本），但 `.gitignore` 排除 `napcat/`。
- `THIRD_PARTY_NOTICES.md` 称 NapCatQQ 为独立项目、许可证另行检查；仓库没有 NapCat 的完整 LICENSE 副本。
- 发布包不应包含 NapCat 登录态、QQ 数据或访问令牌；本轮未验证发布 ZIP 内是否完全排除。

## 7. 缺失与待补

以下条目在仓库中没有可核实的 SPDX/许可证文本，Phase 0 标记为**未知**，不得对外宣称已合规：

- `pypdf`、`playwright`、`ijson`、`websockets`、`Pillow` 的完整许可证文本/版本对应
- APK 内 onnxruntime/cloudflared/ngrok/ML Kit/AndroidX 的版本与许可证
- NapCatQQ 的再分发许可证文本
- 所有 UI 图标/截图/字体的来源与授权

## 8. Phase 0 结论

```text
自有代码：MIT（当前）
目标：Apache-2.0（未执行）
最大阻塞：PySide6-Fluent-Widgets GPLv3 与 MIT 组合分发
Android：仅有 APK，无源码/许可证清单，不能声称合规
```

在任何 Phase 1 代码迁移之前，必须先决定：

1. 继续 GPLv3 组合分发并履行 GPL 义务，或
2. 替换 PySide6-Fluent-Widgets / 获取商业许可，或
3. 将新前端迁到 Tauri/React 后不再分发当前 Qt 组合。
