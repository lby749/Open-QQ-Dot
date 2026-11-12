# Open QQ Dot — Security Hardening & Independent Verification Report

Date: 2026-10-22  
Baseline: Upstream `Open-QQ-Dot (2).zip` codebase.  
Document Scope: Describes concrete code changes implemented; does not claim complete business readiness or exhaustive security certification.

---

## 1. Implemented Security Changes

1. **Approval Identity Authentication** (`approval_manager.py`, `house_web.py`):
   - `create_trusted_identity()` and `register_trusted_admin()` reject registration by name alone; callers must provide valid Web Bearer tokens, with verified identity alignment and server-side admin configuration.
   - Web routes sign approval contexts using the authenticated requester identity, not the target friend QQ account currently being viewed.
   - Approval endpoints reject unauthenticated self-declared approvers, inferred task identities, or using cancellation tokens to authorize high-risk operations.
   - `house_web.is_admin_user()` no longer treats mutable `friend.json.is_admin` as an admin ACL. Administrators must be explicitly configured in server-side `QQ_ADMINS` / `ADMIN_QQ` or local management config `house_web.local.json`.
   - **Compatibility Note**: Calling `approve_request(req, approver_id='alice')` is rejected; authentic session-signed `TrustedIdentityContext` instances are strictly required.
2. **Task and Memory Access Controls** (`task_manager.py`, `agent_memory.py`, `main.py`, `house_web.py`):
   - Task operations (pause, resume, cancel, steer, manual approval, and memory invalidation) no longer pass by default when `owner_id` is omitted; cross-tenant operations and arbitrary `is_admin=True` escalations are rejected.
   - Web entrypoints explicitly pass the authenticated owner of the target task; QQ messaging entrypoints explicitly pass the QQ user identity.
   - User memory correction invalidates specifically related inferred facts rather than wiping all non-explicit preferences.
3. **Persistent Experience HMAC Signing** (`local_retrieval_engine.py`):
   - Eliminated transient, per-startup random `_EXP_HMAC_SECRET`. Replaced with a persistent 32-byte secret `.experience_signing_key_v1` located in the database directory, using HMAC prefix `v1:` to enable cross-process verification.
   - Secret initialization utilizes exclusive file creation, refuses overwriting corrupted keys, and enforces restricted permissions to prevent verified experience degradation upon restarts.
   - **Migration Notice**: Historical experiences signed under previous ephemeral process keys fail verification and safely degrade to unverified; they must be re-verified rather than arbitrarily re-signed.
4. **Browser Site Isolation & Playwright Real Automation** (`browser_automation.py`, `browser_playwright_adapter.py`, `tool_registry.py`):
   - Replaced flat cross-site cookie strings with `MozillaCookieJar` / `HTTPCookieProcessor`, enforcing domain, path, secure, and expiry scoping.
   - Legacy unstructured `cookies.json` without domain metadata is deliberately excluded from migration.
   - Prohibits local/private address navigation by default; access requires explicit whitelist entry in `allowed_local_origins`. Added DNS pre-resolution checks to prevent domain resolution to private IPs.
   - Added dedicated worker thread integration for Playwright/Chromium: live `navigate`, `input`, `click`, `wait`, `read`, `scroll`, `upload`, and genuine PNG screenshot capture. Production `ToolRegistry` requests real browser mode, returning explicit errors if Chromium is missing rather than fabricating placeholder images.
   - Installs via `requirements-browser-optional.txt` and `python -m playwright install chromium`.
   - **Important Security Notice**: DNS pre-checks and route handlers cannot eliminate all DNS rebinding or connection-level SSRF vulnerabilities independently. Deployments allowing visits to untrusted domains should configure OS/container outbound egress firewalls and execute browsers under low-privilege accounts.

---

## 2. Independent Verification Results

- `python -m pytest -q tests/test_security_hardening_20261022.py`: **9 passed**.
- `python -m pytest -q tests/test_security_hardening_20261022.py tests/test_p0_security_rbac.py tests/test_task_manager_p1.py tests/test_round11_verification_root_fix.py`: **26 passed, 2 skipped**.
- Genuine Chromium `page.set_content()` execution: live user input and JS `onclick` DOM update to `real-javascript`; produces genuine PNG binary starting with `89 50 4E 47`, ~7.9 KB.
- Historical tests omitting identity credentials, mocking administrative escalation, or injecting domainless cookies now fail by design. Administrative permission bypasses must not be reintroduced to satisfy legacy test numbers.

---

## 3. Retained Architectural Boundaries

1. **Process-Level Isolation**: Untrusted code executing within the shared host Python process can inspect memory and call internal APIs. Production deployments should isolate untrusted agents into separate sandboxed processes or containers.
2. **Cryptographic Verification Binding**: Long-term hardening requires `TaskVerifier` to write tamper-proof receipts and artifact hashes to prevent in-process state manipulation.
3. **Browser Network Hardening**: Host-level egress firewall rules should complement URL pre-checking against DNS rebinding.

---

## 4. Deployment Instructions

- Back up operational databases and configuration prior to upgrading; never include live credentials in source distribution archives.
- Configure authentic server-side admin lists; do not rely on client-mutable `is_admin` flags in `friend.json`.
- Preserve `.experience_signing_key_v1` across deployments; never delete persistent keys during routine updates.
- Install Playwright and Chromium when browser tooling is enabled.
