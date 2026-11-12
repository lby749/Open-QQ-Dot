import unittest
try:
    import tkinter
except Exception:
    tkinter = None
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock, patch

from persona_gui import PersonaJobService, parse_ids
from persona_stage_plan import (ADVANCED_STAGES, existing_job_action,
                                job_progress_label, job_usage_label,
                                plan_advanced_stage)


class FakeControl:
    def __init__(self, state='queued', *, tokens_unknown=0, attempt=None, checkpoint=None):
        self.job = {'id': 'job-1', 'kind': 'distill', 'state': state,
                    'tokens_unknown': tokens_unknown, 'attempt': attempt or {},
                    'checkpoint': checkpoint or {}, 'input': {}, 'source_refs': []}
        self.calls = []

    def enqueue(self, kind, payload, refs):
        self.calls.append(('enqueue', kind, payload, tuple(refs)))
        return self.job['id']

    def read_status(self, job_id):
        self.calls.append(('read_status', job_id))
        return self.job

    def spawn_worker(self, job_id):
        self.calls.append(('spawn_worker', job_id))
        return 123

    def pause(self, job_id):
        self.calls.append(('pause', job_id))
        self.job['state'] = 'paused'

    def cancel(self, job_id):
        self.calls.append(('cancel', job_id))
        self.job['state'] = 'cancelled'

    def retry(self, job_id):
        self.calls.append(('retry', job_id))
        self.job['state'] = 'queued'


class PersonaStagePlanTests(unittest.TestCase):
    def test_advanced_mode_exposes_only_existing_independent_jobs(self):
        self.assertEqual(set(ADVANCED_STAGES), {'distill', 'pack_media'})
        plan = plan_advanced_stage('distill', {
            'selected_sessions': True, 'verified_source_refs': False})
        self.assertFalse(plan.ready)
        self.assertIn('来源通过同一好友范围的校验', plan.missing)
        self.assertFalse(plan_advanced_stage('judge', {}).ready)

    def test_exact_completed_jobs_are_reused_and_uncertain_jobs_are_blocked(self):
        self.assertEqual(existing_job_action({'state': 'completed'}), 'reuse')
        self.assertEqual(existing_job_action({'state': 'queued'}), 'start')
        self.assertEqual(existing_job_action({'state': 'paused'}), 'resume')
        self.assertEqual(existing_job_action({'state': 'cancelled'}), 'blocked')
        self.assertEqual(existing_job_action({'state': 'failed', 'tokens_unknown': 1}),
                         'blocked_unknown_usage')

    def test_usage_is_never_reported_as_zero_when_unknown_or_missing(self):
        self.assertIn('用量未知', job_usage_label({'kind': 'pack', 'tokens_unknown': 1}))
        self.assertIn('金额未记录', job_usage_label({'kind': 'pack', 'tokens_unknown': 0,
            'state': 'completed', 'checkpoint': {}}))
        self.assertIn('150 tokens', job_usage_label({'kind': 'pack', 'tokens_unknown': 0,
            'checkpoint': {'b1': {'result': {'usage': {'status': 'known',
                'input_tokens': 100, 'output_tokens': 50}}}}}))

    def test_progress_reports_checkpoint_without_guessing_a_percentage(self):
        self.assertEqual(job_progress_label({'checkpoint': {}}), '尚无 checkpoint')
        self.assertIn('judge', job_progress_label({'checkpoint': {
            'b1': {'stage': 'judge'}, 'b2': {'stage': 'judge'}}}))

    def test_real_nested_judge_usage_is_deduplicated_and_conflicts_are_unknown(self):
        usage = {'status': 'known', 'input_tokens': 100, 'output_tokens': 50, 'total_tokens': 150}
        def checkpoint(value):
            return {'hash': 'batch-hash', 'result': {'stage': 'judge', 'run_id': 'run-1',
                                                     'result': {'usage': value}}}
        job = {'kind': 'pack', 'checkpoint': {'z': checkpoint(usage), 'a': checkpoint(dict(usage))}}
        self.assertIn('150 tokens', job_usage_label(job))
        for other in (dict(usage, output_tokens=51, total_tokens=151), dict(usage, status='unknown'), None):
            with self.subTest(other=other):
                job['checkpoint']['a'] = checkpoint(other)
                self.assertIn('用量未知', job_usage_label(job))

    def test_nested_progress_is_a_stage_summary_without_chronology(self):
        job = {'checkpoint': {'z': {'hash': '1', 'result': {'stage': 'judge'}},
                              'a': {'hash': '2', 'result': {'stage': 'distill'}}}}
        label = job_progress_label(job)
        self.assertIn('2 个 checkpoint', label)
        self.assertIn('judge', label)
        self.assertIn('distill', label)
        self.assertNotIn('最近', label)
        self.assertEqual(label, job_progress_label({'checkpoint': dict(reversed(list(job['checkpoint'].items())))}))


class PersonaJobServiceTests(unittest.TestCase):
    def service(self, control):
        return PersonaJobService(SimpleNamespace(), control=control)

    def test_worker_lifecycle_is_delegated_to_job_api_and_unknown_usage_cannot_resume(self):
        control = FakeControl()
        service = self.service(control)
        service.act('job-1', 'pause')
        self.assertEqual(control.job['state'], 'paused')
        service.act('job-1', 'start_or_resume')
        self.assertEqual(control.job['state'], 'queued')
        self.assertIn(('retry', 'job-1'), control.calls)
        self.assertIn(('spawn_worker', 'job-1'), control.calls)

        uncertain = FakeControl('failed', tokens_unknown=1,
                                attempt={'status': 'UNCERTAIN'})
        with self.assertRaisesRegex(ValueError, '外部用量仍未知'):
            self.service(uncertain).act('job-1', 'start_or_resume')
        self.assertNotIn(('retry', 'job-1'), uncertain.calls)
        self.assertNotIn(('spawn_worker', 'job-1'), uncertain.calls)

    def test_completed_identical_job_is_not_spawned_again(self):
        control = FakeControl('completed')
        service = self.service(control)
        job = service._enqueue('distill', {'session_ids': ['s1']}, ())
        self.assertEqual(job['state'], 'completed')
        self.assertEqual([call[0] for call in control.calls], ['enqueue', 'read_status'])

    def test_id_inputs_are_bounded_unique_and_explicit(self):
        self.assertEqual(parse_ids('a, b\nc', 'ids'), ['a', 'b', 'c'])
        with self.assertRaisesRegex(ValueError, 'unique'):
            parse_ids('a\na', 'ids')
        with self.assertRaisesRegex(ValueError, 'required'):
            parse_ids('', 'ids', required=True)


class PersonaPollingTests(unittest.TestCase):
    def workbench(self):
        import persona_gui

        pending, widgets, futures = {}, [], []
        def after(delay, callback):
            timer = 'timer-' + str(after.count)
            after.count += 1
            pending[timer] = callback
            return timer
        after.count = 0
        def widget(parent=None, **options):
            value = Mock()
            value.options = options
            value.configure.side_effect = lambda **updates: options.update(updates)
            value.after.side_effect = after
            value.after_cancel.side_effect = lambda timer: pending.pop(timer, None)
            value.selection.return_value = ('job-1',)
            value.get_children.return_value = ()
            widgets.append(value)
            return value
        def variable(**options):
            return Mock(get=Mock(return_value=options.get('value', '')))
        def submit(*args):
            future = Future()
            futures.append(future)
            return future
        executor = Mock(submit=Mock(side_effect=submit))
        service = Mock(control=FakeControl('running'))
        ttk = SimpleNamespace(**{name: widget for name in
            ('Frame', 'LabelFrame', 'Label', 'Entry', 'Button', 'Checkbutton', 'Notebook', 'Combobox', 'Treeview', 'Scrollbar')})
        tkinter = SimpleNamespace(ttk=ttk, messagebox=Mock(), StringVar=variable,
                                  BooleanVar=variable, Text=widget, TclError=Exception)
        with patch.dict('sys.modules', {'tkinter': tkinter}), \
                patch.object(persona_gui, 'PersonaJobService', return_value=service), \
                patch.object(persona_gui, 'ThreadPoolExecutor', return_value=executor), \
                patch.object(persona_gui, 'missing_local_model_artifacts', return_value=()):
            frame = persona_gui.create_workbench(Mock(), SimpleNamespace(friend_id='12345', project_root='fixture'))
        def run(timer=None):
            timer = timer or next(iter(pending))
            pending.pop(timer)()
        self.addCleanup(lambda: frame.bind.call_args.args[1](SimpleNamespace(widget=frame)))
        buttons = {value.options['text']: value.options['command'] for value in widgets
                   if 'command' in value.options and 'text' in value.options}
        return frame, pending, futures, executor, service, buttons, run

    def test_repeated_actions_keep_one_poll_timer_and_one_inflight_read(self):
        frame, pending, futures, executor, service, buttons, run = self.workbench()
        run()
        self.assertEqual(len(futures), 1)
        for action in ('Pause', 'Cancel', '/Resume') * 3:
            buttons[action]()
            self.assertEqual(len(pending), 1)
            run()
            self.assertEqual(len(futures), 1)
            self.assertEqual(len(pending), 1)
        futures[0].set_result([])
        run()
        self.assertEqual(len(pending), 1)
        self.assertIsNone(frame._persona_poll_future)
        self.assertEqual(service.act.call_count, 9)

    def test_close_cancels_poll_and_rescheduled_source_read_callbacks(self):
        frame, pending, futures, executor, service, buttons, run = self.workbench()
        run()
        buttons['选择会话']()
        self.assertEqual(len(pending), 2)
        read_timer = next(iter(frame._persona_read_after_ids))
        run(read_timer)
        self.assertEqual(len(pending), 2)
        self.assertEqual(len(frame._persona_read_after_ids), 1)
        delayed_callback = pending[next(iter(frame._persona_read_after_ids))]
        frame.bind.call_args.args[1](SimpleNamespace(widget=frame))
        self.assertEqual(pending, {})
        self.assertEqual(frame._persona_read_after_ids, set())
        executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
        service.close.assert_called_once()
        delayed_callback()
        self.assertEqual(pending, {})

    @unittest.skipUnless(tkinter, 'Tk runtime is not installed')
    def test_real_tk_destroy_removes_pending_poll_and_source_timers(self):
        import tkinter as tk
        from tkinter import ttk
        import persona_gui

        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        self.addCleanup(root.destroy)
        futures = []

        def submit(*args):
            future = Future()
            futures.append(future)
            return future

        executor = Mock(submit=Mock(side_effect=submit))
        service = Mock(control=FakeControl('running'))
        with patch.object(persona_gui, 'PersonaJobService', return_value=service), \
                patch.object(persona_gui, 'ThreadPoolExecutor', return_value=executor):
            frame = persona_gui.create_workbench(root,
                SimpleNamespace(friend_id='12345', project_root='fixture'))
        root.update()
        source_box = frame.winfo_children()[0]
        next(button for button in source_box.winfo_children()
             if isinstance(button, ttk.Button) and button.cget('text') == '选择会话').invoke()
        self.assertEqual(len(futures), 2)
        self.assertEqual(len(root.tk.call('after', 'info')), 2)
        frame.destroy()
        self.assertFalse(root.tk.call('after', 'info'))
        self.assertEqual(frame._persona_read_after_ids, set())
        executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
        service.close.assert_called_once()
        for future in futures:
            future.set_result([])
        root.update()




if __name__ == '__main__':
    unittest.main()
