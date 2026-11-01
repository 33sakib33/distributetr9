# Build from the repo root so the agent bundle can ship with the control plane:
#   docker build -f control/Dockerfile .
FROM python:3.12-slim

WORKDIR /app

COPY control/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY control/alembic.ini ./
COPY control/migrations ./migrations
COPY control/app ./app
# The agent bundle and the lab PC install package are served from these.
COPY agent /install/agent
COPY scripts/setup-lab-node.sh /install/scripts/setup-lab-node.sh
COPY supervisor /install/supervisor

ENV AGENT_DIR=/install/agent INSTALL_ROOT=/install
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
