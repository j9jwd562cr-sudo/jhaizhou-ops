import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
from cache_budget import can_save


class CacheTests(unittest.TestCase):
    def test_cache_requires_free_space_and_available_usage(self):
        self.assertTrue(can_save(800, 100, limit=1000, reserve=50))
        for used, size in ((None, 100), (900, 100), (0, 0), (1100, 100)):
            self.assertFalse(can_save(used, size, limit=1000, reserve=50))
