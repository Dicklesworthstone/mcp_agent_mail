.PHONY: serve-http migrate lint typecheck guard-install guard-uninstall claims

PY=uv run
CLI=$(PY) python -m mcp_agent_mail.cli

serve-http:
	$(CLI) serve-http



migrate:
	$(CLI) migrate

lint:
	$(PY) ruff check --fix --unsafe-fixes

typecheck:
	uvx ty check

guard-install:
	$(CLI) guard install $(PROJECT) $(REPO)

guard-uninstall:
	$(CLI) guard uninstall $(REPO)

claims:
	$(CLI) claims list --active-only $(ACTIVE) $(PROJECT)


