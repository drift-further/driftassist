"""The Fit menu presets and the scrollback depth are settings, not constants.

- `terminal.fit_presets` ("Name:COLS" or "Name:COLSxROWS", comma separated)
  is validated on PATCH by `parse_fit_presets`; the browser's copy
  (`js/terminal.js:parseFitPresets`) reads the same strings the same way.
- `terminal.capture_lines` takes 0 as "all", meaning the server cap
  (`limits.max_capture_lines`), while 1–99 stay refused; the stream's line
  count is capped by that setting rather than a hard-coded 20000.

Both directions throughout.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from routes import streaming
from shared import state

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = state.DEFAULT_SETTINGS["terminal"]["fit_presets"]


def _check(path, value):
    section, key = path.split(".")
    return state.check_setting(path, value, state.DEFAULT_SETTINGS[section][key])[0]


class FitPresetParse(unittest.TestCase):
    def test_default_parses_to_the_old_buttons(self):
        self.assertEqual(
            state.parse_fit_presets(DEFAULT),
            [("Phone", 58, None), ("Compact", 100, None), ("Wide", 160, None), ("Desktop", 200, None)],
        )

    def test_accepts_rows_empty_and_spacing(self):
        self.assertEqual(state.parse_fit_presets("Tall : 94 x 400"), [("Tall", 94, 400)])
        self.assertEqual(state.parse_fit_presets("Tall:94×400,Big:120"), [("Tall", 94, 400), ("Big", 120, None)])
        self.assertEqual(state.parse_fit_presets(""), [])
        self.assertTrue(_check("terminal.fit_presets", "Phone:58, Tall:94x400"))

    def test_refuses_malformed_or_out_of_bounds(self):
        for bad in ("Phone", "Phone:", ":58", "Phone:39", "Phone:401", "Tall:94x9",
                    "Tall:94x601", "A:58,,B:60", "A:58;B:60", ",".join(["P:60"] * 9)):
            with self.subTest(bad=bad):
                self.assertIsNone(state.parse_fit_presets(bad))
                self.assertFalse(_check("terminal.fit_presets", bad))
        self.assertFalse(_check("terminal.fit_presets", 58))


@unittest.skipUnless(shutil.which("node"), "node not installed")
class FitPresetClientParity(unittest.TestCase):
    """The browser parser keeps every entry the server accepts, unchanged."""

    def test_same_result_as_server(self):
        src = (ROOT / "js" / "terminal.js").read_text()
        start = src.index("function parseFitPresets(")
        end = src.index("\n}\n", start) + 3
        cases = [DEFAULT, "Tall : 94 x 400", "Tall:94×400,Big:120", ""]
        script = src[start:end] + f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(parseFitPresets)));"
        out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
        for case, got in zip(cases, out):
            want = [{"name": n, "cols": c, "rows": r} for n, c, r in state.parse_fit_presets(case)]
            self.assertEqual(got, want, case)


class CaptureLinesAll(unittest.TestCase):
    def test_zero_means_all_but_small_values_stay_refused(self):
        self.assertTrue(_check("terminal.capture_lines", 0))
        self.assertTrue(_check("terminal.capture_lines", 2000))
        self.assertFalse(_check("terminal.capture_lines", 50))
        self.assertFalse(_check("terminal.default_cols", 0))  # the sentinel is only for capture_lines

    def test_stream_lines_follow_the_server_cap(self):
        with mock.patch.object(state, "get_setting", return_value=30000):
            self.assertEqual(streaming._clamp_lines(25000), 25000)
            self.assertEqual(streaming._clamp_lines(0), 30000)
            self.assertEqual(streaming._clamp_lines(99999), 30000)
        with mock.patch.object(state, "get_setting", return_value=20000):
            self.assertEqual(streaming._clamp_lines(2000), 2000)
            self.assertEqual(streaming._clamp_lines(25000), 20000)


if __name__ == "__main__":
    unittest.main()
