"""Friend-scoped, local-only A/B feedback workbench."""
from concurrent.futures import ThreadPoolExecutor
from queue import Empty, SimpleQueue
import uuid

from persona_types import FriendScope, ScopeMismatch


CHOICES = (
    ('A', '\u0041 \u66f4\u50cf'),
    ('B', '\u0042 \u66f4\u50cf'),
    ('similar', '\u5dee\u4e0d\u591a'),
    ('neither', '\u90fd\u4e0d\u50cf'),
)
KINDS = (('context_only', '\u4ec5\u4e0a\u4e0b\u6587'), ('offline_reply', '\u79bb\u7ebf\u56de\u590d'))


class PersonaFeedbackService:
    def __init__(self, scope):
        if not isinstance(scope, FriendScope):
            raise ScopeMismatch('a verified FriendScope is required')
        self.scope = FriendScope.load(scope.friend_id, project_root=scope.project_root,
            friend_dir=scope.friend_dir, allowed_pack_versions=scope.allowed_pack_versions)

    def list_versions(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('version limit must be between 1 and 500')
        from persona_store import PersonaStore

        store = PersonaStore(self.scope.friend_id, self.scope.friend_dir, readonly=True,
                             project_root=self.scope.project_root)
        try:
            rows = tuple(row for row in reversed(store.artifacts()) if row['state'] == 'COMPLETED')
            return rows[:limit]
        finally:
            store.close()

    def list_recent(self, limit=100):
        from persona_feedback_store import PersonaFeedbackStore

        store = PersonaFeedbackStore(self.scope, readonly=True)
        try:
            return store.list_recent(limit)
        finally:
            store.close()

    def initialize_feedback_store(self):
        from persona_feedback_store import PersonaFeedbackStore
        from source_state import source_lock

        with source_lock(self.scope.friend_dir):
            store = PersonaFeedbackStore(self.scope, readonly=False)
            try:
                return None
            finally:
                store.close()

    def save_feedback(self, *, comparison_id, a_version, b_version, comparison_kind, choice):
        from persona_feedback_store import PersonaFeedbackStore

        reader = PersonaFeedbackStore(self.scope, readonly=True)
        reader.close()
        store = PersonaFeedbackStore(self.scope, readonly=False)
        try:
            return store.save({
                'friend_id': self.scope.friend_id,
                'comparison_id': comparison_id,
                'a_version': a_version,
                'b_version': b_version,
                'comparison_kind': comparison_kind,
                'choice': choice,
            })
        finally:
            store.close()


def _candidate_valid(text):
    return isinstance(text, str) and bool(text.strip())


def _can_start_comparison(a_version, b_version, a_candidate, b_candidate):
    return (bool(a_version) and bool(b_version) and a_version != b_version and
            _candidate_valid(a_candidate) and _candidate_valid(b_candidate))


def _freeze_comparison(friend_id, a_version, b_version, comparison_kind,
                       a_candidate, b_candidate):
    if not a_version or not b_version or a_version == b_version:
        raise ValueError('comparison requires two different versions')
    if not _candidate_valid(a_candidate) or not _candidate_valid(b_candidate):
        raise ValueError('comparison requires two nonempty candidates')
    if comparison_kind not in {value for value, _label in KINDS}:
        raise ValueError('unknown comparison kind')
    return {
        'comparison_id': uuid.uuid4().hex,
        'friend_id': friend_id,
        'a_version': a_version,
        'b_version': b_version,
        'comparison_kind': comparison_kind,
        'a_candidate': a_candidate,
        'b_candidate': b_candidate,
    }


def comparison_preview(comparison):
    kind = dict(KINDS)[comparison['comparison_kind']]
    item = ('\u4e24\u4efd\u5b9e\u9645\u4e0a\u4e0b\u6587'
            if comparison['comparison_kind'] == 'context_only'
            else '\u4e24\u4efd\u79bb\u7ebf\u56de\u590d\u5019\u9009')
    return (f"\u597d\u53cb {comparison['friend_id']} \u00b7 {kind} \u00b7 {item}\n"
            f"A \u7248\u672c\uff1a{comparison['a_version']}\n"
            f"{comparison['a_candidate']}\n\n"
            f"B \u7248\u672c\uff1a{comparison['b_version']}\n"
            f"{comparison['b_candidate']}\n\n"
            '\u64cd\u4f5c\u8005\u63d0\u4f9b\u7684\u5019\u9009\u5185\u5bb9\uff1b\u672c\u754c\u9762\u4e0d\u751f\u6210\u3001\u6765\u6e90\u672a\u6838\u9a8c\u3001\u4e0d\u53d1\u9001 QQ\u3002')


def create_workbench(parent, scope, service=None):
    """Create an embeddable Tk feedback view for one explicit friend scope."""
    import tkinter as tk
    from tkinter import messagebox, ttk

    service = service or PersonaFeedbackService(scope)
    if not isinstance(scope, FriendScope) or service.scope != scope:
        raise ScopeMismatch('workbench and service must use the same FriendScope')

    frame = ttk.Frame(parent, padding=8)
    frame._persona_feedback_closed = False
    frame._persona_feedback_after_ids = set()
    frame.columnconfigure(0, weight=1)
    ttk.Label(frame, text=f'\u597d\u53cb {scope.friend_id} \u7684 A/B \u4eba\u5de5\u53cd\u9988\uff08\u672c\u673a\uff09').grid(
        row=0, column=0, sticky='w')
    ttk.Label(frame, text='\u5019\u9009\u7531\u64cd\u4f5c\u8005\u63d0\u4f9b\uff1b\u672c\u754c\u9762\u4e0d\u751f\u6210\uff0c\u6765\u6e90\u672a\u6838\u9a8c\uff0c\u4e0d\u53d1 QQ\u3002\u4eba\u5de5\u8bc4\u4ef7\u4e0d\u662f Persona \u4e8b\u5b9e\u3002').grid(
        row=1, column=0, sticky='w')

    controls = ttk.Frame(frame)
    controls.grid(row=2, column=0, sticky='ew', pady=6)
    a_version, b_version = tk.StringVar(value='legacy'), tk.StringVar()
    kind = tk.StringVar(value=KINDS[0][0])
    ttk.Label(controls, text='A \u7248\u672c').grid(row=0, column=0, sticky='w')
    a_box = ttk.Combobox(controls, textvariable=a_version, state='readonly', width=34)
    a_box.grid(row=0, column=1, padx=4)
    ttk.Label(controls, text='B \u7248\u672c').grid(row=0, column=2, sticky='w')
    b_box = ttk.Combobox(controls, textvariable=b_version, state='readonly', width=34)
    b_box.grid(row=0, column=3, padx=4)
    ttk.Label(controls, text='\u6bd4\u8f83\u7c7b\u578b').grid(row=0, column=4, sticky='w')
    kind_box = ttk.Combobox(controls, textvariable=kind, state='readonly', width=18,
        values=tuple(value for value, _label in KINDS))
    kind_box.grid(row=0, column=5, padx=4)

    candidates = ttk.Frame(frame)
    candidates.grid(row=3, column=0, sticky='nsew')
    candidates.columnconfigure(0, weight=1)
    candidates.columnconfigure(1, weight=1)
    a_candidate_label = tk.StringVar()
    b_candidate_label = tk.StringVar()
    ttk.Label(candidates, textvariable=a_candidate_label).grid(row=0, column=0, sticky='w')
    ttk.Label(candidates, textvariable=b_candidate_label).grid(row=0, column=1, sticky='w')
    a_candidate = tk.Text(candidates, height=6, wrap='word')
    b_candidate = tk.Text(candidates, height=6, wrap='word')
    a_candidate.grid(row=1, column=0, sticky='nsew', padx=(0, 4))
    b_candidate.grid(row=1, column=1, sticky='nsew', padx=(4, 0))
    start_button = ttk.Button(frame, text='\u5f00\u59cb\u6bd4\u8f83')
    start_button.grid(row=4, column=0, sticky='e', pady=4)
    preview = tk.Text(frame, height=12, wrap='word', state='disabled')
    preview.grid(row=5, column=0, sticky='nsew')
    frame.rowconfigure(5, weight=1)
    version_status = tk.StringVar()
    feedback_status = tk.StringVar()
    ttk.Label(frame, textvariable=version_status).grid(row=6, column=0, sticky='w')
    ttk.Label(frame, textvariable=feedback_status).grid(row=7, column=0, sticky='w')
    recent = tk.Text(frame, height=7, wrap='word', state='disabled')
    recent.grid(row=8, column=0, sticky='nsew', pady=6)
    frame.rowconfigure(8, weight=1)

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='persona-feedback-ui')
    frame._persona_feedback_executor = executor
    completed = SimpleQueue()
    poll_timer = [None]
    current = [None]
    saving = [False]
    blocked = [False]
    choice_buttons = []

    def show_preview(value):
        preview.configure(state='normal')
        preview.delete('1.0', 'end')
        preview.insert('1.0', value)
        preview.configure(state='disabled')

    def show_recent(rows):
        recent.configure(state='normal')
        recent.delete('1.0', 'end')
        for row in rows:
            recent.insert('end', f"{row['created_at']}  {row['choice']}  "
                f"A={row['a_version']} / B={row['b_version']}  "
                f"{dict(KINDS)[row['comparison_kind']]}  HUMAN_SUBJECTIVE\n")
        recent.configure(state='disabled')

    def set_choice_state():
        state = 'normal' if current[0] is not None and not saving[0] and not blocked[0] else 'disabled'
        for button in choice_buttons:
            button.configure(state=state)

    def selected_text(widget):
        return widget.get('1.0', 'end-1c')

    def can_start():
        a, b = selected_text(a_candidate), selected_text(b_candidate)
        return _can_start_comparison(a_version.get(), b_version.get(), a, b)

    def set_inputs_state(enabled):
        state = 'readonly' if enabled else 'disabled'
        a_box.configure(state=state)
        b_box.configure(state=state)
        kind_box.configure(state=state)
        a_candidate.configure(state='normal' if enabled else 'disabled')
        b_candidate.configure(state='normal' if enabled else 'disabled')
        start_button.configure(state='normal' if enabled and can_start() else 'disabled')

    def update_candidate_labels():
        kind_label = dict(KINDS).get(kind.get(), kind.get())
        suffix = '\u4e0a\u4e0b\u6587' if kind.get() == 'context_only' else '\u5019\u9009\u56de\u590d'
        a_candidate_label.set(f'A {kind_label} {suffix}')
        b_candidate_label.set(f'B {kind_label} {suffix}')
        start_button.configure(state='normal' if not saving[0] and can_start() else 'disabled')

    def invalidate():
        current[0] = None
        blocked[0] = False
        show_preview('\u5019\u9009\u6216\u6bd4\u8f83\u9009\u62e9\u5df2\u53d8\u66f4\uff1b\u8bf7\u91cd\u65b0\u5f00\u59cb\u6bd4\u8f83\u3002')
        set_choice_state()
        if not saving[0]:
            start_button.configure(state='normal' if can_start() else 'disabled')

    def candidate_changed(event):
        widget = event.widget
        if widget.edit_modified():
            widget.edit_modified(False)
            invalidate()

    def selection_changed(_event=None):
        invalidate()
        update_candidate_labels()

    def poll():
        if frame._persona_feedback_closed:
            return
        if poll_timer[0] is not None:
            frame._persona_feedback_after_ids.discard(poll_timer[0])
        while True:
            try:
                callback = completed.get_nowait()
            except Empty:
                break
            callback()
        poll_timer[0] = frame.after(50, poll)
        frame._persona_feedback_after_ids.add(poll_timer[0])

    def run(task, on_success, title, on_error=None):
        future = executor.submit(task)

        def finished(value):
            def deliver():
                if frame._persona_feedback_closed:
                    return
                try:
                    on_success(value.result())
                except Exception as exc:
                    if on_error:
                        on_error(exc)
                    else:
                        messagebox.showerror(title, str(exc), parent=frame.winfo_toplevel())
            completed.put(deliver)

        future.add_done_callback(finished)

    def refresh():
        if frame._persona_feedback_closed:
            return
        invalidate()

        def read_data():
            try:
                versions = service.list_versions()
                version_error = None
            except Exception as exc:
                versions, version_error = (), exc
            try:
                rows = service.list_recent()
                feedback_error = None
            except Exception as exc:
                rows, feedback_error = (), exc
            return versions, version_error, rows, feedback_error

        def loaded(result):
            versions, version_error, rows, feedback_error = result
            if version_error:
                version_status.set('\u7248\u672c\u8bfb\u53d6\u5931\u8d25\uff1a' + str(version_error))
                a_box.configure(values=('legacy',))
                b_box.configure(values=())
                a_version.set('legacy')
                b_version.set('')
            else:
                version_status.set(f'\u7248\u672c\u5df2\u8bfb\u53d6\uff1a{len(versions)}')
                version_ids = tuple(row['id'] for row in versions)
                a_values = ('legacy',) + version_ids
                a_box.configure(values=a_values)
                b_box.configure(values=version_ids)
                if a_version.get() not in a_values:
                    a_version.set('legacy')
                if b_version.get() not in version_ids:
                    b_version.set(version_ids[0] if version_ids else '')
            if feedback_error:
                feedback_status.set('\u53cd\u9988\u8bfb\u53d6\u5931\u8d25\uff1a' + str(feedback_error))
            else:
                feedback_status.set(f'\u53cd\u9988\u5df2\u8bfb\u53d6\uff1a{len(rows)}')
            show_recent(rows)
            update_candidate_labels()

        run(read_data, loaded,
            '\u8bfb\u53d6\u53cd\u9988\u5931\u8d25')

    def initialize_feedback_store():
        initialize_button.configure(state='disabled')

        def initialized(_result):
            initialize_button.configure(state='normal')
            refresh()

        def failed(exc):
            initialize_button.configure(state='normal')
            messagebox.showerror('\u521d\u59cb\u5316\u53cd\u9988\u5b58\u50a8\u5931\u8d25', str(exc),
                                 parent=frame.winfo_toplevel())

        run(service.initialize_feedback_store, initialized,
            '\u521d\u59cb\u5316\u53cd\u9988\u5b58\u50a8\u5931\u8d25', failed)

    def begin_compare():
        if saving[0] or not can_start():
            return
        current[0] = _freeze_comparison(scope.friend_id, a_version.get(), b_version.get(),
            kind.get(), selected_text(a_candidate), selected_text(b_candidate))
        blocked[0] = False
        show_preview(comparison_preview(current[0]))
        set_choice_state()

    def submit(choice):
        comparison = current[0]
        if comparison is None or saving[0] or blocked[0]:
            return
        saving[0] = True
        set_inputs_state(False)
        set_choice_state()

        def saved(_row):
            saving[0] = False
            set_inputs_state(True)
            refresh()

        def failed(exc):
            saving[0] = False
            from persona_store import Conflict
            if isinstance(exc, Conflict):
                blocked[0] = True
                message = ('\u8fd9\u4e2a\u6bd4\u8f83\u5df2\u6709\u4e0d\u540c\u7684\u4eba\u5de5\u9009\u62e9\uff0c\u65e7\u9009\u62e9\u4e0d\u4f1a\u88ab\u8986\u76d6\u3002'
                           '\u70b9\u51fb\u201c\u5f00\u59cb\u6bd4\u8f83\u201d\u660e\u786e\u5efa\u7acb\u65b0\u6bd4\u8f83\u3002')
                messagebox.showerror('\u4fdd\u5b58\u53cd\u9988\u51b2\u7a81', message,
                                     parent=frame.winfo_toplevel())
            else:
                messagebox.showerror('\u4fdd\u5b58\u53cd\u9988\u5931\u8d25', str(exc),
                                     parent=frame.winfo_toplevel())
            set_inputs_state(True)
            set_choice_state()

        run(lambda: service.save_feedback(
            comparison_id=comparison['comparison_id'],
            a_version=comparison['a_version'], b_version=comparison['b_version'],
            comparison_kind=comparison['comparison_kind'], choice=choice),
            saved, '\u4fdd\u5b58\u53cd\u9988\u5931\u8d25', failed)

    start_button.configure(command=begin_compare)
    for widget in (a_candidate, b_candidate):
        widget.bind('<<Modified>>', candidate_changed)
        widget.edit_modified(False)
    for box in (a_box, b_box):
        box.bind('<<ComboboxSelected>>', selection_changed)
    kind_box.bind('<<ComboboxSelected>>', lambda event: (selection_changed(event), update_candidate_labels()))
    buttons = ttk.Frame(frame)
    buttons.grid(row=9, column=0, sticky='e')
    initialize_button = ttk.Button(buttons,
        text='\u521d\u59cb\u5316\uff0f\u5347\u7ea7\u53cd\u9988\u5b58\u50a8\uff08V18\uff09',
        command=initialize_feedback_store)
    initialize_button.pack(side='left', padx=3)
    for value, label in CHOICES:
        button = ttk.Button(buttons, text=label, command=lambda choice=value: submit(choice),
                            state='disabled')
        button.pack(side='left', padx=3)
        choice_buttons.append(button)
    ttk.Button(buttons, text='\u5237\u65b0', command=refresh).pack(side='left', padx=3)
    update_candidate_labels()

    def close(_event=None):
        if frame._persona_feedback_closed:
            return
        frame._persona_feedback_closed = True
        for timer in frame._persona_feedback_after_ids:
            try:
                frame.after_cancel(timer)
            except tk.TclError:
                pass
        frame._persona_feedback_after_ids.clear()
        executor.shutdown(wait=False, cancel_futures=True)

    frame.bind('<Destroy>', lambda event: close() if event.widget is frame else None)
    poll_timer[0] = frame.after(50, poll)
    frame._persona_feedback_after_ids.add(poll_timer[0])
    refresh()
    return frame
