.PHONY: test lint format

test:  ## Run the pure unit tests (no Django needed). dhcp-models src is on the
       ## path for the shared nautobot_dhcp_models.ssot.base import.
	PYTHONPATH=src:../nautobot-app-dhcp-models uv run --no-project --with 'diffsync>=2,<3' --with pytest python -m pytest

lint:
	uvx ruff check src tests
	uvx ruff format --check src tests

format:
	uvx ruff format src tests
