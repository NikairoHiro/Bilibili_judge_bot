# -*- coding: utf-8 -*-
"""qr_offline.py —— 纯标准库实现的二维码生成器（供 bili_at_monitor.py 扫码登录使用）

为什么自己写：
    目标环境可能装不上 qrcode / Pillow（本机就没装），而 --login 必须能把登录链接
    变成可扫的二维码。这里按 ISO/IEC 18004 自己实现，支持
    终端字符画 / 离线 PNG / 内嵌 data-URI 的 HTML，全程不需要联网、不依赖任何第三方库。

算法与 python-qrcode 等价：GF(256) Reed-Solomon 纠错、BCH 格式信息、
8 种掩码 + 标准罚分选优、1..40 全版本、L/M/Q/H 四档纠错。数据用 byte 模式编码。
"""

from __future__ import annotations

import struct
import zlib

# 纠错等级：数值是 ISO 标准里的 2bit 编码
ERROR_CORRECT_L = 1
ERROR_CORRECT_M = 0
ERROR_CORRECT_Q = 3
ERROR_CORRECT_H = 2
_DEFAULT_EC = ERROR_CORRECT_M

# GF(256) 指数/对数表（本原多项式 x^8+x^4+x^3+x^2+1 = 0x11D）
EXP_TABLE = [0] * 256
LOG_TABLE = [0] * 256
for _i in range(8):
    EXP_TABLE[_i] = 1 << _i
for _i in range(8, 256):
    EXP_TABLE[_i] = (EXP_TABLE[_i - 4] ^ EXP_TABLE[_i - 5]
                     ^ EXP_TABLE[_i - 6] ^ EXP_TABLE[_i - 8])
for _i in range(255):
    LOG_TABLE[EXP_TABLE[_i]] = _i

# (版本, 纠错等级) -> RS 分块规格，每 3 个一组 (块数, 总码字, 数据码字)，可多段
_RS_BLOCK_TABLE = (
    (1, 26, 19), (1, 26, 16), (1, 26, 13), (1, 26, 9),                          # 1
    (1, 44, 34), (1, 44, 28), (1, 44, 22), (1, 44, 16),                         # 2
    (1, 70, 55), (1, 70, 44), (2, 35, 17), (2, 35, 13),                         # 3
    (1, 100, 80), (2, 50, 32), (2, 50, 24), (4, 25, 9),                         # 4
    (1, 134, 108), (2, 67, 43), (2, 33, 15, 2, 34, 16), (2, 33, 11, 2, 34, 12),  # 5
    (2, 86, 68), (4, 43, 27), (4, 43, 19), (4, 43, 15),                         # 6
    (2, 98, 78), (4, 49, 31), (2, 32, 14, 4, 33, 15), (4, 39, 13, 1, 40, 14),   # 7
    (2, 121, 97), (2, 60, 38, 2, 61, 39), (4, 40, 18, 2, 41, 19), (4, 40, 14, 2, 41, 15),  # 8
    (2, 146, 116), (3, 58, 36, 2, 59, 37), (4, 36, 16, 4, 37, 17), (4, 36, 12, 4, 37, 13),  # 9
    (2, 86, 68, 2, 87, 69), (4, 69, 43, 1, 70, 44), (6, 43, 19, 2, 44, 20), (6, 43, 15, 2, 44, 16),  # 10
    (4, 101, 81), (1, 80, 50, 4, 81, 51), (4, 50, 22, 4, 51, 23), (3, 36, 12, 8, 37, 13),  # 11
    (2, 116, 92, 2, 117, 93), (6, 58, 36, 2, 59, 37), (4, 46, 20, 6, 47, 21), (7, 42, 14, 4, 43, 15),  # 12
    (4, 133, 107), (8, 59, 37, 1, 60, 38), (8, 44, 20, 4, 45, 21), (12, 33, 11, 4, 34, 12),  # 13
    (3, 145, 115, 1, 146, 116), (4, 64, 40, 5, 65, 41), (11, 36, 16, 5, 37, 17), (11, 36, 12, 5, 37, 13),  # 14
    (5, 109, 87, 1, 110, 88), (5, 65, 41, 5, 66, 42), (5, 54, 24, 7, 55, 25), (11, 36, 12, 7, 37, 13),  # 15
    (5, 122, 98, 1, 123, 99), (7, 73, 45, 3, 74, 46), (15, 43, 19, 2, 44, 20), (3, 45, 15, 13, 46, 16),  # 16
    (1, 135, 107, 5, 136, 108), (10, 74, 46, 1, 75, 47), (1, 50, 22, 15, 51, 23), (2, 42, 14, 17, 43, 15),  # 17
    (5, 150, 120, 1, 151, 121), (9, 69, 43, 4, 70, 44), (17, 50, 22, 1, 51, 23), (2, 42, 14, 19, 43, 15),  # 18
    (3, 141, 113, 4, 142, 114), (3, 70, 44, 11, 71, 45), (17, 47, 21, 4, 48, 22), (9, 39, 13, 16, 40, 14),  # 19
    (3, 135, 107, 5, 136, 108), (3, 67, 41, 13, 68, 42), (15, 54, 24, 5, 55, 25), (15, 43, 15, 10, 44, 16),  # 20
    (4, 144, 116, 4, 145, 117), (17, 68, 42), (17, 50, 22, 6, 51, 23), (19, 46, 16, 6, 47, 17),  # 21
    (2, 139, 111, 7, 140, 112), (17, 74, 46), (7, 54, 24, 16, 55, 25), (34, 37, 13),  # 22
    (4, 151, 121, 5, 152, 122), (4, 75, 47, 14, 76, 48), (11, 54, 24, 14, 55, 25), (16, 45, 15, 14, 46, 16),  # 23
    (6, 147, 117, 4, 148, 118), (6, 73, 45, 14, 74, 46), (11, 54, 24, 16, 55, 25), (30, 46, 16, 2, 47, 17),  # 24
    (8, 132, 106, 4, 133, 107), (8, 75, 47, 13, 76, 48), (7, 54, 24, 22, 55, 25), (22, 45, 15, 13, 46, 16),  # 25
    (10, 142, 114, 2, 143, 115), (19, 74, 46, 4, 75, 47), (28, 50, 22, 6, 51, 23), (33, 46, 16, 4, 47, 17),  # 26
    (8, 152, 122, 4, 153, 123), (22, 73, 45, 3, 74, 46), (8, 53, 23, 26, 54, 24), (12, 45, 15, 28, 46, 16),  # 27
    (3, 147, 117, 10, 148, 118), (3, 73, 45, 23, 74, 46), (4, 54, 24, 31, 55, 25), (11, 45, 15, 31, 46, 16),  # 28
    (7, 146, 116, 7, 147, 117), (21, 73, 45, 7, 74, 46), (1, 53, 23, 37, 54, 24), (19, 45, 15, 26, 46, 16),  # 29
    (5, 145, 115, 10, 146, 116), (19, 75, 47, 10, 76, 48), (15, 54, 24, 25, 55, 25), (23, 45, 15, 25, 46, 16),  # 30
    (13, 145, 115, 3, 146, 116), (2, 74, 46, 29, 75, 47), (42, 54, 24, 1, 55, 25), (23, 45, 15, 28, 46, 16),  # 31
    (17, 145, 115), (10, 74, 46, 23, 75, 47), (10, 54, 24, 35, 55, 25), (19, 45, 15, 35, 46, 16),  # 32
    (17, 145, 115, 1, 146, 116), (14, 74, 46, 21, 75, 47), (29, 54, 24, 19, 55, 25), (11, 45, 15, 46, 46, 16),  # 33
    (13, 145, 115, 6, 146, 116), (14, 74, 46, 23, 75, 47), (44, 54, 24, 7, 55, 25), (59, 46, 16, 1, 47, 17),  # 34
    (12, 151, 121, 7, 152, 122), (12, 75, 47, 26, 76, 48), (39, 54, 24, 14, 55, 25), (22, 45, 15, 41, 46, 16),  # 35
    (6, 151, 121, 14, 152, 122), (6, 75, 47, 34, 76, 48), (46, 54, 24, 10, 55, 25), (2, 45, 15, 64, 46, 16),  # 36
    (17, 152, 122, 4, 153, 123), (29, 74, 46, 14, 75, 47), (49, 54, 24, 10, 55, 25), (24, 45, 15, 46, 46, 16),  # 37
    (4, 152, 122, 18, 153, 123), (13, 74, 46, 32, 75, 47), (48, 54, 24, 14, 55, 25), (42, 45, 15, 32, 46, 16),  # 38
    (20, 147, 117, 4, 148, 118), (40, 75, 47, 7, 76, 48), (43, 54, 24, 22, 55, 25), (10, 45, 15, 67, 46, 16),  # 39
    (19, 148, 118, 6, 149, 119), (18, 75, 47, 31, 76, 48), (34, 54, 24, 34, 55, 25), (20, 45, 15, 61, 46, 16),  # 40
)

# 对齐图形中心坐标（版本 1..40）
_PATTERN_POSITION_TABLE = (
    [], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34], [6, 22, 38], [6, 24, 42],
    [6, 26, 46], [6, 28, 50], [6, 30, 54], [6, 32, 58], [6, 34, 62],
    [6, 26, 46, 66], [6, 26, 48, 70], [6, 26, 50, 74], [6, 30, 54, 78],
    [6, 30, 56, 82], [6, 30, 58, 86], [6, 34, 62, 90], [6, 28, 50, 72, 94],
    [6, 26, 50, 74, 98], [6, 30, 54, 78, 102], [6, 28, 54, 80, 106],
    [6, 32, 58, 84, 110], [6, 30, 58, 86, 114], [6, 34, 62, 90, 118],
    [6, 26, 50, 74, 98, 122], [6, 30, 54, 78, 102, 126], [6, 26, 52, 78, 104, 130],
    [6, 30, 56, 82, 108, 134], [6, 34, 60, 86, 112, 138], [6, 30, 58, 86, 114, 142],
    [6, 34, 62, 90, 118, 146], [6, 30, 54, 78, 102, 126, 150],
    [6, 24, 50, 76, 102, 128, 154], [6, 28, 54, 80, 106, 132, 158],
    [6, 32, 58, 84, 110, 136, 162], [6, 26, 54, 82, 110, 138, 166],
    [6, 30, 58, 86, 114, 142, 170],
)

_G15 = (1 << 10) | (1 << 8) | (1 << 5) | (1 << 4) | (1 << 2) | (1 << 1) | 1
_G18 = ((1 << 12) | (1 << 11) | (1 << 10) | (1 << 9) | (1 << 8)
        | (1 << 5) | (1 << 2) | 1)
_G15_MASK = (1 << 14) | (1 << 12) | (1 << 10) | (1 << 4) | (1 << 1)

_PAD0, _PAD1 = 0xEC, 0x11
_BYTE_MODE = 4
_EC_OFFSET = {ERROR_CORRECT_L: 0, ERROR_CORRECT_M: 1, ERROR_CORRECT_Q: 2, ERROR_CORRECT_H: 3}


def _gexp(n: int) -> int:
    return EXP_TABLE[n % 255]


def _glog(n: int) -> int:
    if n < 1:
        raise ValueError("glog(%r)" % n)
    return LOG_TABLE[n]


def _rs_blocks(version: int, ec_level: int):
    spec = _RS_BLOCK_TABLE[(version - 1) * 4 + _EC_OFFSET[ec_level]]
    blocks = []
    for i in range(0, len(spec), 3):
        count, total, data = spec[i:i + 3]
        blocks.extend([(total, data)] * count)
    return blocks


# 各纠错等级下每个版本可容纳的数据位数
_BIT_LIMIT_TABLE = [
    [0] + [8 * sum(d for _, d in _rs_blocks(v, ec)) for v in range(1, 41)]
    for ec in range(4)
]


def _bch_digit(data: int) -> int:
    digit = 0
    while data:
        digit += 1
        data >>= 1
    return digit


def _bch_type_info(data: int) -> int:
    d = data << 10
    while _bch_digit(d) - _bch_digit(_G15) >= 0:
        d ^= _G15 << (_bch_digit(d) - _bch_digit(_G15))
    return ((data << 10) | d) ^ _G15_MASK


def _bch_type_number(data: int) -> int:
    d = data << 12
    while _bch_digit(d) - _bch_digit(_G18) >= 0:
        d ^= _G18 << (_bch_digit(d) - _bch_digit(_G18))
    return (data << 12) | d


# --------------------------------------------------------------------------
# GF(256) 多项式：Reed-Solomon 纠错码
# --------------------------------------------------------------------------


class _Polynomial:
    def __init__(self, num, shift):
        if not num:
            raise ValueError("empty polynomial")
        offset = 0
        for offset in range(len(num)):
            if num[offset] != 0:
                break
        self.num = list(num[offset:]) + [0] * shift

    def __getitem__(self, index):
        return self.num[index]

    def __len__(self):
        return len(self.num)

    def __mul__(self, other):
        num = [0] * (len(self) + len(other) - 1)
        for i, item in enumerate(self):
            if item == 0:
                continue
            for j, other_item in enumerate(other):
                if other_item == 0:
                    continue
                num[i + j] ^= _gexp(_glog(item) + _glog(other_item))
        return _Polynomial(num, 0)

    def __mod__(self, other):
        difference = len(self) - len(other)
        if difference < 0:
            return self
        if self[0] == 0:
            num = list(self.num[1:])
            if difference:
                num.append(0)
            return _Polynomial(num, 0) % other
        ratio = _glog(self[0]) - _glog(other[0])
        num = [item ^ _gexp(_glog(other_item) + ratio)
               for item, other_item in zip(self.num, other.num)]
        if difference:
            num.extend(self.num[-difference:])
        return _Polynomial(num, 0) % other


def _rs_generator_poly(ec_count: int) -> _Polynomial:
    poly = _Polynomial([1], 0)
    for i in range(ec_count):
        poly = poly * _Polynomial([1, _gexp(i)], 0)
    return poly


class _BitBuffer:
    def __init__(self):
        self.buffer = []
        self.length = 0

    def put(self, num, length):
        for i in range(length):
            self.put_bit(((num >> (length - i - 1)) & 1) == 1)

    def get(self, index):
        """取第 index 位（0/1），用于按位切分码字。"""
        buf_index = index // 8
        if buf_index >= len(self.buffer):
            return 0
        return (self.buffer[buf_index] >> (7 - index % 8)) & 1

    def put_bit(self, bit):
        buf_index = self.length // 8
        if len(self.buffer) <= buf_index:
            self.buffer.append(0)
        if bit:
            self.buffer[buf_index] |= 0x80 >> (self.length % 8)
        self.length += 1


def _create_data(version: int, ec_level: int, payload: bytes):
    """byte 模式编码 → 填充 → RS 纠错 → 交织，返回最终码字序列。"""
    blocks = _rs_blocks(version, ec_level)
    bit_limit = sum(data * 8 for _, data in blocks)

    buffer = _BitBuffer()
    buffer.put(_BYTE_MODE, 4)
    buffer.put(len(payload), 8 if version < 10 else 16)
    for byte in payload:
        buffer.put(byte, 8)
    if buffer.length > bit_limit:
        raise ValueError("数据过长：%d bits > %d bits" % (buffer.length, bit_limit))

    for _ in range(min(bit_limit - buffer.length, 4)):
        buffer.put_bit(False)
    delimit = buffer.length % 8
    if delimit:
        for _ in range(8 - delimit):
            buffer.put_bit(False)
    for i in range((bit_limit - buffer.length) // 8):
        buffer.put(_PAD0 if i % 2 == 0 else _PAD1, 8)

    # 交织顺序：先按「码字序号」在所有块之间轮流取数据码字，再轮流取纠错码字。
    # 所以必须按同样的顺序从位流里切码字，不能一个块一个块地连续取。
    dc_data = [[] for _ in blocks]
    ec_data = [[] for _ in blocks]
    max_dc = max(data_count for _, data_count in blocks)
    max_ec = max(total - data_count for total, data_count in blocks)

    offset = 0  # 已消费的「位」数：数据区并不按字节对齐（前 12 位是模式+长度）

    def next_byte():
        nonlocal offset
        byte = 0
        for k in range(8):
            byte = (byte << 1) | (1 if buffer.get(offset + k) else 0)
        offset += 8
        return byte

    for i in range(max_dc):
        for bi, (_, data_count) in enumerate(blocks):
            if i < data_count:
                dc_data[bi].append(next_byte())

    rs_polys = [_rs_generator_poly(total - data_count) for total, data_count in blocks]
    for bi, (total, data_count) in enumerate(blocks):
        ec_count = total - data_count
        rs_poly = rs_polys[bi]
        mod_poly = _Polynomial(dc_data[bi], len(rs_poly) - 1) % rs_poly
        mod_offset = len(mod_poly) - ec_count
        ec_data[bi] = [mod_poly[i + mod_offset] if (i + mod_offset) >= 0 else 0
                       for i in range(ec_count)]

    result = []
    for i in range(max_dc):
        for dc in dc_data:
            if i < len(dc):
                result.append(dc[i])
    for i in range(max_ec):
        for ec in ec_data:
            if i < len(ec):
                result.append(ec[i])
    return result


# --------------------------------------------------------------------------
# 掩码与罚分
# --------------------------------------------------------------------------


def _mask_func(pattern: int):
    if pattern == 0:
        return lambda i, j: (i + j) % 2 == 0
    if pattern == 1:
        return lambda i, j: i % 2 == 0
    if pattern == 2:
        return lambda i, j: j % 3 == 0
    if pattern == 3:
        return lambda i, j: (i + j) % 3 == 0
    if pattern == 4:
        return lambda i, j: (i // 2 + j // 3) % 2 == 0
    if pattern == 5:
        return lambda i, j: (i * j) % 2 + (i * j) % 3 == 0
    if pattern == 6:
        return lambda i, j: ((i * j) % 2 + (i * j) % 3) % 2 == 0
    if pattern == 7:
        return lambda i, j: ((i * j) % 3 + (i + j) % 2) % 2 == 0
    raise ValueError("bad mask pattern: %r" % pattern)


def _lost_point(modules, count: int) -> int:
    lost = 0
    # 规则1：同色连续 >= 5
    container = [0] * (count + 1)
    for row in range(count):
        this_row = modules[row]
        previous = this_row[0]
        length = 0
        for col in range(count):
            if this_row[col] == previous:
                length += 1
            else:
                if length >= 5:
                    container[length] += 1
                length = 1
                previous = this_row[col]
        if length >= 5:
            container[length] += 1
    for col in range(count):
        previous = modules[0][col]
        length = 0
        for row in range(count):
            if modules[row][col] == previous:
                length += 1
            else:
                if length >= 5:
                    container[length] += 1
                length = 1
                previous = modules[row][col]
        if length >= 5:
            container[length] += 1
    lost += sum(container[length] * (length - 2) for length in range(5, count + 1))

    # 规则2：2x2 同色块
    for row in range(count - 1):
        this_row, next_row = modules[row], modules[row + 1]
        for col in range(count - 1):
            top_right = this_row[col + 1]
            if top_right != next_row[col + 1]:
                continue
            if top_right != this_row[col] or top_right != next_row[col]:
                continue
            lost += 3

    # 规则3：1:1:3:1:1 且一侧有 4 个浅色模块
    short = count - 10
    if short > 0:
        for row in range(count):
            this_row = modules[row]
            for col in range(short):
                if (not this_row[col + 1] and this_row[col + 4]
                        and not this_row[col + 5] and this_row[col + 6]
                        and not this_row[col + 9]
                        and ((this_row[col] and this_row[col + 2] and this_row[col + 3]
                              and not this_row[col + 7] and not this_row[col + 8]
                              and not this_row[col + 10])
                             or (not this_row[col] and not this_row[col + 2]
                                 and not this_row[col + 3] and this_row[col + 7]
                                 and this_row[col + 8] and this_row[col + 10]))):
                    lost += 40
        for col in range(count):
            for row in range(short):
                if (not modules[row + 1][col] and modules[row + 4][col]
                        and not modules[row + 5][col] and modules[row + 6][col]
                        and not modules[row + 9][col]
                        and ((modules[row][col] and modules[row + 2][col]
                              and modules[row + 3][col] and not modules[row + 7][col]
                              and not modules[row + 8][col] and not modules[row + 10][col])
                             or (not modules[row][col] and not modules[row + 2][col]
                                 and not modules[row + 3][col] and modules[row + 7][col]
                                 and modules[row + 8][col] and modules[row + 10][col]))):
                    lost += 40

    # 规则4：黑白比例偏离 50%
    dark = sum(1 for row in modules for cell in row if cell)
    percent = float(dark) / (count ** 2)
    lost += int(abs(percent * 100 - 50) / 5) * 10
    return lost


def _needed_bits(payload_len: int, version: int) -> int:
    return 4 + (8 if version < 10 else 16) + 8 * payload_len


# --------------------------------------------------------------------------
# 功能图形（不承载数据的模块）
# --------------------------------------------------------------------------


def _setup_position_probe(modules, row, col, count):
    for r in range(-1, 8):
        if row + r <= -1 or count <= row + r:
            continue
        for c in range(-1, 8):
            if col + c <= -1 or count <= col + c:
                continue
            modules[row + r][col + c] = bool(
                (0 <= r <= 6 and c in (0, 6))
                or (0 <= c <= 6 and r in (0, 6))
                or (2 <= r <= 4 and 2 <= c <= 4)
            )


def _setup_position_adjust(modules, version, count):
    positions = _PATTERN_POSITION_TABLE[version - 1]
    for row in positions:
        for col in positions:
            if modules[row][col] is not None:
                continue
            for r in range(-2, 3):
                for c in range(-2, 3):
                    modules[row + r][col + c] = bool(
                        r in (-2, 2) or c in (-2, 2) or (r == 0 and c == 0)
                    )


def _setup_timing(modules, count):
    # 定时图形位于第 6 行/列、两个定位图形之间，即 8 .. count-9（版本1 为 8..12）；
    # count-8 .. count-1 那几格属于数据区，不能占。
    for r in range(8, count - 8):
        if modules[r][6] is None:
            modules[r][6] = (r % 2 == 0)
    for c in range(8, count - 8):
        if modules[6][c] is None:
            modules[6][c] = (c % 2 == 0)


def _format_info_cells(count):
    """格式信息占用的模块坐标（不含固定的深色模块），这些格子不能放数据。"""
    cells = []
    for i in range(15):
        if i < 6:
            cells.append((i, 8))
        elif i < 8:
            cells.append((i + 1, 8))
        else:
            cells.append((count - 15 + i, 8))
    for i in range(15):
        if i < 8:
            cells.append((8, count - i - 1))
        elif i < 9:
            cells.append((8, 15 - i))
        else:
            cells.append((8, 15 - i - 1))
    return cells


def _blank_modules(version: int):
    """生成只含功能图形的空白矩阵（None = 可以放数据）。

    必须把格式信息/版本信息占用的格子也先占住，否则数据会写进去又被覆盖，
    导致数据位错位（这是本文件早期版本的真实 bug，已由 selfcheck 抓出）。
    """
    count = version * 4 + 17
    modules = [[None] * count for _ in range(count)]
    _setup_position_probe(modules, 0, 0, count)
    _setup_position_probe(modules, count - 7, 0, count)
    _setup_position_probe(modules, 0, count - 7, count)
    _setup_position_adjust(modules, version, count)
    _setup_timing(modules, count)

    # 预留格式信息区（内容稍后由 _draw_type_info 按掩码写入）
    for r, c in _format_info_cells(count):
        if modules[r][c] is None:
            modules[r][c] = False
    modules[count - 8][8] = True   # 固定的深色模块

    if version >= 7:
        bits = _bch_type_number(version)
        for i in range(18):
            mod = ((bits >> i) & 1) == 1
            modules[i // 3][i % 3 + count - 8 - 3] = mod
        for i in range(18):
            mod = ((bits >> i) & 1) == 1
            modules[i % 3 + count - 8 - 3][i // 3] = mod
    return modules, count


def _draw_type_info(modules, count, ec_level, mask_pattern):
    bits = _bch_type_info((ec_level << 3) | mask_pattern)
    for i in range(15):
        mod = ((bits >> i) & 1) == 1
        if i < 6:
            modules[i][8] = mod
        elif i < 8:
            modules[i + 1][8] = mod
        else:
            modules[count - 15 + i][8] = mod
    for i in range(15):
        mod = ((bits >> i) & 1) == 1
        if i < 8:
            modules[8][count - i - 1] = mod
        elif i < 9:
            modules[8][15 - i] = mod
        else:
            modules[8][15 - i - 1] = mod
    modules[count - 8][8] = True   # 固定的深色模块


def _map_data(modules, count, data, mask_pattern):
    mask = _mask_func(mask_pattern)
    inc = -1
    row = count - 1
    bit_index = 7
    byte_index = 0
    data_len = len(data)
    for col in range(count - 1, 0, -2):
        if col <= 6:
            col -= 1
        while True:
            for c in (col, col - 1):
                if modules[row][c] is None:
                    dark = False
                    if byte_index < data_len:
                        dark = ((data[byte_index] >> bit_index) & 1) == 1
                    if mask(row, c):
                        dark = not dark
                    modules[row][c] = dark
                    bit_index -= 1
                    if bit_index == -1:
                        byte_index += 1
                        bit_index = 7
            row += inc
            if row < 0 or count <= row:
                row -= inc
                inc = -inc
                break


# --------------------------------------------------------------------------
# 二维码主体
# --------------------------------------------------------------------------


class QRCode:
    """最小可用的二维码生成器（byte 模式，自动选版本与最优掩码）。"""

    def __init__(self, data, error_correction=_DEFAULT_EC, version=None):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.payload = bytes(data)
        self.ec_level = error_correction
        self.version = version or self._best_fit()
        self.mask_pattern = 0
        self.modules = self._build()

    def _best_fit(self) -> int:
        for version in range(1, 41):
            if _needed_bits(len(self.payload), version) <= _BIT_LIMIT_TABLE[self.ec_level][version]:
                return version
        raise ValueError("数据过长，超出二维码容量")

    def _build(self):
        blank, count = _blank_modules(self.version)
        data = _create_data(self.version, self.ec_level, self.payload)

        best_pattern, best_lost, best_grid = 0, None, None
        for pattern in range(8):
            grid = [row[:] for row in blank]
            _draw_type_info(grid, count, self.ec_level, pattern)
            _map_data(grid, count, data, pattern)
            lost = _lost_point(grid, count)
            if best_lost is None or lost < best_lost:
                best_pattern, best_lost, best_grid = pattern, lost, grid
        self.mask_pattern = best_pattern
        return best_grid

    # -- 输出 ----------------------------------------------------------
    def get_matrix(self, border: int = 0):
        """返回带安静区的布尔矩阵（True = 深色模块）。"""
        if not border:
            return [list(row) for row in self.modules]
        width = len(self.modules) + border * 2
        rows = [[False] * width for _ in range(border)]
        for row in self.modules:
            rows.append([False] * border + list(row) + [False] * border)
        rows.extend([[False] * width for _ in range(border)])
        return rows

    def to_ascii(self, border: int = 2, color: bool = True) -> str:
        """终端渲染：用 ANSI 背景色画方块，任何深色终端上都能扫。

        一个字符位置 = 上下两个模块，深色用反白块表示。
        """
        matrix = self.get_matrix(border)
        lines = []
        for top in range(0, len(matrix), 2):
            row_top = matrix[top]
            row_bottom = matrix[top + 1] if top + 1 < len(matrix) else [False] * len(row_top)
            parts = []
            for x in range(len(row_top)):
                upper, lower = row_top[x], row_bottom[x]
                if color:
                    if upper and lower:
                        parts.append("\x1b[7m \x1b[0m")
                    elif upper:
                        parts.append("\x1b[7m▀\x1b[0m")
                    elif lower:
                        parts.append("\x1b[7m▄\x1b[0m")
                    else:
                        parts.append(" ")
                else:
                    parts.append("█" if upper and lower else
                                 "▀" if upper else "▄" if lower else " ")
            lines.append("".join(parts))
        return "\n".join(lines)

    def to_png_bytes(self, scale: int = 8, border: int = 4) -> bytes:
        """生成 8bit 灰度 PNG，不依赖 Pillow。"""
        matrix = self.get_matrix(border)
        width = len(matrix) * scale
        raw = bytearray()
        for row in matrix:
            line = bytearray([0])  # PNG 行滤波器 0
            for cell in row:
                line.extend((b"\x00" if cell else b"\xff") * scale)
            raw.extend(line * scale)
        ihdr = struct.pack(">IIBBBBB", width, width, 8, 0, 0, 0, 0)

        def chunk(tag, payload):
            return (struct.pack(">I", len(payload)) + tag + payload
                    + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


# --------------------------------------------------------------------------
# 自校验：按标准流程反向解析自己生成的矩阵，确保格式信息/掩码/数据都对得上
# --------------------------------------------------------------------------


def selfcheck(data: str) -> str:
    qr = QRCode(data)
    modules, count = qr.modules, len(qr.modules)

    # 1) 读格式信息并核对 BCH
    bits = 0
    for i in range(15):
        if i < 6:
            bit = modules[i][8]
        elif i < 8:
            bit = modules[i + 1][8]
        else:
            bit = modules[count - 15 + i][8]
        if bit:
            bits |= 1 << i
    found = None
    for ec in range(4):
        for mask in range(8):
            if _bch_type_info((ec << 3) | mask) == bits:
                found = (ec, mask)
                break
        if found:
            break
    if found is None:
        raise ValueError("格式信息无法解析（BCH 不匹配）")
    if found != (qr.ec_level, qr.mask_pattern):
        raise ValueError("格式信息与编码参数不一致：%r != %r"
                         % (found, (qr.ec_level, qr.mask_pattern)))

    # 2) 反掩码读回数据位
    blank, _ = _blank_modules(qr.version)
    mask = _mask_func(qr.mask_pattern)
    bits_list = []
    inc = -1
    row = count - 1
    for col in range(count - 1, 0, -2):
        if col <= 6:
            col -= 1
        while True:
            for c in (col, col - 1):
                if blank[row][c] is None:   # 只读数据区
                    val = modules[row][c]
                    if mask(row, c):
                        val = not val
                    bits_list.append(1 if val else 0)
            row += inc
            if row < 0 or count <= row:
                row -= inc
                inc = -inc
                break

    pos = 0

    def take(n):
        nonlocal pos
        value = 0
        for _ in range(n):
            value = (value << 1) | bits_list[pos]
            pos += 1
        return value

    mode = take(4)
    if mode != _BYTE_MODE:
        raise ValueError("模式不是 byte(0100)：%r" % mode)
    length = take(8 if qr.version < 10 else 16)
    payload = bytes(take(8) for _ in range(length))
    return payload.decode("utf-8")


if __name__ == "__main__":
    import sys
    text = sys.argv[1] if len(sys.argv) > 1 else "https://www.bilibili.com/"
    qr = QRCode(text)
    print("版本=%d  规模=%dx%d  纠错=%d  掩码=%d"
          % (qr.version, len(qr.modules), len(qr.modules), qr.ec_level, qr.mask_pattern))
    print(qr.to_ascii(color=False))
    print("自校验解析结果：%s" % selfcheck(text))
    print("PNG 字节数：%d" % len(qr.to_png_bytes()))
