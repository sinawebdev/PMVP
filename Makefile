.PHONY: up down build logs shell db-shell migrate

up:
	docker compose up -d

down:
	docker compose down

build:
	docker compose build

logs:
	docker compose logs -f web

shell:
	docker compose exec web bash

db-shell:
	docker compose exec db psql -U chrisnat -d chrisnat

# This project uses ensure_phase2_schema() + db.create_all() for additive
# schema changes (no Flask-Migrate migrations folder). `make migrate` re-runs
# that idempotent initialisation inside the container.
migrate:
	docker compose exec web flask init-db
