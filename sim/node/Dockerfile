# A simulated lab PC: its own Docker daemon (Docker-in-Docker) plus the real
# agent, installed in the same release layout as scripts/setup-lab-node.sh.
FROM docker:27.3.1-dind
RUN apk add --no-cache python3 bash curl coreutils procps
COPY agent /src/agent
COPY sim/node/entry.sh /entry.sh
RUN chmod +x /entry.sh
ENTRYPOINT ["/entry.sh"]
