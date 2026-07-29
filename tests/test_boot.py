"""Tests for USB configuration at boot."""

import runpy
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


class BootTests(unittest.TestCase):
    def test_enables_console_and_binary_data_ports(self):
        enable = Mock()
        usb_cdc = types.ModuleType("usb_cdc")
        usb_cdc.enable = enable

        with patch.dict(sys.modules, {"usb_cdc": usb_cdc}):
            runpy.run_path(str(ROOT / "boot.py"), run_name="__main__")

        enable.assert_called_once_with(console=True, data=True)


if __name__ == "__main__":
    unittest.main()
