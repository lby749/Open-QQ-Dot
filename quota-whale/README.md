# Optional: Codex / Claude Quota Widget

This is a local desktop usage display running on Windows. It inspects the current user's local Codex session snapshots and Claude Code usage data without external network transmission or credential access. Values represent local observations or approximations, not official billing invoices.

The upstream whale graphic assets are not covered under the MIT license, so this release package substitutes them with standard system whale symbols. The layout references [MeteorNOX/DeepSeek-Balance-Whale-Widget](https://github.com/MeteorNOX/DeepSeek-Balance-Whale-Widget); upstream MIT license is available in `LICENSE-MeteorNOX`.

After installing `PySide6` and `PySide6-WebEngine`, execute from project root:

```powershell
python launcher.py --quota-widget
```
