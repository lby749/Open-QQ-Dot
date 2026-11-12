"""Friend-scoped editor and status view for interests, topics, and stickers."""
from contextlib import closing
from datetime import datetime, timezone
import sqlite3
import uuid

from persona_store import Conflict, MIGRATIONS, PersonaStore, ScopeMismatch, Unavailable
from persona_types import (DataScope, FriendScope, PrivacyView,
                           ScopeMismatch as TypeScopeMismatch, SourceOrigin, SourceRef)


class PersonaKnowledgeService:
    def __init__(self, scope, *, interest_store_factory=None, topic_store_factory=None):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('a verified FriendScope is required')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
            friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)
        self.interest_store_factory = interest_store_factory
        self.topic_store_factory = topic_store_factory
        self.drafts = {}

    def close(self):
        pass

    def _store(self, kind, readonly):
        from interest_entries import InterestStore
        from topic_knowledge import TopicKnowledgeStore
        factory = (self.interest_store_factory if kind == 'interest' else self.topic_store_factory)
        store = (factory or (InterestStore if kind == 'interest' else TopicKnowledgeStore))(
            self.scope, readonly=readonly)
        if store.scope != self.scope:
            store.close()
            raise TypeScopeMismatch('store scope differs from the complete GUI FriendScope')
        return store

    def entries(self, kind, now=None, limit=100):
        if kind not in ('interest', 'topic'):
            raise ValueError('kind must be interest or topic')
        store = self._store(kind, True)
        try:
            return tuple(dict(item, _scope=self.scope)
                         for item in store.entries(now or datetime.now(timezone.utc).isoformat(), limit))
        finally:
            store.close()

    def save_manual_interest(self, entry, expected_revision):
        from source_state import source_lock
        with source_lock(self.scope.friend_dir):
            self._preflight_backup()
            store = self._store('interest', False)
            try:
                return store.upsert(entry, expected_revision=expected_revision, manual=True)
            finally:
                store.close()

    def save_topic(self, entry, expected_revision):
        if not isinstance(entry, dict) or entry.get('source_type') != 'MANUAL':
            raise ValueError('the GUI can only create or edit MANUAL topics')
        from source_state import source_lock
        with source_lock(self.scope.friend_dir):
            self._preflight_backup()
            store = self._store('topic', False)
            try:
                return store.upsert(entry, expected_revision=expected_revision)
            finally:
                store.close()

    def tombstone(self, kind, entry_id, revision):
        if kind not in ('interest', 'topic'):
            raise ValueError('kind must be interest or topic')
        from source_state import source_lock
        with source_lock(self.scope.friend_dir):
            self._preflight_backup()
            store = self._store(kind, False)
            try:
                if kind == 'topic':
                    row = store._store.db.execute(
                        'SELECT source_type FROM topic_knowledge WHERE friend_id=? AND id=?',
                        (self.scope.friend_id, entry_id)).fetchone()
                    if row and row['source_type'] == 'PUBLIC_RESEARCH':
                        raise ValueError('PUBLIC_RESEARCH 镜像只读；可另建 MANUAL 条目')
                return store.tombstone(entry_id, revision)
            finally:
                store.close()

    def _preflight_backup(self):
        catalog = PersonaStore(self.scope.friend_id, self.scope.friend_dir, readonly=True,
                               project_root=self.scope.project_root)
        try:
            found = {row[0] for row in catalog.db.execute('SELECT version FROM schema_migrations')}
            if found != {version for version, _ in MIGRATIONS}:
                raise Unavailable('Persona sidecar schema is not current')
            backup_dir = self.scope.friend_dir / '.persona_knowledge_gui_backups'
            if backup_dir.resolve() != backup_dir or backup_dir.is_symlink():
                raise TypeScopeMismatch('backup directory redirects outside friend scope')
            backup_dir.mkdir(exist_ok=True)
            path = backup_dir / (uuid.uuid4().hex + '.sqlite3')
            with closing(sqlite3.connect(path)) as backup:
                catalog.db.backup(backup)
            return path
        finally:
            catalog.close()

    def stickers(self):
        """Bounded current-friend read snapshot; inventory rows are not sendability."""
        store = PersonaStore(self.scope.friend_id, self.scope.friend_dir, readonly=True,
                             project_root=self.scope.project_root)
        try:
            rows = store.db.execute(
                'SELECT a.asset_id,a.valid,a.declared_kind,a.local_use_allowed,'
                'a.model_analysis_allowed,a.image_redistribution_allowed,'
                'm.analysis_status,m.sticker_kind,m.unknown_reason,m.analyzed_at '
                'FROM sticker_assets a LEFT JOIN sticker_metadata m '
                'ON m.friend_id=a.friend_id AND m.asset_id=a.asset_id '
                'WHERE a.friend_id=? ORDER BY a.asset_id LIMIT 101',
                (self.scope.friend_id,)).fetchall()
            return tuple(dict(row) for row in rows[:100]), len(rows) > 100
        finally:
            store.close()

    def manual_interest(self, *, entry_id, topic, content, category, strength=0.5,
                        confidence=0.5, valid_to=None):
        ref = SourceRef(dataset_id='manual:interest_entries', import_id=entry_id,
            record_id=entry_id, message_seq=None, speaker_id='operator',
            origin=SourceOrigin.MANUAL, data_scope=DataScope.PRIVATE_FRIEND,
            privacy_view=PrivacyView.LOCAL_FULL, friend_id=self.scope.friend_id,
            event_time=None, recorded_at=datetime.now(timezone.utc).isoformat())
        return {'id': entry_id, 'topic': topic, 'content': content, 'category': category,
            'strength': strength, 'confidence': confidence, 'interest_type': 'manual',
            'source_type': 'MANUAL', 'source_refs': [ref], 'first_seen': None,
            'last_seen': None, 'locked': True, 'valid_to': valid_to}

    def manual_topic(self, *, entry_id, topic, content, summary=None):
        ref = SourceRef(dataset_id='manual:topic_knowledge', import_id=entry_id,
            record_id=entry_id, message_seq=None, speaker_id='operator',
            origin=SourceOrigin.MANUAL, data_scope=DataScope.PRIVATE_FRIEND,
            privacy_view=PrivacyView.LOCAL_FULL, friend_id=self.scope.friend_id,
            event_time=None, recorded_at=datetime.now(timezone.utc).isoformat())
        return {'id': entry_id, 'topic': topic, 'subtopic': '', 'title': topic,
            'content': content, 'summary': summary or content, 'keywords': [],
            'source_type': 'MANUAL', 'source_url': None, 'source_refs': [ref],
            'valid_from': None, 'valid_to': None, 'confidence': 0.5,
            'quality_score': 0.5, 'time_sensitivity': 'UNKNOWN',
            'last_verified_at': None, 'embedding_model': None, 'embedding_version': None}

    def save_manual_edit(self, kind, item, content):
        draft_key = None
        try:
            if (kind not in ('interest', 'topic') or not isinstance(item, dict) or
                    item.get('_scope') != self.scope or item.get('source_type') != 'MANUAL' or
                    item.get('friend_id', self.scope.friend_id) != self.scope.friend_id):
                raise TypeScopeMismatch('manual edit row does not belong to the current FriendScope')
            draft_key = (kind, item['id'])
            refs = item.get('source_refs')
            dataset = 'manual:interest_entries' if kind == 'interest' else 'manual:topic_knowledge'
            if (not refs or any(not isinstance(ref, SourceRef) or
                    ref.data_scope != DataScope.PRIVATE_FRIEND or
                    ref.friend_id != self.scope.friend_id or ref.origin != SourceOrigin.MANUAL or
                    ref.speaker_id != 'operator' or ref.message_seq is not None or
                    ref.event_time is not None or ref.dataset_id != dataset or
                    ref.import_id != item.get('id') or ref.record_id != item.get('id')
                    for ref in refs)):
                raise TypeScopeMismatch('manual edit sources do not belong to the current friend')
            current = next((row for row in self.entries(kind)
                            if row['id'] == item.get('id')), None)
            if current is None or current['source_type'] != 'MANUAL':
                raise Conflict('manual row changed or is no longer available; refresh first')
            if tuple(current['source_refs']) != tuple(refs):
                raise Conflict('manual row sources changed; refresh first')
            if kind == 'interest':
                entry = self.manual_interest(entry_id=item['id'], topic=item['topic'],
                    content=content, category=item['category'], strength=item['strength'],
                    confidence=item['confidence'], valid_to=item['valid_to'])
                result = self.save_manual_interest(entry, item['revision'])
            elif kind == 'topic':
                entry = self.manual_topic(entry_id=item['id'], topic=item['topic'],
                                           content=content, summary=content)
                entry.update({key: item[key] for key in (
                    'subtopic', 'title', 'keywords', 'valid_from', 'valid_to',
                    'confidence', 'quality_score', 'time_sensitivity',
                    'last_verified_at', 'embedding_model', 'embedding_version')})
                result = self.save_topic(entry, item['revision'])
            else:
                raise ValueError('kind must be interest or topic')
            self.drafts.pop(draft_key, None)
            return result
        except Exception:
            if draft_key is not None:
                self.drafts[draft_key] = content
            raise


def display_time(value):
    return value if value else '未知'


def display_permission(value):
    if value is True or type(value) is int and value == 1:
        return '允许'
    if value is False or type(value) is int and value == 0:
        return 'refuse'
    return '未知'


def entry_times(item):
    refs = item.get('source_refs', ())
    events = [ref.event_time for ref in refs if getattr(ref, 'event_time', None)]
    records = [ref.recorded_at for ref in refs if getattr(ref, 'recorded_at', None)]
    return (display_time(', '.join(events) if events else None),
            display_time(', '.join(records) if records else item.get('created_at')),
            display_time(item.get('valid_from')), display_time(item.get('valid_to')),
            display_time(item.get('last_verified_at')))


def create_workbench(parent, scope, service=None):
    import tkinter as tk
    from tkinter import messagebox, simpledialog, ttk
    service = service or PersonaKnowledgeService(scope)
    if not isinstance(scope, FriendScope) or service.scope != scope:
        raise TypeScopeMismatch('workbench and service must use the same FriendScope')
    frame = ttk.Frame(parent, padding=8)
    ttk.Label(frame, text=f'好友 {scope.friend_id} 条目与贴纸（本机）').pack(anchor='w')
    body = ttk.Notebook(frame)
    body.pack(fill='both', expand=True)
    refreshers = {}
    for kind, title in (('interest', '兴趣'), ('topic', '知识')):
        page = ttk.Frame(body)
        body.add(page, text=title)
        if kind == 'topic':
            ttk.Label(page, text='公开研究资料仅可查看；需要修改时，请新增人工资料。').pack(anchor='w')
        columns = ('content', '来源类型', '来源事件时间', '记录时间', '生效时间',
                   '失效时间', '最后核实')
        tree = ttk.Treeview(page, columns=columns,
                            show='headings')
        labels = ('内容', '来源类型', '来源事件时间', '记录时间', '生效时间',
                  '失效时间', '最后核实')
        for column, label in zip(tree['columns'], labels):
            tree.heading(column, text=label)
        tree.pack(fill='both', expand=True)
        rows_by_iid = {}
        def reload_rows(tree=tree, kind=kind, rows_by_iid=rows_by_iid):
            tree.delete(*tree.get_children())
            rows_by_iid.clear()
            try:
                items = service.entries(kind)
                for item in items:
                    draft = service.drafts.get((kind, item['id']))
                    if draft is not None:
                        item = dict(item)
                        item['_draft_content'] = draft
                    shown_content = item.get('_draft_content') or item.get('content') or item.get('summary') or item.get('title') or ''
                    iid = tree.insert('', 'end', values=(shown_content, item.get('source_type', '未知'),
                        *entry_times(item)))
                    rows_by_iid[iid] = item
            except Exception as exc:
                messagebox.showerror('failed', str(exc), parent=frame)
        refreshers[kind] = reload_rows
        actions = ttk.Frame(page)
        actions.pack(fill='x')
        ttk.Button(actions, text='Refresh', command=reload_rows).pack(side='left')
        def add_item(kind=kind, rows_by_iid=rows_by_iid, reload_rows=reload_rows):
            identifier = uuid.uuid4().hex
            topic = simpledialog.askstring('items', '主题', parent=frame)
            content = simpledialog.askstring('items', '内容', parent=frame)
            if not topic or not content:
                return
            try:
                if kind == 'interest':
                    entry = service.manual_interest(entry_id=identifier, topic=topic,
                        content=content, category='人工标记')
                    service.save_manual_interest(entry, 0)
                else:
                    service.save_topic(service.manual_topic(entry_id=identifier,
                        topic=topic, content=content), 0)
                reload_rows()
            except Exception as exc:
                messagebox.showerror('Savefailed', str(exc), parent=frame)
        def edit_item(kind=kind, tree=tree, rows_by_iid=rows_by_iid, reload_rows=reload_rows):
            selected = tree.selection()
            if not selected:
                return
            item = dict(rows_by_iid[selected[0]])
            if item.get('source_type') == 'PUBLIC_RESEARCH':
                messagebox.showinfo('只读镜像', '公开研究资料不可直接编辑；请点“新增”创建人工修订。', parent=frame)
                return
            if item.get('source_type') != 'MANUAL':
                messagebox.showinfo('items', '此类资料不可直接编辑；请点“新增”创建人工资料。', parent=frame)
                return
            value = simpledialog.askstring('Edititems', '内容', initialvalue=item.get('_draft_content') or item.get('content') or
                item.get('summary') or '', parent=frame)
            if value is None:
                return
            try:
                service.save_manual_edit(kind, item, value)
                reload_rows()
            except Exception as exc:
                messagebox.showerror('Savefailed', f'{exc}\n草稿已保留。点击刷新加载最新版本，再编辑提交。', parent=frame)
        def remove_item(kind=kind, tree=tree, rows_by_iid=rows_by_iid, reload_rows=reload_rows):
            selected = tree.selection()
            if not selected:
                return
            item = rows_by_iid[selected[0]]
            if item.get('source_type') == 'PUBLIC_RESEARCH':
                messagebox.showinfo('只读镜像', '公开研究资料仅可查看，不能在此设置失效。', parent=frame)
                return
            try:
                service.tombstone(kind, item['id'], item['revision'])
                reload_rows()
            except Exception as exc:
                messagebox.showerror('Deletefailed', f'{exc}\n请显式刷新后重试；锁定人工兴趣不能删除。', parent=frame)
        ttk.Button(actions, text='新增', command=add_item).pack(side='left')
        ttk.Button(actions, text='Edit', command=edit_item).pack(side='left')
        ttk.Button(actions, text='失效', command=remove_item).pack(side='left')
        reload_rows()
    page = ttk.Frame(body)
    body.add(page, text='贴纸')
    try:
        rows, overflow = service.stickers()
        ttk.Label(page, text=f'本页记录（仅本页，不代表可发送总数）：{len(rows)}' + ('（超过100条，已截断）' if overflow else '')).pack(anchor='w')
        ttk.Label(page, text='贴纸权限与描述仅供参考；本页只展示记录，不会发送贴纸。').pack(anchor='w')
        tree = ttk.Treeview(page, columns=('state', 'permission', '贴纸描述', 'analysis', 'reason'), show='headings')
        for column, label in zip(tree['columns'], ('状态', '权限', '贴纸描述', '分析状态/时间', '原因')):
            tree.heading(column, text=label)
        for row in rows:
            permissions = f"本地:{display_permission(row['local_use_allowed'])} / 转发:{display_permission(row['image_redistribution_allowed'])} / 分析:{display_permission(row['model_analysis_allowed'])}"
            meta = row['sticker_kind'] or ('候选描述' if row['declared_kind'] == 'STICKER' else '缺少描述')
            analysis = f"{row['analysis_status'] or '未分析'} / {display_time(row['analyzed_at'])}"
            tree.insert('', 'end', values=('有效' if row['valid'] else '停用', permissions,
                                           meta, analysis, row['unknown_reason'] or ''))
        tree.pack(fill='both', expand=True)
    except Exception as exc:
        ttk.Label(page, text=f'只读加载失败：{exc}').pack(anchor='w')
    return frame
