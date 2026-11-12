# Open QQ Dot Contributing Guide

Thank you for your interest in contributing to Open QQ Dot! We welcome community contributions, including bug fixes, architectural improvements, documentation updates, and new model backend adapters.

To preserve codebase quality, system stability, and user privacy, please read these guidelines before submitting a pull request.

---

## 1. Development Environment Setup

1. **Python Version**: Python 3.11+ is required (Python 3.12 or 3.13 recommended).
2. **Environment Initialization**:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   pip install -r requirements-ui.txt
   ```
3. **Optional Model Dependencies**:
   ```powershell
   pip install -r requirements-local-models.txt
   ```

---

## 2. Coding Standards & Architectural Conventions

- **Data Privacy & Security**:
  - Never commit real user credentials, tokens, or personal databases (`friends.json`, `data/*.sqlite3`, `*.local.json`).
  - All local file manipulations must resolve paths using `Path.resolve()` and verify confinement within the friend workspace (`workspaces/<QQ>/`).
  - Terminal command executions must strictly check `commands_enabled` permissions and enforce timeouts.
- **Concurrency & Deadlock Prevention**:
  - Decouple singleton locks from database transactions.
  - State mutations must use private reentrant locks (`RLock`).
  - Do not make blocking network calls while holding synchronization locks.
- **Objective Verification**:
  - Avoid trusting self-reported model claims. Verify deliverables via file system inspection, AST analysis, or automated tests.

---

## 3. Pull Request Workflow

1. Fork the repository and create a descriptive branch:
   ```bash
   git checkout -b fix/task-cancellation-leak
   ```
2. Implement your changes adhering to existing design patterns.
3. Run the automated test suite to ensure no regressions:
   ```powershell
   python -m unittest discover tests
   ```
4. Run the release pre-flight checker:
   ```powershell
   python tools/run_release_checks.py
   python tools/package_release.py --check-only
   ```
5. Submit a pull request with a concise description of root cause, fix logic, and test evidence.

---

## 4. Licensing Notice

By contributing to Open QQ Dot, you agree that your contributions are licensed under the GNU General Public License v3.0 or later (GPL-3.0-or-later), the same license as the project.
