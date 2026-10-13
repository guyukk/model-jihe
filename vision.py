"""
视觉检测模块 - 负责物体检测和颜色识别
"""
import cv2
import json
import numpy as np
import os
import time as pytime
from maix import image, time
import config
import lab_color


class VisionDetector:
    """视觉检测器"""

    def __init__(self, detector, camera_width, camera_height):
        self.detector = detector
        self.camera_width = camera_width
        self.camera_height = camera_height

        # HSV阈值
        self.lower_blue = np.array(config.LOWER_BLUE, dtype=np.uint8)
        self.upper_blue = np.array(config.UPPER_BLUE, dtype=np.uint8)
        self.lower_yellow = np.array(config.LOWER_YELLOW, dtype=np.uint8)
        self.upper_yellow = np.array(config.UPPER_YELLOW, dtype=np.uint8)
        self.lower_red = np.array(config.LOWER_RED, dtype=np.uint8)
        self.upper_red = np.array(config.UPPER_RED, dtype=np.uint8)
        # 红色Hue环绕的第二区间（可选）
        lower_red2 = getattr(config, "LOWER_RED2", None)
        upper_red2 = getattr(config, "UPPER_RED2", None)
        if lower_red2 is not None and upper_red2 is not None:
            self.lower_red2 = np.array(lower_red2, dtype=np.uint8)
            self.upper_red2 = np.array(upper_red2, dtype=np.uint8)
        else:
            self.lower_red2 = None
            self.upper_red2 = None
        self.lower_white = np.array(config.LOWER_WHITE, dtype=np.uint8)
        self.upper_white = np.array(config.UPPER_WHITE, dtype=np.uint8)

        # The calibration tool stores its ROI in ROI_CONFIG_PATH. Keep config.py
        # values as the fallback for devices without a saved calibration.
        roi = {
            "x": config.ROI_X,
            "y": config.ROI_Y,
            "width": config.ROI_WIDTH,
            "height": config.ROI_HEIGHT
        }
        roi_config_path = getattr(config, "ROI_CONFIG_PATH", "/root/roi_config.json")
        if os.path.exists(roi_config_path):
            with open(roi_config_path, "r") as roi_file:
                saved_roi = json.load(roi_file)
            if not isinstance(saved_roi, dict):
                raise ValueError(f"ROI配置必须是JSON对象: {roi_config_path}")
            for key in ("x", "y", "width", "height"):
                value = saved_roi.get(key)
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(f"ROI配置字段 {key} 必须为整数: {roi_config_path}")
                roi[key] = value
            if (roi["x"] < 0 or roi["y"] < 0 or roi["width"] <= 0 or
                    roi["height"] <= 0 or
                    roi["x"] + roi["width"] > config.ROI_CALIBRATION_WIDTH or
                    roi["y"] + roi["height"] > config.ROI_CALIBRATION_HEIGHT):
                raise ValueError(f"ROI配置超出标定分辨率范围: {roi_config_path}")
            print(f"已加载ROI标定: {roi_config_path}")

        # ROI缩放
        self.roi_scale_x = camera_width / config.ROI_CALIBRATION_WIDTH
        self.roi_scale_y = camera_height / config.ROI_CALIBRATION_HEIGHT

        self.roi_scaled = {
            "x": int(roi["x"] * self.roi_scale_x),
            "y": int(roi["y"] * self.roi_scale_y),
            "width": int(roi["width"] * self.roi_scale_x),
            "height": int(roi["height"] * self.roi_scale_y)
        }

        print(f"ROI缩放: {self.roi_scale_x:.3f}x, {self.roi_scale_y:.3f}x")
        print(f"缩放后ROI: ({self.roi_scaled['x']}, {self.roi_scaled['y']}) "
              f"{self.roi_scaled['width']}x{self.roi_scaled['height']}")

        # 最近一次扫描的统计信息（供外部汇总打印）
        self.last_scan_stats = {
            "yolo_raw": 0,      # YOLO原始检出数
            "roi_out": 0,       # 中心点在ROI外被剔除数
            "color_unknown": 0, # 无有效采样像素的异常框
            "color_fallback": 0, # 已赋色但未达到可靠掩码覆盖率
            "accepted": 0,      # 最终有效物料数
        }
        self.last_scan_color_estimated = []
        self.last_color_estimated = False
        self.color_mode = "hsv"
        self.lab_data = None
        self.color_calibrated_count = 0
        mode = getattr(config, "COLOR_MODE", "auto")
        if mode not in ("auto", "hsv"):
            raise ValueError("COLOR_MODE must be auto or hsv")
        if mode == "auto":
            try:
                self.lab_data = lab_color.load_data(config.LAB_CONFIG_PATH)
                self.color_mode = "lab"
                self.color_calibrated_count = sum(
                    len(samples) >= lab_color.MIN_SAMPLES
                    for samples in self.lab_data["colors"].values())
                print("LAB颜色已标定 %d/8 色；未标定颜色可能被估测为已启用颜色" %
                      self.color_calibrated_count)
            except (OSError, ValueError, TypeError) as exc:
                print("LAB不可用，回退 HSV: %s" % exc)
        else:
            print("COLOR_MODE=hsv，强制使用旧 HSV 判色")

    def _get_rgb_pixel(self, img, x, y):
        """读取RGB像素"""
        pixel = img.get_pixel(x, y)
        if isinstance(pixel, (tuple, list)):
            if len(pixel) >= 3:
                return pixel[0], pixel[1], pixel[2]
            if len(pixel) == 1:
                pixel = pixel[0]
            else:
                return 0, 0, 0
        pixel = int(pixel)
        return (pixel >> 16) & 0xFF, (pixel >> 8) & 0xFF, pixel & 0xFF

    def _red_mask(self, hsv_roi):
        """红色掩码（支持Hue环绕的双区间）"""
        mask = cv2.inRange(hsv_roi, self.lower_red, self.upper_red)
        if self.lower_red2 is not None:
            mask2 = cv2.inRange(hsv_roi, self.lower_red2, self.upper_red2)
            mask = cv2.bitwise_or(mask, mask2)
        return mask

    def _detect_color(self, hsv_roi):
        """检测HSV图像中的主要颜色
        返回: (颜色名, 最高占比, {各颜色占比})
        """
        masks = {
            "blue": cv2.inRange(hsv_roi, self.lower_blue, self.upper_blue),
            "yellow": cv2.inRange(hsv_roi, self.lower_yellow, self.upper_yellow),
            "red": self._red_mask(hsv_roi),
            "white": cv2.inRange(hsv_roi, self.lower_white, self.upper_white),
        }

        roi_area = hsv_roi.shape[0] * hsv_roi.shape[1]
        if roi_area == 0:
            self.last_color_estimated = False
            return "unknown", 0.0, {}

        ratios = {}
        for name, mask in masks.items():
            ratios[name] = cv2.countNonZero(mask) / roi_area

        color, ratio = max(ratios.items(), key=lambda item: item[1])
        self.last_color_estimated = ratio < config.COLOR_MIN_RATIO
        if ratio == 0:
            color = self._fallback_color(hsv_roi)
        return color, ratio, ratios

    def _fallback_color(self, hsv_roi):
        """硬掩码全部落空时做低置信度判色；只返回协议支持的四种颜色。"""
        pixels = hsv_roi.reshape(-1, 3)
        # 极暗区域的 Hue 通常不稳定；若还有其它像素，先用非极暗像素。
        lit = pixels[pixels[:, 2] >= 20]
        if len(lit):
            pixels = lit
        white = ((pixels[:, 1] <= config.COLOR_FALLBACK_WHITE_MAX_S) &
                 (pixels[:, 2] >= config.COLOR_FALLBACK_WHITE_MIN_V))
        chromatic_hues = pixels[~white, 0].astype(np.int16)
        votes = {"white": int(np.count_nonzero(white))}
        names = tuple(config.COLOR_FALLBACK_HUES)
        if len(chromatic_hues):
            distances = []
            for name in names:
                delta = np.abs(chromatic_hues - config.COLOR_FALLBACK_HUES[name])
                distances.append(np.minimum(delta, 180 - delta))
            nearest = np.argmin(np.stack(distances), axis=0)
            for idx, name in enumerate(names):
                votes[name] = int(np.count_nonzero(nearest == idx))
        else:
            votes.update({name: 0 for name in names})
        return max(votes, key=votes.get)

    def detect_color_in_box(self, img, x1, y1, x2, y2):
        """检测边界框内的颜色
        返回: (颜色名, 最高占比, {各颜色占比})
        """
        self.last_color_estimated = False
        width = x2 - x1
        height = y2 - y1
        if width <= 0 or height <= 0:
            return "unknown", 0.0, {}

        x1, x2 = max(0, x1), min(self.camera_width, x2)
        y1, y2 = max(0, y1), min(self.camera_height, y2)
        if x1 >= x2 or y1 >= y2:
            return "unknown", 0.0, {}
        width, height = x2 - x1, y2 - y1

        # 收缩到bbox中心区域采样，减少边缘背景干扰
        shrink = getattr(config, "COLOR_BOX_SHRINK", 1.0)
        if 0 < shrink < 1.0:
            cx = (x1 + x2) / 2
            cy = (y1 + y2) / 2
            half_w = width * shrink / 2
            half_h = height * shrink / 2
            x1 = max(0, int(cx - half_w))
            x2 = min(self.camera_width, int(cx + half_w))
            y1 = max(0, int(cy - half_h))
            y2 = min(self.camera_height, int(cy + half_h))
            width = x2 - x1
            height = y2 - y1
            if width <= 0 or height <= 0:
                # 小框收缩后可能不足 1px；改采中心像素，不丢弃有效框。
                x1 = max(0, min(self.camera_width - 1, int(cx)))
                y1 = max(0, min(self.camera_height - 1, int(cy)))
                x2, y2 = x1 + 1, y1 + 1
                width = height = 1

        step = config.PIXEL_SAMPLE_STEP
        sample_width = (width + step - 1) // step
        sample_height = (height + step - 1) // step
        rgb_roi = np.empty((sample_height, sample_width, 3), dtype=np.uint8)

        for sample_y, y in enumerate(range(y1, y2, step)):
            for sample_x, x in enumerate(range(x1, x2, step)):
                rgb_roi[sample_y, sample_x] = self._get_rgb_pixel(img, x, y)

        if self.color_mode == "lab":
            name, estimated, distance, margin = lab_color.classify(rgb_roi, self.lab_data)
            self.last_color_estimated = estimated
            return name, 1.0 if not estimated else 0.0, {
                "lab_distance": distance, "lab_margin": margin}
        hsv_roi = cv2.cvtColor(rgb_roi, cv2.COLOR_RGB2HSV)
        return self._detect_color(hsv_roi)

    def _format_color_ratios(self, ratios):
        """格式化各颜色占比用于日志"""
        if not ratios:
            return "(no sample data)"
        return " ".join(f"{name}={ratio:.2f}" for name, ratio in ratios.items())

    def is_in_roi(self, center_x, center_y):
        """检查点是否在ROI内"""
        roi = self.roi_scaled
        x_in = roi["x"] <= center_x <= roi["x"] + roi["width"]
        y_in = roi["y"] <= center_y <= roi["y"] + roi["height"]
        return x_in and y_in

    def normalize_coordinates(self, center_x, center_y):
        """归一化坐标到ROI坐标系(0-1)"""
        roi = self.roi_scaled
        rel_x = center_x - roi["x"]
        rel_y = center_y - roi["y"]

        norm_x = rel_x / roi["width"]
        norm_y = rel_y / roi["height"]

        norm_x = max(0.0, min(1.0, norm_x))
        norm_y = max(0.0, min(1.0, norm_y))

        return norm_x, norm_y

    def detect_objects(self, img):
        """
        检测图像中的物体
        返回: [(shape_name, color_name, norm_x, norm_y, bbox, score), ...]
        """
        debug = getattr(config, "SCAN_DEBUG_LOG", True)
        debug_color = getattr(config, "SCAN_DEBUG_COLOR_DETAIL", True)

        detect_start = pytime.monotonic()
        objects = self.detector.detect(img,
                                      conf_th=config.YOLO_CONF_THRESHOLD,
                                      iou_th=config.YOLO_IOU_THRESHOLD)
        detect_ms = (pytime.monotonic() - detect_start) * 1000
        self.last_inference_ms = detect_ms

        stats = {"yolo_raw": len(objects), "roi_out": 0,
                 "color_unknown": 0, "color_fallback": 0, "accepted": 0,
                 "color_ms": 0.0}
        self.last_scan_stats = stats
        self.last_scan_color_estimated = []

        roi = self.roi_scaled
        if debug:
            print(f"[SCAN] YOLO原始检出: {len(objects)}件 "
                  f"(推理耗时{detect_ms}ms, conf_th={config.YOLO_CONF_THRESHOLD}, "
                  f"iou_th={config.YOLO_IOU_THRESHOLD})")
            print(f"[SCAN] ROI范围: x=[{roi['x']},{roi['x'] + roi['width']}], "
                  f"y=[{roi['y']},{roi['y'] + roi['height']}]")
            if len(objects) == 0:
                print("[SCAN] ⚠ YOLO未检出任何物体，可能原因: "
                      "视野内无物料/光照不足/置信度阈值过高")

        results = []
        for idx, obj in enumerate(objects):
            # 边界框
            x1 = max(0, int(obj.x))
            y1 = max(0, int(obj.y))
            x2 = min(self.camera_width, int(obj.x) + max(0, int(obj.w)))
            y2 = min(self.camera_height, int(obj.y) + max(0, int(obj.h)))

            # 中心点
            center_x = int(obj.x + obj.w / 2)
            center_y = int(obj.y + obj.h / 2)

            # 形状名称
            shape_name = self.detector.labels[obj.class_id]

            if debug:
                print(f"[SCAN] #{idx + 1} {shape_name} score={obj.score:.3f} "
                      f"bbox=({int(obj.x)},{int(obj.y)},{int(obj.w)},{int(obj.h)}) "
                      f"center=({center_x},{center_y})")

            # 检查ROI
            if not self.is_in_roi(center_x, center_y):
                stats["roi_out"] += 1
                if debug:
                    print(f"[SCAN]    ✗ 剔除: 中心点({center_x},{center_y})在ROI外")
                continue

            # 归一化坐标
            norm_x, norm_y = self.normalize_coordinates(center_x, center_y)

            # 颜色检测
            color_start = pytime.monotonic()
            color_name, color_ratio, color_ratios = self.detect_color_in_box(
                img, x1, y1, x2, y2)
            stats["color_ms"] += (pytime.monotonic() - color_start) * 1000

            if debug:
                if debug_color:
                    color_info = f"颜色占比: {self._format_color_ratios(color_ratios)}"
                else:
                    color_info = f"颜色: {color_name}={color_ratio:.2f}"
                if color_name == "unknown":
                    print(f"[SCAN]    ⚠ 无有效颜色采样: {color_info} -> 发送时跳过")
                elif self.last_color_estimated:
                    print(f"[SCAN]    ⚠ ROI内 norm=({norm_x:.3f},{norm_y:.3f}) "
                          f"颜色估测为{color_name} {color_info}")
                else:
                    print(f"[SCAN]    ✓ ROI内 norm=({norm_x:.3f},{norm_y:.3f}) "
                          f"识别为{color_name} {color_info}")

            if color_name == "unknown":
                stats["color_unknown"] += 1
            else:
                stats["accepted"] += 1
                if self.last_color_estimated:
                    stats["color_fallback"] += 1

            bbox = (int(obj.x), int(obj.y), int(obj.w), int(obj.h))
            results.append((shape_name, color_name, norm_x, norm_y, bbox, obj.score))
            self.last_scan_color_estimated.append(bool(self.last_color_estimated))

        if debug:
            print(f"[SCAN] 检测汇总: YOLO检出={stats['yolo_raw']}, "
                  f"ROI外剔除={stats['roi_out']}, "
                  f"颜色未识别={stats['color_unknown']}, "
                  f"颜色估测={stats['color_fallback']}, "
                  f"有效物料={stats['accepted']}")

        return results

    def detect_objects_stable(self, read_frame, service_protocol=None, wait_ms=None):
        """
        多帧投票检测：连续检测多帧，跨帧合并结果，过滤偶发误检/漏检

        Args:
            read_frame: 无参函数，每次调用返回一帧新的（干净）图像
            service_protocol: 可选无参回调，在采集前与检测后服务协议
            wait_ms: 可选等待回调，接收毫秒数；不提供时使用原来的 sleep

        返回: [(shape_name, color_name, norm_x, norm_y, bbox, score), ...]
        """
        frame_count = getattr(config, "SCAN_FRAME_COUNT", 3)
        min_hits = getattr(config, "SCAN_MIN_HITS", 2)
        match_dist = getattr(config, "SCAN_MATCH_DIST", 20)
        delay_ms = getattr(config, "SCAN_FRAME_DELAY_MS", 50)
        debug = getattr(config, "SCAN_DEBUG_LOG", True)

        # 逐帧检测
        frame_dets = []
        frame_stats = []
        frame_color_estimated = []
        self.last_frame_metrics = []
        for i in range(frame_count):
            if service_protocol is not None:
                service_protocol()
            capture_start = pytime.monotonic()
            img = read_frame()
            captured_at = pytime.monotonic()
            if debug:
                print(f"[SCAN] ---- 第{i + 1}/{frame_count}帧 ----")
            dets = self.detect_objects(img)
            self.last_frame_metrics.append(dict(
                capture_copy_ms=round((captured_at - capture_start) * 1000, 3),
                detect_total_ms=round((pytime.monotonic() - captured_at) * 1000, 3),
                inference_ms=getattr(self, "last_inference_ms", None)))
            frame_dets.append(dets)
            frame_stats.append(dict(self.last_scan_stats))
            flags = list(getattr(self, "last_scan_color_estimated", []))
            frame_color_estimated.append(flags if len(flags) == len(dets)
                                         else [False] * len(dets))
            if service_protocol is not None:
                service_protocol()
            if i < frame_count - 1 and delay_ms > 0:
                if wait_ms is None:
                    time.sleep_ms(delay_ms)
                else:
                    wait_ms(delay_ms)

        # 跨帧聚类
        # 两条硬约束，缺一个就会算错票：
        #   1) 同一帧里一个聚类最多吸收一个框（否则同帧的几个相邻框能凑满票数）
        #   2) 一个框只归一个聚类（否则两件相距很近的物料会被并成一件）
        # 票数按"出现过该目标的帧数"计，不按框数计。
        def _det_center(det):
            bbox = det[4]
            return (bbox[0] + bbox[2] / 2, bbox[1] + bbox[3] / 2)

        def _cluster_center(cluster):
            pts = [_det_center(d) for d in cluster["frames"].values()]
            return (sum(p[0] for p in pts) / len(pts),
                    sum(p[1] for p in pts) / len(pts))

        clusters = []   # {"shape": str, "frames": {帧号: det}, "estimated": {帧号: bool}}
        for fi, dets in enumerate(frame_dets):
            # 2.1 列出本帧所有 (框, 已有聚类) 的候选配对
            pairs = []
            for di, det in enumerate(dets):
                shape_name = det[0]
                cx, cy = _det_center(det)
                for ci, cluster in enumerate(clusters):
                    if cluster["shape"] != shape_name:
                        continue
                    if fi in cluster["frames"]:
                        continue        # 该聚类本帧已命中，不能再收
                    ccx, ccy = _cluster_center(cluster)
                    d = ((ccx - cx) ** 2 + (ccy - cy) ** 2) ** 0.5
                    if d < match_dist:
                        pairs.append((d, di, ci))

            # 2.2 按距离由近到远贪心配对，保证一对一
            pairs.sort(key=lambda p: p[0])
            used_det = set()
            used_clu = set()
            for _d, di, ci in pairs:
                if di in used_det or ci in used_clu:
                    continue
                used_det.add(di)
                used_clu.add(ci)
                clusters[ci]["frames"][fi] = dets[di]
                clusters[ci]["estimated"][fi] = frame_color_estimated[fi][di]

            # 2.3 本帧没配上的框各自新建一个聚类
            for di, det in enumerate(dets):
                if di in used_det:
                    continue
                clusters.append({"shape": det[0], "frames": {fi: det},
                                 "estimated": {fi: frame_color_estimated[fi][di]}})

        # 合并聚类结果
        merged = []
        merged_estimated = []
        for cluster in clusters:
            hits = list(cluster["frames"].values())
            hit_frames = len(cluster["frames"])
            if hit_frames < min_hits:
                if debug:
                    cx, cy = _cluster_center(cluster)
                    print(f"[SCAN] ✗ 丢弃不稳定目标: {cluster['shape']} "
                          f"仅出现{hit_frames}/{frame_count}帧 "
                          f"center=({cx:.0f},{cy:.0f})")
                continue

            # 坐标取均值；颜色优先采信有可靠掩码的帧，兜底颜色只在
            # 没有可靠观测时参与多数票，避免几帧猜测盖过一次实测。
            norm_x = sum(h[2] for h in hits) / len(hits)
            norm_y = sum(h[3] for h in hits) / len(hits)
            score = max(h[5] for h in hits)
            bbox = max(hits, key=lambda h: h[5])[4]

            color_votes = {}
            for h in hits:
                color_votes[h[1]] = color_votes.get(h[1], 0) + 1
            non_unknown = {c: n for c, n in color_votes.items()
                           if c != "unknown"}
            confident_votes = {}
            for fi, h in cluster["frames"].items():
                if h[1] != "unknown" and not cluster["estimated"][fi]:
                    confident_votes[h[1]] = confident_votes.get(h[1], 0) + 1
            if confident_votes:
                color_name = max(confident_votes.items(), key=lambda kv: kv[1])[0]
            elif non_unknown:
                color_name = max(non_unknown.items(),
                                 key=lambda kv: kv[1])[0]
            else:
                color_name = "unknown"

            # 只有被选中颜色的所有观测都靠兜底判色时，合并结果才标估测。
            estimated = color_name != "unknown" and all(
                cluster["estimated"][fi] for fi, h in cluster["frames"].items()
                if h[1] == color_name)

            if debug:
                vote_str = " ".join(f"{c}x{n}" for c, n in color_votes.items())
                print(f"[SCAN] ✓ 合并目标: {cluster['shape']}/{color_name} "
                      f"出现{hit_frames}/{frame_count}帧 颜色投票[{vote_str}] "
                      f"norm=({norm_x:.3f},{norm_y:.3f}) score={score:.3f}")

            merged.append((cluster["shape"], color_name,
                           norm_x, norm_y, bbox, score))
            merged_estimated.append(estimated)

        # 更新统计（汇总各帧，accepted/color_unknown按合并结果计）
        self.last_scan_color_estimated = merged_estimated
        self.last_scan_stats = {
            "yolo_raw": sum(s["yolo_raw"] for s in frame_stats),
            "roi_out": sum(s["roi_out"] for s in frame_stats),
            "color_unknown": sum(1 for m in merged if m[1] == "unknown"),
            "color_fallback": sum(merged_estimated),
            "accepted": sum(1 for m in merged if m[1] != "unknown"),
            "color_ms": sum(s.get("color_ms", 0) for s in frame_stats),
            "frames": frame_count,
            "merged": len(merged),
        }

        if debug:
            s = self.last_scan_stats
            print(f"[SCAN] 多帧汇总: {frame_count}帧累计YOLO检出={s['yolo_raw']}, "
                  f"ROI外剔除={s['roi_out']}, 合并后物料={s['merged']}件 "
                  f"(颜色未识别={s['color_unknown']}, 估测={s['color_fallback']})")

        return merged

    def draw_roi(self, img):
        """在图像上绘制ROI"""
        roi = self.roi_scaled
        img.draw_rect(roi["x"], roi["y"], roi["width"], roi["height"],
                     color=image.COLOR_BLUE, thickness=2)

    def draw_detections(self, img, detections):
        """在图像上绘制检测结果"""
        for shape_name, color_name, norm_x, norm_y, bbox, score in detections:
            x, y, w, h = bbox
            img.draw_rect(x, y, w, h, color=image.COLOR_RED, thickness=2)
            img.draw_string(x, y, f"{shape_name} {color_name}",
                          color=image.COLOR_RED, scale=0.8)
            img.draw_string(x, y + 15, f"({norm_x:.2f},{norm_y:.2f})",
                          color=image.COLOR_GREEN, scale=0.7)
