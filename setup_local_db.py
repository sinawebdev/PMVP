"""Run this once before starting the app for the first time on PostgreSQL."""

import os
import sys
from urllib.parse import urlparse

from dotenv import load_dotenv
import psycopg2


def postgres_connection_settings(database_url):
    if not database_url:
        return "payrolla", {"dbname": "postgres"}

    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)

    parsed_url = urlparse(database_url)
    db_name = parsed_url.path.lstrip("/") or "payrolla"
    settings = {
        "dbname": "postgres",
        "user": parsed_url.username,
        "password": parsed_url.password,
        "host": parsed_url.hostname,
        "port": parsed_url.port,
    }
    return db_name, {key: value for key, value in settings.items() if value}


def ensure_database_exists(connect, db_name="payrolla", **connect_kwargs):
    if not db_name.replace("_", "").isalnum():
        raise ValueError("Database name may only contain letters, numbers, and underscores.")

    connection_settings = {"dbname": "postgres", **connect_kwargs}
    connection = connect(**connection_settings)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (db_name,),
            )
            if cursor.fetchone():
                print(f"Database '{db_name}' already exists.")
                return

            cursor.execute(f"CREATE DATABASE {db_name}")
            print(f"Database '{db_name}' created successfully.")
    finally:
        connection.close()


def main():
    load_dotenv()
    try:
        db_name, connection_settings = postgres_connection_settings(os.getenv("DATABASE_URL"))
        ensure_database_exists(
            psycopg2.connect,
            db_name=db_name,
            **connection_settings,
        )
        print("Local PostgreSQL database setup complete.")
    except Exception as exc:
        print(f"Error setting up local PostgreSQL database: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
