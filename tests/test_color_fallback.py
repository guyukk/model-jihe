"""低覆盖率颜色不丢物料，同时把估测与可靠命中区分开。"""
import os
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from _stubs import ensure_project_path, install_maix_stub

ensure_project_path()
install_maix_stub()

import cv2
import numpy as np
import config
import vision
from protocol_handler import snapshot_details


class SolidImage:
    def __init__(self, rgb):
        self.rgb = rgb

    def get_pixel(self, x, y):
        return self.rgb


class ColorFallbackTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(config, "SCAN_DEBUG_LOG", False).start()
        self.detector = types.SimpleNamespace(
            labels=["cube"],
            detect=lambda img, **kw: [types.SimpleNamespace(
                x=100, y=100, w=20, h=20, class_id=0, score=.9)])
        self.vision = vision.VisionDetector(self.detector, 320, 256)

    def hsv(self, h, s, v):
        return np.array([[[h, s, v]]], dtype=np.uint8)

    def rgb(self, h, s, v):
        return tuple(int(n) for n in cv2.cvtColor(
            self.hsv(h, s, v), cv2.COLOR_HSV2RGB)[0, 0])

    def test_strong_mask_keeps_calibrated_choice(self):
        color, ratio, _ = self.vision._detect_color(self.hsv(20, 200, 220))
        self.assertEqual(color, "blue")  # 现有阈值在此处优先命中蓝色
        self.assertEqual(ratio, 1)
        self.assertFalse(self.vision.last_color_estimated)

    def test_no_mask_match_uses_hue_and_white_fallback(self):
        for hsv, expected in [((0, 100, 140), "red"),
                              ((179, 100, 140), "red"),
                              ((30, 100, 140), "yellow"),
                              ((120, 100, 140), "blue"),
                              ((0, 20, 120), "white")]:
            with self.subTest(hsv=hsv):
                color, ratio, _ = self.vision._detect_color(self.hsv(*hsv))
                self.assertEqual(color, expected)
                self.assertEqual(ratio, 0)
                self.assertTrue(self.vision.last_color_estimated)

    def test_subthreshold_mask_match_is_colored_but_marked_estimated(self):
        roi = np.tile(self.hsv(0, 100, 140), (10, 10, 1))
        roi[0, 0] = (20, 200, 220)
        color, ratio, ratios = self.vision._detect_color(roi)
        self.assertEqual(color, "blue")
        self.assertAlmostEqual(ratio, .01)
        self.assertEqual(ratios["blue"], ratio)
        self.assertTrue(self.vision.last_color_estimated)

    def test_valid_small_box_is_colored_and_published(self):
        color, _, _ = self.vision.detect_color_in_box(
            SolidImage(self.rgb(0, 100, 140)), 110, 110, 111, 111)
        self.assertEqual(color, "red")
        self.assertTrue(self.vision.last_color_estimated)

        detections = self.vision.detect_objects(SolidImage(self.rgb(0, 100, 140)))
        self.assertEqual([d[1] for d in detections], ["red"])
        self.assertEqual(self.vision.last_scan_stats["color_fallback"], 1)
        pieces, stats, _ = snapshot_details(
            detections, types.SimpleNamespace(normalized_to_mechanical=lambda x, y: (600, 1400)))
        self.assertEqual(len(pieces), 1)
        self.assertEqual(stats["unknown_color"], 0)

    def test_stable_vote_reports_estimate_only_without_confident_hit(self):
        with patch.object(config, "SCAN_FRAME_COUNT", 3), \
             patch.object(config, "SCAN_MIN_HITS", 2), \
             patch.object(config, "SCAN_FRAME_DELAY_MS", 0):
            low = SolidImage(self.rgb(0, 100, 140))
            result = self.vision.detect_objects_stable(lambda: low)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0][1], "red")
            self.assertEqual(self.vision.last_scan_stats["color_fallback"], 1)
            self.assertEqual(self.vision.last_scan_color_estimated, [True])

    def test_confident_observation_clears_merged_estimate(self):
        flags = iter([True, False, True])

        def detect(_img):
            self.vision.last_scan_color_estimated = [next(flags)]
            self.vision.last_scan_stats = dict(yolo_raw=1, roi_out=0,
                color_unknown=0, color_fallback=0, accepted=1)
            return [("cube", "red", .5, .5, (100, 100, 20, 20), .9)]

        self.vision.detect_objects = detect
        with patch.object(config, "SCAN_FRAME_COUNT", 3), \
             patch.object(config, "SCAN_MIN_HITS", 2), \
             patch.object(config, "SCAN_FRAME_DELAY_MS", 0):
            self.assertEqual(len(self.vision.detect_objects_stable(lambda: None)), 1)
        self.assertEqual(self.vision.last_scan_stats["color_fallback"], 0)
        self.assertEqual(self.vision.last_scan_color_estimated, [False])

    def test_confident_color_outvotes_two_guesses(self):
        readings = iter([("blue", True), ("blue", True), ("red", False)])

        def detect(_img):
            color, estimated = next(readings)
            self.vision.last_scan_color_estimated = [estimated]
            self.vision.last_scan_stats = dict(yolo_raw=1, roi_out=0,
                color_unknown=0, color_fallback=int(estimated), accepted=1)
            return [("cube", color, .5, .5, (100, 100, 20, 20), .9)]

        self.vision.detect_objects = detect
        with patch.object(config, "SCAN_FRAME_COUNT", 3), \
             patch.object(config, "SCAN_MIN_HITS", 2), \
             patch.object(config, "SCAN_FRAME_DELAY_MS", 0):
            result = self.vision.detect_objects_stable(lambda: None)
        self.assertEqual(result[0][1], "red")
        self.assertEqual(self.vision.last_scan_color_estimated, [False])

    def test_truly_empty_box_remains_invalid(self):
        color, ratio, _ = self.vision.detect_color_in_box(SolidImage((0, 0, 0)), 1, 1, 1, 1)
        self.assertEqual((color, ratio), ("unknown", 0))
        self.assertFalse(self.vision.last_color_estimated)


if __name__ == "__main__":
    unittest.main()
