# 安装与运行

Open QQ Dot 是本地运行的 Windows 桌面程序。源码使用 GPL-3.0-or-later 分发。

## 环境要求

- Windows 10/11 64 位
- Python 3.11–3.12（默认使用 3.12；AMD DirectML 需要 3.11/3.12）
- 可选：Chrome（网页端模型）
- 可选：Tailscale（手机远程配对）
- 可选：NVIDIA 显卡（本地语音克隆）

## 安装方式一：安装器

1. 下载 `OpenQQDots-Setup.exe`。
2. 选择独立的新目录，不要覆盖旧运行目录。
3. 安装器会创建 Python 环境并安装依赖，不会下载 QQ 运行时。
4. 安装器**不会**下载 QQ 直连运行时（NapCat）。安装完成后启动，进入「QQ 直连」点击连接；程序会先检测本机已有 NapCat。检测到时会询问是否直接使用（不复制、不修改其 JS 文件）；没有时才弹出下载确认，确认后从 NapCat 官方 GitHub 下载并自动配置，同时保存完整 NapCat 许可证、来源、版本和 SHA-256。

安装/升级不会复制 API Key、QQ 登录态、聊天记录或 NapCat 运行时到安装包。

## 安装方式二：源码运行

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-ui.txt
python launcher.py --check
python launcher.py
```

需要网页端模型时：

```powershell
pip install -r requirements-browser-optional.txt
```

需要本地检索/语音模型时，在设置页选择下载，或安装：

```powershell
pip install -r requirements-local-models.txt
```

## 首次配置

1. 打开「QQ 直连」登录 QQ。
2. 添加第一位好友，完成三步快速配置。
3. 在好友状态页生成手机配对二维码。
4. 手机和电脑加入同一 Tailscale 网络后扫码。

## 数据位置

```text
data/                 运行数据库
friends/<QQ>/         好友配置、记忆与聊天记录
workspaces/<QQ>/      好友工作区
exports/              导出文件
```

安装包和 Git 仓库不包含这些目录。

## 升级

- 安装器升级会保留 `data/`、`friends/`、`workspaces/` 和 API Key。
- 升级前建议在「迁移助手」中创建 `.oqdbackup`。
- 大版本升级前先备份整个运行目录。

## 卸载

- 使用安装器卸载或 Windows「应用和功能」。
- 卸载不会自动删除 `data/`、`friends/`、`workspaces/`，需要手动清理。
- 清理前确认已导出聊天记录和记忆。

## 许可证

GNU General Public License v3.0 or later。完整文本见 [LICENSE](LICENSE)，第三方依赖见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
