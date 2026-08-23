# M03 — Lab inventory

## Goal

Know every lab PC’s GPU class and health *before* the scheduler runs on real hardware. Quarantine unsupported cards. Name compatibility classes used by matching.

## In scope

- Offline or one-shot inventory tool (PowerShell + WSL)
- Record: GPU model, VRAM, CUDA CC, Windows version, NVIDIA driver, CPU, RAM, Windows disk, Linux VHD free space, WSL/kernel, PyTorch CUDA smoke test
- Compatibility class mapping
- Quarantine list

## Out of scope

- Continuous heartbeats (M07)
- Automatic driver updates (M13)

## Dependencies

None. Can run in parallel with M01–M06.

## Unlocks

M07 real nodes, M12 matching, M14 fleet rollout.

## Contracts

Tool output: JSONL, one object per PC, imported later as `nodes` seed or kept as `inventory/lab.jsonl`.

```json
{
  "hostname": "lab-12",
  "gpu_model": "NVIDIA GeForce RTX 4070",
  "vram_total_mb": 12282,
  "compute_capability": "8.9",
  "driver_version": "560.94",
  "windows_version": "10.0.26100",
  "cpu_cores": 16,
  "ram_mb": 32500,
  "disk_windows_free_bytes": 0,
  "disk_wsl_free_bytes": 0,
  "wsl_version": "2.4.x",
  "kernel": "...",
  "pytorch_cuda_ok": true,
  "pytorch_error": null,
  "compatibility_class": "ada-12g",
  "recommended_eligibility": "eligible"
}
```

v1 class catalog (adjust to the real lab, then freeze):

| class | meaning |
|-------|---------|
| `ada-8g` | Ada, ~8 GB (4060 class) |
| `ada-12g` | Ada, ~12 GB (4070 class) |
| `ada-16g` | Ada, ~16 GB |
| `ampere-8g` | Ampere, ~8 GB (3050 / 3070 8GB class) |
| `ampere-12g` | Ampere, ~12 GB |
| `unsupported` | too old, unstable, or no NVIDIA |

## Behavior

1. Run as admin on the PC or remotely via existing lab management.
2. CUDA test: tiny tensor `cuda:0` add in a known-good image or local Python.
3. `pytorch_cuda_ok=false` ⇒ `recommended_eligibility=quarantined`.
4. Group by class. Count how many PCs per class for capacity planning.

## Failure modes

- No NVIDIA driver: mark unsupported, do not crash the tool.
- WSL missing: record and quarantine until M07 provisions it.

## Implementation steps

1. Script: nvidia-smi, wmic/systeminfo, `wsl --status`, `df` inside WSL.
2. Optional CUDA test container.
3. Class mapping table in repo `inventory/classes.yaml`.
4. Human review of the quarantine list.

## Acceptance criteria

- One JSONL covering targeted PCs.
- Every row has a `compatibility_class`.
- Quarantine list exists before any real claim on those hosts.
- Scheduler matching docs (M12) use the same class names.
