"""Local-only, read-only retrieval diagnostics and context comparison."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from contextlib import closing
import json
import os
import queue
import re
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from pathlib import Path

from persona_snapshot import PreviewSnapshot
from persona_pack_bindings import PackBindingStore
from persona_store import Conflict, ScopeMismatch
from persona_types import (ContextBudgets, DataScope, FriendScope, RetrievalFilters,
                           RetrievalRequest)
from retrieval_orchestrator import RetrievalOrchestrator
from runtime_context import RuntimeContextBuilder
from source_state import snapshot as source_snapshot


def _artifact_directory(preview):
    """Return the only accepted artifact directory under a fresh preview root."""
    if not isinstance(preview, PreviewSnapshot) or preview.source_friend_id != preview.scope.friend_id:
        raise ScopeMismatch('preview snapshot identity mismatch')
    root = preview.scope.project_root
    base = root.parent
    if (root.name.casefold() not in ('',) and not re.fullmatch(r'[0-9a-f]{32}', root.name)):
        raise ScopeMismatch('preview root is not a generated UUID directory')
    if (base.name != 'persona-previews' or base.parent.name != 'build' or
            root.parent.resolve() != root.parent):
        raise ScopeMismatch('preview root is outside the isolated artifact area')
    current = base
    for part in (root.name, 'retrieval-debug'):
        current = current / part
        if current.exists() and (current.is_symlink() or current.resolve() != current):
            raise ScopeMismatch('artifact path redirects outside the preview root')
    if root.resolve() != root or preview.scope.friend_dir != root / 'friends' / preview.source_friend_id:
        raise ScopeMismatch('preview scope does not match its isolated root')
    return root / 'retrieval-debug'


def _write_artifact(preview, payload):
    directory = _artifact_directory(preview)
    directory.mkdir(exist_ok=True)
    if directory.is_symlink() or directory.resolve() != directory:
        raise ScopeMismatch('artifact directory redirected during creation')
    target = directory / 'result.json'
    temporary = directory / '.result.json.tmp'
    if target.is_symlink() or temporary.is_symlink():
        raise ScopeMismatch('artifact file cannot be a symlink')
    # Exclusive creation rejects a pre-existing file, including a hardlink.
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            if os.fstat(stream.fileno()).st_nlink != 1:
                raise ScopeMismatch('new artifact file has additional hardlinks')
            stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


class RetrievalDebugService:
    """One bounded retrieval and a local context comparison for one FriendScope."""

    def __init__(self, scope, *, snapshotter=source_snapshot,
                 snapshot_factory=PreviewSnapshot.create,
                 orchestrator_factory=RetrievalOrchestrator,
                 context_builder_factory=RuntimeContextBuilder):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('a verified FriendScope is required')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
            friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)
        self.snapshotter = snapshotter
        self.snapshot_factory = snapshot_factory
        self.orchestrator_factory = orchestrator_factory
        self.context_builder_factory = context_builder_factory

    def run(self, *, query, recent_context, scene, legacy_context, cancel_event=None,
            privacy_scope=DataScope.PRIVATE_FRIEND):
        for name, value in (('query', query), ('recent_context', recent_context),
                            ('scene', scene), ('legacy_context', legacy_context)):
            if not isinstance(value, str):
                raise TypeError(name + ' must be text')
        if not query.strip() or not scene.strip():
            raise ValueError('query and scene are required')

        privacy_scope = DataScope(privacy_scope)
        if privacy_scope not in (DataScope.PRIVATE_FRIEND, DataScope.PUBLIC_PACK):
            raise ValueError('diagnostics require PRIVATE_FRIEND or PUBLIC_PACK')
        before = self.snapshotter(self.scope)
        run_scope = self.scope
        namespaces = ()
        if privacy_scope == DataScope.PUBLIC_PACK:
            with closing(PackBindingStore(self.scope, readonly=True)) as bindings:
                run_scope = bindings.bound_scope()
            namespaces = ('persona', 'example', 'style')
            if self.snapshotter(self.scope) != before:
                raise Conflict('pack bindings changed before retrieval')

        now = datetime.now(timezone.utc).isoformat()
        request = RetrievalRequest(friend_id=self.scope.friend_id, query=query,
            recent_context=recent_context, scene=scene, plan=None,
            filters=RetrievalFilters(namespace=namespaces, privacy_scope=privacy_scope),
            budgets=ContextBudgets(),
            allowed_pack_versions=run_scope.allowed_pack_versions, now=now)

        orchestrator = self.orchestrator_factory(run_scope, embedding_provider=None)
        relevant = orchestrator.retrieve(request)
        if relevant.trace.get('snapshot_changed'):
            raise Conflict('retrieval source changed; result discarded')
        after_retrieval = self.snapshotter(self.scope)
        if after_retrieval != before:
            raise Conflict('source changed during retrieval; result discarded')

        router = relevant.router
        builder = self.context_builder_factory()
        a_bytes = len(legacy_context.encode('utf-8'))
        built = builder.build(request, relevant, legacy_context,
            {'enabled': True, 'provider_kind': 'LOCAL'})
        if self.snapshotter(self.scope) != before:
            raise Conflict('source changed before context comparison could be published')
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError('friend scope changed; result discarded')
        preview = self.snapshot_factory(self.scope)
        if preview.source_friend_id != self.scope.friend_id:
            raise ScopeMismatch('preview snapshot belongs to another friend')
        if self.snapshotter(self.scope) != before:
            raise Conflict('source changed while preparing the isolated result directory')
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError('friend scope changed; result discarded')

        result = {
            'friend_id': self.scope.friend_id,
            'source_version': before,
            'request': {'query': query, 'recent_context': recent_context, 'scene': scene,
                        'scene_origin': 'operator_input', 'now': now,
                        'plan': None, 'privacy_scope': privacy_scope.value,
                        'allowed_pack_versions': sorted((p.pack_id, p.version)
                            for p in run_scope.allowed_pack_versions),
                        'budgets': {'persona': request.budgets.persona,
                            'examples': request.budgets.examples,
                            'knowledge': request.budgets.knowledge}},
            'comparison_kind': 'context_only_not_model_reply',
            'preview_snapshot_role': 'artifact_isolation_only_not_retrieval_scope',
            'retrieval': {'router': {'scene': router.scene, 'query_terms': router.query_terms,
                'entities': router.entities, 'time_range': router.time_range,
                'reason': router.reason, 'mode': router.mode,
                'routes': router.retrievers}, 'trace': relevant.trace,
                'degraded': relevant.degraded},
            'A': {'label': 'operator_legacy_context', 'context': legacy_context,
                  'bytes_proxy': a_bytes},
            'B': {'label': 'same_legacy_context_plus_retrieval',
                  'context': built.prompt_blocks, 'bytes_proxy': built.token_usage['total'],
                  'degraded': built.degraded, 'trace': built.trace},
            'measurement': 'UTF-8 byte proxy; not model tokens or API usage',
            'unimplemented': {'full_model_reply_ab': 'not_implemented',
                'hidden_target': 'not_implemented', 'evaluation': 'not_implemented',
                'sticker_route': 'not_connected'},
        }
        path = _write_artifact(preview, result)
        if (cancel_event is not None and cancel_event.is_set()) or self.snapshotter(self.scope) != before:
            path.unlink(missing_ok=True)
            raise Conflict('source or friend scope changed before result publication; artifact discarded')
        result['artifact_path'] = str(path)
        return result


class RetrievalDebugWindow:
    """Tk front end. All database reads and preview work run on one worker."""

    def __init__(self, root, project_root):
        self.root = root
        self.project_root = Path(project_root).resolve()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='retrieval-debug')
        self.results = queue.Queue()
        self.generation = 0
        self.closed = False
        self.future = None
        self.cancel_event = None
        self._build()
        if isinstance(self.root, (tk.Tk, tk.Toplevel)):
            self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.bind('<Destroy>', lambda event: self.close() if event.widget is self.root else None, add='+')
        self.poll_after_id = self.root.after(50, self._poll)

    def _build(self):
        if isinstance(self.root, (tk.Tk, tk.Toplevel)):
            self.root.title('只读检索诊断 / 上下文对比')
            self.root.geometry('1180x820')
        frame = ttk.Frame(self.root, padding=8)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='friend').grid(row=0, column=0, sticky='w')
        self.friend = tk.StringVar()
        self.friend_box = ttk.Combobox(frame, textvariable=self.friend, values=self._friend_ids(),
                                       state='readonly', width=18)
        self.friend_box.grid(row=0, column=1, sticky='w')
        self.friend_box.bind('<<ComboboxSelected>>', self._friend_changed)
        self.privacy_scope = tk.StringVar(value='当前好友私有资料')
        ttk.Combobox(frame, textvariable=self.privacy_scope,
            values=('当前好友私有资料', '已绑定的公共人格包'),
            state='readonly', width=22).grid(row=0, column=2, sticky='w')
        self.fields = {}
        for row, (name, label, height) in enumerate((
                ('query', '检索问题（手动输入）', 2), ('recent', '近期上下文（手动输入）', 3),
                ('scene', '聊天场景', 1), ('legacy', '原有上下文', 5)), 1):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky='nw', pady=3)
            box = tk.Text(frame, height=height, wrap='word')
            box.grid(row=row, column=1, columnspan=2, sticky='nsew', pady=3)
            self.fields[name] = box
        self.fields['scene'].insert('1.0', '日常聊天')
        self.run_button = ttk.Button(frame, text='运行只读检索并比较上下文', command=self.run)
        self.run_button.grid(row=5, column=1, columnspan=2, sticky='w', pady=5)
        ttk.Label(frame, text='仅对比原有内容和补充检索后的内容，不生成回复。长度按文本字节数估算。').grid(
            row=6, column=0, columnspan=3, sticky='w')
        self.status = tk.StringVar(value='本地只读；不会调用模型、网络或发送消息。')
        ttk.Label(frame, textvariable=self.status).grid(row=7, column=0, columnspan=3, sticky='w')
        panes = ttk.Panedwindow(frame, orient='horizontal')
        panes.grid(row=8, column=0, columnspan=3, sticky='nsew', pady=5)
        self.a_text = self._text_pane(panes, '原有上下文')
        self.b_text = self._text_pane(panes, '加入检索后的上下文')
        self.trace_text = self._text_pane(panes, '检索过程与排除原因')
        for row in (1, 2, 3, 4, 8):
            frame.rowconfigure(row, weight=1 if row == 8 else 0)
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(2, weight=1)

    @staticmethod
    def _text_pane(parent, title):
        container = ttk.LabelFrame(parent, text=title, padding=4)
        widget = tk.Text(container, wrap='word', state='disabled')
        scroll = ttk.Scrollbar(container, command=widget.yview)
        widget.configure(yscrollcommand=scroll.set)
        widget.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        parent.add(container, weight=1)
        return widget

    def _friend_ids(self):
        path = self.project_root / 'friends.json'
        try:
            if path.is_symlink() or path.resolve() != path:
                return []
            data = json.loads(path.read_text(encoding='utf-8'))
            return sorted(friend_id for friend_id, row in data['friends'].items()
                if row.get('qq_id') == friend_id and re.fullmatch(r'[0-9]{5,12}', friend_id))
        except (OSError, ValueError, KeyError, TypeError):
            return []

    def _friend_changed(self, _event=None):
        if self.cancel_event is not None:
            self.cancel_event.set()
        self.generation += 1
        self.status.set('好友已切换；之前运行的迟到结果将忽略。')

    def run(self):
        if self.future and not self.future.done():
            return
        friend_id = self.friend.get()
        if not friend_id:
            messagebox.showerror('friend', '请选择 好友编号。', parent=self.root)
            return
        values = {name: widget.get('1.0', 'end-1c') for name, widget in self.fields.items()}
        if not values['query'].strip() or not values['scene'].strip():
            messagebox.showerror('缺少输入', '请填写检索问题和聊天场景。', parent=self.root)
            return
        try:
            scope = FriendScope.load(friend_id, project_root=self.project_root,
                                     allowed_pack_versions=())
            service = RetrievalDebugService(scope)
        except Exception as exc:
            self.status.set('当前好友范围验证失败；未读取检索结果。')
            messagebox.showerror('范围验证失败', str(exc), parent=self.root)
            return
        generation = self.generation
        privacy_scope = self.privacy_scope.get()
        privacy_scope = {'当前好友私有资料': DataScope.PRIVATE_FRIEND.value,
                         '已绑定的公共人格包': DataScope.PUBLIC_PACK.value}.get(privacy_scope, privacy_scope)
        values['scene'] = {'日常聊天': 'daily_chat', '情绪支持': 'emotional_support',
                           '知识问答': 'knowledge_query'}.get(values['scene'].strip(), values['scene'])
        self.cancel_event = threading.Event()
        self.run_button.configure(state='disabled')
        self.status.set('后台正在建立隔离快照并执行一次只读检索…')
        self.future = self.executor.submit(service.run, query=values['query'],
            recent_context=values['recent'], scene=values['scene'],
            legacy_context=values['legacy'], cancel_event=self.cancel_event,
            privacy_scope=privacy_scope)
        self.future.add_done_callback(lambda future: self.results.put((generation, future)))

    def _poll(self):
        if self.closed:
            return
        try:
            while True:
                generation, future = self.results.get_nowait()
                if generation != self.generation:
                    self.run_button.configure(state='normal')
                    continue
                self.run_button.configure(state='normal')
                try:
                    self._deliver(generation, future.result())
                except Exception as exc:
                    self.status.set('诊断失败；结果已丢弃。')
                    messagebox.showerror('检索诊断失败', str(exc), parent=self.root)
        except queue.Empty:
            pass
        self.poll_after_id = self.root.after(50, self._poll)

    def _show(self, result):
        self._replace(self.a_text, result['A']['context'])
        self._replace(self.b_text, result['B']['context'])
        trace = {'对比方式': '原有内容与加入检索后的内容对比',
            '数据副本': '本地隔离副本', '聊天场景来源': '本页手动输入',
            '资料范围': {'PRIVATE_FRIEND': '当前好友私有资料', 'PUBLIC_PACK': '公共人格包'}.get(
                result['request']['privacy_scope'], result['request']['privacy_scope']),
            '可用人格包版本': result['request']['allowed_pack_versions'],
            '长度计算方式': '按文本字节数估算',
            '原有内容长度': result['A']['bytes_proxy'],
            '加入检索后的长度': result['B']['bytes_proxy'],
            '检索路线（技术记录）': result['retrieval']['router'],
            '检索详细记录（排查时使用）': result['retrieval']['trace'],
            '上下文补充记录': result['B']['trace'],
            '尚未实现的功能': result['unimplemented'],
            '结果保存位置': result['artifact_path']}
        self._replace(self.trace_text, json.dumps(trace, ensure_ascii=False, indent=2))
        self.status.set('完成；上下文对比已保存在隔离目录（未调用模型）。')

    @staticmethod
    def _replace(widget, value):
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        widget.insert('1.0', value)
        widget.configure(state='disabled')

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.generation += 1
        if self.cancel_event is not None:
            self.cancel_event.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
        if getattr(self, 'poll_after_id', None) is not None:
            self.root.after_cancel(self.poll_after_id)
        self.root.destroy()

    def change_friend(self, friend_id):
        """Small explicit hook used by the combobox and delayed-result tests."""
        self.friend.set(friend_id)
        self._friend_changed()

    def _deliver(self, generation, result):
        """Ignore delayed worker completions after switching scope or closing."""
        if not self.closed and generation == self.generation:
            self._show(result)


def main():
    root = tk.Tk()
    RetrievalDebugWindow(root, Path(__file__).resolve().parent)
    root.mainloop()


if __name__ == '__main__':
    main()
