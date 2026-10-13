"""Touch calibration session retains prior data until a complete atomic save."""
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from _stubs import ensure_project_path, install_maix_stub
ensure_project_path()
install_maix_stub()

import lab_color
from test_lab_color import SolidFrame, data, four

ROOT = os.path.dirname(os.path.dirname(__file__))
spec = importlib.util.spec_from_file_location("lab_app_main",
    os.path.join(ROOT, "calibration", "lab_app", "main.py"))
lab_app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab_app)


class LabAppTests(unittest.TestCase):
    def test_fresh_four_color_calibration_saves(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "lab.json")
            session = lab_app.CalibrationSession(path)
            for name, rgb in (("red", (220, 0, 0)), ("yellow", (220, 220, 0)),
                              ("blue", (0, 0, 220)), ("white", (220, 220, 220))):
                for _ in range(4):
                    session.add(name, SolidFrame(rgb), 100, 100)
            self.assertTrue(session.save(), session.message)
            self.assertEqual(len(lab_color.load_data(path)["colors"]), 4)

    def test_touch_mapping_fit_contain(self):
        self.assertEqual(lab_app.screen_to_image(160, 128, 320, 256), (160, 128))
        self.assertEqual(lab_app.screen_to_image(400, 240, 800, 480), (160, 128))
        self.assertEqual(lab_app.screen_to_image(0, 240, 800, 480), (None, None))

    def test_preview_uses_maix_color_object(self):
        marker = object()
        with patch.object(lab_app.image, "Color",
                          types.SimpleNamespace(from_rgb=lambda r, g, b: marker), create=True):
            self.assertIs(lab_app.preview_color({"rgb": [10, 20, 30]}), marker)

    def test_add_undo_retake_and_save_preserve_old_on_failure(self):
        old = data({"red": four((220, 0, 0)), "yellow": four((220, 220, 0)),
                    "blue": four((0, 0, 220)), "white": four((220, 220, 220))})
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "lab.json")
            lab_color.save_data(path, old)
            session = lab_app.CalibrationSession(path)
            raw = SolidFrame((0, 160, 0))
            for _ in range(4):
                session.add("green", raw, 100, 100)
            self.assertEqual(session.count("green"), 4)
            session.undo("green")
            self.assertFalse(session.save())
            self.assertEqual(lab_color.load_data(path), old)
            session.add("green", raw, 100, 100)
            self.assertTrue(session.save())
            self.assertEqual(len(lab_color.load_data(path)["colors"]["green"]), 4)
            session.clear("green")
            self.assertFalse(session.save())
            self.assertEqual(len(lab_color.load_data(path)["colors"]["green"]), 4)


if __name__ == "__main__":
    unittest.main()
