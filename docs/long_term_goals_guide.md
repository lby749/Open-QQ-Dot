# Long-Term Goals User Guide

Open the "Task Kanban & Approval Center" in the House dashboard to view "Long-Term Goals" and "Create Goal". These workflows inherit the active friend's model configurations, workspace paths, and tool privileges.

---

## 1. Creation and Execution Flow

1. **Goal Specification**: Define the objective, authorized scope, and high-level acceptance criteria in "Create Goal".
2. **Stage Planning**: Enter manual stage steps using the format `Title | Execution Details`, or trigger "Generate Stage Draft with AI".
3. **Draft Inspection**: The AI-generated plan appears in the stage preview table. Review all subtasks carefully and click "Confirm Plan and Create Goal" to materialize the stages into the task queue.
4. **Autonomous Execution**: Stages execute sequentially through the background TaskManager engine. Deliverables (code, files, tests) undergo verification via `TaskVerifier`. Stages flagged for manual judgment require confirmation in the UI.
5. **Acceptance & Completion**: Inspect progress, output artifacts, and audit trails in "Long-Term Goals". Once all stages pass verification, provide final review notes to transition the goal to `COMPLETED`.

The background worker must be running for tasks to execute. Tasks remain queued safely without progress loss if the computer shuts down.

---

## 2. Hard Budgets and Execution Safeguards

- Goals can be paused, resumed, or terminated at any time. Individually paused stages do not automatically resume upon goal resumption.
- Reboots do not re-execute completed stages; interrupted subtasks pause with intact checkpoints, awaiting explicit operator action.
- Token budgets and invocation limits are allocated across stages. Hard budgets block further execution upon exhaustion.
- Natural language scopes do not expand underlying OS permissions. File and terminal access are strictly governed by the friend's workspace settings.
