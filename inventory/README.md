# Lab inventory (M03)

Run this **on each lab PC** (Windows admin PowerShell or inside WSL) before that machine claims real jobs.

```powershell
python inventory/probe.py --append inventory/lab.jsonl
```

Add `--cuda-test` when PyTorch is installed and you want a real `torch.cuda` smoke test. A failed CUDA test marks `recommended_eligibility=quarantined`.

Class names live in [`classes.json`](classes.json) and are the same ones the agent registers with:

| class | meaning |
|-------|---------|
| `cpu` | no NVIDIA GPU |
| `ada-8g` | Ada, ~8 GB (4060 class) |
| `ada-12g` | Ada, ~12 GB (4070 class) |
| `ada-16g` | Ada, ~16 GB |
| `ada-24g` | Ada, ~24 GB (4090 class) |
| `ampere-8g` | Ampere, ~8 GB (3050 / 3070 8GB class) |
| `ampere-12g` | Ampere, ~12 GB |
| `ampere-24g` | Ampere, ~24 GB (3090 class) |
| `unsupported` | too old, unknown, or outside a VRAM band |

Copy `inventory/lab.jsonl` back into this repo after probing. Rows with `recommended_eligibility=quarantined` belong on the human-reviewed quarantine list — do not let those hosts claim until an admin overrides.

This Mac (control plane) is expected to show `compatibility_class=cpu`. That is not a lab GPU node.
