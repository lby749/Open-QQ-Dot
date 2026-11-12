import sqlite3

import experience_service


def test_snapshot_reads_states_without_loading_models(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    con = sqlite3.connect(data / 'retrieval_engine.sqlite3')
    con.execute('''CREATE TABLE task_experiences (
        experience_id TEXT PRIMARY KEY, task_id TEXT, owner_id TEXT, agent_provider TEXT,
        model_name TEXT, problem_statement TEXT, action_taken TEXT, result_status TEXT,
        confidence_status TEXT, lifecycle_state TEXT, created_at TEXT)''')
    con.execute("INSERT INTO task_experiences VALUES ('e1','t1','owner','codex','m','问题','动作','success_verified','high_verified','VALIDATED','2026-01-01')")
    con.execute("INSERT INTO task_experiences VALUES ('e2','t2','owner','claude','m','问题2','动作2','unverified','low_unverified','CANDIDATE','2026-01-02')")
    con.commit()
    con.close()
    value = experience_service.snapshot(tmp_path, owner='owner')
    assert value['available'] is True
    assert value['total'] == 2
    assert value['verified'] == 1
    assert value['by_state']['VALIDATED'] == 1
    assert value['recent'][0]['id'] == 'e2'


def test_missing_database_is_explicit(tmp_path):
    value = experience_service.snapshot(tmp_path, owner='owner')
    assert value['available'] is False
    assert value['total'] == 0


def test_search_returns_only_matching_experience(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    con = sqlite3.connect(data / 'retrieval_engine.sqlite3')
    con.execute('''CREATE TABLE task_experiences (
        experience_id TEXT PRIMARY KEY, task_id TEXT, owner_id TEXT, agent_provider TEXT,
        model_name TEXT, problem_statement TEXT, action_taken TEXT, result_status TEXT,
        confidence_status TEXT, lifecycle_state TEXT, created_at TEXT, is_shared INTEGER DEFAULT 0, root_cause_or_condition TEXT)''')
    con.execute('''CREATE VIRTUAL TABLE task_experiences_fts USING fts5(
        experience_id UNINDEXED, problem_statement, action_taken, root_cause_or_condition, tools_used)''')
    con.execute("INSERT INTO task_experiences VALUES ('e1','t1','owner','codex','m','长期目标巡检','动作','success_verified','high_verified','VALIDATED','2026-01-01',0,'原因')")
    con.execute("INSERT INTO task_experiences_fts VALUES ('e1','长期目标巡检','动作','原因','')")
    con.commit()
    con.close()
    results = experience_service.search(tmp_path, '长期目标', owner='owner')
    assert len(results) == 1 and results[0]['id'] == 'e1'
    assert experience_service.search(tmp_path, 'x', owner='owner') == []


def test_record_candidate_is_unverified_never_validated(tmp_path, monkeypatch):
    import local_retrieval_engine
    engine = local_retrieval_engine.LocalHybridSearchEngine(tmp_path / 'data/retrieval_engine.sqlite3')
    monkeypatch.setattr(engine, 'embed_documents', lambda texts: [])
    monkeypatch.setattr(local_retrieval_engine, 'get_local_retrieval_engine', lambda path: engine)
    row = experience_service.record_candidate(
        tmp_path, 'WSS final 帧竞态', '等待 final 后再关闭连接', '连接关闭顺序', owner='12345')
    assert row['state'] == 'CANDIDATE'
    assert row['confidence'] == 'low_unverified'
    assert '未经验证' in row['note']
