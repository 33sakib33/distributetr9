import os
from pathlib import Path

out = Path(os.environ.get("OUTPUT_DIR", "output"))
out.mkdir(parents=True, exist_ok=True)
message = "hello from " + os.uname().nodename if hasattr(os, "uname") else "hello from agent"
print(message)
(out / "result.txt").write_text(message + "\n")
print("wrote", out / "result.txt")
