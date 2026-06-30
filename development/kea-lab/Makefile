# 2-node Kea lab (kea-dhcp4 + Control Agent REST API on each), with HA between them.
# Control Agents: node1 -> http://127.0.0.1:8001, node2 -> http://127.0.0.1:8002
.PHONY: up down logs ps config-get status leases ha-status reseed

up:  ## Build + start both Kea nodes
	docker compose up -d --build
	@echo "node1 CA: http://127.0.0.1:8001   node2 CA: http://127.0.0.1:8002"

down:  ## Stop and remove the nodes
	docker compose down

reseed:  ## Restart (re-seeds each node from its read-only config)
	docker compose restart

logs:
	docker compose logs -f --tail=50

ps:
	docker compose ps

# --- Control Agent REST API helpers (run a command against a node's CA) ---
# Usage: make config-get NODE=8001
NODE ?= 8001

config-get:  ## Full running Dhcp4 config from a node's CA (NODE=8001|8002)
	@curl -s -X POST http://127.0.0.1:$(NODE)/ -H "Content-Type: application/json" \
	  -d '{"command":"config-get","service":["dhcp4"]}' | python3 -m json.tool | head -60

status:  ## status-get from a node's CA
	@curl -s -X POST http://127.0.0.1:$(NODE)/ -H "Content-Type: application/json" \
	  -d '{"command":"status-get","service":["dhcp4"]}' | python3 -m json.tool

leases:  ## All v4 leases from a node's CA (needs lease_cmds hook)
	@curl -s -X POST http://127.0.0.1:$(NODE)/ -H "Content-Type: application/json" \
	  -d '{"command":"lease4-get-all","service":["dhcp4"]}' | python3 -m json.tool | head -40

ha-status:  ## HA heartbeat/state from a node's CA
	@curl -s -X POST http://127.0.0.1:$(NODE)/ -H "Content-Type: application/json" \
	  -d '{"command":"ha-heartbeat","service":["dhcp4"]}' | python3 -m json.tool
