.PHONY: test lint docker-gpu up down smoke replay experiment

# Python tests always exist (schemas/tests); Go tests run once the gateway module exists.
test:
	uv run --locked pytest
	if [ -f gateway/go.mod ]; then cd gateway && go test ./...; fi

lint:
	uv run --locked pre-commit run --all-files

# Docker Engine + NVIDIA Container Toolkit inside WSL2 (P0.2).
docker-gpu:
	bash infra/install-docker-gpu.sh

up:
	@echo "not implemented yet: P1.0"; exit 1

down:
	@echo "not implemented yet: P1.0"; exit 1

smoke:
	@echo "not implemented yet: P1.5"; exit 1

replay:
	@echo "not implemented yet: P2.3"; exit 1

experiment:
	@echo "not implemented yet: P2.5"; exit 1
