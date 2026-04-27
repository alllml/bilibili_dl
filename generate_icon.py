"""
生成应用图标（纯标准库，无需 Pillow）
生成一个 32x32 的简单下载箭头图标
"""
import struct
import zlib

def create_bmp_data(size=32):
    """创建一个简单的下载箭头 BMP 位图数据"""
    pixels = []
    center = size // 2

    for y in range(size):
        row = []
        # BMP 是从下到上存储的，翻转 y
        fy = size - 1 - y
        for x in range(size):
            # 圆形背景
            dx, dy = x - center, fy - center
            dist = (dx * dx + dy * dy) ** 0.5

            if dist > center - 1:
                # 透明区域
                row.append((0, 0, 0, 0))
                continue

            # 箭头区域（白色）
            in_arrow = False

            # 竖线部分（上半段）
            if abs(x - center) <= 3 and 6 <= fy <= 20:
                in_arrow = True

            # 三角形部分（下半段）
            tri_top = 16
            tri_bottom = 26
            if tri_top <= fy <= tri_bottom:
                tri_width = int((fy - tri_top) * 0.9) + 4
                if abs(x - center) <= tri_width:
                    in_arrow = True

            # 底部横线
            if 4 <= fy <= 6 and 6 <= abs(x - center) <= 12:
                in_arrow = True
            if 4 <= fy <= 8 and abs(x - center) >= 11 and abs(x - center) <= 13:
                in_arrow = True

            if in_arrow:
                row.append((255, 255, 255, 255))
            else:
                # 蓝色背景
                row.append((185, 128, 41, 255))  # BGRA: #2980B9

        pixels.append(row)

    return pixels

def pixels_to_ico(pixels, size):
    """将像素数据转换为 ICO 格式的字节"""
    # BMP 信息头（BITMAPINFOHEADER）
    # ICO 中的 BMP 高度是实际高度的 2 倍（包含 AND mask）
    header = struct.pack('<IiiHHIIiiII',
        40,           # biSize
        size,         # biWidth
        size * 2,     # biHeight (x2 for ICO)
        1,            # biPlanes
        32,           # biBitCount (BGRA)
        0,            # biCompression
        0,            # biSizeImage
        0,            # biXPelsPerMeter
        0,            # biYPelsPerMeter
        0,            # biClrUsed
        0,            # biClrImportant
    )

    # 像素数据（BGRA）
    pixel_data = b''
    for row in pixels:
        for b, g, r, a in row:
            pixel_data += struct.pack('BBBB', b, g, r, a)

    # AND mask（全 0 = 不透明，由 alpha 通道控制）
    and_mask = b'\x00' * (((size + 31) // 32) * 4 * size)

    return header + pixel_data + and_mask

def generate_ico(output_path='icon.ico'):
    size = 32
    pixels = create_bmp_data(size)
    bmp_data = pixels_to_ico(pixels, size)

    # ICO 文件头
    ico_header = struct.pack('<HHH', 0, 1, 1)  # reserved, type=ICO, count=1

    # ICO 目录项
    data_offset = 6 + 16  # header(6) + 1 entry(16)
    entry = struct.pack('<BBBBHHII',
        size if size < 256 else 0,  # width
        size if size < 256 else 0,  # height
        0,    # color count
        0,    # reserved
        1,    # color planes
        32,   # bits per pixel
        len(bmp_data),  # data size
        data_offset,    # data offset
    )

    with open(output_path, 'wb') as f:
        f.write(ico_header + entry + bmp_data)

    print(f"icon generated: {output_path}")

if __name__ == '__main__':
    generate_ico()
