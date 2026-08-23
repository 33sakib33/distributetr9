"""CPU demo that needs a custom env (requests), not torch.

No network: lab job containers use --network none.
Submit archive train.py + paste requirements.txt in the portal,
pick the TensorFlow catalog base (python:3.12.7-slim).
"""
from __future__ import annotations

import os
from pathlib import Path

import requests

out = Path(os.environ.get("OUTPUT_DIR", "output"))
out.mkdir(parents=True, exist_ok=True)

line = "requests %s ok (no torch)" % requests.__version__
print(line)
(out / "result.txt").write_text(line + "\n")
print("wrote", out / "result.txt")
