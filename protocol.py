"""
智能分拣装置串口通信协议 - MaixCAM侧实现
协议版本: v2.0
"""
import struct
import time


class Protocol:
    """串口协议封装"""

    # 帧头
    FRAME_HEAD = bytes([0xAA, 0x55])

    # 协议版本
    PROTO_VER = 0x02

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
            print(f"CRC错误: 接收={crc_recv:02X}, 计算={crc_calc:02X}")
            return None

        return cmd, seq, payload

    def encode_heartbeat(self, state, err=0):
        """编码心跳帧"""
        payload = bytes([state, err, 0])
        return self.build_frame(self.CMD_HB_V, payload)

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
        """解码RECHECK_REQ"""
        if len(payload) < 1:
            return None
        count = payload[0]
        expected = []
        for i in range(count):
            if len(payload) < 1 + (i+1)*2:
                break
            shape = payload[1 + i*2]
            color = payload[1 + i*2 + 1]
            expected.append((shape, color))
        return expected

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
        """
        frames = []

        while len(self.rx_buffer) >= 6:
            # 查找帧头
            idx = self.rx_buffer.find(self.FRAME_HEAD)
            if idx == -1:
                # 没有帧头，清空缓冲区
                self.rx_buffer.clear()
                break

            if idx > 0:
                # 丢弃帧头前的数据
                self.rx_buffer = self.rx_buffer[idx:]

            # 检查是否有足够数据
            if len(self.rx_buffer) < 6:
                break

            length = self.rx_buffer[2]
            if length > 128:
                # 长度非法，丢弃这个帧头
                self.rx_buffer = self.rx_buffer[2:]
                continue

            frame_len = 6 + length
            if len(self.rx_buffer) < frame_len:
                # 数据不完整，等待更多数据
                break

            # 提取帧
            frame_data = bytes(self.rx_buffer[:frame_len])
            self.rx_buffer = self.rx_buffer[frame_len:]

            # 解析帧
            result = self.parse_frame(frame_data)
            if result:
                frames.append(result)

        return frames


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
