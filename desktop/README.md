# Open QQ Dot vNext Desktop (Phase 1)

This is the new Tauri 2 + React + TypeScript + Tailwind desktop shell. It is a
**read-only Phase 1 client** for the existing Python Core; the old Qt UI remains
the production UI until writes are migrated.

## Run

From the repository root, start the typed bridge:

```powershell
.venv\Scripts\python.exe -X utf8 vnext_bridge.py --port 8791
```

Then, in `desktop/`:

```powershell
npm install
npm run dev
```

Open `http://127.0.0.1:5173`. The UI reports `Bridge 已连接` only after it receives
real data from `/api/overview`; it never fills the page with mock counts.

Tauri development:

```powershell
npm run tauri dev
```

Windows Tauri compilation succeeds with the installed MSVC 14.44 toolchain
(`vcvars64.bat`). The native exe must be launched from a plain ASCII path; from
this repository path WebView2 reports `Access is denied`. Use:

```powershell
powershell -ExecutionPolicy Bypass -File tools/run_tauri.ps1
```

It stages the exe and `WebView2Loader.dll` under
`%LOCALAPPDATA%\OpenQQDot\Desktop` and starts it there.

## What is real in Phase 1

- `GET /api/health`
- `GET /api/overview`
- `GET /api/friends` (no API keys or chat bodies)
- `GET /api/agents`
- `GET /api/tasks`
- `GET /api/capabilities`

Bridge binds to `127.0.0.1` only, opens SQLite read-only, and can require
`QQDOT_VNEXT_TOKEN`.

## Not implemented

- Chat send / QQ bridge handover
- Agent creation or group collaboration in the new UI
- AccountService / ProviderRouter
- ExperienceEngine
- DeviceGateway / Android pairing
- HealthBridge
- Android client

These pages say “未接入” instead of showing mock data.
