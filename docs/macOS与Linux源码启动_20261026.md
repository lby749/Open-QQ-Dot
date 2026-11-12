# macOS 与 Linux 源码启动适配

本轮补齐源码入口的主要平台依赖。**没有在本机交付或验证 macOS .app、Linux 安装包或实际桌面运行**；当前电脑是 Windows，仅有 Docker 内部 WSL 分发，不当作 Linux 桌面环境安装程序。

## 入口

- Linux：在解压后的项目目录执行 `sh start.sh`。
- macOS：双击 `Open QQ Dot.command`，或执行 `sh start.sh`。
- 环境检查：`python3 launcher.py --check`。此检查不启动 GUI、桥接或模型；`sh start.sh --check` 同样不安装依赖。
- 仅准备环境：`sh start.sh --prepare-only`，安装 Python 与基础依赖后退出，不启动图形界面、QQ 或模型。
- 后台桥接：环境安装后使用对应虚拟环境 Python 执行 `launcher.py --bridge-run`。

首次正常启动优先复用项目环境和本机 uv，缺少时从 Astral 官方发行地址准备项目私有的 uv，再自动寻找 Python 3.11–3.13 或按需下载解释器。在 Debian/Ubuntu 上会检查 Qt 系统库；缺少时，由已经以管理员身份启动的安装过程通过 apt 准备下载工具、FreeType、Fontconfig、OpenGL、XCB、D-Bus、ALSA 与中文字体，普通用户则得到管理员安装提示，不自动提权。其他系统需要已有 curl 或 wget 及 Qt 系统运行库。不会改写系统 Python、终端配置或下载全部模型。界面依赖使用与 Windows 安装器相同的 core 清单和独立 Fluent 清单。若目录中已有 Windows `.venv`，使用独立 `.venv-posix`，不覆盖或删除 Windows 环境。安装失败时保留终端错误，不能当成已启动。

源码 ZIP 为两个 POSIX 入口保留可执行位。部分解压软件仍可能丢失权限，可手动执行 `chmod +x start.sh 'Open QQ Dot.command'`。macOS 系统安全提示、实际 GUI/托盘依赖、Linux 图形显示环境仍需在目标机器核验。

## 已处理

- Windows 继续使用原有命名互斥锁，兼容已经运行的旧版本；POSIX 使用自动随进程退出释放的文件锁，残留锁文件不会被当作活进程。
- 桥接子进程不再强依赖 Windows `CREATE_NO_WINDOW` 常量。
- 本地命令工具按平台选择 Windows PowerShell 或 macOS/Linux `/bin/sh`；原有权限和超时检查保留。
- 子进程解释器能寻找 POSIX 虚拟环境路径，不会误选复制过来的 Windows EXE。
- Tk/pystray 等旧面板依赖延迟到实际回退时导入，Qt/后台入口不再因未安装 Tk 提前失败。
- 非 Windows 不调用 PowerShell 自动启动 NapCat，也不走 PowerShell 桥接状态脚本。

macOS/Linux 的 NapCat 需要按目标环境手动启动并登录，配置项目中的 `napcat.local.json`。自动安装 NapCat、原生包签名、开机服务和多 QQ 账号并行尚未完成。

## 验证边界

- 当前 Windows 的 `launcher.py --check` 返回依赖齐全、模型请求 0、服务启动 false。
- Windows 回归验证了命令路由、无 Windows 进程标志时的创建参数、原有启动器/桥接兼容和 ZIP 可执行位。
- 两个 shell 入口通过 Git Bash 的语法检查；这不是 Linux/macOS 实际启动证明。
- 新增 `portable-startup.yml`，在未来 GitHub CI 的 Windows、Ubuntu、macOS 上检查基础入口与 POSIX 锁。当前本地没有 Git 远程发布，此 CI 未实际执行，不把配置文件当作验收通过。

---

感谢您的支持，如果有帮助的话，请点一个 STAR，谢谢。
