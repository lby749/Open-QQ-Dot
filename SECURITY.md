# Security Policy

Open QQ Dot is an autonomous personal AI companion deployed locally on Windows. Because the system interfaces with local file systems, terminal command execution, multi-model APIs, and QQ messaging channels, security and privacy are paramount.

---

## 1. Supported Versions

| Version | Supported |
| :--- | :--- |
| 2.0.x (Current) | Yes |
| < 2.0.0 | No |

---

## 2. Reporting a Vulnerability

If you discover a security vulnerability or privilege boundary bypass, please report it privately. Do not open public issues for zero-day vulnerabilities.

- **Private Reporting**: Send reports to the project maintainers with a reproducible Proof of Concept (PoC).
- **Response Timeline**: Acknowledgments are provided within 48 hours, followed by a remediation plan within 7 days.

---

## 3. Security Architecture & Threat Boundaries

1. **Host Privilege Boundary**:
   - Terminal commands execute under the security context of the logged-in Windows user.
   - The workspace path (`workspaces/<QQ>/`) enforces directory traversal filtering but does not substitute for kernel-level sandboxing.
   - Command execution must remain disabled unless explicitly permitted per friend.
2. **Multi-Tenant Isolation**:
   - Web API endpoints enforce Bearer Token RBAC matching the requesting friend's QQ ID against the target resource.
   - Cross-friend read/write attempts yield immediate HTTP 403 Forbidden errors.
3. **Sensitive Data Redaction**:
   - Real keys, active databases, and user chat history are excluded from Git via `.gitignore`.
   - API keys are protected using Windows DPAPI.
