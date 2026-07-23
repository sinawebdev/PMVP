web: flask db upgrade && gunicorn run:app --bind 0.0.0.0:$PORT
# Dedicated distribution worker (Phase 4). Enable this process type on the host
# (Railway: turn on the `worker` process; Heroku-style: `scale worker=1`) and set
# DISTRIBUTION_WORKER_INLINE=false on `web` so only this process sends. It handles
# SIGTERM for a graceful shutdown, so a deploy restart never interrupts a send.
worker: flask --app run:app distribution-worker
