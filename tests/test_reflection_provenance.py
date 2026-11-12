from agent_memory import AgentMemoryStore
import memory_hub


def test_model_reflection_is_an_unverified_private_warning_until_validated(tmp_path):
    path = tmp_path / 'memory.sqlite3'
    store = AgentMemoryStore(path)
    store.record_failure_experience('code', '修改数据库', '可能缺少事务', '尝试原子提交', owner_id='alice')
    items = memory_hub._agent_items(path, 'alice', '')
    assert len(items) == 1 and items[0]['kind'] == 'pitfall'
    assert items[0]['source']['verified'] is False
    assert '待验证' in items[0]['text'] and '验证解决方案' not in items[0]['text']
    assert memory_hub._agent_items(path, 'bob', '') == []


def test_shared_model_guesses_do_not_become_another_owners_long_term_rules(tmp_path):
    path = tmp_path / 'memory.sqlite3'
    store = AgentMemoryStore(path)
    store.record_failure_experience('code', '未经验证', '猜测原因', '猜测改法', owner_id='system', is_shared=True)
    assert memory_hub._agent_items(path, 'alice', '') == []
    store.record_failure_experience('code', '已经验证', '实际事务失败', '已完成回归验证',
                                    owner_id='system', is_shared=True, verified=True)
    items = memory_hub._agent_items(path, 'alice', '')
    assert len(items) == 1 and items[0]['kind'] == 'verified_fix' and items[0]['source']['verified']
