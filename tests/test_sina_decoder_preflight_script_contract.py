from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


class SinaDecoderPreflightScriptContractTest(unittest.TestCase):
    def test_recorded_klc_fixture_is_checked_without_python_command_string(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        script = repository / "scripts" / "sina_decoder_preflight.py"
        fixture = repository / "tests" / "providers" / "fixtures" / "sina" / "recorded_klc_kl.js"
        self.assertTrue(script.is_file(), "the standalone Sina decoder preflight must exist")

        completed = subprocess.run(
            [sys.executable, str(script), "--fixture", str(fixture)],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            {
                "status": "ok",
                "rows": 1,
                "first": {
                    "date": "2024-04-02",
                    "prevclose": 17.43,
                    "open": 50,
                    "high": 58.1,
                    "low": 48.1,
                    "close": 51.88,
                    "volume": 28_991_027,
                    "amount": 1_522_338_078,
                },
            },
            json.loads(completed.stdout),
        )
        self.assertEqual("", completed.stderr)


if __name__ == "__main__":
    unittest.main()
