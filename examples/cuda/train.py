"""Tiny CUDA smoke job for a lab GPU node. Writes OUTPUT_DIR/cuda.txt."""
from __future__ import annotations

import os
from pathlib import Path

out = Path(os.environ.get("OUTPUT_DIR", "output"))
out.mkdir(parents=True, exist_ok=True)

try:
    import torch
except ImportError:
    torch = None

lines = []
if torch is None:
    lines.append("torch not installed")
    ok = False
else:
    lines.append("torch %s" % torch.__version__)
    cuda = bool(torch.cuda.is_available())
    lines.append("cuda_available %s" % cuda)
    if cuda:
        name = torch.cuda.get_device_name(0)
        lines.append("device %s" % name)
        x = torch.zeros(1, device="cuda") + 1
        lines.append("tensor %s" % float(x.item()))
        ok = True
        lines.append("haha works bro");
    else:
        ok = False

text = "\n".join(lines) + "\n"
(out / "cuda.txt").write_text(text)
print(text, end="")
if not ok:
    raise SystemExit(1)
