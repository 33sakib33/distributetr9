---
title: "Campus Image Registry"
subtitle: "Why custom environments need a shared, content-addressed store"
date: 20 August 2026
---

# What this is about

This lab platform runs student PyTorch jobs on Windows PCs with consumer GPUs. Each PC has Docker inside WSL. A control API on the LAN queues work. Agents pull jobs.

The feature documented here is **image distribution**: a campus Docker registry (`registry:2`), a **builder** role that publishes images, and a rule that a job is not runnable until the image exists as a **registry digest** any lab PC can pull.

Without that, a custom `requirements.txt` build lives only on one machine's Docker disk. A second PC can claim the job and fail. The first PC also wastes GPU time installing pip packages.

This document is for learning the idea, not only operating the lab. It covers motivation, the theory underneath, how this repo implements it, how to turn it on, and where the same pattern appears in industry and research computing.

# The problem in one story

Alice submits `train.py` plus `requirements.txt` with `transformers`. The control plane hashes the deps, parks the job as `building environment`, and waits.

PC-A is idle. It used to claim that build, run `docker build` for twenty minutes, tag `lab-env:deadbeef`, and store `docker inspect`'s local `.Id` (`sha256:abc…`). The job became `queued`.

PC-B is also idle and matches the GPU class. It claims the training job. `docker pull sha256:abc…` fails: that id never left PC-A. Alice sees `environment` failure. If PC-A happened to claim the job, it worked by accident until reboot or image GC.

That is not a scheduler bug. The scheduler did its job: one attempt, one node, a fencing token. The **artifact was not a platform object**. It was a local cache entry pretending to be one.

A second failure sat next to it: every training GPU could take a build. Pip occupied the scarce resource (the 3050) while other students waited.

# Intuition

Think of three different things people call "the image":

1. **A tag** such as `pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime`. A pointer. It can move. Tomorrow the same tag can be different bytes.
2. **A local image id** such as `sha256:e3b0c4…` from `docker inspect .Id`. Content-addressed **on that daemon only**. Another daemon has a different id space until it pulls the same layers from a registry.
3. **A registry digest** such as `192.168.68.10:5000/lab-env/abcd@sha256:e3b0c4…`. A name plus a content hash **the registry advertised after a successful push**. Any daemon that can reach the registry can fetch those exact bytes.

The feature is: only (3) may mark an environment `ready`.

A useful analogy is git. A file on your laptop is not "in the repo." `git push` publishes objects. A commit SHA is what other clones fetch. `docker inspect .Id` is like a loose object in your `.git` that nobody else has. `name@sha256:…` is like `origin/main` pointing at a commit that `git fetch` can retrieve.

Another analogy is a university library. Writing a thesis in your room does not make it a library holding. Accession, catalog number, and a copy on the shelf do. The registry is the stack. The digest is the catalog number. Training nodes are readers, not the bindery.

# Why do it

Student GPUs are the scarce, flaky resource. Image builds are CPU, disk, and network. Mixing them means a 20-minute `pip install` blocks a 2-hour training slot.

Jobs must be **relocatable**. The next free `ampere-8g` should run Alice's job whether or not it was the machine that built the env. That is the whole point of a queue over a pile of desktops.

Reproducibility needs **immutability**. If two students share the same hash of deps + base image + GPU class, they should get the same bytes, not "whatever Hub served that afternoon" and not "whatever happened to be on disk on PC-A."

Campus networks also fail at Docker Hub: rate limits, broken Wi-Fi, exam week. A local registry plus a mirrored catalog image lets the lab run if Hub is down.

The v1 product freeze already said this. Catalog images only until a registry exists. Custom `requirements.txt` without push is a half-feature that strands jobs.

# Theory

## Content-addressed storage

A **content-addressed store** (CAS) names an object by a hash of its bytes. If you have the hash, you can verify you got the right payload. If two producers publish the same bytes, they collide on the same name (that is a feature).

Docker layers and manifests are CAS blobs (`sha256:…`). The Distribution API (what `registry:2` speaks) is a named index over those blobs: repositories, tags, and manifests.

A **tag** is a mutable pointer (`:ready`, `:latest`). A **digest** is the immutable manifest hash. Production systems pin `@sha256:…` so a rolling tag cannot silently change what ran last week.

This repo accepts a ready image only if it matches:

```
repo/name@sha256:<64 hex chars>
```

Bare `sha256:<64 hex>` is a local daemon id. It is rejected. That is the contract that closes the Alice/PC-B hole.

## Location vs content

Location addressing: "the image on PC-A." If PC-A dies, the name dies.

Content addressing: "these bytes, wherever they are replicated." The registry is the replication point for the lab.

Pinning a job to the builder (node affinity) looks like a fix and is not. The builder reboots, fills disk, or gets drained, and the environment dies with it.

## Build once, run many

The cache key in this system is:

```
sha256( canonical JSON of { requirements, base, compat } )
```

Same deps, same base image string, same GPU class → same `environment_hash` → one `environment_builds` row. Two jobs share it. The second does not start a second build if the first is pending or ready.

That is **input hashing**, the same idea as Nix derivations, Bazel action keys, and Docker build cache keys. The hash is of the *recipe*, not of the output. The output is then stored under a *content* digest after push. Both matter: the recipe hash finds the cache row; the output digest is what trainers pull.

## Separation of build and run

In compiler terms: compilation is not execution. You do not compile C on the same exclusive GPU you reserved for training, if you can avoid it.

Roles:

| Role | Claims | Must not |
|------|--------|----------|
| `train` (default) | GPU jobs | Environment builds |
| `build` | Environment builds | Training attempts |

The control plane enforces this in `POST /agent/claim`. A student 3050 with `--role train` never receives a `build` payload.

CUDA import tests still need a GPU for `assert torch.cuda.is_available()`. That can be the dedicated builder if it has a GPU, or a later split (CPU build, short GPU test slot). v1 uses one builder box.

## Pull, not push, for nodes

Lab PCs are behind Windows, WSL, and Wi-Fi. The control plane cannot reliably open a connection *to* a student desktop. Agents already **pull** jobs. They also **pull** images. The registry is another pull target, same direction.

This matches Kubernetes (kubelet pulls), Nomad, and most CI runners. HPC `srun` plus a shared filesystem is a different design: the artifact is a file on Lustre/NFS, not an OCI pull. Apptainer libraries sit between those worlds.

## What a registry actually is

`registry:2` is the official Distribution image. It is a small HTTP server on port 5000 implementing the Registry HTTP API v2 (OCI Distribution). It does not build images, run GPUs, or schedule jobs.

On `docker push` it stores layers (filesystem tarballs) and a manifest (JSON listing those layers). On `docker pull host:5000/lab-env/abcd@sha256:…` it sends the manifest, then only layers the client lacks. Two custom envs that share a PyTorch base reuse those layers. That is why a registry beats `docker save` of an 8 GB tarball through MinIO.

MinIO in this project stores **job bytes** (archives, logs, outputs). The registry stores **image bytes**. Do not conflate them. Object storage can *back* a registry (S3 driver), but speaking the image API is what makes `docker pull` work.

## Trust and the LAN

An open HTTP registry on the lab VLAN is a prototype. Anyone who can push can make trainers run their layers. Production adds TLS, `docker login`, and often a robot account: builder pushes, agents pull-only.

This repo does not yet wire registry auth to node tokens. Treat that as unfinished ops, not as "HTTP is the design."

# How this lab implements it

## Components

```
Portal / smoke script
        |
        v
FastAPI control API
  - reject requirements.txt if REGISTRY_URL is empty
  - hash deps -> environment_builds
  - claim: train -> jobs, build -> builds
  - ready iff image_digest is repo/name@sha256:64hex
        |
        +-- PostgreSQL (metadata, including that digest string)
        +-- MinIO (archives, logs, outputs)
        +-- registry:2 (layers + manifests)
        |
        v
Agents (pull)
  --role train  -> docker pull digest, run one container
  --role build  -> docker build, test, docker push, return RepoDigests[0]
```

## State machine (environments)

```
requirements.txt submitted
        |
        v
  environment_builds.state = pending
  job pending_reason = "building environment"
        |
        v
  build-role agent claims
  state = building
        |
        +-- docker build / test / push fail --> failed
        |     job completed_at set, reason "environment build failed"
        |
        +-- push ok AND digest matches regex --> ready
              env.image_digest = host/lab-env/hash@sha256:...
              job queued for any matching train node
```

Catalog jobs skip this path. They use `environments.image_digest` from seed (today a Hub tag until you mirror).

## Code map

| Piece | Where |
|-------|--------|
| `REGISTRY_URL` | `control/app/config.py` (`registry_url`) |
| Digest regex | `control/app/envbuild.py` `is_registry_digest` |
| Reject custom deps without registry | `control/app/routers/portal.py` `create_job` |
| Train vs build claim | `control/app/routers/agent.py` `claim_job` |
| Builders skipped for training fit | `control/app/scheduler.py` `is_train_node` |
| Push after test | `agent/executor.py` `build_custom_image` |
| Agent flag | `agent/agent.py` `--role train\|build` |
| Compose service | `docker-compose.yml` `registry` |
| Mirror catalog | `scripts/mirror-catalog.sh` |

The agent is stdlib-only. It duplicates the digest regex rather than importing the control app.

## What "ready" stores

After `docker push`, the builder inspects:

```
docker image inspect --format '{{index .RepoDigests 0}}' $TAG
```

That string is what Postgres keeps. Trainers pass it to `docker pull`. `LAB_IMAGE` still overrides on a developer Mac. Do not set it on lab agents; it bypasses the digest.

# Workflow (end to end)

1. Operator starts Postgres, MinIO, and `registry:2`. Sets `REGISTRY_URL` to a host **lab PCs can reach** (not `127.0.0.1` from another machine). Restarts the API.
2. Operator mirrors the catalog PyTorch image into the registry and optionally pins that RepoDigest in `environments`.
3. Each lab Docker daemon lists the registry under `insecure-registries` (HTTP on the LAN) and restarts.
4. One agent process runs `--role build --executor docker`. Student PCs stay `--role train`.
5. Student submits a catalog job: queued immediately, train node pulls the catalog image, runs.
6. Student submits `requirements.txt`: job is `building` until a builder pushes. Then any matching train node pulls the new digest and runs. Build logs are `/jobs/{id}/build-logs`, not training logs.

Two jobs with the same normalized requirements and class share one build row.

# How to turn it on

## Prerequisites

- Docker on the machine that will host `registry:2`
- Lab PCs with Docker in WSL that can open TCP 5000 to that host
- Control API already reachable (the same firewall story as port 8080)
- Agent tree version **0.6.0** or later on every node

## Steps

1. Start the registry next to the other stores:

```bash
docker compose up -d postgres minio registry
curl http://127.0.0.1:5001/v2/
```

Expected: `{}`.

2. From a **second** machine, `curl http://<lab-server>:5001/v2/`. If that fails, you have the Mac/Docker port publishing problem. Move the registry (and probably the API) to a Linux box agents can hit. Do not proceed.

3. On each lab Docker daemon, `/etc/docker/daemon.json`:

```json
{
  "insecure-registries": ["192.168.68.118:5001"]
}
```

Then restart Docker (`sudo service docker restart` in WSL). Use the real reachable host:port.

4. Mirror the catalog image:

```bash
scripts/mirror-catalog.sh 192.168.68.118:5001
```

It prints a `RepoDigest`. Prove the second PC can:

```bash
docker pull 192.168.68.118:5001/lab/pytorch@sha256:...
```

5. Set `REGISTRY_URL=192.168.68.118:5001` in `.env` (or the environment of `./scripts/run-control-lan.sh`) and restart the API. Until this is set, the API returns 400 on custom `requirements.txt`.

6. Start a builder:

```bash
python3 agent/agent.py --api http://192.168.68.118:8080 --role build --executor docker
```

Keep training agents on the default role.

## Verification

- Catalog smoke still runs on a train node with no `LAB_IMAGE`.
- Submit two jobs with the same `requirements.txt`. One build is claimed. After complete, both queue. The train node’s `image_digest` looks like `host:5000/lab-env/…@sha256:…`.
- Completing a build with a bare `sha256:…` id marks the build failed. The job never queues.
- A train-role agent with a pending build receives no work. A build-role agent does not receive catalog training jobs.

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| `curl :5001/v2/` works locally, not from lab PC | Firewall / Docker Desktop port publish / AP isolation |
| Host port 5000 already in use | macOS AirPlay/Control Center; this compose file publishes **5001** |
| `http: server gave HTTP response to HTTPS client` | Missing `insecure-registries` |
| Custom job 400 mentioning `REGISTRY_URL` | API started without the env var |
| Build succeeds on builder, train pull fails | Stored `.Id` from an old agent; upgrade to 0.6.0 |
| Train node sits in pip for 20 minutes | Agent still default and old code that claimed builds |
| Push fails | Registry down, wrong `image_tag` host, or disk full on registry volume |

# Application in this product

Students get `requirements.txt` without turning every desktop into a snowflake. Researchers reuse a hashed env. Admins drain a builder without deleting every custom image. The queue can place a job on whichever 8 GB Ampere card is free.

Catalog-only mode (empty `REGISTRY_URL`) remains the safe default until the two-machine pull works. That is intentional: Hub tags are already pullable. Custom envs are not, until you publish them.

# Where else this pattern lives

**Container platforms.** Docker Hub, GitHub Container Registry, Google Artifact Registry, Amazon ECR, Azure ACR, Harbor, Quay. Kubernetes almost always pulls from one of these. Serious clusters pin `@sha256` in the Pod spec (`imagePullPolicy: IfNotPresent` plus a digest) so a retagged `:latest` cannot change a Deployment under you.

**CI/CD.** GitHub Actions, GitLab CI, and Buildkite typically `docker build` on a builder, `docker push`, then deploy by digest. The runner that builds is not the production replica that runs.

**HPC.** Traditional clusters use shared filesystems and environment modules, or Apptainer/Singularity image libraries (`oras://`, library.sylabs.io, or a local SIF archive). The invariant is the same: the compute node **obtains a copy of an immutable artifact**, it does not compile the world on the GPU node as a side effect of first use (except for uncached Singularity pulls, which operators still treat as a distribution problem).

**Nix and Guix.** The Nix store is a CAS (`/nix/store/<hash>-name`). Substituters are remote stores. That is the purest form of "build once, copy by hash." Docker is a coarser, layered cousin.

**Bazel, Pants, Buck.** Remote cache: action key in, content-addressed outputs out. Build farm machines are not the machines that serve the binary, necessarily.

**Language package registries.** npm, PyPI, Maven, crates.io. Weaker cousins: many still use mutable tags (`latest`, floating versions). Lockfiles (and our `pip freeze` lockfile on a ready env) exist because the registry name was not enough.

**Git.** Objects by SHA, remotes as distribution. Already used as the analogy above.

**Linux distros.** apt/dnf repositories plus checksums in `Release` / `repomd`. The package is not "whatever is on the buildd's disk"; it is published.

If you remember one industry sentence: **compute nodes consume immutable artifacts from a store; they do not invent them locally and then hope the next node has the same disk.**

# Trade-offs

What you gain: relocatable jobs, GPU time back, a path to Hub-independent catalog images, a real meaning for "environment ready."

What you pay: another daemon and volume (layers are large; budget hundreds of GB), `insecure-registries` or TLS on every PC, a dedicated builder, and the operational fact that a Mac laptop is a bad registry host.

What this still is not: Harbor (scanning, RBAC, replication), digest-pinned catalog seed by default, registry credentials tied to node tokens, or splitting CPU `docker build` from GPU CUDA tests.

Wrong shortcuts: `docker save`/`load` through MinIO, pinning jobs to the builder, rebuilding on every cache miss on a training GPU.

# Glossary

| Term | Meaning |
|------|--------|
| Registry | HTTP service that stores image manifests and layers (here: `registry:2`) |
| Tag | Mutable name (`:ready`). Do not treat as identity. |
| Digest | Immutable manifest hash. `name@sha256:…` |
| Local image id | `docker inspect .Id`. Not pullable by other daemons. |
| RepoDigest | Registry's name for a pushed image; what we store |
| Environment hash | SHA-256 of normalized deps + base + GPU class (recipe key) |
| Builder | Agent with `--role build` |
| Trainer | Agent with `--role train` (default) |
| Fencing token | Separate mechanism: which *attempt* may complete a job. Not a substitute for image distribution. |

# Related files in this repo

- `spec/modules/M09-environment-builder.md` — original contract (dedicated builder, push, digest)
- `images/README.md` — operator notes for catalog and insecure registries
- `tests/test_envbuild.py` — builder/trainer split and local-id rejection
- `README.md` — start commands
