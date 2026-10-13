"""LAB units, prototypes, partial rollout, persistence and old HSV comparison."""
import importlib.util
import json
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

import numpy as np
import config
import lab_color
import screen_ui
import vision
from protocol_handler import snapshot_details


class SolidFrame:
    def __init__(self, rgb):
        self.rgb = rgb
        self.drawn = False

    def get_pixel(self, x, y):
        return (0, 0, 0) if self.drawn else self.rgb

    def draw_rect(self, *args, **kwargs):
        self.drawn = True

    def copy(self):
        return SolidFrame(self.rgb)


def sample(rgb, radius=8):
    lab = lab_color.rgb_to_lab(np.array([rgb], dtype=np.uint8))[0]
    return {"lab": [float(v) for v in lab], "dispersion": 2, "radius": radius,
            "rgb": list(rgb)}


def data(colors):
    return {"version": 1, "colors": {
        name: [sample(rgb) for rgb in shades]
        for name, shades in colors.items()}}


def four(rgb):
    return [rgb] * 4


class LabTests(unittest.TestCase):
    def test_eight_protocol_ids_and_names(self):
        self.assertEqual(list(lab_color.COLOR_IDS.values()), list(range(1, 9)))
        self.assertEqual(config.COLOR_MAPPING, lab_color.COLOR_IDS)
        self.assertEqual(screen_ui.names(1, 6, True)[0], "紫色")
        self.assertEqual(screen_ui.names(1, 7, True)[0], "黑色")

    def test_opencv_lab_units(self):
        white = lab_color.rgb_to_lab(np.array([[255, 255, 255]], dtype=np.uint8))[0]
        black = lab_color.rgb_to_lab(np.array([[0, 0, 0]], dtype=np.uint8))[0]
        self.assertAlmostEqual(white[0], 100, places=1)
        self.assertAlmostEqual(black[0], 0, places=1)
        self.assertTrue(all(abs(float(x)) < 1 for x in white[1:]))

    def test_multiple_lighting_and_angle_prototypes(self):
        colors = data({"red": [(200, 15, 20), (160, 20, 25), (130, 10, 15), (100, 8, 12)],
                       "yellow": four((220, 200, 10)), "blue": four((10, 35, 190)),
                       "white": four((225, 225, 225))})
        lab_color.validate_data(colors)
        for rgb in ((200, 15, 20), (160, 20, 25), (130, 10, 15), (100, 8, 12)):
            name, estimated, distance, _ = lab_color.classify(np.array([[rgb]], np.uint8), colors)
            self.assertEqual(name, "red")
            self.assertFalse(estimated)
            self.assertAlmostEqual(distance, 0)

    def test_near_color_and_out_of_range_are_estimated(self):
        same = data({"red": four((200, 30, 20)), "orange": four((200, 30, 20)),
                     "yellow": four((230, 230, 0)), "blue": four((0, 20, 220)),
                     "white": four((230, 230, 230))})
        self.assertTrue(lab_color.classify(np.array([[(200, 30, 20)]], np.uint8), same)[1])
        different = data({"red": four((200, 0, 0)), "yellow": four((240, 230, 0)),
                          "blue": four((0, 0, 220)), "white": four((245, 245, 245))})
        self.assertTrue(lab_color.classify(np.array([[(60, 60, 60)]], np.uint8), different)[1])

    def test_partial_color_enable_and_validation(self):
        colors = data({"red": four((200, 0, 0)), "yellow": four((240, 220, 0)),
                       "blue": four((0, 0, 220)), "white": four((240, 240, 240)),
                       "orange": four((240, 100, 0))})
        lab_color.validate_data(colors)
        self.assertEqual(lab_color.classify(np.array([[(240, 100, 0)]], np.uint8), colors)[0], "orange")
        colors["colors"]["green"] = [sample((0, 180, 0))] * 3
        with self.assertRaises(ValueError):
            lab_color.validate_data(colors)

    def test_atomic_save_and_corrupt_file_hsv_fallback(self):
        colors = data({"red": four((220, 0, 0)), "yellow": four((220, 220, 0)),
                       "blue": four((0, 0, 220)), "white": four((220, 220, 220))})
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "lab.json")
            lab_color.save_data(path, colors)
            self.assertEqual(lab_color.load_data(path), colors)
            broken = dict(colors, version=99)
            with self.assertRaises(ValueError):
                lab_color.save_data(path, broken)
            self.assertEqual(lab_color.load_data(path), colors)
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("{broken")
            detector = types.SimpleNamespace(labels=["cube"], detect=lambda *a, **kw: [])
            with patch.object(config, "LAB_CONFIG_PATH", path):
                instance = vision.VisionDetector(detector, 320, 256)
            self.assertEqual(instance.color_mode, "hsv")

    def test_raw_patch_is_not_tainted_by_overlay(self):
        raw = SolidFrame((0, 0, 255))
        display = raw.copy()
        display.draw_rect(0, 0, 320, 256)
        color = lab_color.sample_patch(raw, 100, 100)
        self.assertEqual(color["rgb"], [0, 0, 255])
        self.assertEqual(lab_color.sample_patch(display, 100, 100)["rgb"], [0, 0, 0])

    def test_maix_single_packed_value_list(self):
        frame = types.SimpleNamespace(get_pixel=lambda x, y: [0x4D3A2B])
        self.assertEqual(lab_color.pixel_rgb(frame, 1, 1), (0x4D, 0x3A, 0x2B))
        self.assertEqual(lab_color.sample_patch(frame, 100, 100)["rgb"], [0x4D, 0x3A, 0x2B])

    def test_same_frame_old_hsv_vs_new_lab_and_protocol_payload(self):
        colors = data({"red": four((220, 0, 0)), "yellow": four((235, 230, 0)),
                       "blue": four((0, 20, 220)), "white": four((230, 230, 230)),
                       "orange": four((240, 110, 0))})
        detector = types.SimpleNamespace(labels=["cube"], detect=lambda img, **kw: [
            types.SimpleNamespace(x=120, y=100, w=20, h=20, class_id=0, score=.9)])
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "lab.json")
            lab_color.save_data(path, colors)
            with patch.object(config, "LAB_CONFIG_PATH", path):
                new = vision.VisionDetector(detector, 320, 256)
            with patch.object(config, "COLOR_MODE", "hsv"):
                old = vision.VisionDetector(detector, 320, 256)
            frame = SolidFrame((240, 110, 0))
            new_detections = new.detect_objects(frame)
            old_detections = old.detect_objects(frame)
            self.assertEqual(new_detections[0][1], "orange")
            self.assertNotEqual(old_detections[0][1], "orange")
            pieces, _, _ = snapshot_details(new_detections,
                types.SimpleNamespace(normalized_to_mechanical=lambda x, y: (600, 1400)))
            self.assertEqual(pieces[0][1], 2)


if __name__ == "__main__":
    unittest.main()
