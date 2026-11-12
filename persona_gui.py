"""Local Tk workbench for the existing per-friend Persona Job API."""
from concurrent.futures import ThreadPoolExecutor
import json

from persona_stage_plan import (ADVANCED_STAGES, PRESET_KIND, existing_job_action,
                                job_progress_label, job_usage_label,
                                plan_advanced_stage)


def parse_ids(value, name, *, required=False):
    values = [part.strip() for part in value.replace(',', '\n').splitlines() if part.strip()]
    if len(values) > 64 or len(values) != len(set(values)):
        raise ValueError(name + ' must contain at most 64 unique IDs')
    if required and not values:
        raise ValueError(name + ' is required')
    return values


def missing_local_model_artifacts():
    from local_persona_analyzer import missing_local_model

    return missing_local_model()


MODEL_CHOICES = {
    '程序统计（无需模型）': False,
    '本地大模型 (LLM)': True,
    ' 9B model': True,
    'model (friendmodelSettings) ': 'cloud',
}


def _read_recent_jobs(scope, limit=100):
    from persona_jobs import JobControl

    control = JobControl(scope, readonly=True)
    try:
        return control.list_recent(limit)
    finally:
        control.close()


class PersonaJobService:
    def __init__(self, scope, control=None, pair_store_factory=None):
        from persona_jobs import JobControl
        from dialogue_pairs import PairStore
        from persona_store import ScopeMismatch

        self.scope = scope
        self.control = control if control is not None else JobControl(scope)
        self.owns_control = control is None
        control_scope = getattr(self.control, 'scope', None)
        if (control_scope is not None and
                (control_scope.friend_id != scope.friend_id or
                 control_scope.project_root != scope.project_root)):
            raise ScopeMismatch('workbench and JobControl must share the same friend scope')
        self.pair_store_factory = pair_store_factory or PairStore

    def close(self):
        if self.owns_control:
            self.control.close()

    def list_sessions(self, limit=100):
        pairs = self.pair_store_factory(self.scope)
        try:
            return pairs.dialogues.list_sessions(limit)
        finally:
            pairs.close()

    def list_pairs(self, session_ids, target_speaker_ids=(), limit=500):
        session_ids = tuple(parse_ids('\n'.join(session_ids), 'session_ids', required=True))
        target_speaker_ids = set(target_speaker_ids)
        pairs = self.pair_store_factory(self.scope)
        try:
            result = []
            for session_id in session_ids:
                for pair in pairs.load_pairs(session_id):
                    if target_speaker_ids and pair.target_speaker_id not in target_speaker_ids:
                        continue
                    result.append({'id': pair.id, 'session_id': pair.session_id,
                                   'target_speaker_id': pair.target_speaker_id})
                    if len(result) > limit:
                        raise ValueError('候选 Pair 超过界面上限，请缩小会话或目标范围')
            return tuple(result)
        finally:
            pairs.close()

    def _pairs_and_sources(self, session_ids, pair_ids, target_speaker_ids):
        pairs = self.pair_store_factory(self.scope)
        try:
            candidates = []
            for session_id in session_ids:
                candidates.extend(pairs.load_pairs(session_id))
            by_id = {pair.id: pair for pair in candidates}
            selected = [by_id[pair_id] for pair_id in pair_ids if pair_id in by_id]
            if len(selected) != len(pair_ids):
                raise ValueError('所选 Pair 不属于当前好友的已验证会话')
            if any(pair.target_speaker_id not in target_speaker_ids for pair in selected):
                raise ValueError('所选 Pair 与目标说话人不一致')
            refs = tuple(dict.fromkeys(ref for pair in selected for ref in pair.source_refs))
            return refs
        finally:
            pairs.close()

    def enqueue_preset(self, *, pack_id, session_ids, pair_ids, target_speaker_ids,
                       target_spec, source_partitions, selection_limit=64, judge_batch_size=8):
        session_ids = parse_ids(session_ids, 'session_ids', required=True)
        pair_ids = parse_ids(pair_ids, 'pair_ids', required=True)
        target_speaker_ids = parse_ids(target_speaker_ids, 'target_speaker_ids', required=True)
        refs = self._pairs_and_sources(session_ids, pair_ids, set(target_speaker_ids))
        payload = {'pack_id': pack_id, 'target_spec': target_spec,
                   'session_ids': session_ids, 'pair_ids': pair_ids,
                   'target_speaker_ids': target_speaker_ids,
                   'source_partitions': source_partitions,
                   'selection_limit': selection_limit, 'judge_batch_size': judge_batch_size,
                   'use_local_model': False}
        return self._enqueue(PRESET_KIND, payload, refs)

    def enqueue_advanced(self, kind, *, session_ids=(), pair_ids=(), target_speaker_ids=(),
                         friend_speaker_ids=(), use_local_model=False,
                         pack_id='', base_version='', base_manifest_sha256=''):
        if kind == 'distill':
            from dialogue_pairs import PairStore
            from persona_analysis_input import AnalysisInputBuilder

            session_ids = parse_ids('\n'.join(session_ids), 'session_ids', required=True)
            pair_ids = list(pair_ids)
            target_speaker_ids = list(target_speaker_ids)
            friend_speaker_ids = list(friend_speaker_ids)
            if use_local_model is True:
                missing = missing_local_model_artifacts()
                if missing:
                    raise ValueError(' 9B model/ok: ' + ', '.join(missing))
            pairs = PairStore(self.scope)
            try:
                selected = AnalysisInputBuilder(self.scope, pairs).build(
                    session_ids, target_speaker_ids, friend_speaker_ids,
                    pair_ids=pair_ids or None)
                if not selected.role_pairs and not selected.relationship_events:
                    raise ValueError('所选来源没有可蒸馏的目标 Pair 或关系事件')
                refs = selected.source_refs
            finally:
                pairs.close()
            plan = plan_advanced_stage(kind, {
                'selected_sessions': bool(session_ids),
                'verified_source_refs': bool(refs),
            })
            if not plan.ready:
                raise ValueError('items: ' + '; '.join(plan.missing))
            payload = {'session_ids': session_ids, 'pair_ids': pair_ids or None,
                       'target_speaker_ids': target_speaker_ids,
                       'friend_speaker_ids': friend_speaker_ids,
                       'use_local_model': use_local_model}
            return self._enqueue(kind, payload, refs)

        if kind == 'pack_media':
            from persona_pack import _media_base

            payload = {'pack_id': pack_id, 'base_version': base_version,
                       'base_manifest_sha256': base_manifest_sha256,
                       'media_mode': 'EMPTY_UNKNOWN',
                       'media_schema_version': 'sticker-profile-v1'}
            _media_base(self.scope, payload)
            plan = plan_advanced_stage(kind, {
                'active_five_file_pack': True,
                'verified_public_grants': True,
            })
            if not plan.ready:
                raise ValueError('items: ' + '; '.join(plan.missing))
            return self._enqueue(kind, payload, ())

        raise ValueError('此阶段没有独立 Job 接口')

    def _enqueue(self, kind, payload, source_refs):
        job_id = self.control.enqueue(kind, payload, source_refs)
        job = self.control.read_status(job_id)
        action = existing_job_action(job)
        if action == 'start':
            self.control.spawn_worker(job_id)
            job = self.control.read_status(job_id)
        return job

    def act(self, job_id, action):
        job = self.control.read_status(job_id)
        state_action = existing_job_action(job)
        if action == 'pause' and job['state'] in ('queued', 'running'):
            self.control.pause(job_id)
        elif action == 'cancel' and job['state'] != 'completed':
            self.control.cancel(job_id)
        elif action in ('start', 'start_or_resume') and state_action == 'start':
            self.control.spawn_worker(job_id)
        elif action in ('resume', 'start_or_resume') and state_action == 'resume':
            self.control.retry(job_id)
            self.control.spawn_worker(job_id)
        else:
            raise ValueError('该任务状态不允许此操作，或外部用量仍未知')
        return self.control.read_status(job_id)


def active_pack_bases(project_root, limit=100):
    """List hash-verified active five-file public bases; enqueue rechecks live grants."""
    from persona_pack import _manifest

    root = project_root / 'data' / 'persona_packs'
    if not root.is_dir() or root.resolve() != root:
        return ()
    result = []
    for directory in sorted(root.iterdir(), key=lambda path: path.name):
        if len(result) >= limit:
            break
        if not directory.is_dir() or directory.is_symlink():
            continue
        active_path = directory / 'active.json'
        if active_path.is_symlink() or not active_path.is_file() or active_path.resolve() != active_path:
            continue
        try:
            active = json.loads(active_path.read_text('utf-8'))
            version = active['version']
            version_path = directory / 'versions' / version
            if version_path.resolve() != version_path or version_path.is_symlink():
                continue
            manifest, digest = _manifest(version_path, directory.name, version)
            if (manifest.get('scope') != 'PUBLIC_PACK' or manifest.get('friend_id') is not None or
                    manifest.get('schema_version') not in (None, 'persona-pack-manifest-v1') or
                    active.get('manifest_sha256') != digest):
                continue
            result.append({'pack_id': directory.name, 'version': version,
                           'manifest_sha256': digest})
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return tuple(result)


def parse_workbench_json(text):
    """Accept Chinese field names in the view; preserve the stored contract."""
    aliases = {'性格特点': 'traits', '行为要求': 'behavior_requirements',
               '不希望出现的行为': 'anti_patterns', '数据集编号': 'dataset_id',
               'Import': 'import_id', '记录编号': 'record_id', '用途': 'split',
               '独立验证': 'is_holdout', '来源校验码': 'source_hash'}

    def convert(value):
        if isinstance(value, list):
            return [convert(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            canonical = aliases.get(key, key)
            if canonical in result:
                raise ValueError('同一字段不能同时填写中文和英文名称。')
            if key == '用途':
                item = {'训练': 'train', '验证': 'validation', 'test': 'test'}.get(item, item)
                if item not in ('train', 'validation', 'test'):
                    raise ValueError('请按来源清单将用途填写为“训练”“验证”或“测试”。')
            elif key == '独立验证' and isinstance(item, str):
                if item not in ('是', '否'):
                    raise ValueError('确认来源用途后，请将独立验证填写为“是”或“否”。')
                item = item == '是'
            result[canonical] = convert(item)
        return result

    return convert(json.loads(text))


def create_workbench(parent, scope, control=None):
    """Create an embeddable workbench Frame; the worker remains a separate process."""
    import tkinter as tk
    from tkinter import messagebox, ttk

    service = PersonaJobService(scope, control)
    frame = ttk.Frame(parent, padding=10)
    frame._persona_job_service = service
    frame._persona_after_id = None
    frame._persona_poll_executor = ThreadPoolExecutor(max_workers=1,
                                                       thread_name_prefix='persona-job-list')
    frame._persona_poll_future = None
    frame._persona_read_after_ids = set()
    frame._persona_closed = False
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(2, weight=1)

    source_box = ttk.LabelFrame(frame,
        text=f'好友 {scope.friend_id} 的来源选择（仅本机；不显示消息正文）', padding=8)
    source_box.grid(row=0, column=0, sticky='ew')
    source_box.columnconfigure(1, weight=1)
    sessions_var = tk.StringVar()
    targets_var = tk.StringVar()
    pairs_var = tk.StringVar()
    friends_var = tk.StringVar()

    ttk.Label(source_box, text='会话编号').grid(row=0, column=0, sticky='w')
    sessions_entry = ttk.Entry(source_box, textvariable=sessions_var)
    sessions_entry.grid(row=0, column=1, sticky='ew', padx=5, pady=2)
    ttk.Button(source_box, text='选择会话', command=lambda: choose_sessions()).grid(row=0, column=2)
    ttk.Label(source_box, text='目标说话人编号').grid(row=1, column=0, sticky='w')
    ttk.Entry(source_box, textvariable=targets_var).grid(row=1, column=1, sticky='ew', padx=5, pady=2)
    ttk.Label(source_box, text='对话片段编号').grid(row=2, column=0, sticky='w')
    ttk.Entry(source_box, textvariable=pairs_var).grid(row=2, column=1, sticky='ew', padx=5, pady=2)
    ttk.Button(source_box, text='选择对话片段', command=lambda: choose_pairs()).grid(row=2, column=2)
    ttk.Label(source_box, text='关系分析对象编号（可选）').grid(row=3, column=0, sticky='w')
    ttk.Entry(source_box, textvariable=friends_var).grid(row=3, column=1, sticky='ew', padx=5, pady=2)

    tabs = ttk.Notebook(frame)
    tabs.grid(row=1, column=0, sticky='ew', pady=8)
    preset = ttk.Frame(tabs, padding=8)
    advanced = ttk.Frame(tabs, padding=8)
    tabs.add(preset, text='预设流程')
    tabs.add(advanced, text='高级模式')

    def entry(parent_widget, row, label, default=''):
        ttk.Label(parent_widget, text=label).grid(row=row, column=0, sticky='w', pady=2)
        value = tk.StringVar(value=default)
        ttk.Entry(parent_widget, textvariable=value).grid(row=row, column=1, sticky='ew', padx=5, pady=2)
        parent_widget.columnconfigure(1, weight=1)
        return value

    pack_id = entry(preset, 0, '人格包编号')
    ttk.Label(preset, text='目标描述（留空时由模型从“基本信息”备注和“聊天风格”自动提取，可修改）').grid(
        row=1, column=0, sticky='w')
    ttk.Button(preset, text='从基本信息和聊天风格提取', command=lambda: fill_target_spec()).grid(
        row=1, column=1, sticky='e')
    target_spec_text = tk.Text(preset, height=5, width=72)
    target_spec_text.grid(row=2, column=0, columnspan=2, sticky='ew', pady=3)
    ttk.Label(preset, text='来源用途：请逐条注明制作或验证用途，以及是否独立验证。').grid(
        row=3, column=0, columnspan=2, sticky='w')
    partitions_text = tk.Text(preset, height=5, width=72)
    partitions_text.insert('1.0', '[{"数据集编号":"...","导入编号":"...","记录编号":"...",'
        '"用途":"按来源清单填写训练、验证或测试","独立验证":"确认后填写是或否","来源校验码":"..."}]')
    partitions_text.grid(row=4, column=0, columnspan=2, sticky='ew', pady=3)
    ttk.Label(preset, text='一键运行：规则筛选 → 本地初筛 → 质量评审 → 多样性筛选 → 人格提炼 → 证据核验 → 发布。评审仅使用已验证的公共数据，可能产生调用费用。').grid(
        row=5, column=0, columnspan=2, sticky='w', pady=3)
    ttk.Button(preset, text='运行预设流程', command=lambda: submit_preset()).grid(row=6, column=1, sticky='e')

    stage_labels = {spec.label.replace('Pack', '人格包'): kind for kind, spec in ADVANCED_STAGES.items()}
    stage_var = tk.StringVar(value=ADVANCED_STAGES['distill'].label)
    ttk.Label(advanced, text='可独立执行阶段').grid(row=0, column=0, sticky='w')
    stage_box = ttk.Combobox(advanced, textvariable=stage_var, state='readonly',
                             values=tuple(stage_labels))
    stage_box.grid(row=0, column=1, sticky='ew', padx=5, pady=2)
    pack_media_id = entry(advanced, 1, '媒体补版专用 人格包编号')
    pack_media_version = entry(advanced, 2, '媒体补版使用的原版本')
    pack_media_hash = entry(advanced, 3, '原版本文件校验码')
    local_var = tk.StringVar(value=next(iter(MODEL_CHOICES)))
    ttk.Label(advanced, text='蒸馏时使用的模型').grid(row=4, column=0, sticky='w')
    ttk.Combobox(advanced, textvariable=local_var, state='readonly',
                 values=tuple(MODEL_CHOICES)).grid(row=4, column=1, sticky='ew', padx=5, pady=2)
    stage_help = ttk.Label(advanced, text='')
    stage_help.grid(row=6, column=0, columnspan=2, sticky='w', pady=5)

    def update_stage_help(*_):
        kind = stage_labels.get(stage_var.get())
        spec = ADVANCED_STAGES.get(kind)
        text = (spec.note + ' 输出：' + spec.output) if spec else ''
        for old, new in (('PersonaVersion', '人格版本'), ('DRAFT', 'lol'),
                         ('Pack', '人格包'), ('Job', '任务'), ('Judge', '质量评审')):
            text = text.replace(old, new)
        stage_help.configure(text=text)

    stage_box.bind('<<ComboboxSelected>>', update_stage_help)
    update_stage_help()
    ttk.Label(advanced, text='高级模式不开放人格包内部步骤；缺少来源或基版时会提示，需先补齐输入或运行预设流程。').grid(
        row=7, column=0, columnspan=2, sticky='w')
    ttk.Button(advanced, text='运行所选阶段', command=lambda: submit_advanced()).grid(
        row=8, column=1, sticky='e')
    ttk.Button(advanced, text='选择当前生效的人格包原版本', command=lambda: choose_pack_base()).grid(
        row=5, column=1, sticky='w')

    jobs_box = ttk.LabelFrame(frame, text='最近任务（显示已完成进度；未记录费用时会注明）', padding=6)
    jobs_box.grid(row=2, column=0, sticky='nsew')
    jobs_box.columnconfigure(0, weight=1)
    jobs_box.rowconfigure(0, weight=1)
    columns = ('id', 'kind', 'state', 'progress', 'usage')
    tree = ttk.Treeview(jobs_box, columns=columns, show='headings', height=8)
    for name, title, width in (('id', '任务编号', 210), ('kind', '类型', 90),
                               ('state', '状态', 90), ('progress', '检查点', 90),
                               ('usage', '用量/成本', 330)):
        tree.heading(name, text=title)
        tree.column(name, width=width, stretch=name in ('id', 'usage'))
    tree.grid(row=0, column=0, sticky='nsew')
    scrollbar = ttk.Scrollbar(jobs_box, orient='vertical', command=tree.yview)
    scrollbar.grid(row=0, column=1, sticky='ns')
    tree.configure(yscrollcommand=scrollbar.set)
    controls = ttk.Frame(jobs_box)
    controls.grid(row=1, column=0, sticky='ew', pady=5)
    detail = tk.StringVar(value='')
    ttk.Label(controls, textvariable=detail).pack(side='left', fill='x', expand=True)

    def selected_job_id():
        selection = tree.selection()
        return selection[0] if selection else None

    def act(action):
        job_id = selected_job_id()
        if not job_id:
            return
        try:
            service.act(job_id, action)
            schedule_refresh(0)
        except Exception as exc:
            messagebox.showerror('任务操作失败', str(exc), parent=frame.winfo_toplevel())

    pause_button = ttk.Button(controls, text='Pause', command=lambda: act('pause'))
    pause_button.pack(side='left')
    cancel_button = ttk.Button(controls, text='Cancel', command=lambda: act('cancel'))
    cancel_button.pack(side='left', padx=3)
    resume_button = ttk.Button(controls, text='/Resume', command=lambda: act('start_or_resume'))
    resume_button.pack(side='left')
    for button in (pause_button, cancel_button, resume_button):
        button.configure(state='disabled')

    kind_labels = {'pack': '预设流程', 'distill': '本地蒸馏草稿',
                   'pack_media': '公共 Pack 媒体补版'}
    state_labels = {'queued': 'medium', 'running': 'running', 'paused': 'paused',
                    'cancelled': 'cancelled', 'failed': 'failed', 'completed': 'completed'}
    action_labels = {'reuse': 'completed, ', 'start': '排队中，可启动',
                     'monitor': '正在运行', 'resume': 'Resume',
                     'blocked_unknown_usage': '用量未确认，禁止自动重试',
                     'blocked': '当前状态不可操作'}

    def schedule_refresh(delay):
        if frame._persona_closed:
            return
        if frame._persona_after_id is not None:
            try:
                frame.after_cancel(frame._persona_after_id)
            except tk.TclError:
                pass

        def run_refresh():
            frame._persona_after_id = None
            refresh()

        frame._persona_after_id = frame.after(delay, run_refresh)

    def refresh(*_):
        if frame._persona_closed:
            return
        future = frame._persona_poll_future
        if future is None:
            future = frame._persona_poll_executor.submit(_read_recent_jobs, scope)
            frame._persona_poll_future = future
        if not future.done():
            schedule_refresh(75)
            return
        try:
            jobs = future.result()
            selected = selected_job_id()
            for item in tree.get_children():
                tree.delete(item)
            for job in jobs:
                tree.insert('', 'end', iid=job['id'], values=(job['id'][:20],
                    kind_labels.get(job['kind'], job['kind']),
                    state_labels.get(job['state'], job['state']),
                    job_progress_label(job), job_usage_label(job)))
            if selected in tree.get_children():
                tree.selection_set(selected)
                select_details()
        except Exception as exc:
            detail.set('读取任务状态失败：' + str(exc))
        finally:
            frame._persona_poll_future = None
        schedule_refresh(1500)

    def select_details(_event=None):
        job_id = selected_job_id()
        if not job_id:
            detail.set('')
            pause_button.configure(state='disabled')
            cancel_button.configure(state='disabled')
            resume_button.configure(state='disabled')
            return
        job = service.control.read_status(job_id)
        action = existing_job_action(job)
        detail.set(job_usage_label(job) + '；操作状态：' + action_labels.get(action, action))
        pause_button.configure(state=('normal' if job['state'] in ('queued', 'running')
                                      else 'disabled'))
        cancel_button.configure(state=('normal' if job['state'] not in ('completed', 'cancelled')
                                       else 'disabled'))
        resume_button.configure(state=('normal' if action in ('start', 'resume') else 'disabled'))

    tree.bind('<<TreeviewSelect>>', select_details)

    def async_read(task, title, on_success):
        future = frame._persona_poll_executor.submit(task)

        def schedule_collect():
            def run_collect():
                frame._persona_read_after_ids.discard(callback_id)
                collect()

            callback_id = frame.after(75, run_collect)
            frame._persona_read_after_ids.add(callback_id)

        def collect():
            if frame._persona_closed:
                return
            if not future.done():
                schedule_collect()
                return
            try:
                on_success(future.result())
            except Exception as exc:
                messagebox.showerror(title, str(exc), parent=frame.winfo_toplevel())

        schedule_collect()

    def chosen_ids(value, name, required=False):
        return parse_ids(value.get(), name, required=required)

    def fill_target_spec(then=None):
        from friend_persona_spec import extract_target_spec

        def show(spec):
            labels = {'traits': '性格特点', 'behavior_requirements': '行为要求',
                      'anti_patterns': '不希望出现的行为'}
            target_spec_text.delete('1.0', 'end')
            target_spec_text.insert('1.0', json.dumps({labels[k]: v for k, v in spec.items()},
                                                      ensure_ascii=False, indent=1))
            if then:
                then()

        detail.set('正在从基本信息和聊天风格提取目标描述…')
        async_read(lambda: extract_target_spec(scope), '提取目标描述失败', show)

    def submit_preset():
        if not target_spec_text.get('1.0', 'end').strip():
            fill_target_spec(then=submit_preset)
            return
        try:
            spec = parse_workbench_json(target_spec_text.get('1.0', 'end'))
            partitions = parse_workbench_json(partitions_text.get('1.0', 'end'))
            job = service.enqueue_preset(pack_id=pack_id.get().strip(),
                session_ids=sessions_var.get(), pair_ids=pairs_var.get(),
                target_speaker_ids=targets_var.get(), target_spec=spec,
                source_partitions=partitions)
            label = ('复用已完成的预设结果' if existing_job_action(job) == 'reuse'
                     else '预设任务')
            detail.set(label + ' ' + job['id'] + ': ' + state_labels.get(job['state'], job['state']) +
                       '; ' + job_usage_label(job))
            schedule_refresh(0)
        except Exception as exc:
            messagebox.showerror('无法运行预设流程', str(exc), parent=frame.winfo_toplevel())

    def submit_advanced():
        try:
            kind = stage_labels.get(stage_var.get())
            if kind == 'distill':
                job = service.enqueue_advanced(kind,
                    session_ids=chosen_ids(sessions_var, 'session_ids', True),
                    pair_ids=chosen_ids(pairs_var, 'pair_ids'),
                    target_speaker_ids=chosen_ids(targets_var, 'target_speaker_ids'),
                    friend_speaker_ids=chosen_ids(friends_var, 'friend_speaker_ids'),
                    use_local_model=MODEL_CHOICES[local_var.get()])
            else:
                job = service.enqueue_advanced(kind, pack_id=pack_media_id.get().strip(),
                    base_version=pack_media_version.get().strip(),
                    base_manifest_sha256=pack_media_hash.get().strip())
            label = ('复用已完成的高级任务结果' if existing_job_action(job) == 'reuse'
                     else '高级任务')
            detail.set(label + ' ' + job['id'] + ': ' + state_labels.get(job['state'], job['state']) +
                       '; ' + job_usage_label(job))
            schedule_refresh(0)
        except Exception as exc:
            messagebox.showerror('无法运行所选阶段', str(exc), parent=frame.winfo_toplevel())

    def choose_sessions():
        async_read(service.list_sessions, '读取会话失败', lambda choices: _choose_values(frame,
            choices,
            lambda row: f"{row['start_time'] or '?'} — {row['end_time'] or '?'} | "
                        f"{', '.join(row['speaker_ids'])} | {row['id']}",
            lambda selected: sessions_var.set(', '.join(row['id'] for row in selected))))

    def choose_pairs():
        try:
            session_ids = chosen_ids(sessions_var, 'session_ids', True)
            targets = chosen_ids(targets_var, 'target_speaker_ids')
        except Exception as exc:
            messagebox.showerror('读取对话片段失败', str(exc), parent=frame.winfo_toplevel())
            return
        async_read(lambda: service.list_pairs(session_ids, targets), '读取对话片段失败',
            lambda choices: _choose_values(frame, choices,
                lambda row: f"{row['target_speaker_id']} | {row['session_id']} | {row['id']}",
                lambda selected: pairs_var.set(', '.join(row['id'] for row in selected))))

    def choose_pack_base():
        choices = active_pack_bases(scope.project_root)
        if not choices:
            messagebox.showinfo('没有可用基版', '未找到可补版的当前生效的公共人格包。',
                                parent=frame.winfo_toplevel())
            return

        def accept(selected):
            if len(selected) != 1:
                return
            base = selected[0]
            pack_media_id.set(base['pack_id'])
            pack_media_version.set(base['version'])
            pack_media_hash.set(base['manifest_sha256'])

        _choose_values(frame, choices,
            lambda row: f"{row['pack_id']} | {row['version']} | {row['manifest_sha256']}", accept)

    def close(_event=None):
        if frame._persona_closed:
            return
        frame._persona_closed = True
        if frame._persona_after_id is not None:
            try:
                frame.after_cancel(frame._persona_after_id)
            except tk.TclError:
                pass
        for callback_id in frame._persona_read_after_ids:
            try:
                frame.after_cancel(callback_id)
            except tk.TclError:
                pass
        frame._persona_read_after_ids.clear()
        frame._persona_poll_executor.shutdown(wait=False, cancel_futures=True)
        service.close()

    frame.bind('<Destroy>', lambda event: close() if event.widget is frame else None)
    schedule_refresh(0)
    return frame


def _choose_values(parent, rows, label, on_accept):
    import tkinter as tk
    from tkinter import ttk

    dialog = tk.Toplevel(parent.winfo_toplevel())
    dialog.title('选择已验证来源')
    dialog.transient(parent.winfo_toplevel())
    dialog.geometry('760x420')
    dialog.columnconfigure(0, weight=1)
    dialog.rowconfigure(0, weight=1)
    listing = tk.Listbox(dialog, selectmode='extended', exportselection=False)
    listing.grid(row=0, column=0, sticky='nsew', padx=(8, 0), pady=8)
    scroll = ttk.Scrollbar(dialog, orient='vertical', command=listing.yview)
    scroll.grid(row=0, column=1, sticky='ns', padx=(0, 8), pady=8)
    listing.configure(yscrollcommand=scroll.set)
    for row in rows:
        listing.insert('end', label(row))

    def accept():
        selected = [rows[index] for index in listing.curselection()]
        on_accept(selected)
        dialog.destroy()

    ttk.Button(dialog, text='确定', command=accept).grid(row=1, column=0, sticky='e', padx=8, pady=8)

