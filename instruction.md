docker pull pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime
# confirm GPU in a container:
docker run --rm --gpus all pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"


export LAB_IMAGE=pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime
LAB_API=http://192.168.68.118:8080 bash ~/distributetr9_node/run.sh



sudo apt-get update
sudo apt-get install -y --no-install-recommends ca-certificates curl gnupg2
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -sL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
sudo nvidia-ctk config --set nvidia-container-cli.no-cgroups --in-place
sudo service docker restart


powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\lab\supervisor\install.ps1 -Distro Ubuntu -Api http://192.168.68.118:8080 -AgentDir home/sakib/distributetr9_node


curl -sS -X POST http://10.10.126.226:8080/agent/register \
  -H "Content-Type: application/json" \
  --data-binary '{"hostname":"test","agent_version":"0.6.0","gpu_model":"none","vram_total_mb":0,"vram_allocatable_mb":0,"driver_version":"none","compute_capability":"0.0","compatibility_class":"cpu","cuda_preflight_ok":true,"cpu_cores":1,"ram_mb":1024,"disk_free_bytes":0,"windows_version":"x","wsl_version":"x","role":"train"}'