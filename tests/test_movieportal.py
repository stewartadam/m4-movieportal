"""Tests for the application scaffold."""

import unittest

import movieportal


class MoviePortalTests(unittest.TestCase):
    def test_scaffold_has_no_runtime_behavior_yet(self):
        self.assertIsNone(movieportal.main())


if __name__ == "__main__":
    unittest.main()
