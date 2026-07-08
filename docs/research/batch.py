import sys
sys.path.insert(0, "/private/tmp/claude-501/-Users-jessi-Desktop-KOS-MOS-ver--2/5ea55355-2934-4326-bf36-a16019cdf8e1/scratchpad/tanaka-hunt")
sys.path.insert(0, "/Users/jessi/Desktop/KOS-MOS ver. 2/Xenosaga1PythonExtractor")
from probe import TAN, OUT, build_canvas, unswizzle, apply_pal
import browse
from pathlib import Path

def best_tile(canvas, clen, max_x, max_y):
    """Scan 16x16 tiles on an 8px grid for the most CLUT-like one."""
    best = None
    for ty in range(0, clen - 15, 8):
        for tx in range(0, clen - 15, 8):
            # raw alpha must be GS-style (<= 0x80) across the tile
            ok = True
            for ey in range(0, 16, 3):
                row = ((ty + ey) * clen + tx) * 4
                for ex in range(0, 16, 3):
                    if canvas[row + ex * 4 + 3] > 0x80:
                        ok = False
                        break
                if not ok:
                    break
            if not ok:
                continue
            pal = browse._clut_at(canvas, clen, tx, ty)
            if pal is None:
                continue
            colors = len(set(p[:3] for p in pal))
            if colors < 64:
                continue
            # prefer tiles low/late in the canvas (palettes live at the tail)
            score = (colors, ty, tx)
            if best is None or score > best[0]:
                best = (score, tx, ty, pal)
    return best

results = []
outdir = OUT / "all"
outdir.mkdir(exist_ok=True)
for f in sorted(TAN.glob("*.xtx")):
    data = f.read_bytes()
    canvas, clen, W, H = build_canvas(data)
    b = best_tile(canvas, clen, W // 2, H // 2)
    if b is None:
        results.append((f.name, None))
        continue
    score, tx, ty, pal = b
    idx = unswizzle(canvas, clen, W, H)
    out = apply_pal(idx, W, H, pal)
    browse.write_png(outdir / (f.stem + ".png"), W, H, out)
    results.append((f.name, (tx, ty, score[0])))

for n, r in results:
    print(n, r)
