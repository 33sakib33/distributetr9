
University Lab ML Job Platform

Implementation spec (modular, step-by-step): [spec/README.md](spec/README.md)
Option A: WSL2 with Docker Engine
User features
University login and user/project accounts
Upload a Python project, script, or notebook
Select the entrypoint and command-line arguments
Submit requirements.txt, environment.yml, or select a maintained environment
Request minimum GPU VRAM, GPU class, CPU, RAM, disk, and maximum runtime
View queued, building, running, uploading, completed, failed, cancelling, and cancelled states
Stream stdout and stderr
Cancel running or queued jobs
Download logs, output files, metrics, checkpoints, and executed notebooks
Retry a failed job from its latest checkpoint
View understandable queue explanations, such as “waiting for a 12 GB GPU”
Per-user and per-project concurrency limits
Job history and resource-usage reports
Execution features
WSL2 as the Linux compute environment
Docker Engine or containerd installed directly inside WSL
No Docker Desktop requirement
One container per job attempt
One training job per physical consumer GPU
Read-only container image
Read-only input and dataset mounts
Writable attempt-specific output, cache, and temporary directories
CPU, RAM, process-count, and runtime limits
Graceful termination followed by forced termination
Entire container removed after completion
No Docker socket or agent credentials available inside jobs
Conda permitted inside images when required
Environment features
Catalog of maintained PyTorch and TensorFlow base images
Dedicated environment-build queue
Build image once and reuse it by digest
Campus-local container registry
Exact dependency lockfiles
Compatibility-specific images for CUDA, PyTorch, Python, and GPU architecture
Local image cache on each PC
Image cache quotas and automatic garbage collection
Build logs shown separately from training logs
CUDA preflight test before an image is accepted
Scheduling and reliability features
Pull-based agent with long-polling
Node registration and capability inventory
Job-attempt leases with expiration
Lease renewal while jobs run
Attempt fencing token to reject stale completion reports
unknown state for disconnected nodes
Configurable disconnect grace period
Agent self-termination after lease loss
Idempotent claim, cancel, renewal, and completion operations
Node drain and quarantine controls
Automatic retry policy with maximum attempts
Attempt-specific outputs to prevent competing attempts from overwriting each other
Checkpoint upload during long jobs
Operational features
Dedicated Windows service account
Windows supervisor that starts and monitors WSL
GPU and Windows health collection from the host
WSL, Windows, driver, agent, and image-version inventory
Windows and Linux disk-space monitoring
Node disk high-watermark protection
Staged Windows, WSL, and driver updates
Local dataset cache
Central object storage for submissions, checkpoints, and results
Alerts for offline nodes, disk pressure, lease loss, repeated failures, and queue age
Recommended component layout
Portal frontend
    │
    ▼
FastAPI control API
    ├── Scheduler and lease service
    ├── Environment build service
    ├── Log ingestion
    └── Artifact service
           │
           ├── PostgreSQL
           ├── Object storage
           └── Container registry — Docker version only

Windows supervisor on every PC
    │
    ▼
WSL agent
    ├── Docker container executor
    └── or systemd/conda executor
For a new project, use PostgreSQL for users, jobs, attempts, nodes, leases, environment builds, quotas, and artifact metadata. Store uploaded projects, logs, checkpoints, and outputs in S3-compatible object storage such as MinIO.
Do not store large logs or artifacts inside the database.
Minimum data model
nodes
Stable node ID
Current boot/session ID
Hostname
GPU model
Total and allocatable VRAM
Compute capability
Driver version
Windows version
WSL version
Agent version
CPU and RAM capacity
Disk availability
Last server-observed heartbeat
Scheduling eligibility
Drain or quarantine reason
jobs
Job ID
User and project
Submission artifact
Entrypoint and arguments
Environment specification
Resource requirements
Priority
Retry policy
Desired state
Current accepted attempt
Creation and completion timestamps
job_attempts
Attempt ID and attempt number
Job ID and node ID
Fencing token
Lease expiration
Observed state
Exit code
Failure category
Start and completion timestamps
Output namespace
Last log sequence
Resource usage
environment_builds
Environment hash
Build state
Base image or base environment
Lockfile
CUDA and GPU compatibility
Image digest or environment artifact checksum
Build logs
artifacts
Job and attempt ID
Type: input, output, checkpoint, log, or executed notebook
Object-storage key
Size and checksum
Creation timestamp
API surface
Portal API
POST /jobs
GET /jobs/{job_id}
POST /jobs/{job_id}/cancel
POST /jobs/{job_id}/retry
GET /jobs/{job_id}/logs
GET /jobs/{job_id}/artifacts
GET /nodes for administrators
POST /nodes/{node_id}/drain
POST /nodes/{node_id}/quarantine
Use Server-Sent Events or WebSockets for the live log view.
Agent API
POST /agent/register
POST /agent/heartbeat
POST /agent/claim
POST /attempts/{id}/acknowledge
POST /attempts/{id}/renew
POST /attempts/{id}/events
POST /attempts/{id}/logs
POST /attempts/{id}/complete
POST /attempts/{id}/cleanup-complete
Every mutating request should be safe to repeat after a timeout.
Step-by-step development plan
Step 1: Restrict the first release
Start with:
Python scripts only
One GPU per job
One job per PC
Single-node training only
Maintained PyTorch and TensorFlow environments
One input archive and one output directory
Maximum runtime
Cancellation
Logs and artifact download
Add notebook execution, arbitrary environments, distributed training, and checkpoint resumption later.
Step 2: Inventory the lab
Create an inventory tool that records:
GPU model and VRAM
CUDA compute capability
Windows and NVIDIA driver versions
CPU and RAM
Physical disk availability
WSL and kernel versions
A small PyTorch CUDA test result
Group PCs into compatibility classes. Any unsupported or unreliable GPU class should be quarantined before scheduler development begins.
Step 3: Define the state machine
Write down every allowed transition before writing the portal:
submitted
→ validating
→ building
→ queued
→ leased
→ starting
→ running
→ uploading
→ completed

Any active state
→ cancelling
→ cancelled

leased/running
→ unknown
→ failed or retrying
Specify what happens when:
The agent disappears
The API disappears
A lease expires
Cancellation and completion happen simultaneously
Completion is reported twice
A node restarts with a leftover process
The completion response is lost
Implement these rules in one scheduler service.
Step 4: Build a fake-agent test harness
Before using real PCs, simulate approximately 100 agents.
The simulator should:
Register different GPU types
Claim jobs concurrently
Renew leases
Disconnect randomly
Complete jobs twice
Lose API responses
Cancel while completing
Automated tests must prove:
A job has at most one accepted result
A node receives at most one active GPU job
Stale attempts cannot finalize a job
Cancellation is eventually enforced
Expired jobs are retried according to policy
Step 5: Build the real Windows and WSL agent
Provision a dedicated Windows service account and import the same managed WSL distribution on every machine.
The Windows supervisor should:
Start after boot, whether anyone is logged in or not.
Launch the agent as a foreground WSL process.
Restart it after failure.
Report host health and reboot status.
Detect whether the Windows desktop is already heavily using the GPU.
The WSL agent should:
Register capabilities.
Run an end-to-end CUDA health check.
Long-poll for work.
Stage the submission and environment.
Start the executor.
Renew the lease.
Capture logs.
Handle cancellation.
Upload outputs.
Report completion using the fencing token.
Clean the attempt directory.
Reconcile leftover work after restart.
Initially, execute only one fixed administrator-maintained test environment.
Step 6A: Implement the Docker executor
Create maintained base images such as:
PyTorch stable with a supported CUDA runtime
TensorFlow stable
PyTorch with common training tools
A development image with compilers for extension builds
For each attempt:
Pull the image by immutable digest.
Create attempt-specific input, output, cache, and temporary directories.
Mount input and datasets read-only.
Mount output and caches read-write.
Start the container with GPU access.
Apply CPU, RAM, PID, and runtime limits.
Capture stdout and stderr.
On cancellation, send a graceful signal.
Force-kill after the grace period.
Upload outputs and remove the container.
Next, add the environment builder:
Normalize the submitted dependency specification.
Combine it with the base-image digest and compatibility class.
Calculate an environment hash.
Return an existing image when the hash is cached.
Otherwise build and test the image.
Push it to the local registry.
Queue the job only after the image passes its CUDA/import test.
Step 6B: Implement the non-Docker executor
Create a central environment builder:
Resolve the dependency specification once.
Produce an exact lockfile.
Build the environment.
Run import and CUDA tests.
Package it as an immutable archive.
Upload it with a checksum.
For each attempt, the agent should:
Download or locate the cached environment.
Verify its checksum.
Ensure it is root-owned and read-only.
Allocate an unprivileged job UID.
Create private home, scratch, cache, input, and output directories.
Set all writable-cache environment variables.
Launch the job as a systemd transient unit.
Apply memory, CPU, PID, runtime, filesystem, and user restrictions.
Capture logs outside the job’s writable area.
Kill the complete cgroup on cancellation or lease loss.
Upload outputs.
Remove the job user and writable directories.
Verify that the shared environment was not modified.
Do not reuse a writable conda environment between jobs.
Step 7: Add artifact and log handling
Store submissions and results centrally.
Implement:
Sequenced, compressed log chunks
Log rate and size limits
Periodic checkpoint upload
Output-size limits
Checksums for every uploaded artifact
Attempt-specific artifact paths
Final result promotion only after the fencing token is verified
A node should be disposable without losing already-uploaded checkpoints.
Step 8: Build the portal
After scheduling and execution work through APIs, build the Next.js interface.
First-release pages:
Submit job
Job list
Job detail with live logs
Artifact download
Cancel and retry controls
Environment build status
Administrator node list
Drain and quarantine controls
Display separate times for environment building, queue waiting, execution, and artifact uploading.
Step 9: Add scheduling policy
Implement matching on:
GPU model or compatibility class
Minimum allocatable VRAM
Compute capability
CPU and RAM
Required environment compatibility
Node health and disk pressure
Then add:
Per-user concurrency
Per-project concurrency
Queue aging
Maximum job runtime
Retry limits
Reservation of high-VRAM GPUs for jobs that need them
Clear pending reasons shown in the portal
Step 10: Add operations and updates
Create administrator procedures for:
Draining a node
Replacing its WSL distribution
Updating the agent
Updating NVIDIA drivers
Updating WSL
Cleaning image or environment caches
Compacting the WSL VHD
Rotating node credentials
Quarantining failing hardware
Restoring the control database and object store
Roll updates through a small canary group before touching all PCs.
Step 11: Failure-test five machines
Test the following deliberately:
Kill the agent during training
Kill WSL during training
Reboot Windows
Disconnect Tailscale
Stop the scheduler
Lose an assignment or completion response
Fill the Linux VHD
Fill the Windows volume
Cancel during startup
Cancel during artifact upload
Start two claim requests from the same node
Run a Windows GPU application during training
Submit a broken environment
Submit output exceeding its quota
Do not expand until every outcome is deterministic and visible in the portal.
Step 12: Roll out gradually
Use rollout stages:
One development PC
Five PCs covering different GPU classes
Fifteen PCs with real volunteer users
Thirty PCs
All sixty PCs
At each stage, measure:
Job success rate
Environment build time
Queue time
Lease-loss frequency
Node offline frequency
Cold-cache download load
Disk growth
Cancellation latency
Checkpoint recovery success
Failures by GPU and driver class
Recommended implementation order
Build in this order:
State machine and database
Fake agents and concurrency tests
One real WSL agent
Fixed-environment GPU execution
Lease renewal and cancellation
Logs and artifacts
Docker or hardened conda executor
Environment builder
Portal
Fair scheduling and quotas
Operations tooling
Gradual rollout
The portal should not be the first component. The hardest and highest-risk part is the scheduler-to-agent failure protocol.

