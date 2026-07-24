"""Test package bootstrap.

Make the suite hermetic: never load the developer `.env` (which points at the
live production Supabase pooler and sets PERSISTENCE_REQUIRED=true). Each test
module sets its own DATABASE_URL (in-memory SQLite); without this, create_app()
would load .env and the persistence assertion would reject SQLite. Set before
any test module imports `app`.
"""

import os

os.environ["SKIP_DOTENV"] = "true"
os.environ.setdefault("PERSISTENCE_REQUIRED", "false")
# The suite posts to routes directly without rendering a token, so disable CSRF
# enforcement here (production leaves WTF_CSRF_ENABLED at its true default). A
# dedicated test re-enables it to prove enforcement.
os.environ.setdefault("WTF_CSRF_ENABLED", "false")
