"""
协议处理模块 - 封装协议通信逻辑
"""
import time as pytime
from maix import time
import protocol as proto_module
import config
from coordinate import CoordinateConverter


class ProtocolHandler:
    """协议处理器"""

    def __init__(self, serial):
        self.serial = serial
        self.proto = proto_module.Protocol()
        self.converter = CoordinateConverter()

        # 状态
        self.current_state = proto_module.Protocol.STATE_INIT
        self.link_alive = False
        self.last_heartbeat_time = 0
        self.last_mcu_heartbeat_time = 0

        # 扫描请求
        self.scan_requested = False
        self.scan_req_seq = 0

        # PICK_REPORT队列
        self.pick_report_queue = []

        # 复核请求
        self.recheck_requested = False
        self.recheck_req_seq = 0
        self.recheck_expected = []

        # 倾倒/结束显示事件
        self.dump_display_pending = False
        self.finish_display_pending = None  # (total_ok, total, elapsed_s)

    def send_heartbeat(self):
        """发送心跳"""
        frame, _ = self.proto.encode_heartbeat(self.current_state, err=0)
        self.serial.write(frame)
        self.last_heartbeat_time = pytime.time()

    def check_link_alive(self):
        """检查链路状态"""
        elapsed = pytime.time() - self.last_mcu_heartbeat_time
        if elapsed > config.LINK_TIMEOUT:
            if self.link_alive:
                print("⚠ 链路中断！")
                self.link_alive = False
        else:
            if not self.link_alive:
                print("✓ 链路恢复")
                self.link_alive = True

    def process_incoming(self):
        """处理接收数据"""
        # 读取串口
        if self.serial.available() > 0:
            data = self.serial.read(self.serial.available())
            self.proto.rx_buffer.extend(data)

        # 解析帧
        frames = self.proto.process_rx_buffer()

        for cmd, seq, payload in frames:
            self._handle_frame(cmd, seq, payload)

    def _handle_frame(self, cmd, seq, payload):
        """处理单个帧"""
        if cmd == proto_module.Protocol.CMD_HB_M:
            # MCU心跳
            self.last_mcu_heartbeat_time = pytime.time()

        elif cmd == proto_module.Protocol.CMD_SYS_START:
            self._handle_sys_start(seq, payload)

        elif cmd == proto_module.Protocol.CMD_SCAN_REQ:
            self._handle_scan_req(seq, payload)

        elif cmd == proto_module.Protocol.CMD_PICK_REPORT:
            self._handle_pick_report(seq, payload)

        elif cmd == proto_module.Protocol.CMD_RECHECK_REQ:
            self._handle_recheck_req(seq, payload)

        elif cmd == proto_module.Protocol.CMD_DUMP_REPORT:
            self._handle_dump_report(seq, payload)

        elif cmd == proto_module.Protocol.CMD_FINISH:
            self._handle_finish(seq, payload)

        elif cmd == proto_module.Protocol.CMD_PING:
            self._handle_ping(seq, payload)

    def _handle_sys_start(self, seq, payload):
        """处理SYS_START"""
        mode, proto_ver = self.proto.decode_sys_start(payload)
        print(f"收到SYS_START: mode={mode}, proto_ver=0x{proto_ver:02X}")

        if proto_ver != proto_module.Protocol.PROTO_VER:
            # 版本不匹配
            ack_frame, _ = self.proto.encode_ack(
                proto_module.Protocol.CMD_ACK_SYS_START,
                proto_module.Protocol.R_PARAM,
                seq,
                extra=bytes([proto_module.Protocol.PROTO_VER])
            )
            self.serial.write(ack_frame)
            print(f"❌ 协议版本不匹配！期望0x{proto_module.Protocol.PROTO_VER:02X}")
            return

        # 应答成功
        ack_frame, _ = self.proto.encode_ack(
            proto_module.Protocol.CMD_ACK_SYS_START,
            proto_module.Protocol.R_OK,
            seq,
            extra=bytes([proto_module.Protocol.PROTO_VER])
        )
        self.serial.write(ack_frame)
        self.current_state = proto_module.Protocol.STATE_READY
        print("✓ 系统启动确认，进入READY状态")

    def _handle_scan_req(self, seq, payload):
        """处理SCAN_REQ"""
        reason = self.proto.decode_scan_req(payload)
        timestamp = pytime.strftime("%H:%M:%S", pytime.localtime())
        payload_hex = " ".join(f"{b:02X}" for b in payload) if payload else "(空)"

        print("=" * 50)
        print(f"[{timestamp}] 收到SCAN_REQ: seq={seq}, reason={reason}, "
              f"payload=[{payload_hex}]")

        if reason is None:
            print(f"⚠ SCAN_REQ负载长度不足(len={len(payload)})，仍按默认流程扫描")

        self.scan_requested = True
        self.scan_req_seq = seq
        self.current_state = proto_module.Protocol.STATE_SCANNING

    def _handle_pick_report(self, seq, payload):
        """处理PICK_REPORT"""
        result = self.proto.decode_pick_report(payload)
        if result:
            seq_no, shape, color, res, total_ok = result
            print(f"收到PICK_REPORT: 序号={seq_no}, 形状={shape}, 颜色={color}, "
                  f"结果={res}, 累计={total_ok}")
            self.pick_report_queue.append((seq, seq_no, shape, color, res, total_ok))

    def _handle_recheck_req(self, seq, payload):
        """处理RECHECK_REQ（复核请求）
        PAYLOAD: [count, (shape, color) * count]，MCU期望的物料清单
        """
        expected = self.proto.decode_recheck_req(payload)
        timestamp = pytime.strftime("%H:%M:%S", pytime.localtime())
        print("=" * 50)
        if expected is None:
            print(f"[{timestamp}] 收到RECHECK_REQ: 负载解析失败"
                  f"(len={len(payload)})，忽略")
            return

        names = []
        for shape, color in expected:
            s = proto_module.SHAPE_NAMES.get(shape, f"0x{shape:02X}")
            c = proto_module.COLOR_NAMES.get(color, f"0x{color:02X}")
            names.append(f"{c}{s}")
        print(f"[{timestamp}] 收到RECHECK_REQ: seq={seq}, "
              f"期望{len(expected)}件: {chr(12289).join(names) if names else '(空)'}")

        self.recheck_expected = expected
        self.recheck_req_seq = seq
        self.recheck_requested = True
        self.current_state = proto_module.Protocol.STATE_RECHECKING

    def _handle_dump_report(self, seq, payload):
        """处理DUMP_REPORT（倾倒完成）
        PAYLOAD: 保留，当前不解析
        """
        payload_hex = " ".join(f"{b:02X}" for b in payload) if payload else "(空)"
        print(f"收到DUMP_REPORT: seq={seq}, payload=[{payload_hex}]")

        ack_frame, _ = self.proto.encode_ack(
            proto_module.Protocol.CMD_ACK_DUMP_REPORT,
            proto_module.Protocol.R_OK, seq)
        self.serial.write(ack_frame)
        print("✓ 已应答DUMP_REPORT (0xC7)")

        self.dump_display_pending = True

    def _handle_finish(self, seq, payload):
        """处理FINISH（比赛结束）
        PAYLOAD: total_ok(u16 LE), total(u8), elapsed_s(u16 LE)
        """
        result = self.proto.decode_finish(payload)
        if result is None:
            print(f"⚠ FINISH负载解析失败(len={len(payload)})")
            total_ok, total, elapsed_s = 0, 0, 0
        else:
            total_ok, total, elapsed_s = result

        print(f"收到FINISH: 累计成功={total_ok}, 总数={total}, 用时={elapsed_s}s")

        ack_frame, _ = self.proto.encode_ack(
            proto_module.Protocol.CMD_ACK_FINISH,
            proto_module.Protocol.R_OK, seq)
        self.serial.write(ack_frame)
        print("✓ 已应答FINISH (0xC8)，进入FINISHED状态")

        self.finish_display_pending = (total_ok, total, elapsed_s)
        self.current_state = proto_module.Protocol.STATE_FINISHED

    def _handle_ping(self, seq, payload):
        """处理PING"""
        ack_frame, _ = self.proto.encode_ack(
            proto_module.Protocol.CMD_ACK_PING,
            proto_module.Protocol.R_OK,
            seq,
            extra=payload
        )
        self.serial.write(ack_frame)
        print("✓ 回应PING")

    def send_scan_result(self, detections):
        """
        发送扫描结果

        Args:
            detections: [(shape_name, color_name, norm_x, norm_y, bbox, score), ...]
        """
        debug = getattr(config, "SCAN_DEBUG_LOG", True)

        pieces = []
        skipped = 0
        for idx, (shape_name, color_name, norm_x, norm_y, bbox, score) in enumerate(detections):
            # 映射到协议ID
            shape_id = config.SHAPE_MAPPING.get(shape_name, proto_module.Protocol.SHAPE_IRREGULAR)
            color_id = config.COLOR_MAPPING.get(color_name, 0)

            if shape_name not in config.SHAPE_MAPPING and debug:
                print(f"[SEND] #{idx + 1} ⚠ 形状'{shape_name}'未在SHAPE_MAPPING中，"
                      f"按异形(0x{proto_module.Protocol.SHAPE_IRREGULAR:02X})处理")

            if color_id == 0:
                skipped += 1
                if debug:
                    print(f"[SEND] #{idx + 1} ✗ 跳过: {shape_name} "
                          f"颜色'{color_name}'无法映射到协议颜色ID "
                          f"(norm=({norm_x:.3f},{norm_y:.3f}), score={score:.3f})")
                continue

            # 模型检测得到的ROI归一化坐标 -> 实际机械坐标
            mech_x, mech_y = self.converter.normalized_to_mechanical(norm_x, norm_y)

            # 角度：当前不计算朝向。协议规定 0xFFFF=ANGLE_NONE(无方向)，
            # 发0会被MCU误解为"旋转到0度"
            angle = 0xFFFF

            if debug:
                print(f"[SEND] #{idx + 1} ✓ {shape_name}/{color_name} "
                      f"-> 形状ID=0x{shape_id:02X} 颜色ID=0x{color_id:02X} "
                      f"norm=({norm_x:.3f},{norm_y:.3f}) "
                      f"机械坐标=({mech_x},{mech_y})(0.1mm) score={score:.3f}")

            pieces.append((shape_id, color_id, mech_x, mech_y, angle))

        # 发送结果
        frames, _ = self.proto.encode_scan_result(1, pieces, self.scan_req_seq)

        if frames and len(frames) > 0:
            for frame in frames:
                self.serial.write(frame)
                time.sleep_ms(50)  # 批次间延迟
            print(f"✓ 发送扫描结果: {len(pieces)}件物料, {len(frames)}帧, "
                  f"共{sum(len(f) for f in frames)}字节 (跳过{skipped}件)")
        else:
            print("⚠ 编码扫描结果失败")

        if skipped > 0:
            print(f"⚠ 有{skipped}件检出物料因颜色未识别未上报，"
                  f"请检查HSV阈值或光照条件")
        print("=" * 50)

        return len(pieces)

    def send_recheck_result(self, expected, detections):
        """
        发送复核结果

        Args:
            expected: [(shape_id, color_id), ...] MCU期望的物料清单
            detections: [(shape_name, color_name, norm_x, norm_y, bbox, score), ...]

        Returns:
            (match, sent_count): match=1一致/0不一致
        """
        debug = getattr(config, "SCAN_DEBUG_LOG", True)

        pieces = []
        actual_pairs = []
        skipped = 0
        for idx, (shape_name, color_name, norm_x, norm_y, bbox, score) in enumerate(detections):
            shape_id = config.SHAPE_MAPPING.get(shape_name, proto_module.Protocol.SHAPE_IRREGULAR)
            color_id = config.COLOR_MAPPING.get(color_name, 0)

            if color_id == 0:
                skipped += 1
                if debug:
                    print(f"[RECHECK] #{idx + 1} ✗ 跳过: {shape_name} "
                          f"颜色'{color_name}'未识别")
                continue

            mech_x, mech_y = self.converter.normalized_to_mechanical(norm_x, norm_y)
            pieces.append((shape_id, color_id, mech_x, mech_y, 0xFFFF))
            actual_pairs.append((shape_id, color_id))

        # 比对：形状+颜色的多重集合是否一致
        match = 1 if sorted(actual_pairs) == sorted(expected) else 0

        print(f"[RECHECK] 期望{len(expected)}件, 实际{len(actual_pairs)}件"
              f"(颜色未识别跳过{skipped}件), "
              f"比对结果: {'✓ 一致' if match else '✗ 不一致'}")
        if not match:
            def _names(pairs):
                return "、".join(
                    f"{proto_module.COLOR_NAMES.get(c, hex(c))}"
                    f"{proto_module.SHAPE_NAMES.get(s, hex(s))}"
                    for s, c in pairs) or "(空)"
            print(f"[RECHECK]   期望: {_names(expected)}")
            print(f"[RECHECK]   实际: {_names(actual_pairs)}")

        # 安全限制：payload = 2 + count*10 不能超过128字节
        max_pieces = 12
        if len(pieces) > max_pieces:
            print(f"⚠ 复核结果物料数{len(pieces)}超过单帧上限，"
                  f"截断为{max_pieces}件")
            pieces = pieces[:max_pieces]

        frame, _ = self.proto.encode_recheck_result(
            match, pieces, self.recheck_req_seq)
        self.serial.write(frame)
        print(f"✓ 发送复核结果: match={match}, "
              f"{len(pieces)}件物料, {len(frame)}字节")
        print("=" * 50)

        return match, len(pieces)

    def send_pick_ack(self, req_seq):
        """发送PICK_REPORT应答"""
        ack_frame, _ = self.proto.encode_ack(
            proto_module.Protocol.CMD_ACK_PICK_REPORT,
            proto_module.Protocol.R_OK,
            req_seq
        )
        self.serial.write(ack_frame)

    def update(self):
        """周期性更新"""
        current_time = pytime.time()

        # 发送心跳
        if current_time - self.last_heartbeat_time >= config.HEARTBEAT_INTERVAL:
            self.send_heartbeat()

        # 检查链路
        self.check_link_alive()

        # 处理接收
        self.process_incoming()
