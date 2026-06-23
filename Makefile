.PHONY: dev seed test down

VENV ?= .venv
PYTHON ?= $(shell command -v python3 || command -v python)

# Locate the venv Python; fall back to system python3 if no venv exists.
ifneq (,$(wildcard $(VENV)/bin/python))
  PY = $(VENV)/bin/python
else
  PY = $(PYTHON)
endif

## dev — zero-config local run (SQLite, no secrets needed).
##   Prefers docker if available; falls back to local uvicorn + venv.
dev:
	@if command -v docker > /dev/null 2>&1; then \
		docker compose up --build -d; \
		echo ""; \
		echo "Waiting for API to be healthy..."; \
		until curl -fsS http://localhost:8200/api/healthz > /dev/null 2>&1; do \
			sleep 2; \
		done; \
		echo ""; \
		echo "LoopSkill is running at http://localhost:8200"; \
		echo "Dev API key: rec_dev_wiserecipes_local_testing_key"; \
		echo ""; \
	else \
		$(MAKE) _local_run; \
	fi

_local_run:
	@test -d $(VENV) || python3 -m venv $(VENV)
	@$(VENV)/bin/pip install -q -r requirements.txt
	@WR_DATABASE_URL=sqlite:///./loopskill.db \
	 WR_COOKIES_SECURE=false \
	 $(VENV)/bin/python scripts/bootstrap.py
	@WR_DATABASE_URL=sqlite:///./loopskill.db \
	 WR_COOKIES_SECURE=false \
	 $(VENV)/bin/uvicorn app.main:app --host 0.0.0.0 --port 8200

## seed — (re)run the starter catalog seed against the local database.
seed:
	@WR_DATABASE_URL=$${WR_DATABASE_URL:-sqlite:///./loopskill.db} \
	 WR_COOKIES_SECURE=$${WR_COOKIES_SECURE:-false} \
	 $(PY) scripts/bootstrap.py

## test — run the full test suite (backgrounded; prints summary when done).
test:
	$(PY) -m pytest -q -p no:cacheprovider -n 8 --dist loadfile

## down — stop and remove the docker compose stack.
down:
	docker compose down
