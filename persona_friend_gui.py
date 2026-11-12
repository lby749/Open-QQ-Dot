"""Friend-scoped Persona version and Example workbench."""
from contextlib import closing
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import os
import sqlite3
import uuid

from persona_types import (ContextBudgets, DataScope, FriendScope, InvalidSource,
                           RetrievalFilters, RetrievalRequest, ScopeMismatch)


def _readonly_pairs(scope):
    """Build the same read-only PairStore view used by PersonaRetriever."""
    from dataset_normalizer import DatasetNormalizer
    from dataset_registry import DatasetRegistry
    from dialogue_pairs import DialoguePairBuilder, PairStore
    from dialogue_turns import DialogueStore
    from persona_store import PersonaStore

    store = PersonaStore(scope.friend_id, scope.friend_dir, readonly=True,
                         project_root=scope.project_root)
    registry = None
    try:
        dialogues = DialogueStore.__new__(DialogueStore)
        dialogues.scope = scope
        dialogues.store = store
        registry = DatasetRegistry(scope, readonly=True)
        dialogues.registry = registry
        dialogues.normalizer = DatasetNormalizer(scope, dialogues.registry)
        dialogues.db = store.db
        pairs = PairStore.__new__(PairStore)
        pairs.scope = scope
        pairs.dialogues = dialogues
        pairs.db = store.db
        pairs.builder = DialoguePairBuilder(scope)
        return pairs
    except Exception:
        try:
            if registry is not None:
                registry.close()
        finally:
            store.close()
        raise


class PersonaFriendService:
    """Call the approved PersonaVersionStore and ExampleRetriever APIs."""

    def __init__(self, scope, *, pair_store_factory=None, example_retriever_factory=None):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('a verified FriendScope is required')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
            friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)
        self.pair_store_factory = pair_store_factory
        self.example_retriever_factory = example_retriever_factory

    def close(self):
        pass

    def read_active(self):
        return self.read_snapshot()[0]

    def read_snapshot(self):
        from persona_versions import PersonaVersionStore
        pairs = _readonly_pairs(self.scope)
        try:
            versions = PersonaVersionStore(self.scope, pairs)
            active = versions.read_active()
            if active is not None:
                versions._recheck(active['manifest'])
            return active, pairs.revision
        finally:
            pairs.close()

    def list_versions(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise InvalidSource('version limit must be 1..500')
        from persona_store import PersonaStore
        catalog = PersonaStore(self.scope.friend_id, self.scope.friend_dir, readonly=True,
                               project_root=self.scope.project_root)
        try:
            rows = catalog.db.execute('SELECT * FROM artifact_versions WHERE friend_id=? '
                'AND state=? ORDER BY created_at DESC,id DESC LIMIT ?',
                (self.scope.friend_id, 'COMPLETED', limit))
            return tuple(dict(row) for row in rows)
        finally:
            catalog.close()

    def edit_manual(self, *, expected_revision, expected_active_version,
                    expected_manual_revision, overlay=None, tombstones=(), style_overrides=None):
        from dialogue_pairs import PairStore
        pairs = self._writer_pairs()
        try:
            from persona_versions import PersonaVersionStore
            return PersonaVersionStore(self.scope, pairs).edit_manual(
                overlay or {}, tombstones, style_overrides or {},
                expected_revision=expected_revision,
                expected_active_version=expected_active_version,
                expected_manual_revision=expected_manual_revision)
        finally:
            pairs.close()

    def rollback(self, version, *, expected_revision, expected_active_version,
                 expected_manual_revision):
        pairs = self._writer_pairs()
        try:
            from persona_versions import PersonaVersionStore
            return PersonaVersionStore(self.scope, pairs).rollback(version,
                expected_revision=expected_revision,
                expected_active_version=expected_active_version,
                expected_manual_revision=expected_manual_revision)
        finally:
            pairs.close()

    def _writer_pairs(self):
        # Refuse missing/old sidecars before PairStore can initialize or migrate one.
        from persona_store import MIGRATIONS, PersonaStore, Unavailable
        from source_state import source_lock
        with source_lock(self.scope.friend_dir):
            with closing(PersonaStore(self.scope.friend_id, self.scope.friend_dir, readonly=True,
                                      project_root=self.scope.project_root)) as catalog:
                versions = {row[0] for row in catalog.db.execute(
                    'SELECT version FROM schema_migrations')}
                if versions != {version for version, _ in MIGRATIONS}:
                    raise Unavailable('Persona sidecar schema is not current')
                backup_dir = self.scope.friend_dir / '.persona_friend_gui_backups'
                if backup_dir.resolve() != backup_dir or backup_dir.is_symlink():
                    raise ScopeMismatch('backup directory leaves the friend scope')
                backup_dir.mkdir(exist_ok=True)
                backup_path = backup_dir / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') +
                                            '-' + uuid.uuid4().hex + '.sqlite3')
                with closing(sqlite3.connect(backup_path)) as backup:
                    catalog.db.backup(backup)
                try:
                    os.chmod(backup_path, 0o600)
                except OSError:
                    pass
            from dialogue_pairs import PairStore
            return self.pair_store_factory(self.scope) if self.pair_store_factory else PairStore(self.scope)

    def preview_examples(self, query, *, limit=10, persona_budget=5000,
                         example_budget=4000, knowledge_budget=2000, scene='ordinary'):
        if type(limit) is not int or not 1 <= limit <= 20:
            raise InvalidSource('K must be 1..20')
        budgets = ContextBudgets(persona=persona_budget, examples=example_budget,
                                  knowledge=knowledge_budget)
        request = RetrievalRequest(friend_id=self.scope.friend_id, query=query,
            recent_context=query, scene=scene, plan=None,
            filters=RetrievalFilters(namespace=('example',),
                                     privacy_scope=DataScope.PRIVATE_FRIEND),
            budgets=budgets, allowed_pack_versions=self.scope.allowed_pack_versions,
            now=datetime.now(timezone.utc).isoformat())
        from example_retriever import ExampleRetriever
        factory = self.example_retriever_factory or ExampleRetriever
        retriever = factory(self.scope)
        try:
            result = retriever.retrieve(request, limit)
            return tuple({'id': item.id, 'content': json.loads(item.content),
                          'score': item.score} for item in result.candidates), result
        finally:
            retriever.close()


def create_workbench(parent, scope, service=None):
    """Create an embeddable Tk view for the explicit current friend scope."""
    import tkinter as tk
    from tkinter import messagebox, ttk

    service = service or PersonaFriendService(scope)
    if not isinstance(scope, FriendScope) or service.scope != scope:
        raise ScopeMismatch('workbench and service must use the same FriendScope')
    frame = ttk.Frame(parent, padding=8)
    frame._persona_friend_service = service
    frame._persona_friend_closed = False
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(1, weight=1)
    ttk.Label(frame, text=f'Friend {scope.friend_id}  Persona Profile (Local)').grid(
        row=0, column=0, sticky='w')
    tabs = ttk.Notebook(frame)
    tabs.grid(row=1, column=0, sticky='nsew')
    claims_tab, style_tab, examples_tab, versions_tab = (ttk.Frame(tabs, padding=6) for _ in range(4))
    tabs.add(claims_tab, text='人格特点与依据')
    tabs.add(style_tab, text='自定义聊天风格')
    tabs.add(examples_tab, text='对话示例')
    tabs.add(versions_tab, text='版本')
    claims_tab.columnconfigure(0, weight=1)
    claims_tab.rowconfigure(1, weight=1)
    claim_list = ttk.Treeview(claims_tab, columns=('claim', 'quality'), show='headings')
    claim_list.heading('claim', text='人格特点')
    claim_list.heading('quality', text='来源/质量')
    claim_list.column('claim', width=520, stretch=True)
    claim_list.column('quality', width=140, stretch=False)
    claim_list.grid(row=0, column=0, sticky='nsew')
    evidence = tk.Text(claims_tab, height=8, wrap='word')
    evidence.grid(row=1, column=0, sticky='nsew', pady=4)
    edit_row = ttk.Frame(claims_tab)
    edit_row.grid(row=2, column=0, sticky='ew')
    claim_text = tk.StringVar()
    ttk.Entry(edit_row, textvariable=claim_text).pack(side='left', fill='x', expand=True)
    detail = tk.StringVar(value='')
    ttk.Label(edit_row, textvariable=detail).pack(side='left', padx=4)
    versions_var = tk.StringVar()
    style_tab.columnconfigure(1, weight=1)
    ttk.Label(style_tab, text='由模型按“基本信息”备注和“聊天风格”自动校准，无需手填。').grid(
        row=0, column=0, columnspan=2, sticky='w')
    style_output = tk.Text(style_tab, height=18, wrap='word')
    style_output.grid(row=2, column=0, columnspan=2, sticky='nsew', pady=4)
    style_tab.rowconfigure(2, weight=1)
    active = None
    active_revision = None
    claim_draft = None

    def cas():
        if active is None or active_revision is None:
            raise InvalidSource('当前好友还没有已发布的 Persona')
        return active, dict(expected_revision=active_revision,
            expected_active_version=active['manifest']['version'],
            expected_manual_revision=active['manifest']['manual_revision'])

    def refresh(preserve_draft=False):
        if frame._persona_friend_closed:
            return
        selected = claim_list.selection()
        draft = (selected[0] if selected else None, claim_text.get()) if preserve_draft else None
        run_async(lambda: (service.read_snapshot(), service.list_versions()),
                  lambda result: render_snapshot(result, draft),
                  '读取人格资料失败')

    def render_snapshot(result, draft=None):
        nonlocal active, active_revision, claim_draft
        claim_draft = draft
        (active, active_revision), rows = result
        for row in claim_list.get_children():
            claim_list.delete(row)
        if active is None:
            evidence.delete('1.0', 'end')
            evidence.insert('1.0', '当前好友暂无活动版本。')
            style_output.delete('1.0', 'end')
            detail.set('')
            return
        profile, style = active['profile'], active['style']
        for claim in profile['claims']:
            claim_list.insert('', 'end', iid=claim['claim_key'],
                values=(claim.get('claim', ''), claim.get('quality', '')))
        evidence.delete('1.0', 'end')
        selected = next((claim for claim in profile['claims']
                         if draft is None or claim['claim_key'] == draft[0]), None)
        if selected:
            claim_list.selection_set(selected['claim_key'])
            show_evidence()
        if draft is not None:
            claim_text.set(draft[1])
        style_output.delete('1.0', 'end')
        style_output.insert('1.0', '统计得到的聊天风格：\n' + json.dumps(
            style.get('observed', {}), ensure_ascii=False, indent=2) +
            '\n\n人工指定的聊天风格：\n' + json.dumps(
            style.get('manual_overrides', {}), ensure_ascii=False, indent=2))
        detail.set('版本 ' + active['manifest']['version'][:12] +
                   ' · 人工修改版本 ' + str(profile['manual_revision']))
        versions_var.set(rows[0]['id'] if rows else '')
        version_box.configure(values=tuple(row['id'] for row in rows))

    ttk.Button(frame, text='刷新当前版本（保留草稿）',
               command=lambda: refresh(preserve_draft=True)).grid(row=0, column=0, sticky='e')

    def show_evidence(_event=None):
        nonlocal claim_draft
        if active is None:
            return
        selection = claim_list.selection()
        claim = next((item for item in active['profile']['claims']
                      if item['claim_key'] in selection), None)
        if claim is None:
            return
        if claim_draft is not None and claim_draft[0] == claim['claim_key']:
            claim_text.set(claim_draft[1])
        else:
            claim_draft = None
            claim_text.set(claim.get('claim', ''))
        evidence.delete('1.0', 'end')
        evidence.insert('1.0', json.dumps({
            'quality': claim.get('quality'), 'source_origin': claim.get('source_origin'),
            'evidence': claim.get('evidence', []),
            'counter_evidence': claim.get('counter_evidence', [])},
            ensure_ascii=False, indent=2))

    def save_claim():
        try:
            selected = claim_list.selection()
            if len(selected) != 1:
                raise InvalidSource('请选择一个 Claim')
            _, expected = cas()
            overlay = {selected[0]: {'claim': claim_text.get()}}
            run_async(lambda: service.edit_manual(overlay=overlay, **expected),
                      lambda _result: refresh(), '人工锁定失败')
        except Exception as exc:
            messagebox.showerror('人工锁定失败', str(exc), parent=frame.winfo_toplevel())

    ttk.Button(edit_row, text='锁定并保存人格特点', command=save_claim).pack(side='left')
    claim_list.bind('<<TreeviewSelect>>', show_evidence)

    def calibrate():
        try:
            current, expected = cas()
            claims, observed = current['profile']['claims'], current['style'].get('observed', {})

            def task():
                from friend_persona_spec import calibrate_persona
                overlay, overrides = calibrate_persona(scope, claims, observed)
                if overlay or overrides:
                    service.edit_manual(overlay=overlay, style_overrides=overrides, **expected)
                return len(overlay), len(overrides)

            def done(counts):
                refresh()
                messagebox.showinfo('自动校准', '已按基本信息和聊天风格校准：人格特点 %d 条，聊天风格 %d 项。' % counts
                                    if any(counts) else '基本信息和聊天风格与当前人格一致，无需调整。',
                                    parent=frame.winfo_toplevel())

            detail.set('正在按基本信息和聊天风格校准…')
            run_async(task, done, '自动校准失败')
        except Exception as exc:
            messagebox.showerror('自动校准失败', str(exc), parent=frame.winfo_toplevel())

    ttk.Button(style_tab, text='按基本信息和聊天风格自动校准', command=calibrate).grid(
        row=0, column=2, sticky='e')
    ttk.Button(edit_row, text='按基本信息和聊天风格自动校准', command=calibrate).pack(side='left', padx=4)

    query_var, k_var = tk.StringVar(), tk.StringVar(value='10')
    persona_var, examples_var = tk.StringVar(value='5000'), tk.StringVar(value='4000')
    knowledge_var = tk.StringVar(value='2000')
    ttk.Label(examples_tab, text='本地查询').grid(row=0, column=0, sticky='w')
    ttk.Entry(examples_tab, textvariable=query_var).grid(row=0, column=1, sticky='ew')
    ttk.Label(examples_tab, text='items').grid(row=0, column=2)
    ttk.Entry(examples_tab, textvariable=k_var, width=5).grid(row=0, column=3)
    ttk.Label(examples_tab, text='内容长度上限：人格 / 对话示例 / 知识').grid(
        row=1, column=0, sticky='w')
    ttk.Entry(examples_tab, textvariable=persona_var, width=8).grid(row=1, column=1, sticky='w')
    ttk.Entry(examples_tab, textvariable=examples_var, width=8).grid(row=1, column=2, sticky='w')
    ttk.Entry(examples_tab, textvariable=knowledge_var, width=8).grid(row=1, column=3, sticky='w')
    examples_tab.columnconfigure(1, weight=1)
    example_output = tk.Text(examples_tab, height=18, wrap='word')
    example_output.grid(row=2, column=0, columnspan=4, sticky='nsew')
    examples_tab.rowconfigure(2, weight=1)

    def preview():
        try:
            query, limit = query_var.get(), int(k_var.get())
            persona_budget, example_budget = int(persona_var.get()), int(examples_var.get())
            knowledge_budget = int(knowledge_var.get())
            run_async(lambda: service.preview_examples(query, limit=limit,
                persona_budget=persona_budget, example_budget=example_budget,
                knowledge_budget=knowledge_budget), render_examples, '对话示例预览失败')
        except Exception as exc:
            messagebox.showerror('对话示例预览失败', str(exc), parent=frame.winfo_toplevel())

    def render_examples(value):
        rows, result = value
        example_output.delete('1.0', 'end')
        example_output.insert('1.0', json.dumps(rows, ensure_ascii=False, indent=2) +
            ('\n\n: ' + '; '.join(result.errors) if result.degraded else ''))

    ttk.Button(examples_tab, text='本地预览', command=preview).grid(row=0, column=4, sticky='e')
    version_box = ttk.Combobox(versions_tab, textvariable=versions_var, state='readonly')
    version_box.pack(fill='x', pady=4)
    ttk.Label(versions_tab, text='回滚会发布新不可变版本，并保留当前人工锁定与 自定义聊天风格。').pack(anchor='w')

    def rollback():
        try:
            _, expected = cas()
            version = versions_var.get()
            run_async(lambda: service.rollback(version, **expected),
                      lambda _result: refresh(), '版本回滚失败')
        except Exception as exc:
            messagebox.showerror('版本回滚失败', str(exc), parent=frame.winfo_toplevel())

    ttk.Button(versions_tab, text='回滚到所选版本', command=rollback).pack(anchor='e', pady=4)

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='persona-friend-ui')
    frame._persona_friend_executor = executor
    frame._persona_friend_after_ids = set()

    def run_async(task, on_success, title):
        if frame._persona_friend_closed:
            return
        future = executor.submit(task)

        def collect():
            frame._persona_friend_after_ids.discard(timer_id[0])
            if frame._persona_friend_closed:
                return
            if not future.done():
                timer_id[0] = frame.after(50, collect)
                frame._persona_friend_after_ids.add(timer_id[0])
                return
            try:
                on_success(future.result())
            except Exception as exc:
                from persona_store import Conflict
                if isinstance(exc, Conflict):
                    detail.set('版本已变化，请刷新当前版本；编辑草稿已保留。')
                messagebox.showerror(title, str(exc), parent=frame.winfo_toplevel())

        timer_id = [frame.after(50, collect)]
        frame._persona_friend_after_ids.add(timer_id[0])

    def close(_event=None):
        if not frame._persona_friend_closed:
            frame._persona_friend_closed = True
            for after_id in frame._persona_friend_after_ids:
                try:
                    frame.after_cancel(after_id)
                except tk.TclError:
                    pass
            frame._persona_friend_after_ids.clear()
            executor.shutdown(wait=False, cancel_futures=True)
            service.close()

    frame.bind('<Destroy>', lambda event: close() if event.widget is frame else None)
    refresh()
    return frame
