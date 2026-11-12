import unittest

import sticker_library


class StickerFrequencyTests(unittest.TestCase):
    def test_frequency_is_clamped_and_default_is_every_round(self):
        self.assertEqual(sticker_library.usage_frequency({}), 100)
        self.assertEqual(sticker_library.usage_frequency({'sticker_frequency': 30}), 30)
        self.assertEqual(sticker_library.usage_frequency({'sticker_frequency': -5}), 0)
        self.assertEqual(sticker_library.usage_frequency({'sticker_frequency': 500}), 100)

    def test_zero_never_offers_and_percent_uses_rng(self):
        self.assertTrue(sticker_library.offer_enabled({'sticker_frequency': 0}) is False)
        self.assertTrue(sticker_library.offer_enabled({'sticker_frequency': 30}))
        self.assertFalse(sticker_library.decide_sticker({'sticker_frequency': 0}, 'yes'))
        self.assertTrue(sticker_library.decide_sticker({'sticker_frequency': 30}, 'yes'))
        self.assertFalse(sticker_library.decide_sticker({'sticker_frequency': 30}, 'no'))
        self.assertTrue(sticker_library.decide_sticker({'sticker_frequency': 30}, 'uncertain', rng=lambda: 0.0))
        self.assertFalse(sticker_library.decide_sticker({'sticker_frequency': 30}, 'uncertain', rng=lambda: 0.5))
        self.assertTrue(sticker_library.decide_sticker({'sticker_frequency': 100}, 'uncertain', rng=lambda: 0.999))


if __name__ == '__main__':
    unittest.main()
