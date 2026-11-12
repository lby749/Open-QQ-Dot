import unittest
from unittest.mock import patch

from persona_pack import PersonaPackBuilder
from persona_pack_bindings import PackBindingStore


class P4IntegrationTests(unittest.TestCase):
    def test_actual_pack_pipeline_output_is_bindable_without_source_changes(self):
        from tests.test_persona_pack import PackIntegrationTests
        case = PackIntegrationTests('test_real_pairs_publication_costs_and_terminal_failures')
        publish = PersonaPackBuilder._publish
        checked = []
        def bind_actual(builder, *args, **kwargs):
            result = publish(builder, *args, **kwargs)
            if not checked:
                bindings = PackBindingStore(builder.scope)
                try:
                    revision = bindings.store.revision
                    row = bindings.bind(result['pack_id'], result['version'])
                    scope = bindings.bound_scope()
                    scope.require_pack(result['pack_id'], result['version'])
                    pack = bindings.read_pack(result['pack_id'], result['version'])
                    self.assertEqual(len(pack['examples']), result['stage_counts']['examples'])
                    self.assertEqual(bindings.store.revision, revision)
                    self.assertEqual(row['revision'], 1)
                    checked.append(result['version'])
                finally:
                    bindings.close()
            return result
        try:
            with patch.object(PersonaPackBuilder, '_publish', bind_actual):
                case.test_real_pairs_publication_costs_and_terminal_failures()
        finally:
            case.doCleanups()
        self.assertEqual(len(checked), 1)


if __name__ == '__main__':
    unittest.main()
