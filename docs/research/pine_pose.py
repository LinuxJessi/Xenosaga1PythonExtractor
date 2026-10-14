"""Capture a character's modelled (bind) pose from the running game.

The .jnt skeleton's rest pose is a T-pose with the hair raised, but the
meshes are modelled arms-down. The engine keeps every node's world matrix
for a loaded character in a table in EE RAM; this reads it over PCSX2's
PINE socket and writes `<stem>.pose.json`, which `browse --kinds models`
(and `lex.to_glb`) use as the bind pose so the armature lands on the mesh.

    python docs/research/pine_pose.py <dump-dir> <model path e.g. chain0/char/pc/kosmos> [--ram file]

Needs PCSX2 running the game with PINE enabled and the character loaded
(any save state with them on screen); the flag-hunt/pine.py client from
the parent workspace is used if importable, else pass --ram with a 32 MB
EE RAM dump. The table is located by matching the rest-pose positions of
the character's static joints (hips, spine) in block order.
"""
import json
import struct
import sys
from pathlib import Path

import numpy as np

KIT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(KIT))
import arx  # noqa: E402
import lex  # noqa: E402


def load(p: Path) -> bytes:
    r = p.read_bytes()
    return arx.decompress(r) if arx.is_arx(r) else r


def find_table(ram: bytes, sk: lex.Skeleton):
    """Byte offset of the node-matrix table: 64-byte column-major 4x4
    matrices, one per block, so that table[b] is block b's world matrix."""
    rest = np.array([[w[r][3] for r in range(3)] for w in sk.world_matrices()])
    # anchor on the first block with a non-zero rest position (the hips)
    anchors = [b for b in range(len(sk.types)) if sk.types[b] != 6 and np.linalg.norm(rest[b]) > 0.3][:6]
    arr = np.frombuffer(ram, dtype="<f4")
    best = None
    for b in anchors:
        t = rest[b]
        cand = np.where(np.abs(arr - t[0]) < 1e-4)[0]
        for c in cand:
            if c + 2 >= len(arr) or abs(arr[c + 1] - t[1]) > 1e-4 or abs(arr[c + 2] - t[2]) > 1e-4:
                continue
            off = c * 4 - 48          # translation sits at +48 of the 64-byte matrix
            base = off - 64 * b
            if base < 0 or off % 16:
                continue
            # score: how many static blocks (within 1 cm of rest) agree
            score = 0
            for bb in range(len(sk.types)):
                if sk.types[bb] == 6:
                    continue
                o = base + 64 * bb + 48
                if o + 12 > len(ram):
                    break
                tt = struct.unpack_from("<3f", ram, o)
                if np.linalg.norm(np.array(tt) - rest[bb]) < 0.01:
                    score += 1
            if best is None or score > best[0]:
                best = (score, base)
    return best[1] if best and best[0] >= 8 else None


def capture(ram: bytes, sk: lex.Skeleton) -> dict:
    base = find_table(ram, sk)
    if base is None:
        raise SystemExit("node-matrix table not found: is the character loaded?")
    pose = {}
    for b in range(len(sk.types)):
        if sk.types[b] == 6:
            continue
        M = np.frombuffer(ram[base + 64 * b: base + 64 * (b + 1)], dtype="<f4").reshape(4, 4)
        if np.all(np.isfinite(M)) and np.allclose(M[:3, :3] @ M[:3, :3].T, np.eye(3), atol=5e-3):
            R = M.T.copy(); R[3] = [0, 0, 0, 1]          # rows are stored as 4-vectors with w = 1
            pose[str(b)] = [float(x) for x in R.flatten()]   # row-major, translation in the last column
    return {"table_base": hex(base), "blocks": pose}


def main() -> None:
    dump, model = Path(sys.argv[1]), sys.argv[2]
    jnt = dump / (model + ".jnt")
    sk = lex.parse_jnt(load(jnt))
    if "--ram" in sys.argv:
        ram = Path(sys.argv[sys.argv.index("--ram") + 1]).read_bytes()
    else:
        sys.path.insert(0, str(KIT.parent / "flag-hunt"))
        import pine  # type: ignore
        p = pine.Pine()
        print("PCSX2:", p.title(), p.status())
        ram = p.read_block(0, 32 * 1024 * 1024)
    pose = capture(ram, sk)
    out = jnt.with_suffix(".pose.json")
    out.write_text(json.dumps({"model": model, "source": "PCSX2 EE RAM via PINE", **pose}))
    print(f"captured {len(pose['blocks'])} block matrices (table at {pose['table_base']}) -> {out}")


if __name__ == "__main__":
    main()
