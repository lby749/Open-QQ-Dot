import json
import unittest
from contextlib import contextmanager
from concurrent.futures import Future
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    import tkinter  # noqa: F401
except Exception as exc:
    raise unittest.SkipTest(f'tkinter unavailable: {exc}')

from persona_friend_gui import PersonaFriendService
from persona_store import Conflict, Unavailable
from persona_types import FriendScope, InvalidSource, ScopeMismatch
from tests import test_persona_versions as version_tests


class PersonaFriendServiceTests(unittest.TestCase):
    def setUp(self):
        version_tests.PersonaVersionTests.setUp(self)
        self.service = PersonaFriendService(self.scope)
        self.addCleanup(self.service.close)

    def _build(self):
        return version_tests.PersonaVersionTests._build(self)

    def _publish(self):
        return version_tests.PersonaVersionTests._publish(self)

    def _cas(self):
        current = self.versions.read_active()
        return dict(expected_revision=self.pairs.revision,
            expected_active_version=current['manifest']['version'] if current else None,
            expected_manual_revision=current['manifest']['manual_revision'] if current else 0)

    def _service_cas(self):
        active, revision = self.service.read_snapshot()
        return dict(expected_revision=revision,
            expected_active_version=active['manifest']['version'] if active else None,
            expected_manual_revision=active['manifest']['manual_revision'] if active else 0)

    def test_manual_edit_uses_cas_and_stale_edit_does_not_overwrite(self):
        first = self._publish()
        claim = first['profile']['claims'][0]
        stale = self._service_cas()
        active = self.service.edit_manual(overlay={claim['claim_key']: {'claim': '人工锁定'}},
                                          **stale)
        self.assertEqual(active['profile']['claims'][0]['quality'], 'MANUAL_LOCK')
        backups = tuple((self.scope.friend_dir / '.persona_friend_gui_backups').glob('*.sqlite3'))
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].read_bytes().startswith(b'SQLite format 3\x00'))
        with self.assertRaises(Conflict):
            self.service.edit_manual(overlay={claim['claim_key']: {'claim': '过期编辑'}},
                                     **stale)
        self.assertEqual(self.service.read_active()['profile']['claims'][0]['claim'], '人工锁定')

    def test_rollback_keeps_latest_manual_claim_and_style_override(self):
        first = self._publish()
        key = first['profile']['claims'][0]['claim_key']
        self.service.edit_manual(overlay={key: {'claim': '人工保留'}},
            style_overrides={'generated_at': None}, **self._service_cas())
        active = self.service.rollback(first['manifest']['version'], **self._service_cas())
        self.assertEqual(active['profile']['claims'][0]['claim'], '人工保留')
        self.assertEqual(active['style']['manual_overrides']['generated_at'],
                         {'value': None, 'source': 'MANUAL_LOCK'})

    def test_each_stale_cas_field_is_rejected(self):
        first = self._publish()
        key = first['profile']['claims'][0]['claim_key']
        current = self._service_cas()
        for field, value in (('expected_revision', current['expected_revision'] + 1),
                             ('expected_active_version', '0' * 32),
                             ('expected_manual_revision', current['expected_manual_revision'] + 1)):
            with self.subTest(field=field), self.assertRaises(Conflict):
                self.service.edit_manual(overlay={key: {'claim': '过期草稿'}},
                    **dict(current, **{field: value}))
        self.assertEqual(self.service.read_active()['manifest']['version'], first['manifest']['version'])

    def test_readonly_pair_initialization_failure_closes_both_connections(self):
        from persona_friend_gui import _readonly_pairs

        store, registry = Mock(), Mock()
        with patch('persona_store.PersonaStore', return_value=store), \
                patch('dataset_registry.DatasetRegistry', return_value=registry), \
                patch('dataset_normalizer.DatasetNormalizer', side_effect=InvalidSource('initialization failed')):
            with self.assertRaisesRegex(InvalidSource, 'initialization failed'):
                _readonly_pairs(self.scope)
        registry.close.assert_called_once_with()
        store.close.assert_called_once_with()

    def test_writer_preflight_backup_and_open_share_source_lock(self):
        import source_state
        from dialogue_pairs import PairStore
        from persona_store import PersonaStore

        original_lock = source_state.source_lock
        original_init = PersonaStore.__init__
        original_backup = source_state.SourceConnection.backup
        depth = 0
        operations = []

        @contextmanager
        def guarded_lock(directory, **options):
            nonlocal depth
            with original_lock(directory, **options):
                depth += 1
                try:
                    yield
                finally:
                    depth -= 1

        def guarded_init(instance, *args, **options):
            self.assertGreater(depth, 0)
            operations.append('preflight' if options.get('readonly') else 'store')
            original_init(instance, *args, **options)

        def guarded_backup(instance, destination, *args, **options):
            self.assertGreater(depth, 0)
            operations.append('backup')
            return original_backup(instance, destination, *args, **options)

        def writer(scope):
            self.assertGreater(depth, 0)
            operations.append('open_writer')
            return PairStore(scope)

        service = PersonaFriendService(self.scope, pair_store_factory=writer)
        with patch.object(source_state, 'source_lock', guarded_lock), \
                patch.object(PersonaStore, '__init__', guarded_init), \
                patch.object(source_state.SourceConnection, 'backup', guarded_backup):
            pairs = service._writer_pairs()
            pairs.close()
        self.assertLess(operations.index('preflight'), operations.index('backup'))
        self.assertLess(operations.index('backup'), operations.index('open_writer'))

    def test_workbench_rejects_same_friend_id_in_another_project(self):
        from persona_friend_gui import create_workbench

        with TemporaryDirectory() as folder:
            root = Path(folder)
            directory = root / 'friends' / self.scope.friend_id
            directory.mkdir(parents=True)
            (directory / 'friend.json').write_text(json.dumps({'qq_id': self.scope.friend_id}), encoding='utf-8')
            (root / 'friends.json').write_text(json.dumps({'friends': {
                self.scope.friend_id: {'qq_id': self.scope.friend_id, 'directory': str(directory)}}}), encoding='utf-8')
            other_scope = FriendScope.load(self.scope.friend_id, project_root=root)
            with self.assertRaises(ScopeMismatch):
                create_workbench(Mock(), self.scope, service=SimpleNamespace(scope=other_scope))

    def test_manual_claim_and_style_override_survive_later_automatic_publish(self):
        first = self._publish()
        key = first['profile']['claims'][0]['claim_key']
        self.service.edit_manual(overlay={key: {'claim': '人工设定'}},
            style_overrides={'generated_at': None}, **self._service_cas())
        self._build()
        active = self._publish()
        self.assertEqual(active['profile']['claims'][0]['claim'], '人工设定')
        self.assertEqual(active['style']['manual_overrides']['generated_at']['value'], None)

    def test_example_preview_uses_temporary_bounded_k_and_context_budgets(self):
        class FakeRetriever:
            def __init__(self, scope):
                self.scope = scope
                self.request = None

            def retrieve(self, request, limit):
                self.request, self.limit = request, limit
                return Mock(candidates=(), errors=(), degraded=False)

            def close(self):
                pass

        retriever = FakeRetriever(self.scope)
        service = PersonaFriendService(self.scope,
            example_retriever_factory=lambda scope: retriever)
        self.addCleanup(service.close)
        rows, _ = service.preview_examples('问候', limit=7, persona_budget=900,
                                           example_budget=500, knowledge_budget=200)
        self.assertEqual(rows, ())
        self.assertEqual((retriever.request.friend_id, retriever.request.filters.privacy_scope.value),
                         ('12345', 'PRIVATE_FRIEND'))
        self.assertEqual((retriever.limit, retriever.request.budgets.persona,
                          retriever.request.budgets.examples), (7, 900, 500))
        with self.assertRaises(InvalidSource):
            service.preview_examples('问候', limit=21)
        with self.assertRaises(InvalidSource):
            service.preview_examples('问候', persona_budget=100, example_budget=101)

    def test_other_friend_scope_never_falls_back_to_current_friend_database(self):
        other_dir = self.root / 'friends' / '23456'
        other_dir.mkdir(parents=True)
        (other_dir / 'friend.json').write_text(json.dumps({'qq_id': '23456'}), encoding='utf-8')
        registry_path = self.root / 'friends.json'
        registry = json.loads(registry_path.read_text(encoding='utf-8'))
        registry['friends']['23456'] = {'qq_id': '23456', 'directory': str(other_dir)}
        registry_path.write_text(json.dumps(registry), encoding='utf-8')
        other_scope = FriendScope.load('23456', project_root=self.root)
        other_service = PersonaFriendService(other_scope)
        with self.assertRaises(Unavailable):
            other_service.read_active()
        other_service.close()
        self.assertFalse((other_dir / 'persona.sqlite3').exists())
        self.assertTrue((self.scope.friend_dir / 'persona.sqlite3').exists())

    def workbench(self, service):
        import persona_friend_gui

        pending, widgets, tasks = {}, [], []

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
            rows, selected = {}, []
            value.get_children.side_effect = lambda: tuple(rows)
            value.insert.side_effect = lambda *args, iid=None, **kwargs: (
                rows.update({iid: kwargs}) if iid is not None else None)
            def delete(*items):
                for row in items:
                    rows.pop(row, None)
                    if row in selected:
                        selected.remove(row)
            value.delete.side_effect = delete
            value.selection.side_effect = lambda: tuple(selected)
            value.selection_set.side_effect = lambda key: selected.__setitem__(slice(None), [key])
            widgets.append(value)
            return value

        def variable(**options):
            state = {'value': options.get('value', '')}
            return Mock(get=Mock(side_effect=lambda: state['value']),
                        set=Mock(side_effect=lambda value: state.update(value=value)))

        def submit(task):
            future = Future()
            tasks.append((future, task))
            return future

        executor = Mock(submit=Mock(side_effect=submit))
        ttk = SimpleNamespace(**{name: widget for name in
            ('Frame', 'Label', 'Notebook', 'Treeview', 'Entry', 'Button', 'Combobox')})
        messages = Mock()
        tkinter = SimpleNamespace(ttk=ttk, messagebox=messages, StringVar=variable,
                                  Text=widget, TclError=Exception)
        with patch.dict('sys.modules', {'tkinter': tkinter}), \
                patch.object(persona_friend_gui, 'ThreadPoolExecutor', return_value=executor):
            frame = persona_friend_gui.create_workbench(Mock(), self.scope, service=service)

        def finish():
            future, task = tasks.pop(0)
            try:
                future.set_result(task())
            except Exception as exc:
                future.set_exception(exc)
            timer = next(iter(pending))
            pending.pop(timer)()

        close = lambda: frame.bind.call_args.args[1](SimpleNamespace(widget=frame))
        self.addCleanup(close)
        buttons = {item.options['text']: item.options['command'] for item in widgets
                   if 'command' in item.options}
        claim_text = next(item.options['textvariable'] for item in widgets
                          if 'textvariable' in item.options and 'width' not in item.options)
        tree = next(item for item in widgets if 'columns' in item.options)
        return frame, pending, executor, messages, buttons, claim_text, tree, finish, close

    def test_conflict_refresh_keeps_draft_and_updates_all_cas_fields(self):
        def active(version, manual_revision):
            return {'manifest': {'version': version, 'manual_revision': manual_revision},
                    'profile': {'claims': [{'claim_key': 'key-1', 'claim': '已发布文本'}],
                                'manual_revision': manual_revision}, 'style': {}}

        first, second = active('old-version', 1), active('new-version', 2)
        service = Mock(scope=self.scope)
        service.read_snapshot.side_effect = [(first, 10), (second, 20), (second, 21)]
        service.list_versions.return_value = ({'id': 'old-version'},)
        service.edit_manual.side_effect = [Conflict('catalog changed'), second]
        frame, pending, executor, messages, buttons, draft, tree, finish, close = self.workbench(service)
        finish()
        draft.set('Savelol')
        buttons['锁定并保存人格特点']()
        finish()
        self.assertEqual(draft.get(), 'Savelol')
        messages.showerror.assert_called_once()
        self.assertEqual(service.edit_manual.call_args.kwargs['expected_revision'], 10)
        buttons['刷新当前版本（保留草稿）']()
        finish()
        tree.bind.call_args.args[1]()  # A queued TreeviewSelect event must also retain the draft.
        self.assertEqual(draft.get(), 'Savelol')
        buttons['锁定并保存人格特点']()
        finish()
        self.assertEqual(service.edit_manual.call_args.kwargs, {
            'overlay': {'key-1': {'claim': 'Savelol'}}, 'expected_revision': 20,
            'expected_active_version': 'new-version', 'expected_manual_revision': 2})
        finish()

    def test_close_cancels_rescheduled_callbacks_and_ignores_late_result(self):
        service = Mock(scope=self.scope)
        frame, pending, executor, messages, buttons, draft, tree, finish, close = self.workbench(service)
        timer = next(iter(pending))
        pending.pop(timer)()
        delayed = next(iter(pending.values()))
        close()
        delayed()
        self.assertFalse(pending)
        self.assertEqual(frame._persona_friend_after_ids, set())
        executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
        service.close.assert_called_once()
        messages.showerror.assert_not_called()


if __name__ == '__main__':
    unittest.main()
