.PHONY: dev test lint migrate sim sim-down sim-exam guide

PY := control/.venv/bin/python

dev:
	./scripts/dev.sh

test:
	docker compose up -d --wait postgres minio
	control/.venv/bin/pytest tests -q

lint:
	control/.venv/bin/ruff check control agent harness tests scripts

migrate:
	cd control && ../$(PY) -m alembic upgrade head

sim:
	docker compose -f docker-compose.yml -f sim/docker-compose.sim.yml --profile sim up -d --build --wait

sim-down:
	docker compose -f docker-compose.yml -f sim/docker-compose.sim.yml --profile sim rm -sf sim-control sim-pc-1 sim-pc-2 sim-pc-3 sim-pc-4 sim-builder

sim-exam:
	$(PY) -m sim.exam

guide:
	./docs/guide/build.sh
