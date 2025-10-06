install-dev:
	uv sync

test-unit:
	uv run pytest tests/unit/ -v

test-integration:
	uv run pytest tests/integration/ -v

test-http:
	uv run pytest tests/integration/test_http_transport.py -v

test-all:
	uv run pytest tests/ -v

all: install-dev test-all 