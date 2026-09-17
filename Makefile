COMPOSE = docker compose -f docker-compose.local.yml
APP = app
DB = postgres

.PHONY: help start stop restart build rebuild logs logs-app logs-db status ps shell db-shell test regression release-gate backup install clean down reset

help:
	@echo "VGMU Buffet"
	@echo ""
	@echo "make start         - Start project"
	@echo "make stop          - Stop project"
	@echo "make restart       - Restart project"
	@echo "make build         - Build Docker images"
	@echo "make rebuild       - Rebuild and start project"
	@echo "make logs          - Show all logs"
	@echo "make logs-app      - Show application logs"
	@echo "make logs-db       - Show PostgreSQL logs"
	@echo "make status        - Show container status"
	@echo "make shell         - Open shell inside application"
	@echo "make db-shell      - Open PostgreSQL console"
	@echo "make test          - Run regression tests"
	@echo "make regression    - Run regression tests"
	@echo "make release-gate  - Run release gate"
	@echo "make backup        - Create PostgreSQL backup"
	@echo "make install       - Install/update Python dependencies"
	@echo "make clean         - Stop project and remove containers"
	@echo "make reset         - DELETE database volume and recreate project"

start:
	$(COMPOSE) up -d

stop:
	$(COMPOSE) stop

restart:
	$(COMPOSE) restart

build:
	$(COMPOSE) build

rebuild:
	$(COMPOSE) up -d --build

logs:
	$(COMPOSE) logs -f

logs-app:
	$(COMPOSE) logs -f $(APP)

logs-db:
	$(COMPOSE) logs -f $(DB)

status:
	$(COMPOSE) ps

ps:
	$(COMPOSE) ps

shell:
	$(COMPOSE) exec $(APP) sh

db-shell:
	$(COMPOSE) exec $(DB) sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

test:
	$(COMPOSE) exec $(APP) python tests/run_regression.py

regression:
	$(COMPOSE) exec $(APP) python tests/run_regression.py

release-gate:
	$(COMPOSE) exec $(APP) python tests/run_release_gate.py

backup:
	@mkdir -p backups
	$(COMPOSE) exec -T $(DB) sh -c 'pg_dump -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"' > backups/postgres_manual_$$(date +%Y%m%d_%H%M%S).sql
	@echo "Backup created in backups/"

install:
	$(COMPOSE) exec $(APP) python -m pip install --upgrade -r requirements.txt

clean:
	$(COMPOSE) down

down:
	$(COMPOSE) down

reset:
	$(COMPOSE) down -v
	$(COMPOSE) up -d --build