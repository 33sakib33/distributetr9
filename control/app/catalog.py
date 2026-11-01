"""Administrator-maintained catalog images (v1). Tags until a digest is recorded."""

PYTORCH_IMAGE = "pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime"
TENSORFLOW_IMAGE = "tensorflow/tensorflow:2.17.0-gpu"
# Plain Python for CPU-only jobs (and the smoke test); small and multi-arch.
PYTHON_CPU_IMAGE = "python:3.12.7-slim"
# TensorFlow's catalog row held this image until a real TF image was pinned.
TENSORFLOW_PLACEHOLDER_IMAGE = PYTHON_CPU_IMAGE
