"""
协议处理模块 - 封装协议通信逻辑
"""
import time as pytime
from collections import deque
import protocol as proto_module
import config
from coordinate import CoordinateConverter


class ProtocolHandler:
    """协议处理器"""

    # 帧 PAYLOAD 硬上限，与 protocol.build_frame 的校验保持一致
    MAX_FRAME_PAYLOAD = 128
    # PICK_REPORT 屏显队列深度上限，防止 MCU 连发时内存无界增长
    MAX_PICK_QUEUE = 8

    def __init__(self, serial, event_sink=None):
        self.serial = serial
        self.event_sink = event_sink
        self.proto = proto_module.Protocol()
        self.converter = CoordinateConverter()

        # 状态
        self.current_state = proto_module.Protocol.STATE_INIT
        self.link_alive = False
        self.last_heartbeat_time = 0
        self.last_mcu_heartbeat_time = None

        # 串口写失败计数（写异常只累计，不终止程序）
        self.write_error_count = 0
        self.write_partial_count = 0   # 只写出部分字节的次数
        self.tx_attempt_count = 0
        self.tx_success_count = 0
        self.tx_bytes_total = 0
        self.pick_drop_count = 0

        # 诊断计数：定位"连着 MCU 才卡死"类问题
        self.rx_bytes_total = 0
        self.rx_frames_total = 0
        self.max_rx_chunk = 0
        self.cmd_counts = {}
        self.scan_count = 0
        self.recheck_count = 0

        # 最近一次实际上报的物料快照，供屏幕结果面板读取
        # 元素结构: (shape_id, color_id, mech_x, mech_y, angle)
        self.last_scan_pieces = []
        self.last_recheck_pieces = []

        # PICK_REPORT队列
        self.pick_report_queue = []

        # 复核请求
        self.recheck_requested = False
        self.recheck_req_seq = 0
        self.recheck_expected = []

        # 倾倒/结束显示事件
        self.dump_display_pending = False
        self.finish_display_pending = None  # (total_ok, total, elapsed_s)

        # 通信进程只保存不可变的已完成快照；推理期间按周期重发该缓存。
        self.snapshot_seq = 0
        self.snapshot_pieces = ()
        self.snapshot_send_count = 0
        self.snapshot_repeat_count = 0
        self.last_sent_snapshot_seq = None
        self.next_snapshot_time = 0.0
        self.snapshot_result_version = 0
        self.snapshot_completed_at = None
        self.last_snapshot_write_version = 0
        self.last_snapshot_write_at = None
        self.snapshot_write_error = ""
        self.pick_ack_cache = deque(maxlen=32)

    def _emit(self, kind, data):
        if self.event_sink is not None:
            self.event_sink(kind, data)

    def publish_snapshot(self, pieces, result_version=None, completed_at=None):
        """安装一次完整识别结果；发送 SEQ 与快照号互相独立。"""
        self.snapshot_pieces = tuple(tuple(p) for p in pieces[:12])
        self.snapshot_seq = (self.snapshot_seq + 1) & 0xFF
        self.scan_count += 1
        self.snapshot_result_version = (self.snapshot_result_version + 1
            if result_version is None else result_version)
        self.snapshot_completed_at = (pytime.monotonic()
            if completed_at is None else completed_at)

    def send_snapshot(self):
        started = pytime.monotonic()
        frame, _ = self.proto.encode_snapshot(self.snapshot_seq, self.snapshot_pieces)
        if self._write(frame):
            self.snapshot_send_count += 1
            if self.last_sent_snapshot_seq == self.snapshot_seq:
                self.snapshot_repeat_count += 1
            self.last_sent_snapshot_seq = self.snapshot_seq
            self.last_snapshot_write_version = self.snapshot_result_version
            self.last_snapshot_write_at = pytime.monotonic()
            self.snapshot_write_error = ""
        else:
            self.snapshot_write_error = "UART write failed/partial"
        self.next_snapshot_time = started + config.SNAPSHOT_INTERVAL_MS / 1000.0

    def _send_alarm(self, code, ref_seq=0):
        """向 MCU 发一条告警（用于明确拒绝某条事务）"""
        frame, _ = self.proto.encode_alarm(code, ref_seq)
        self._write(frame)

    def _write(self, frame):
        """安全写串口

        串口写异常若冒泡到主循环，会被兜底 except 捕获并终止整个进程，
        因此这里只累计失败计数并打日志，绝不向上抛出。

        返回值不能忽略：MaixPy 的 uart.write 返回实际写入的字节数，
        负值表示出错，也可能只写出去一部分。只看"没抛异常"会把
        截断发送当成成功，屏幕上就会显示已发送而 MCU 其实没收到完整帧。
        """
        self.tx_attempt_count += 1
        try:
            written = self.serial.write(frame)
        except Exception as e:
            self.write_error_count += 1
            print(f"⚠ 串口写失败(累计{self.write_error_count}次): "
                  f"{type(e).__name__}: {e}")
            return False

        # 兼容返回 None 的实现：None 视为不提供长度信息，按成功处理
        if written is None:
            self.tx_success_count += 1
            self.tx_bytes_total += len(frame)
            return True

        try:
            written = int(written)
        except (TypeError, ValueError):
            self.tx_success_count += 1
            self.tx_bytes_total += len(frame)
            return True

        if written < 0:
            self.write_error_count += 1
            print(f"⚠ 串口写返回错误码 {written}"
                  f"(累计{self.write_error_count}次)，帧{len(frame)}字节未发出")
            return False

        if written != len(frame):
            self.write_partial_count += 1
            self.tx_bytes_total += max(0, written)
            print(f"⚠ 串口只写出 {written}/{len(frame)} 字节"
                  f"(累计{self.write_partial_count}次)，帧被截断")
            return False

        self.tx_success_count += 1
        self.tx_bytes_total += len(frame)
        return True

    def send_heartbeat(self):
        """发送心跳"""
        frame, _ = self.proto.encode_heartbeat(self.current_state, err=0)
        self._write(frame)
        # 定时一律用单调时钟。
        # time.time() 会被系统校时跳变：后跳推迟心跳，前跳造成假的链路超时。
        # 给人看的时间戳仍用 pytime.strftime(pytime.localtime())。
        self.last_heartbeat_time = pytime.monotonic()

    def check_link_alive(self):
        """检查链路状态"""
        elapsed = (float("inf") if self.last_mcu_heartbeat_time is None else
                   pytime.monotonic() - self.last_mcu_heartbeat_time)
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
        # 步骤1： 读取串口
        if self.serial.available() > 0:
            chunk = self.serial.available()
            data = self.serial.read(chunk)
            if len(data) > self.max_rx_chunk:
                self.max_rx_chunk = len(data)
            self.rx_bytes_total += len(data)
            self.proto.rx_buffer.extend(data)
            self.proto.note_rx()

        # 先解析完整帧；每次超时丢弃后立即再解析，保住伪帧头后的合法帧。
        # 不能连续 resync_stale()：时间戳仍是超时状态，会把整段缓冲清空。
        frames = self.proto.process_rx_buffer()
        while self.proto.resync_stale():
            frames.extend(self.proto.process_rx_buffer())
        self.rx_frames_total += len(frames)

        for cmd, seq, payload in frames:
            self.cmd_counts[cmd] = self.cmd_counts.get(cmd, 0) + 1
            self._handle_frame(cmd, seq, payload)

    def _handle_frame(self, cmd, seq, payload):
        """处理单个帧

        单帧异常一律就地吞掉并打日志。原因是帧处理异常会一路冒泡到
        main.run() 的兜底 except，进而执行 cleanup() 结束整个进程。
        """
        try:
            self._dispatch_frame(cmd, seq, payload)
        except Exception as e:
            print(f"⚠ 帧处理异常已忽略: CMD=0x{cmd:02X} SEQ={seq} "
                  f"len={len(payload)} {type(e).__name__}: {e}")

    def _dispatch_frame(self, cmd, seq, payload):
        """按命令码分发"""
        if cmd == proto_module.Protocol.CMD_HB_M:
            # MCU心跳
            self.last_mcu_heartbeat_time = pytime.monotonic()

        elif cmd == proto_module.Protocol.CMD_SYS_START:
            self._handle_sys_start(seq, payload)

        elif cmd == proto_module.Protocol.CMD_SCAN_REQ:
            return  # v3.1 废弃：不入队、不扫描、不应答。

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
            self._write(ack_frame)
            print(f"❌ 协议版本不匹配！期望0x{proto_module.Protocol.PROTO_VER:02X}")
            return

        # 应答成功
        ack_frame, _ = self.proto.encode_ack(
            proto_module.Protocol.CMD_ACK_SYS_START,
            proto_module.Protocol.R_OK,
            seq,
            extra=bytes([proto_module.Protocol.PROTO_VER])
        )
        self._write(ack_frame)
        self.pick_ack_cache.clear()
        self.current_state = proto_module.Protocol.STATE_READY
        self._emit("start", None)
        print("✓ 系统启动确认，进入READY状态")

    def _handle_scan_req(self, seq, payload):
        """v3.1 废弃；旧工具即使直接调用此入口也不会产生响应。"""
        return

    def _handle_pick_report(self, seq, payload):
        """处理PICK_REPORT"""
        result = self.proto.decode_pick_report(payload)
        if not result:
            return

        seq_no, shape, color, res, total_ok = result
        print(f"收到PICK_REPORT: 序号={seq_no}, 形状={shape}, 颜色={color}, "
              f"结果={res}, 累计={total_ok}")

        item = (seq, seq_no, shape, color, res, total_ok)
        if item in self.pick_ack_cache:
            self.send_pick_ack(seq)  # 同一报告之前已实际显示，丢失 ACK 可重回。
            return
        if item in self.pick_report_queue:
            return  # MCU 重试不重复排队。

        # 不能在未显示时回成功 ACK；MCU 自己的显示超时策略负责放行。
        if len(self.pick_report_queue) >= self.MAX_PICK_QUEUE:
            self.pick_drop_count += 1
            print(f"⚠ PICK_REPORT队列已满({self.MAX_PICK_QUEUE})，"
                  f"未显示，不回成功应答 (累计跳过{self.pick_drop_count}条)")
            return

        self.pick_report_queue.append(item)
        self._emit("pick", item)

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
        self.recheck_count += 1
        self.current_state = proto_module.Protocol.STATE_RECHECKING
        self._emit("recheck", [seq, expected])

    def _handle_dump_report(self, seq, payload):
        """处理DUMP_REPORT（倾倒完成）
        PAYLOAD: 保留，当前不解析
        """
        payload_hex = " ".join(f"{b:02X}" for b in payload) if payload else "(空)"
        print(f"收到DUMP_REPORT: seq={seq}, payload=[{payload_hex}]")

        ack_frame, _ = self.proto.encode_ack(
            proto_module.Protocol.CMD_ACK_DUMP_REPORT,
            proto_module.Protocol.R_OK, seq)
        self._write(ack_frame)
        print("✓ 已应答DUMP_REPORT (0xC7)")

        self.dump_display_pending = True
        self._emit("dump", None)

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
        self._write(ack_frame)
        print("✓ 已应答FINISH (0xC8)，进入FINISHED状态")

        self.finish_display_pending = (total_ok, total, elapsed_s)
        self.current_state = proto_module.Protocol.STATE_FINISHED
        self._emit("finish", [total_ok, total, elapsed_s])

    def _handle_ping(self, seq, payload):
        """处理PING

        应答格式为 结果码(1字节) + 原样回显，因此回显长度必须留出 1 字节。
        请求 LEN 达到 128（协议允许的合法上限）时直接回显会让总 PAYLOAD
        变成 129，触发 protocol.build_frame 的 ValueError。
        """
        max_echo = self.MAX_FRAME_PAYLOAD - 1
        if len(payload) > max_echo:
            print(f"⚠ PING负载过长({len(payload)}字节)，回显截断为{max_echo}字节")
            payload = payload[:max_echo]

        ack_frame, _ = self.proto.encode_ack(
            proto_module.Protocol.CMD_ACK_PING,
            proto_module.Protocol.R_OK,
            seq,
            extra=payload
        )
        self._write(ack_frame)
        print("✓ 回应PING")

    def send_recheck_result(self, expected, detections, req_seq=None):
        """
        发送复核结果

        Args:
            expected: [(shape_id, color_id), ...] MCU期望的物料清单
            detections: [(shape_name, color_name, norm_x, norm_y, bbox, score), ...]
            req_seq: 本次事务对应的 RECHECK_REQ SEQ，由调用方在复核开始时固定。
                     复核期间可能又收到 RECHECK_REQ，共享字段会被改写。

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
        # 注意：颜色未识别的目标在上面被 continue 掉了。如果只是删掉，
        #      实际清单会变短；当期望清单恰好也短（例如都是空）时，
        #      就会把"有一件识别不了"误判成"清单一致"。
        #      所以只要存在未识别项，一律判为不一致。
        has_unknown = skipped > 0
        match = 0 if has_unknown else (
            1 if sorted(actual_pairs) == sorted(expected) else 0)

        if has_unknown:
            print(f"⚠ 有{skipped}件物料颜色未识别，无法与期望清单比对，"
                  f"复核结果判为不一致")

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
            match, pieces,
            self.recheck_req_seq if req_seq is None else req_seq)
        self._write(frame)
        self.last_recheck_pieces = list(pieces)
        print(f"✓ 发送复核结果: match={match}, "
              f"{len(pieces)}件物料, {len(frame)}字节")
        print("=" * 50)

        return match, len(pieces)

    def send_pick_ack(self, req_seq):
        """发送PICK_REPORT应答"""
        for item in list(self.pick_report_queue):
            if item[0] == req_seq:
                self.pick_report_queue.remove(item)
                self.pick_ack_cache.append(item)
                break
        ack_frame, _ = self.proto.encode_ack(
            proto_module.Protocol.CMD_ACK_PICK_REPORT,
            proto_module.Protocol.R_OK,
            req_seq
        )
        self._write(ack_frame)

    def update(self):
        """周期性更新"""
        current_time = pytime.monotonic()

        # 发送心跳
        if current_time - self.last_heartbeat_time >= config.HEARTBEAT_INTERVAL:
            self.send_heartbeat()

        # 检查链路
        self.check_link_alive()

        # 处理接收
        self.process_incoming()

        # 每次先收控制消息，再按期限发送，ACK 不参与快照重发调度。
        if pytime.monotonic() >= self.next_snapshot_time:
            self.send_snapshot()


def prepare_snapshot_pieces(detections, converter):
    pieces, stats, _details = snapshot_details(detections, converter)
    return pieces, stats


def snapshot_details(detections, converter):
    """业务层遵守硬坐标域；未知颜色和越界件记数，不伪造/钳位坐标。"""
    pieces = []
    details = []
    stats = {"unknown_color": 0, "invalid_coord": 0, "truncated": 0}
    for shape, color, nx, ny, _bbox, _score in detections:
        shape_id = config.SHAPE_MAPPING.get(shape, proto_module.Protocol.SHAPE_IRREGULAR)
        if not 1 <= shape_id <= 6:
            shape_id = proto_module.Protocol.SHAPE_IRREGULAR
        color_id = config.COLOR_MAPPING.get(color, 0)
        x, y = converter.normalized_to_mechanical(nx, ny)
        item = dict(shape=shape_id, color=color_id, pickable=int(shape_id != 6),
                    x=x, y=y, reason="")
        details.append(item)
        if not 1 <= color_id <= 8:
            stats["unknown_color"] += 1
            item["reason"] = "unknown_color"
            continue
        if not (0 <= x <= 1800 and 0 <= y <= 2500):
            stats["invalid_coord"] += 1
            item["reason"] = "invalid_coord"
            continue
        if len(pieces) >= 12:
            item["reason"] = "truncated"
        pieces.append((shape_id, color_id, int(shape_id != 6), x, y, 0xFFFF))
    stats["truncated"] = max(0, len(pieces) - 12)
    return pieces[:12], stats, details
