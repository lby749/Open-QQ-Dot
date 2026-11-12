"""Opens one per-friend Tk persona workbench in its own window.

The Qt desktop cannot host Tk frames, so each workbench runs in a child process started by launch():

    python persona_workbench_host.py --tool friend --friend-id 123456

Scopes follow the desktop panel: the job workbench uses the plain friend scope, the others the
scope with the friend's bound persona packs. Feature switches are not written here; the desktop applies them.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get('QQ_BRIDGE_HOME', Path(__file__).resolve().parent))
TOOLS = {
    'jobs': '人格提炼任务',
    'friend': '人格版本与示例',
    'knowledge': '兴趣、知识与贴纸',
    'feedback': '人格 A/B 对比反馈',
    'retrieval': '检索诊断（只读）',
}
_children: dict[tuple[str, str], subprocess.Popen] = {}


def launch(tool: str, friend_id: str, root: Path | None = None) -> bool:
    """Start the workbench window; False when that friend's window for the tool is still open."""
    from house_settings import python_executable
    if tool not in TOOLS:
        raise ValueError(f'未知的工作台：{tool}')
    root = Path(root or ROOT)
    key = (tool, str(friend_id))
    running = _children.get(key)
    if running is not None and running.poll() is None:
        return False
    _children[key] = subprocess.Popen(
        [python_executable(root), str(Path(__file__).resolve()), '--tool', tool, '--friend-id', str(friend_id),
         '--project-root', str(root)],
        cwd=root, env={**os.environ, 'QQ_BRIDGE_HOME': str(root)},
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    return True


def scope_for(tool: str, friend_id: str, root: Path):
    from persona_store import PersonaStore
    from persona_types import FriendScope
    scope = FriendScope.load(friend_id, project_root=root)
    if not (scope.friend_dir / 'persona.sqlite3').is_file():
        # The workbenches read this friend's persona store; create the empty one the job workbench would.
        PersonaStore(scope.friend_id, scope.friend_dir, readonly=False, project_root=scope.project_root).close()
    if tool == 'jobs':
        return scope
    from persona_pack_bindings import PackBindingStore
    bindings = PackBindingStore(scope, readonly=True)
    try:
        return bindings.bound_scope()
    finally:
        bindings.close()


def build(tool: str, window, scope):
    if tool == 'retrieval':
        from retrieval_debug_gui import RetrievalDebugWindow
        controller = RetrievalDebugWindow(window, scope.project_root)
        controller.change_friend(scope.friend_id)
        controller.friend_box.configure(state='disabled')  # this window belongs to one friend
        return controller
    module = {'jobs': 'persona_gui', 'friend': 'persona_friend_gui', 'knowledge': 'persona_knowledge_gui',
              'feedback': 'persona_feedback_gui'}[tool]
    frame = __import__(module).create_workbench(window, scope)
    frame.pack(fill='both', expand=True)
    return frame


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--tool', choices=sorted(TOOLS), required=True)
    parser.add_argument('--friend-id', required=True)
    parser.add_argument('--project-root', default=str(ROOT))
    args = parser.parse_args(argv)
    import tkinter as tk
    from tkinter import messagebox
    window = tk.Tk()
    window.title(f'{TOOLS[args.tool]} · 好友 {args.friend_id}')
    window.geometry('1180x820')
    try:
        build(args.tool, window, scope_for(args.tool, args.friend_id, Path(args.project_root).resolve()))
    except Exception as exc:
        window.withdraw()
        messagebox.showerror(f'无法打开{TOOLS[args.tool]}', str(exc), parent=window)
        window.destroy()
        return 1
    window.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
