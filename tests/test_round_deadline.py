import unittest
from pathlib import Path
from unittest.mock import patch

import main
import round_clock


class RoundClockTests(unittest.TestCase):
    def test_outside_an_attempt_nothing_changes(self):
        round_clock.check()
        self.assertEqual(round_clock.clamp(180), 180)

    def test_clamp_takes_the_time_left_when_it_is_shorter(self):
        with round_clock.limit(60):
            self.assertLessEqual(round_clock.clamp(180), 60)
            self.assertEqual(round_clock.clamp(10), 10)

    def test_nested_limit_keeps_the_earlier_end(self):
        # Crossing the float exponent boundary reproduced 60.00000000000023.
        with patch.object(round_clock.time, 'monotonic', return_value=2047.000001), \
                patch.object(round_clock.time, 'monotonic_ns', return_value=2047000001000):
            with round_clock.limit(60):
                with round_clock.limit(600):
                    self.assertEqual(round_clock.clamp(600), 60)
                with round_clock.limit(10):
                    self.assertEqual(round_clock.clamp(600), 10)
                self.assertEqual(round_clock.clamp(600), 60)

    def test_expired_attempt_raises_before_a_model_call(self):
        with round_clock.limit(-1):
            with self.assertRaises(RuntimeError) as raised:
                round_clock.check()
        self.assertIn('总时限', str(raised.exception))

    def test_codex_text_does_not_start_an_expired_call(self):
        with patch.object(main, '_codex_text') as call, round_clock.limit(-1):
            with self.assertRaises(RuntimeError):
                main.codex_text('x', Path('.'))
        call.assert_not_called()

    def test_make_round_stops_before_the_store_is_touched(self):
        # store is None on purpose: the attempt's clock is checked before any store or model use
        with patch.object(main, 'session_codex_text') as model_call, round_clock.limit(-1):
            with self.assertRaises(RuntimeError) as raised:
                main.make_round(None, {}, Path('.'), 1)
        model_call.assert_not_called()
        self.assertIn('总时限', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
