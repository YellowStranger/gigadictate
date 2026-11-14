"""Download a GigaAM model into ./models (used by CI and for the portable build)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gigadictate.engine import download_model, is_model_present  # noqa: E402

root = Path(__file__).resolve().parents[1] / "models"
for name in sys.argv[1:] or ["gigaam-v3-e2e-rnnt"]:
    if is_model_present(name, root):
        print(name, "already present")
        continue
    download_model(name, root, progress=lambda d, t: print(f"\r{name}: {d * 100 // max(t, 1)}%", end=""))
    print()
