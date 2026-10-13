"""Shared, device-side LAB calibration and classification (OpenCV CIE Lab units)."""
import json
import math
import os

import cv2
import numpy as np

VERSION = 1
MIN_SAMPLES = 4
COLOR_IDS = {"red": 1, "orange": 2, "yellow": 3, "green": 4,
             "blue": 5, "purple": 6, "black": 7, "white": 8}
REQUIRED = ("red", "yellow", "blue", "white")


def rgb_to_lab(rgb):
    """RGB uint8 array -> L 0..100, a/b signed; never compare raw OpenCV bytes."""
    array = np.asarray(rgb, dtype=np.uint8)
    if array.shape[-1] != 3:
        raise ValueError("RGB needs three channels")
    raw = cv2.cvtColor(array.reshape(-1, 1, 3), cv2.COLOR_RGB2LAB).astype(np.float32)
    out = raw.reshape(array.shape)
    out[..., 0] *= 100.0 / 255.0
    out[..., 1:] -= 128.0
    return out


def pixel_rgb(img, x, y):
    pixel = img.get_pixel(x, y)
    if isinstance(pixel, (tuple, list)):
        if len(pixel) >= 3:
            return tuple(int(v) & 255 for v in pixel[:3])
        if len(pixel) == 1:
            pixel = pixel[0]
        else:
            raise ValueError("empty RGB pixel")
    pixel = int(pixel)
    return (pixel >> 16) & 255, (pixel >> 8) & 255, pixel & 255


def sample_patch(img, x, y, width=320, height=256, size=9):
    """Call against the untouched camera frame; samples never reference draw buffers."""
    half = size // 2
    rgb = np.asarray([pixel_rgb(img, px, py)
                      for py in range(max(0, y-half), min(height, y+half+1))
                      for px in range(max(0, x-half), min(width, x+half+1))], dtype=np.uint8)
    if not len(rgb):
        raise ValueError("sample outside image")
    lab = rgb_to_lab(rgb)
    center = np.median(lab, axis=0)
    distances = np.linalg.norm(lab-center, axis=1)
    # Patch variability plus a small sensor/quantization margin, bounded so
    # a mixed background patch cannot create an enormous acceptance region.
    dispersion = float(np.percentile(distances, 90))
    radius = min(25.0, max(8.0, dispersion + 5.0))
    return {"lab": [round(float(v), 2) for v in center],
            "dispersion": round(dispersion, 2), "radius": round(radius, 2),
            "rgb": [int(v) for v in np.median(rgb, axis=0)]}


def validate_data(data):
    if not isinstance(data, dict) or data.get("version") != VERSION:
        raise ValueError("LAB calibration version mismatch")
    colors = data.get("colors")
    if not isinstance(colors, dict) or set(colors) - set(COLOR_IDS):
        raise ValueError("invalid LAB color keys")
    for name, samples in colors.items():
        if not isinstance(samples, list) or len(samples) < MIN_SAMPLES:
            raise ValueError("%s requires at least %d samples" % (name, MIN_SAMPLES))
        for sample in samples:
            if not isinstance(sample, dict) or not isinstance(sample.get("lab"), list) or len(sample["lab"]) != 3:
                raise ValueError("invalid %s LAB sample" % name)
            lab = sample["lab"]
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in lab):
                raise ValueError("non-finite LAB sample")
            if not (0 <= lab[0] <= 100 and -128 <= lab[1] <= 127 and -128 <= lab[2] <= 127):
                raise ValueError("LAB sample outside unit range")
            radius = sample.get("radius")
            if isinstance(radius, bool) or not isinstance(radius, (int, float)) or not math.isfinite(radius) or not 0 < radius <= 25:
                raise ValueError("invalid LAB radius")
            dispersion = sample.get("dispersion")
            if isinstance(dispersion, bool) or not isinstance(dispersion, (int, float)) or not math.isfinite(dispersion) or not 0 <= dispersion <= 200:
                raise ValueError("invalid LAB dispersion")
    if any(len(colors.get(name, [])) < MIN_SAMPLES for name in REQUIRED):
        raise ValueError("red/yellow/blue/white require four samples each")
    return data


def load_data(path):
    with open(path, encoding="utf-8") as stream:
        return validate_data(json.load(stream))


def save_data(path, data):
    validate_data(data)
    temp = path + ".tmp"
    try:
        with open(temp, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def classify(rgb, data):
    center = np.median(rgb_to_lab(rgb).reshape(-1, 3), axis=0)
    ranking = []
    for name, samples in data["colors"].items():
        if len(samples) < MIN_SAMPLES:
            continue
        nearest = min(samples, key=lambda item: np.linalg.norm(center-np.asarray(item["lab"])))
        distance = float(np.linalg.norm(center-np.asarray(nearest["lab"])))
        ranking.append((distance, name, nearest["radius"]))
    if not ranking:
        raise ValueError("no enabled LAB colors")
    ranking.sort()
    best = ranking[0]
    margin = ranking[1][0] - best[0] if len(ranking) > 1 else float("inf")
    estimated = best[0] > best[2] or margin < 8.0
    return best[1], estimated, best[0], margin
