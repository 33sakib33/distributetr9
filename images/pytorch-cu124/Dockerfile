# Pin this tag, then record the immutable digest in environments.image_digest.
#   docker pull pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime
#   docker inspect --format='{{index .RepoDigests 0}}' pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime
FROM pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime
ENV PYTHONUNBUFFERED=1
WORKDIR /input
