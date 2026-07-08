import sys, struct
sys.path.insert(0, "/Users/jessi/Desktop/KOS-MOS ver. 2/Xenosaga1PythonExtractor")
import browse
from pathlib import Path

TAN = Path("/Users/jessi/Desktop/KOS-MOS ver. 2/Xenosaga Episode I - Der Wille zur Macht (USA)/out/dump/chain0/tanaka")
OUT = Path("/private/tmp/claude-501/-Users-jessi-Desktop-KOS-MOS-ver--2/5ea55355-2934-4326-bf36-a16019cdf8e1/scratchpad/tanaka-hunt")

# PSMCT32 block layout within a page (8 cols x 4 rows of 8x8-pixel blocks)
BLK = [
    [0, 1, 4, 5, 16, 17, 20, 21],
    [2, 3, 6, 7, 18, 19, 22, 23],
    [8, 9, 12, 13, 24, 25, 28, 29],
    [10, 11, 14, 15, 26, 27, 30, 31],
]
B2XY = {}
for r in range(4):
    for c in range(8):
        B2XY[BLK[r][c]] = (c * 8, r * 8)

def cbp_to_xy(cbp, clen):
    pages_per_row = clen // 64
    page = cbp // 32
    bip = cbp % 32
    px = (page % pages_per_row) * 64
    py = (page // pages_per_row) * 32
    bx, by = B2XY[bip]
    return px + bx, py + by

def build_canvas(data):
    _total, count, hdr = struct.unpack_from("<III", data, 4)
    subs = []
    for i in range(count):
        base = hdr + 20 * i
        w, bufw, h = struct.unpack_from("<HHH", data, base)
        gs_off, _size, addr = struct.unpack_from("<III", data, base + 8)
        subs.append((w, bufw, h, gs_off, addr))
    bufw = subs[0][1] or 8
    clen = {4: 256, 8: 512}[bufw]
    canvas = bytearray(clen * clen * 4)
    max_x = max_y = 0
    for w, _, h, gs_off, addr in subs:
        px = data[addr + 32: addr + 32 + w * h * 4]
        block = gs_off // 4096
        x0 = (block % (bufw // 2)) * 64
        y0 = (block // (bufw // 2)) * 32
        for y in range(h):
            dst = ((y0 + y) * clen + x0) * 4
            canvas[dst: dst + w * 4] = px[y * w * 4: (y + 1) * w * 4]
        max_x = max(max_x, (x0 + w) * 2)
        max_y = max(max_y, (y0 + h) * 2)
    return canvas, clen, max_x, max_y

def unswizzle(canvas, clen, W, H):
    tw = clen * 2
    idx = bytearray(W * H)
    for y in range(H):
        block_row = (y & ~0xF) * tw
        swap_selector = (((y + 2) >> 2) & 1) * 4
        col_row = ((((y & ~3) >> 1) + (y & 1)) & 7) * tw * 2
        byte_y = (y >> 1) & 1
        drow = y * W
        for x in range(W):
            idx[drow + x] = canvas[
                block_row + (x & ~0xF) * 2 + col_row
                + ((x + swap_selector) & 7) * 4 + byte_y + ((x >> 2) & 2)]
    return idx

def pal_score(pal):
    if pal is None:
        return -1
    colors = set(p[:3] for p in pal)
    bad_alpha = sum(1 for p in pal if p[3] > 255)  # already scaled
    return len(colors)

def apply_pal(idx, W, H, pal):
    out = bytearray(W * H * 4)
    for i, v in enumerate(idx):
        out[i * 4: i * 4 + 4] = pal[v]
    return bytes(out)

def decode_with_cbp(name, cbp, tag):
    data = (TAN / name).read_bytes()
    canvas, clen, W, H = build_canvas(data)
    x, y = cbp_to_xy(cbp, clen)
    pal = browse._clut_at(canvas, clen, x, y)
    print(name, "cbp", hex(cbp), "-> tile", (x, y), "distinct", pal_score(pal))
    idx = unswizzle(canvas, clen, W, H)
    out = apply_pal(idx, W, H, pal)
    p = OUT / f"{Path(name).stem}_{tag}.png"
    browse.write_png(p, W, H, out)
    print("wrote", p)

if __name__ == "__main__":
    decode_with_cbp("BG.xtx", 0x3FC, "cbp3fc")
    decode_with_cbp("01_Y1.xtx", 0x3FC, "cbp3fc")
    decode_with_cbp("sam.xtx", 0x7FE, "cbp7fe")
