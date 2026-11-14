"""Write GigaDictate.ico (multi-size) for the exe, from the same code that draws the tray icon."""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gigadictate.assets import COLORS, icon_resource, render_icon_rgba  # noqa: E402

sizes = [16, 24, 32, 48, 64, 128]
images = [icon_resource(render_icon_rgba(s, COLORS["idle"])) for s in sizes]
out = struct.pack("<HHH", 0, 1, len(sizes))
offset = 6 + 16 * len(sizes)
for s, img in zip(sizes, images):
    out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(img), offset)
    offset += len(img)
out += b"".join(images)
Path(sys.argv[1] if len(sys.argv) > 1 else "GigaDictate.ico").write_bytes(out)
