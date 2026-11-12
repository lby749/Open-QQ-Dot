import json
import sqlite3

import pytest

import local_retrieval_engine as lre
import memory_hub


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(lre.LocalHybridSearchEngine, 'embed_documents', lambda *_: None)
    engine = lre.get_local_retrieval_engine(tmp_path / 'retrieval_engine.sqlite3')
    return lre.TaskExperienceStore(engine)


def test_other_friends_shared_candidates_cannot_push_private_experience_out_of_recall(store):
    own = store.record_experience('own-task', 'alice', 'default', 'codex', '', [], '数据库锁问题', {},
        '实际写入失败，先检查事务', lre.ExperienceResultStatus.FAILED_CONFIRMED)
    # A different owner floods the shared feed with later, unsigned claims.
    with store.engine._get_connection() as db:
        row = db.execute('SELECT * FROM task_experiences WHERE experience_id=?', (own.experience_id,)).fetchone()
        columns = list(row.keys())
        for index in range(450):
            values = dict(row)
            values.update(experience_id=f'foreign-{index}', owner_id='bob', is_shared=1,
                          lifecycle_state='VALIDATED', verifier_signature='', updated_at='2099-01-01')
            db.execute('INSERT INTO task_experiences('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',
                       [values[column] for column in columns])
    hits = memory_hub._experience_items(store.engine.db_path, 'alice', 'default')
    assert len(hits) == 1 and hits[0]['source']['id'] == own.experience_id
    assert not hits[0]['source']['verified']


def test_context_reads_skip_global_scrub_and_vectors_but_still_reject_bad_signatures(store, monkeypatch):
    own = store.record_experience('own-task', 'alice', 'default', 'codex', '', [], '任务', {},
        '未验证结果', lre.ExperienceResultStatus.EXECUTION_FINISHED)
    with store.engine._get_connection() as db:
        db.execute("UPDATE task_experiences SET lifecycle_state='VALIDATED',verifier_signature='v1:forged',vector_json=? WHERE experience_id=?",
                   (json.dumps([1.0]*10000), own.experience_id))
    monkeypatch.setattr(lre.TaskExperienceStore, 'sanitize_historical_experiences',
                        lambda *_: pytest.fail('普通检索不应全库读写清理'))
    reads = []
    original = sqlite3.connect
    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        def authorize(action, table, column, *_):
            if action == sqlite3.SQLITE_READ and table == 'task_experiences':
                reads.append(column)
            return sqlite3.SQLITE_OK
        connection.set_authorizer(authorize)
        return connection
    monkeypatch.setattr(sqlite3, 'connect', connect)
    hits = memory_hub._experience_items(store.engine.db_path, 'alice', 'default')
    assert hits[0]['kind'] == 'task_log' and not hits[0]['source']['verified']
    assert reads and 'vector_json' not in reads
    with original(store.engine.db_path) as db:
        assert db.execute('SELECT lifecycle_state FROM task_experiences').fetchone()[0] == 'VALIDATED'


@pytest.mark.parametrize('metadata', ['not-json', '[]'])
def test_one_malformed_experience_does_not_break_other_private_references(store, metadata):
    bad = store.record_experience('bad', 'alice', 'default', 'codex', '', [], '坏记录', {},
        '不能污染其他记录', lre.ExperienceResultStatus.FAILED_CONFIRMED)
    good = store.record_experience('good', 'alice', 'default', 'codex', '', [], '有效失败', {},
        '仅作提醒', lre.ExperienceResultStatus.FAILED_CONFIRMED)
    with store.engine._get_connection() as db:
        db.execute('UPDATE task_experiences SET environment_conditions=? WHERE experience_id=?', (metadata,bad.experience_id))
    hits = memory_hub._experience_items(store.engine.db_path, 'alice', 'default')
    assert [hit['source']['id'] for hit in hits] == [good.experience_id]
