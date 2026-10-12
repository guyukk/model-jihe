"""Tests for transforming ROI detections into calibrated mechanical coordinates."""
import unittest

from coordinate import CoordinateConverter


class CoordinateConverterTests(unittest.TestCase):
    def setUp(self):
        self.converter = CoordinateConverter()

    def test_converts_documented_roi_point(self):
        # The previous axis mapping produced (1126, 510) for this detection.
        norm_x = 510 / 1600
        norm_y = 1126 / 1600
        self.assertEqual(
            self.converter.normalized_to_mechanical(norm_x, norm_y),
            (310, 1726),
        )

    def test_roi_center_maps_to_calibrated_mechanical_center(self):
        self.assertEqual(
            self.converter.normalized_to_mechanical(0.5, 0.5),
            (600, 1400),
        )

    def test_roi_corners_include_origin_translation(self):
        self.assertEqual(
            self.converter.normalized_to_mechanical(0.0, 0.0),
            (-200, 600),
        )
        self.assertEqual(
            self.converter.normalized_to_mechanical(1.0, 1.0),
            (1400, 2200),
        )


if __name__ == "__main__":
    unittest.main()
