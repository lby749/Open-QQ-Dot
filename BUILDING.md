# Building and Testing Open QQ Dot

Open QQ Dot has **one source tree**. The same tree builds the desktop application, the public source archive, the green package, and the Windows installer. There is no separate "application source" copy.

## Requirements

- Windows 11 x64 for the installer build and the real GUI/QQ checks.
- Python 3.11, 3.12, or 3.13. The installer uses an isolated `.venv` (managed by `uv` when no compatible system Python is available).
- Node.js only if you build the optional NapCat runtime; it is **not** required to build or run Open QQ Dot itself.
- `uv` for the installer build (`tools/build_windows_installer.py`).
- PyInstaller for the Windows installer (installed by the build script's environment).
- Linux/macOS source checks use Python 3.12 and do not require PySide6 or Tk; Qt-only tests are conditionally skipped.

## One-time environment

```bash
python -m venv .venv
# Windows: .venv\Scripts\python -m pip install -r requirements.txt
# Linux/macOS: .venv/bin/python -m pip install -r requirements.txt
```

Optional model, voice, browser, and UI dependencies are in the separate `requirements-*.txt` files. They are not required for source collection or packaging.

## Verify the source

```bash
python -m pytest --collect-only -q tests
python -m pytest -q tests
python tools/check_license.py
python tools/package_release.py --check-only
```

`--check-only` scans the complete public source tree, including `tests/`, `tools/`, and `docs/`. It must not report a secret or private-data leak.

## Build the public source archive

```bash
python tools/package_release.py --root . --output ../OpenQQDots-原代码.zip
```

This includes the application source, `tests/`, `tools/`, public docs, `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `BUILDING.md`, requirements files, and `THIRD_PARTY_NOTICES.md`. It excludes runtime data, virtual environments, model weights, logs, shortcuts, generated test artifacts, NapCat itself, and all `.exe` launchers/binaries.

## Build the installed application / green package

The installed application must not ship tests, build tools, or developer docs:

```bash
python tools/package_release.py --root . --include-launcher --app-only --output ../application.zip
```

`--app-only` excludes `tests/`, `tools/`, `docs/`, `examples/`, and `.github/` while retaining the application, launcher, assets, licenses, README, and notices. The installer build calls this mode automatically.

## Build the Windows installer

```bash
python tools/build_windows_installer.py             # app payload, no NapCat runtime
python tools/build_windows_installer.py --with-runtime  # only with a local qq-runtime tree
```

The installer embeds `application.zip`, creates an isolated `.venv` on the target machine, and preserves `friends/`, `data/`, `workspaces/`, `exports/`, and `capsules/` during upgrade and uninstall. It never downloads or bundles NapCat unless the operator explicitly provides/accepts a runtime.

## Repository layout

```text
main.py, house_qt/, assets/...    application and Qt desktop UI
tests/                            regression and platform tests
tools/                            packaging, license, release verification, build helpers
docs/                             architecture and feature documentation
windows_installer.py, installer_ui.py, "Open QQ Dot.spec"
                                  Windows installer source
requirements*.txt                 runtime and optional dependency sets
```

## Platform notes

- Linux: Qt/Tk tests skip when those runtimes are absent; DPAPI, PowerShell, and DotLink launcher tests are Windows-only. Use the Linux commands above and expect conditional skips, not collection errors.
- Windows: run the full suite with `python -X utf8 -m pytest -q tests`; the installer lifecycle and GUI/QQ checks require Windows.
- API key storage uses Windows DPAPI. On non-Windows platforms the program reports that secure key storage is unavailable instead of crashing.
