#!/bin/sh
set -eu
cd "$(CDPATH= cd "$(dirname "$0")" && pwd)"
venv_dir='.venv'
if [ -d .venv/Scripts ] && [ ! -x .venv/bin/python ]; then venv_dir='.venv-posix'; fi
runtime_python="$PWD/$venv_dir/bin/python"
if [ "${1:-}" = '--check' ]; then
  check_python="$runtime_python"
  if [ ! -x "$check_python" ]; then
    if ! command -v python3 >/dev/null 2>&1; then
      echo '环境尚未安装，请运行 sh start.sh --prepare-only。' >&2
      exit 1
    fi
    check_python='python3'
  fi
  exec "$check_python" -X utf8 launcher.py --check
fi
probe='import sqlite3, ssl, PySide6, qfluentwidgets, PIL, psutil, websockets, jsonschema, numpy, pypdf, ijson, qrcode, botpy, cryptography'
if ! "$runtime_python" -c "$probe" >/dev/null 2>&1; then
  if [ -x /sbin/ldconfig ] && command -v apt-get >/dev/null 2>&1; then
    libraries="$(/sbin/ldconfig -p)"
    missing_system=false
    for library in libfreetype.so.6 libfontconfig.so.1 libEGL.so.1 libOpenGL.so.0 libglib-2.0.so.0 libxkbcommon.so.0 libdbus-1.so.3 libasound.so.2 libxcb-cursor.so.0; do
      if ! printf '%s\n' "$libraries" | grep -F -q "$library"; then missing_system=true; fi
    done
    if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then missing_system=true; fi
    if [ "$missing_system" = true ]; then
      if [ "$(id -u)" -ne 0 ]; then
        echo '缺少界面系统运行库。请让管理员先安装 curl、ca-certificates、FreeType、Fontconfig、Qt 所需的 OpenGL / XCB / D-Bus / ALSA 运行库和中文字体，再重新启动。' >&2
        exit 1
      fi
      echo '正在准备系统下载工具、界面运行库和中文字体……'
      apt-get update
      system_packages='curl ca-certificates libfreetype6 libfontconfig1 libegl1 libopengl0 libgl1 libx11-6 libxext6 libxrender1 libsm6 libice6 libxkbcommon0 libxkbcommon-x11-0 libdbus-1-3 libnss3 libxcb-cursor0 libxcb-xinerama0 libxcb-icccm4 libxcb-keysyms1 libxcb-shape0 libxcb-render-util0 libxcb-image0 fonts-noto-cjk'
      for package in libglib2.0-0 libasound2; do
        if apt-cache show "${package}t64" >/dev/null 2>&1; then
          system_packages="$system_packages ${package}t64"
        else
          system_packages="$system_packages $package"
        fi
      done
      # All names above are fixed distribution packages; intentional word splitting.
      apt-get install -y --no-install-recommends $system_packages
    fi
  fi
  if [ -x .bootstrap/uv ]; then
    uv="$PWD/.bootstrap/uv"
  elif command -v uv >/dev/null 2>&1; then
    uv="$(command -v uv)"
  else
    mkdir -p .bootstrap
    installer="$PWD/.bootstrap/uv-install.sh"
    trap 'rm -f "$installer"' EXIT HUP INT TERM
    echo '正在下载项目私有安装工具，不修改系统 Python 或终端配置。'
    if command -v curl >/dev/null 2>&1; then
      curl --fail --location --silent --show-error --connect-timeout 15 --max-time 90 \
        https://github.com/astral-sh/uv/releases/download/0.13.5/uv-installer.sh --output "$installer"
    elif command -v wget >/dev/null 2>&1; then
      wget --quiet --timeout=90 --tries=1 https://github.com/astral-sh/uv/releases/download/0.13.5/uv-installer.sh -O "$installer"
    else
      echo '缺少系统下载工具 curl 或 wget，无法准备环境。' >&2
      exit 1
    fi
    UV_UNMANAGED_INSTALL="$PWD/.bootstrap" sh "$installer"
    rm -f "$installer"
    trap - EXIT HUP INT TERM
    uv="$PWD/.bootstrap/uv"
  fi
  if [ ! -x "$runtime_python" ]; then
    "$uv" venv --python '>=3.11,<3.14' "$venv_dir"
  fi
  echo '首次启动：正在为当前系统安装项目依赖，保留现有模型与好友数据。'
  "$uv" pip install --python "$runtime_python" -r requirements-ui-core.txt
  "$uv" pip install --python "$runtime_python" --no-deps -r requirements-ui-qfluent.txt
  "$runtime_python" -c "$probe"
fi
if [ "${1:-}" = '--prepare-only' ]; then
  echo '环境已准备好。'
  exit 0
fi
exec "$runtime_python" -X utf8 launcher.py "$@"
