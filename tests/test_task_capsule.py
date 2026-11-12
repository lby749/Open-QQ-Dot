import json
import zipfile
from pathlib import Path

import pytest

import task_capsule
from task_manager import TaskManager, TaskStatus
from task_workflows import WorkflowStore


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("QQ_BRIDGE_HOME", str(tmp_path))
    directory = tmp_path / "friends/12345"
    directory.mkdir(parents=True)
    (directory / "friend.json").write_text(json.dumps({
        "qq_id": "12345", "computer_tools": {"enabled": True, "commands": True},
        "model_api": {"provider": "codex", "active_providers": ["codex"]}}), "utf-8")
    manager = TaskManager(tmp_path / "data/tasks.sqlite3", tmp_path)
    store = WorkflowStore(manager)
    yield manager, store, directory
    manager.stop_worker()


def definition():
    return {"scope": "只在当前项目内", "criteria": "两个阶段客观验收通过", "max_calls": 4, "token_budget": 4000,
            "steps": [
                {"title": "整理", "description": "整理数据，返回 first 字段", "category": "data", "backend": "codex",
                 "spec": {"expected_keys": ["first"], "test_command": "python -c \"print(1)\""}},
                {"title": "复核", "description": "复核数据，返回 second 字段", "category": "data",
                 "spec": {"expected_keys": ["second"]}}]}


def test_export_inspect_install_roundtrip(tmp_path, setup):
    manager, store, directory = setup
    workflow_id = store.save("12345", "整理复核", definition())
    skill = tmp_path / "skills" / "review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# 复核技能\n\n逐项核对字段。\n", "utf-8")
    output = tmp_path / "capsule.oqtc"
    manifest = task_capsule.export_capsule(tmp_path, store, "12345", workflow_id, output,
                                           description="测试胶囊", skills=[skill])
    assert output.is_file()
    assert manifest["requirements"]["models"]["providers"] == ["codex"]
    assert manifest["requirements"]["permissions"]["writes_workspace"] is False
    inspected = task_capsule.inspect_capsule(output)
    assert inspected["capsule_id"] == manifest["capsule_id"]
    assert inspected["definition"]["steps"][0]["title"] == "整理"
    assert inspected["samples"][0]["expect"]["keys"] == ["first"]
    assert inspected["demo"]["kind"] == "stage_plan"
    installed = task_capsule.install_capsule(tmp_path, output, owner="12345")
    assert installed["installed"] and not installed["duplicate"]
    assert (Path(installed["store_path"]) / "skills/review/SKILL.md").is_file()
    again = task_capsule.install_capsule(tmp_path, output, owner="12345")
    assert again["duplicate"] is True
    listed = task_capsule.list_capsules(tmp_path)
    assert listed[0]["capsule_id"] == manifest["capsule_id"]


def test_capsule_tampering_and_traversal_are_rejected(tmp_path, setup):
    _, store, _ = setup
    workflow_id = store.save("12345", "校验流程", definition())
    output = tmp_path / "capsule.oqtc"
    task_capsule.export_capsule(tmp_path, store, "12345", workflow_id, output)
    with zipfile.ZipFile(output) as package:
        members = {name: package.read(name) for name in package.namelist()}
    members["workflow.json"] = members["workflow.json"].replace("整理数据".encode(), "删除数据".encode())
    tampered = tmp_path / "tampered.oqtc"
    with zipfile.ZipFile(tampered, "w") as package:
        for name, data in members.items():
            package.writestr(name, data)
    with pytest.raises(task_capsule.CapsuleError, match="校验失败"):
        task_capsule.inspect_capsule(tampered)

    traversal = tmp_path / "traversal.oqtc"
    with zipfile.ZipFile(traversal, "w") as package:
        for name, data in members.items():
            package.writestr(name, data)
        package.writestr("../../evil.json", b"{}")
    with pytest.raises(task_capsule.CapsuleError, match="越界"):
        task_capsule.inspect_capsule(traversal)


def test_skill_with_secret_is_refused(tmp_path, setup):
    _, store, _ = setup
    workflow_id = store.save("12345", "含密钥", definition())
    skill = tmp_path / "bad" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("api_key = '" + "sk-" + "a" * 24 + "'\n", "utf-8")
    with pytest.raises(task_capsule.CapsuleError, match="密钥"):
        task_capsule.export_capsule(tmp_path, store, "12345", workflow_id, tmp_path / "bad.oqtc", skills=[skill])


def test_run_capsule_checks_requirements_and_queues_goal(tmp_path, setup):
    manager, store, directory = setup
    workflow_id = store.save("12345", "直接运行", definition())
    output = tmp_path / "run.oqtc"
    manifest = task_capsule.export_capsule(tmp_path, store, "12345", workflow_id, output)
    task_capsule.install_capsule(tmp_path, output)
    manager.start_worker = lambda: None
    result = task_capsule.run_capsule(tmp_path, "12345", manifest["capsule_id"], store=store)
    assert result["status"] == "queued" and result["goal_id"]
    goal = manager.get_goal(result["goal_id"])
    assert goal["status"] in ("ACTIVE", "RUNNING", "PENDING") and len(goal["tasks"]) == 2

    profile = json.loads((directory / "friend.json").read_text("utf-8"))
    profile["computer_tools"] = {"enabled": True, "commands": False}
    (directory / "friend.json").write_text(json.dumps(profile), "utf-8")
    with pytest.raises(PermissionError, match="命令"):
        task_capsule.run_capsule(tmp_path, "12345", manifest["capsule_id"], store=store)


def test_browser_demo_and_samples_validation(tmp_path, setup):
    _, store, _ = setup
    value = definition()
    value["steps"][0]["category"] = "browser"
    value["steps"][0]["spec"] = {"browser_steps": [{"action": "navigate", "url": "https://example.invalid"},
                                                   {"action": "click", "selector": "#go"}]}
    workflow_id = store.save("12345", "网页流程", value)
    output = tmp_path / "web.oqtc"
    manifest = task_capsule.export_capsule(
        tmp_path, store, "12345", workflow_id, output,
        samples=[{"name": "打开", "input": {"stage": "整理"}, "expect": {"contains": "示例"}}],
        demo={"kind": "browser_steps", "steps": [{"action": "navigate", "url": "https://example.invalid"}],
              "note": "录制的浏览器示范"})
    inspected = task_capsule.inspect_capsule(output)
    assert inspected["requirements"]["permissions"]["uses_browser"] is True
    assert inspected["demo"]["kind"] == "browser_steps"
    installed = task_capsule.install_capsule(tmp_path, output)
    assert task_capsule.test_capsule(tmp_path, installed["capsule_id"])["ok"] is True


def test_learn_capsule_from_completed_goal(tmp_path, setup):
    manager, store, _ = setup
    workflow_id = store.save("12345", "学习来源", definition())
    manager.register_custom_executor("data", lambda task, log: {
        "output": json.dumps({"first": 7} if task.title == "整理" else {"second": True})})
    goal_id = store.run(workflow_id, "12345")
    for _ in range(2):
        manager._process_single_task(manager._fetch_next_executable_task())
    assert manager.goal_action(goal_id, "12345", "complete", review="两步均通过") is True
    record = task_capsule.learn_capsule(tmp_path, "12345", goal_id, store, description="从真实运行学习")
    assert record["installed"] is True
    capsule = task_capsule.load_capsule(tmp_path, record["capsule_id"])
    assert capsule["demo"]["kind"] == "verified_run"
    assert {sample["name"] for sample in capsule["samples"]} == {"整理", "复核"}


def test_zip_archive_with_single_folder_is_accepted(tmp_path, setup):
    _, store, _ = setup
    workflow_id = store.save("12345", "压缩包导入", definition())
    archive = tmp_path / "capsule.zip"
    task_capsule.export_capsule(tmp_path, store, "12345", workflow_id, archive)
    wrapped = tmp_path / "wrapped.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(wrapped, "w") as target:
        for name in source.namelist():
            target.writestr("我的胶囊/" + name, source.read(name))
    inspected = task_capsule.inspect_capsule(wrapped)
    assert inspected["title"] == "压缩包导入"
    installed = task_capsule.install_capsule(tmp_path, wrapped, owner="12345")
    assert installed["installed"] is True
    assert task_capsule.load_capsule(tmp_path, installed['capsule_id'], owner='12345')['title'] == '压缩包导入'


def test_shared_files_keep_owner_acl_and_detect_post_install_changes(tmp_path, setup):
    _, store, _ = setup
    workflow = store.save('12345', '隔离胶囊', definition())
    archive = tmp_path / 'private.oqtc'
    manifest = task_capsule.export_capsule(tmp_path, store, '12345', workflow, archive)
    first = task_capsule.install_capsule(tmp_path, archive, owner='12345')
    identifier = manifest['capsule_id']
    assert not task_capsule.list_capsules(tmp_path, owner='67890')
    with pytest.raises(task_capsule.CapsuleError):
        task_capsule.load_capsule(tmp_path, identifier, owner='67890')
    second = task_capsule.install_capsule(tmp_path, archive, owner='67890')
    assert second['duplicate'] and second['store_path'] == first['store_path']
    task_capsule.remove_capsule(tmp_path, identifier, owner='12345')
    assert not task_capsule.list_capsules(tmp_path, owner='12345')
    assert task_capsule.load_capsule(tmp_path, identifier, owner='67890')
    installed = Path(second['store_path']) / 'workflow.json'
    installed.write_text('{}', 'utf-8')
    with pytest.raises(task_capsule.CapsuleError, match='校验失败'):
        task_capsule.load_capsule(tmp_path, identifier, owner='67890')
    task_capsule.remove_capsule(tmp_path, identifier, owner='67890')
    assert not task_capsule.list_capsules(tmp_path)


def test_run_inputs_skills_current_agent_and_no_duplicate_round(tmp_path, setup):
    manager, store, directory = setup
    workflow = store.save('12345', '实际输入', definition())
    skill = tmp_path / 'check/SKILL.md'
    skill.parent.mkdir()
    skill.write_text('逐项核对，不把未验收的结果当成功。', 'utf-8')
    manifest = task_capsule.export_capsule(tmp_path, store, '12345', workflow, tmp_path/'run.oqtc', skills=[skill])
    task_capsule.install_capsule(tmp_path, tmp_path/'run.oqtc', owner='12345')
    manager.start_worker = lambda: None
    identifier = manifest['capsule_id']
    result = task_capsule.run_capsule(tmp_path, '12345', identifier, store=store,
                                     input_text='整理本次材料 A', use_current_agent=True)
    goal = manager.get_goal(result['goal_id'])
    assert '整理本次材料 A' in goal['tasks'][0]['description']
    assert '逐项核对，不把未验收的结果当成功。' in goal['tasks'][0]['description']
    assert goal['tasks'][0]['checkpoint']['backend'] == 'codex'
    with pytest.raises(RuntimeError, match='上一轮尚未结束'):
        task_capsule.run_capsule(tmp_path, '12345', identifier, store=store, use_current_agent=True)
    with pytest.raises(RuntimeError, match='上一轮尚未结束'):
        task_capsule.run_capsule(tmp_path, '12345', identifier, store=store, use_current_agent=False)
    runs = task_capsule.capsule_runs(tmp_path, '12345', identifier, store=store)
    assert len(runs) == 1 and runs[0]['goal_id'] == result['goal_id']
    with pytest.raises(task_capsule.CapsuleError):
        task_capsule.capsule_runs(tmp_path, '67890', identifier, store=store)


def test_current_custom_api_alias_is_a_usable_capsule_provider(tmp_path, setup):
    manager, store, directory = setup
    workflow = store.save('12345', '切换来源', definition())
    manifest = task_capsule.export_capsule(tmp_path, store, '12345', workflow, tmp_path/'switch.oqtc')
    task_capsule.install_capsule(tmp_path, tmp_path/'switch.oqtc', owner='12345')
    profile = json.loads((directory/'friend.json').read_text('utf-8'))
    profile['model_api'] = {'provider': 'api'}
    (directory/'friend.json').write_text(json.dumps(profile), 'utf-8')
    manager.start_worker = lambda: None
    result = task_capsule.run_capsule(tmp_path, '12345', manifest['capsule_id'], store=store, use_current_agent=True)
    assert manager.get_goal(result['goal_id'])['tasks'][0]['checkpoint']['backend'] == 'custom'


def test_model_tools_cannot_inspect_another_friends_capsule(tmp_path, setup):
    import capsule_tools
    _, store, _ = setup
    workflow = store.save('12345', '私有胶囊', definition())
    manifest = task_capsule.export_capsule(tmp_path, store, '12345', workflow, tmp_path/'private.oqtc')
    task_capsule.install_capsule(tmp_path, tmp_path/'private.oqtc', owner='12345')
    directory = tmp_path / 'friends/67890'
    directory.mkdir()
    assert not capsule_tools.tools(directory)
    for action in ('capsule_inspect', 'capsule_test', 'capsule_run'):
        with pytest.raises(task_capsule.CapsuleError):
            capsule_tools.call(directory, action, {'capsule_id': manifest['capsule_id']})
