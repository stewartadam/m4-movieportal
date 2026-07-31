import unittest
from pathlib import Path
import tempfile

import iina_seek


class FakeIpc:
    def __init__(self):
        self.commands = []

    def command(self, name, *arguments):
        self.commands.append((name, arguments))


class IinaSeekTests(unittest.TestCase):
    def test_loads_timestamps_from_toml(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "movie.config"
            path.write_text(
                'timestamps = [887.261, "01:20:53"]\n',
                encoding="utf-8",
            )

            timestamps = iina_seek.load_timestamps(path)

        self.assertEqual(timestamps, (887.261, 4853.0))

    def test_seeks_configured_timestamps_in_order_and_dwells(self):
        ipc = FakeIpc()
        pauses = []
        timestamps = (1.25, 8.5)

        iina_seek.run(ipc, 0.5, timestamps, sleep=pauses.append)

        self.assertEqual(
            ipc.commands,
            [
                ("seek", (1.25, "absolute", "exact")),
                ("seek", (8.5, "absolute", "exact")),
            ],
        )
        self.assertEqual(pauses, [0.5, 0.5])

    def test_rejects_invalid_dwell(self):
        with self.assertRaisesRegex(ValueError, "finite"):
            iina_seek.run(FakeIpc(), float("nan"), (1,))


if __name__ == "__main__":
    unittest.main()
