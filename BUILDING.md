# Open QQ Dot 构建与验证说明

本项目维护同一份应用源码，打包时分别生成桌面应用、公开源码 ZIP、绿色包和 Windows 安装器。正式蓝色安卓客户端位于 `android/`。

## 普通用户

无需先安装 Python。Windows 用户从[发行页](https://github.com/lby749/Open-QQ-Dot/releases/tag/v1.0.0-beta.3)下载一键配置安装器，或完整解压绿色包并运行其中的 `Open QQ Dot.exe`。模型、账号、外部服务和可选 QQ 运行时按需要配置。

后续候选修订的 Codex 一键配置使用校验过的官方独立安装脚本，无需预装 Node.js；外部工具下载和账号调用仍需要可用网络。不要把应用首装、外部工具安装与账号实际收发当成同一项通过，最新边界见 [安装器复核](docs/INSTALLER_ACCEPTANCE_20261112.md)。

## 源码开发环境

应用支持 Python 3.11—3.13，安装器缺少合适的本机 Python 时准备私有 Python 3.12。先建立独立环境，再安装界面依赖与测试工具：

```text
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-ui-core.txt pytest
.venv\Scripts\python -m pip install --no-deps -r requirements-ui-qfluent.txt
```

macOS/Linux 请改用 `.venv/bin/python`。Fluent 组件需要单独安装；只安装基础 requirements 不能收集全部界面测试。使用 `--no-deps` 是为了沿用已装的 Qt Essentials/Addons，避免额外引入未使用的 WebEngine。

本地向量、语音、浏览器及其他模型的依赖按对应 `requirements-*.txt` 安装；缺少可选能力时，应记录跳过原因，不能把跳过写成验收通过。

## 验证

```text
python -X utf8 -m pytest --collect-only -q tests
python -X utf8 -m pytest -q tests
python -X utf8 tools/run_release_checks.py
python -X utf8 tools/check_license.py
python -X utf8 tools/package_release.py --check-only
```

回归入口使用 pytest 执行混合用例，确保隔离夹具和普通函数用例生效。测试只能使用临时身份和资料目录。真实模型、QQ、摄像头及实体手机另行授权并验证；源码测试不等于真实环境全部验收。

## 公开源码与应用包

```text
python tools/package_release.py --root . --output ../OpenQQDot-source.zip
python tools/verify_release_archive.py --source ../OpenQQDot-source.zip
python tools/package_release.py --root . --include-launcher --app-only --output ../OpenQQDot-portable.zip
```

源码包包含应用源码、蓝色安卓源码、测试、工具、公开文档及第三方许可证。过滤运行数据、账号与登录态、工作区、日志、模型权重、本机依赖环境、签名私钥，以及 `build`、`target` 和编译类文件等缓存。安卓源码的 `data` 包是程序源码，仍需保留；不能为过滤私人数据而删掉它。

绿色包与安装负载使用 `--app-only`，不附带测试和开发工具。启动 EXE 必须与完整资源目录一起使用。

## Windows 安装器

使用 Windows x64、uv 与 PyInstaller；构建环境需要上述依赖及合法的第三方授权文件：

```text
python tools/build_windows_installer.py
```

安装器内嵌经过过滤的应用负载，在目标目录建立私有环境。升级和修复保留用户数据。源码、安装器负载、最终附件和 Git 仓库需要分别核对，不能只依赖 Git 忽略规则。

## 安卓与其他平台

蓝色 APK 的版本、签名和构建步骤见 [安卓构建说明](android/README_构建与版本.md)，实际边界见 [安卓发布检查](docs/ANDROID_BETA3.md)。签名私钥与 `local.properties` 不提交。

macOS/Linux 目前提供源码启动入口，真实图形桌面、系统权限和原生发行包仍需分别验收。完整限制见 [KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md)。

感谢您的支持，如果有帮助的话，请点一个 STAR，谢谢。当前项目仍不完善，欢迎通过[问题反馈与建议](https://github.com/lby749/Open-QQ-Dot/issues/new/choose)提出改进，非常感谢。
