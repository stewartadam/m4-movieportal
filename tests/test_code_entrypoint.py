"""Tests for the CircuitPython entrypoint."""

import runpy
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


class CodeEntrypointTests(unittest.TestCase):
    def test_entrypoint_runs_application(self):
        main = Mock()
        module = types.ModuleType("movieportal")
        module.main = main

        with patch.dict(sys.modules, {"movieportal": module}):
            runpy.run_path(str(ROOT / "code.py"), run_name="__main__")

        main.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
