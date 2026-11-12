"""CLI for task capsules: list, inspect, import, export, test, run, learn.

Examples:
  python tools/capsule_cli.py list
  python tools/capsule_cli.py inspect my.oqtc
  python tools/capsule_cli.py import my.oqtc --friend 12345
  python tools/capsule_cli.py export --friend 12345 --workflow wf_x --output my.oqtc
  python tools/capsule_cli.py run --friend 12345 --capsule cap_xxx
  python tools/capsule_cli.py learn --friend 12345 --goal goal_xxx
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import task_capsule  # noqa: E402


def manager_and_store():
    from task_manager import TaskManager
    from task_workflows import WorkflowStore
    manager = TaskManager.get_instance()
    return manager, WorkflowStore(manager)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Open QQ Dot 任务胶囊工具")
    parser.add_argument("--home", default=os.environ.get("QQ_BRIDGE_HOME", str(Path(__file__).resolve().parents[1])))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list")

    inspect = sub.add_parser("inspect")
    inspect.add_argument("path")

    install = sub.add_parser("import")
    install.add_argument("path")
    install.add_argument("--friend", default="")

    export = sub.add_parser("export")
    export.add_argument("--friend", required=True)
    export.add_argument("--workflow", required=True)
    export.add_argument("--output", required=True)
    export.add_argument("--title", default="")
    export.add_argument("--description", default="")
    export.add_argument("--skill", action="append", default=[])

    test = sub.add_parser("test")
    test.add_argument("capsule_id")

    run = sub.add_parser("run")
    run.add_argument("--friend", required=True)
    run.add_argument("capsule_id")
    run.add_argument("--version", type=int, default=0)

    learn = sub.add_parser("learn")
    learn.add_argument("--friend", required=True)
    learn.add_argument("--goal", required=True)
    learn.add_argument("--output", default="")
    learn.add_argument("--skill", action="append", default=[])

    args = parser.parse_args(argv)
    home = Path(args.home).resolve()
    if args.command == "list":
        print(json.dumps(task_capsule.list_capsules(home), ensure_ascii=False, indent=1))
    elif args.command == "inspect":
        print(json.dumps(task_capsule.inspect_capsule(Path(args.path)), ensure_ascii=False, indent=1))
    elif args.command == "import":
        print(json.dumps(task_capsule.install_capsule(home, Path(args.path), owner=args.friend),
                         ensure_ascii=False, indent=1))
    elif args.command == "export":
        _, store = manager_and_store()
        print(json.dumps(task_capsule.export_capsule(
            home, store, args.friend, args.workflow, Path(args.output), title=args.title,
            description=args.description, skills=args.skill), ensure_ascii=False, indent=1))
    elif args.command == "test":
        print(json.dumps(task_capsule.test_capsule(home, args.capsule_id), ensure_ascii=False, indent=1))
    elif args.command == "run":
        print(json.dumps(task_capsule.run_capsule(home, args.friend, args.capsule_id,
                                                  version=args.version or None), ensure_ascii=False, indent=1))
    elif args.command == "learn":
        _, store = manager_and_store()
        output = Path(args.output) if args.output else None
        print(json.dumps(task_capsule.learn_capsule(home, args.friend, args.goal, store, output,
                                                    skills=args.skill), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
