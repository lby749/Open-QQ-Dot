import json
import tempfile
import unittest
from pathlib import Path
from usage_accounting import record, cache_summary

class CacheUsageReportTests(unittest.TestCase):
    def test_only_verified_usage_counts_and_tenant_isolation(self):
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td)/'alice', Path(td)/'bob'
            a.mkdir(); b.mkdir()
            record(a, 'gpt-test', 'api', {'usage_source':'provider', 'input_tokens':2000,
                'cached_input_tokens':1500, 'cache_write_input_tokens':300})
            record(a, 'gpt-test', 'api', {'usage_source':'estimate', 'input_tokens':1000,
                'cached_input_tokens':0, 'cache_write_input_tokens':0})
            record(a, 'gpt-test', 'api', {'usage_source':'provider', 'input_tokens':100,
                'cached_input_tokens':500, 'cache_write_input_tokens':0})
            record(b, 'gpt-test', 'api', {'usage_source':'provider', 'input_tokens':100,
                'cached_input_tokens':0, 'cache_write_input_tokens':0})
            r = cache_summary(a)
            self.assertEqual(r['calls'], 1)
            self.assertEqual(r['excluded'], 2)
            self.assertEqual(r['hit_rate'], 0.75)
            self.assertEqual(cache_summary(b)['input_tokens'], 100)
            self.assertIsNone(cache_summary(Path(td)/'missing')['hit_rate'])

if __name__ == '__main__': unittest.main()
