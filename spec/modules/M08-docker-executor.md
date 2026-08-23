# M08 — Docker executor

## Goal

One container per attempt, one job per physical GPU, isolation and limits, clean teardown. v1 runs only catalog images pulled by digest.

## In scope

- Maintained images: PyTorch stable + supported CUDA, TensorFlow stable, PyTorch + common training tools, optional compiler/dev image
- Pull by immutable digest
- Attempt dirs: input (ro), output, cache, tmp
- Dataset mount ro when present (v1: optional local cache path)
- GPU access (`--gpus`)
- CPU, RAM, PID, runtime limits
- Capture stdout/stderr
- Graceful stop then force kill
- Remove container always
- No Docker socket in the container
- Conda OK *inside the image* if the image already has it; do not mutate a shared env on the host
- Local image cache; later GC in M13

## Out of scope

- Building user images (M09)
- systemd/conda non-Docker executor (deferred entirely)

## Dependencies

M07 executor interface.

## Unlocks

Real training jobs; M09; M10 output upload from `output/` .

## Contracts

Run equivalent (illustrative):

```text
docker run -d --name lab-{attempt_id} \
  --runtime nvidia \
  --gpus '"device=0"' \
  --read-only \
  --tmpfs /tmp:size=2g \
  --pids-limit 512 \
  --cpus 4 --memory 16g \
  --network none \          # v1 default; allowlisted net later if needed
  --user 10000:10000 \
  -v {input}:/input:ro \
  -v {output}:/output \
  -v {cache}:/cache \
  -v {tmp}:/jobtmp \
  --workdir /input \
  IMAGE_DIGEST \
  python /input/{entrypoint} {args...}
```

Working directory and entrypoint must match how the archive is unpacked (flatten or keep top-level folder — pick one: unpack so `entrypoint` is relative to `/input`).

Stop: `docker kill --signal SIGTERM`, wait `cancel_grace_seconds`, then SIGKILL / `docker rm -f`.

## Behavior

1. Pull digest if missing. Never run `:latest`.
2. Disk watermark: refuse start if output filesystem below high watermark; report failure_category `resource`.
3. Runtime limit: container kill after `max_runtime_seconds`; complete `timeout`.
4. Image is read-only; writes only to mounted writable dirs.
5. After wait: copy/upload `/output`, then `docker rm -f`.

## Failure modes

- Pull fails: attempt `environment` failure, do not retry forever (2 attempts).
- GPU in use by Windows: should have been skipped via `gpu_desktop_busy`; if NVIDIA error, fail `resource` and quarantine after N repeats (M13).
- Container leak: reconcile lists `lab-*` names and removes those not in the current attempt.

## Implementation steps

1. Golden Dockerfiles + pin CUDA/PyTorch versions to inventory classes.
2. Push to campus registry; store digest in `environments`.
3. Implement executor against M07 interface.
4. CUDA import test on agent start using the catalog image.
5. Limit tests (memory killer, pid fork bomb, runtime).

## Acceptance criteria

- Job using catalog PyTorch sees `torch.cuda.is_available()==True`.
- `docker ps` shows at most one `lab-*` container per PC.
- After completion, no container remains.
- Job cannot reach the Docker socket or agent config file.
- Exceeding memory or runtime produces a failed attempt with the right category, not a wedged node.
