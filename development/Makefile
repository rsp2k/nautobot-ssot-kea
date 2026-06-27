COMPOSE := docker compose

.PHONY: wheel build up down logs restart migrate nbshell e2e

wheel:  ## Rebuild the nautobot-dhcp-models wheel from the sibling repo.
	cd ../../nautobot-app-dhcp-models && rm -rf dist && uv build --wheel
	cp ../../nautobot-app-dhcp-models/dist/*.whl ./wheels/

build:
	$(COMPOSE) build

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=100 nautobot

restart:
	$(COMPOSE) restart nautobot worker

migrate:
	$(COMPOSE) run --rm nautobot nautobot-server migrate

nbshell:
	$(COMPOSE) exec nautobot nautobot-server shell_plus

e2e:  ## Run the fixture config through both adapters and verify (no UI needed).
	$(COMPOSE) run --rm -T nautobot nautobot-server shell < scripts/e2e_sync.py
