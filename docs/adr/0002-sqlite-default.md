# Default database

`DATABASE_URL` defaults to a SQLite file so `uvicorn` starts without Docker. The SQLAlchemy models are the same ones Alembic creates for Postgres. Use the Compose Postgres service when you need concurrent writers or a shared server.
