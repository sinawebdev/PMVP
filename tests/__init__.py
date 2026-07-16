"""Test package bootstrap.

Make the suite hermetic: never load the developer `.env` (which points at the
live pmvp-v1 Supabase pooler and sets PERSISTENCE_REQUIRED=true). Each test
module sets its own DATABASE_URL (in-memory SQLite); without this, create_app()
would load .env and the persistence assertion would reject SQLite. Set before
any test module imports `app`.
"""

import os

os.environ["SKIP_DOTENV"] = "true"
os.environ.setdefault("PERSISTENCE_REQUIRED", "false")
