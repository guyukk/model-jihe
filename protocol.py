"""
智能分拣装置串口通信协议 - MaixCAM侧实现
协议版本: v2.0

本模块刻意不依赖 maix、也不依赖 config，便于在 PC 上离线测试。
接收停滞阈值由上层通过 rx_resync_timeout_ms 注入。
"""
import struct
import time


class Protocol:
    """串口协议封装"""

    # 帧头
    FRAME_HEAD = bytes([0xAA, 0x55])

    # 协议版本
    PROTO_VER = 0x02

    # 告警码（CMD_ALARM_V 的 PAYLOAD[0]）
    # 说明：协议里没有 SCAN_REQ 的应答码，队列溢出无法用 SCAN_RESULT 表达
    #      （total=0 会被 MCU 理解成"没有物料"而提前完赛）。
    #      因此用已有的 ALARM 通道显式告知被拒绝的事务序号。
    ALARM_SCAN_QUEUE_FULL = 0x01

    # 托盘快照单帧最大件数（协议 §3.3）
    SNAPSHOT_MAX_PIECES = 12

    # 命令码 - 视觉发起
    CMD_HB_V = 0x01            # 心跳
    CMD_SCAN_RESULT = 0x02     # 扫描结果
    CMD_RECHECK_RESULT = 0x03  # 复核结果
    CMD_ALARM_V = 0x04         # 告警

    # 命令码 - MCU发起
    CMD_HB_M = 0x41            # 心跳
    CMD_SYS_START = 0x42       # 系统启动
    CMD_SCAN_REQ = 0x43        # 扫描请求
    CMD_PICK_REPORT = 0x44     # 分拣上报
    CMD_RECHECK_REQ = 0x45     # 复核请求
    CMD_DUMP_REPORT = 0x47     # 倾倒完成
    CMD_FINISH = 0x48          # 比赛结束
    CMD_PING = 0x4F            # 链路测试

    # 应答命令码 (请求码 | 0x80)
    CMD_ACK_SYS_START = 0xC2
    CMD_ACK_SCAN_RESULT = 0x82
    CMD_ACK_PICK_REPORT = 0xC4
    CMD_ACK_RECHECK_RESULT = 0x83
    CMD_ACK_DUMP_REPORT = 0xC7
    CMD_ACK_FINISH = 0xC8
    CMD_ACK_PING = 0xCF

    # 状态码
    STATE_INIT = 0
    STATE_READY = 1
    STATE_SCANNING = 2
    STATE_PICKING = 3
    STATE_DISPLAYING = 4
    STATE_RECHECKING = 5
    STATE_DUMPING = 6
    STATE_FINISHED = 7
    STATE_FAULT = 8

    # 结果码
    R_OK = 0x00
    R_BUSY = 0x01
    R_PARAM = 0x02
    R_HW_FAULT = 0x03
    R_NOT_READY = 0x04
    R_TIMEOUT = 0x05
    R_TRAY_MISMATCH = 0x06
    R_CAMERA_FAULT = 0x10
    R_DISPLAY_FAULT = 0x11
    R_OTHER = 0xFF

    # 形状ID
    SHAPE_CIRCLE = 0x01
    SHAPE_SQUARE = 0x02
    SHAPE_TRIANGLE = 0x03
    SHAPE_RECT = 0x04
    SHAPE_HEXAGON = 0x05
    SHAPE_IRREGULAR = 0x06

    # 颜色ID
    COLOR_RED = 0x01
    COLOR_ORANGE = 0x02
    COLOR_YELLOW = 0x03
    COLOR_GREEN = 0x04
    COLOR_BLUE = 0x05
    COLOR_PURPLE = 0x06
    COLOR_BLACK = 0x07
    COLOR_WHITE = 0x08

    def __init__(self):
        self.seq = 0  # 事务序号
        self.rx_buffer = bytearray()
        self.last_hb_time = 0
        # 链路质量计数：用于判断"对端数据是否合法"
        self.crc_errors = 0      # CRC 校验失败帧数
        self.dropped_bytes = 0   # 无帧头被丢弃的字节数
        self.bad_len = 0         # 长度字段非法(>128)次数
        self.recheck_req_truncated = 0  # 清单残缺的 RECHECK_REQ 次数
        self.crc_print_limit = 5  # CRC 错误最多打印几条，避免日志洪流

        # 接收停滞阈值：超过这么久没有新字节，队列首部的半截帧头判为伪帧头。
        # 上电初期与真正空闲时都要生效，所以 last_rx_ms 初始化为当前时刻。
        self.rx_resync_timeout_ms = 300
        self.last_rx_ms = self._now_ms()
        self.resync_stale_count = 0

    def crc8(self, data):
        """计算CRC8校验"""
        crc = 0
        for b in data:
            crc ^= b
            for _ in range(8):
                if crc & 0x80:
                    crc = ((crc << 1) ^ 0x07) & 0xFF
                else:
                    crc = (crc << 1) & 0xFF
        return crc

    def build_frame(self, cmd, payload=b'', seq=None):
        """构建协议帧"""
        if seq is None:
            seq = self.seq
            self.seq = (self.seq + 1) & 0xFF

        length = len(payload)
        if length > 128:
            raise ValueError("Payload too large")

        frame = self.FRAME_HEAD + bytes([length, cmd, seq]) + payload
        crc = self.crc8(frame)
        frame += bytes([crc])

        return frame, seq

    def parse_frame(self, data):
        """解析接收到的帧
        返回: (cmd, seq, payload) 或 None
        """
        if len(data) < 6:
            return None

        # 检查帧头
        if data[0] != 0xAA or data[1] != 0x55:
            return None

        length = data[2]
        if length > 128:
            return None

        # 检查帧长度
        expected_len = 6 + length
        if len(data) < expected_len:
            return None

        # 提取字段
        cmd = data[3]
        seq = data[4]
        payload = data[5:5+length]
        crc_recv = data[5+length]

        # 校验CRC
        crc_calc = self.crc8(data[:5+length])
        if crc_recv != crc_calc:
            self.crc_errors += 1
            # 限流打印：对端数据脏时可能是每秒几十条，不能无限刷屏
            if self.crc_errors <= self.crc_print_limit:
                print(f"CRC错误: 接收={crc_recv:02X}, 计算={crc_calc:02X} "
                      f"(累计{self.crc_errors})")
            elif self.crc_errors % 100 == 0:
                print(f"CRC错误累计 {self.crc_errors} 条，疑似链路质量差")
            return None

        return cmd, seq, payload

    def encode_heartbeat(self, state, err=0):
        """编码心跳帧"""
        payload = bytes([state, err, 0])
        return self.build_frame(self.CMD_HB_V, payload)

    def encode_alarm(self, code, ref_seq=0):
        """编码告警帧

        PAYLOAD: [告警码, 关联事务序号]
        用于把"视觉端拒绝了某条事务"明确告知 MCU，而不是让它超时。
        """
        return self.build_frame(self.CMD_ALARM_V,
                                bytes([code & 0xFF, ref_seq & 0xFF]))

    def encode_snapshot(self, snap_seq, pieces, seq=None):
        """编码 0x02 托盘快照（协议 v3.1）

        载荷布局：
          [0] snap_seq  快照号，每产生一次新扫描结果 +1；同帧重发保持不变
          [1] total     恒等于 count（一帧即一次完整快照，不分批）
          [2] batch     恒为 0（保留字段，只为与 v3.0 字节布局兼容）
          [3] count     本帧件数，0~12
          [4+] 件×count 每件 10 字节

        count=0 时 LEN=4；空盘也必须发这一帧，不许静默。
        pieces: [(shape, color, pickable, x_mm, y_mm, angle), ...]
        返回 (frame, seq)
        """
        pieces = list(pieces)[:self.SNAPSHOT_MAX_PIECES]
        count = len(pieces)

        payload = bytearray([snap_seq & 0xFF, count, 0, count])
        for shape, color, pickable, x_mm, y_mm, angle in pieces:
            payload.extend(struct.pack('<BBBB hh H',
                                       shape, color, pickable, 0,
                                       int(x_mm), int(y_mm), angle))

        return self.build_frame(self.CMD_SCAN_RESULT, bytes(payload), seq=seq)

    def encode_scan_result(self, reason, pieces, req_seq):
        """编码扫描结果
        pieces: [(shape, color, x_mm, y_mm, angle), ...]
        x_mm, y_mm: 机械坐标，单位0.1mm (已转换好)
        """
        total = len(pieces)
        frames = []

        # 如果没有物料，发送一个空的结果帧
        if total == 0:
            payload = bytearray([reason, 0, 0, 0])  # reason, total=0, batch=0, count=0
            frame, _ = self.build_frame(self.CMD_SCAN_RESULT, bytes(payload), seq=req_seq)
            frames.append(frame)
            return frames, req_seq

        # 分批发送，每批最多12件
        batch_size = 12
        for batch_idx in range(0, total, batch_size):
            batch_pieces = pieces[batch_idx:batch_idx + batch_size]
            count = len(batch_pieces)
            batch_num = batch_idx // batch_size

            # 构建payload
            payload = bytearray([reason, total, batch_num, count])

            for shape, color, x_mm, y_mm, angle in batch_pieces:
                # piece结构: shape, color, pickable, rsv, x(i16), y(i16), angle(u16)
                pickable = 1 if shape != self.SHAPE_IRREGULAR else 0

                # 打包成10字节
                piece_data = struct.pack('<BBBB hh H',
                    shape, color, pickable, 0,
                    int(x_mm), int(y_mm), angle
                )
                payload.extend(piece_data)

            frame, _ = self.build_frame(self.CMD_SCAN_RESULT, bytes(payload), seq=req_seq)
            frames.append(frame)

        return frames, req_seq

    def encode_ack(self, ack_cmd, result, req_seq, extra=None):
        """编码应答帧"""
        if extra is None:
            payload = bytes([result])
        else:
            payload = bytes([result]) + extra

        return self.build_frame(ack_cmd, payload, seq=req_seq)

    def decode_sys_start(self, payload):
        """解码SYS_START"""
        if len(payload) < 2:
            return None, None
        return payload[0], payload[1]  # mode, proto_ver

    def decode_scan_req(self, payload):
        """解码SCAN_REQ"""
        if len(payload) < 2:
            return None
        return payload[0]  # reason

    def decode_pick_report(self, payload):
        """解码PICK_REPORT"""
        if len(payload) < 6:
            return None
        seq_no, shape, color, result = struct.unpack('<BBBB', payload[:4])
        total_ok, = struct.unpack('<H', payload[4:6])
        return seq_no, shape, color, result, total_ok

    def decode_recheck_req(self, payload):
        """解码RECHECK_REQ

        返回 None 表示请求不可用：长度不足以容纳声明数量的清单。
        残缺请求若被接受，复核会拿一份短清单去比对，可能误判为"一致"。
        """
        if len(payload) < 1:
            return None
        count = payload[0]
        need = 1 + count * 2
        if len(payload) < need:
            self.recheck_req_truncated += 1
            return None
        return [(payload[1 + i * 2], payload[1 + i * 2 + 1])
                for i in range(count)]

    def encode_recheck_result(self, match, pieces, req_seq):
        """编码复核结果
        pieces: [(shape, color, x_mm, y_mm, angle), ...]
        """
        count = len(pieces)
        payload = bytearray([match, count])

        for shape, color, x_mm, y_mm, angle in pieces:
            pickable = 1 if shape != self.SHAPE_IRREGULAR else 0
            piece_data = struct.pack('<BBBB hh H',
                shape, color, pickable, 0,
                int(x_mm), int(y_mm), angle
            )
            payload.extend(piece_data)

        return self.build_frame(self.CMD_RECHECK_RESULT, bytes(payload), seq=req_seq)

    def decode_finish(self, payload):
        """解码FINISH"""
        if len(payload) < 5:
            return None
        total_ok, total, elapsed_s = struct.unpack('<HBH', payload[:5])
        return total_ok, total, elapsed_s

    def process_rx_buffer(self):
        """处理接收缓冲区，提取完整帧
        返回: [(cmd, seq, payload), ...]

        重同步规则（噪声之后必须能重新对上帧）：
        1) 找不到帧头时保留末尾的 0xAA —— 它可能是被分包截断的帧头前半，
           直接清掉会让紧接着到达的 0x55 永远接不上
        2) 帧头对上但 CRC 不过时只丢弃这 2 个帧头字节，不丢弃声称的整帧长度，
           否则一个长度字段错误的伪帧会把它后面的合法帧一起吞掉
        3) 帧头声明的长度还没凑齐、而后面已经有一个能凑齐的真帧头时，
           认定当前是伪帧头并让位
        """
        frames = []

        while True:
            # 步骤1： 定位帧头
            idx = self.rx_buffer.find(self.FRAME_HEAD)

            if idx == -1:
                # 1.1 没有完整帧头。末尾若是单个 0xAA，保留它等下半截
                keep = 1 if self.rx_buffer[-1:] == self.FRAME_HEAD[:1] else 0
                self.dropped_bytes += len(self.rx_buffer) - keep
                if keep:
                    del self.rx_buffer[:-1]
                else:
                    self.rx_buffer.clear()
                break

            # 1.2 丢掉帧头之前的垃圾
            if idx > 0:
                self.dropped_bytes += idx
                del self.rx_buffer[:idx]

            # 步骤2： 长度与完整性检查
            if len(self.rx_buffer) < 6:
                break                       # 等更多数据

            length = self.rx_buffer[2]
            if length > 128:
                self.bad_len += 1
                self.dropped_bytes += 2
                del self.rx_buffer[:2]
                continue

            frame_len = 6 + length
            if len(self.rx_buffer) < frame_len:
                # 帧还没到齐就继续等。
                # 这里**不能**按"后面还有一个看起来完整的帧头"抢先重同步：
                # 合法帧的负载里完全可能出现 AA 55 开头的字节序列，
                # 那样会把分片到达的合法帧拆散，甚至把负载里的字节当成
                # 一条独立命令执行。判定伪帧头交给 resync_stale() 的接收超时。
                break

            # 步骤3： 提取并解析
            frame_data = bytes(self.rx_buffer[:frame_len])
            result = self.parse_frame(frame_data)
            if result:
                del self.rx_buffer[:frame_len]
                frames.append(result)
            else:
                # 3.1 CRC 不过：只丢帧头，下一轮重新找 AA 55
                self.dropped_bytes += 2
                del self.rx_buffer[:2]

        return frames

    def note_rx(self):
        """记录最近一次收到字节的时刻，供 resync_stale 判断接收是否停滞"""
        self.last_rx_ms = self._now_ms()

    @staticmethod
    def _now_ms():
        return int(time.monotonic() * 1000)

    def resync_stale(self):
        """接收停滞超过阈值时，判定队首帧头是伪的并丢弃它

        为什么用超时而不是"后面有完整帧头"：合法帧的负载可能内嵌
        AA 55 开头的序列，甚至内嵌一条 CRC 正确的完整帧。按长度或
        CRC 抢先重同步都会误拆合法帧，只有"对方已经不再发数据了"
        才是可靠的中止判据。

        返回 True 表示这次丢弃了 2 字节。
        调用方必须先解析缓冲区，并在每次丢弃后立即重新解析，
        避免超时状态下连续丢弃伪帧头后面的完整合法帧。
        """
        if self.last_rx_ms is None or not self.rx_buffer:
            return False
        if self._now_ms() - self.last_rx_ms <= self.rx_resync_timeout_ms:
            return False

        # 走到这里说明接收已停滞，而缓冲区里还留着没解析掉的东西。
        # 正常完整帧在读入当次就被取走了，留下的必然是补不齐的半截帧，
        # 其帧头应判为伪帧头。
        self.bad_len += 1
        self.dropped_bytes += min(2, len(self.rx_buffer))
        del self.rx_buffer[:2]
        self.resync_stale_count += 1
        return True


# 名称映射（用于显示）
SHAPE_NAMES = {
    Protocol.SHAPE_CIRCLE: "圆形",
    Protocol.SHAPE_SQUARE: "方形",
    Protocol.SHAPE_TRIANGLE: "三角形",
    Protocol.SHAPE_RECT: "长方形",
    Protocol.SHAPE_HEXAGON: "六边形",
    Protocol.SHAPE_IRREGULAR: "异形"
}

COLOR_NAMES = {
    Protocol.COLOR_RED: "红色",
    Protocol.COLOR_ORANGE: "橙色",
    Protocol.COLOR_YELLOW: "黄色",
    Protocol.COLOR_GREEN: "绿色",
    Protocol.COLOR_BLUE: "蓝色",
    Protocol.COLOR_PURPLE: "紫色",
    Protocol.COLOR_BLACK: "黑色",
    Protocol.COLOR_WHITE: "白色"
}
