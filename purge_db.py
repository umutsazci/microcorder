"""Drop and recreate every table in the configured DATABASE_URL.

DESTRUCTIVE — wipes every user, webhook event, subscription, task. Pass --yes
to confirm. Reads DATABASE_URL from `.env` (or current environment).

Usage:
    python purge_db.py --yes
"""
import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

# Import models AFTER load_dotenv so engine binds to the right URL,
# and BEFORE drop_all/create_all so all tables are registered on Base.
from app.database import Base, engine
from app import models  # noqa: F401  (registers User, Subscription, Task, WebhookEvent)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yes", action="store_true",
        help="Required confirmation flag — without it the script refuses to run.",
    )
    args = parser.parse_args()

    target = os.environ.get("DATABASE_URL", "(unset, falling back to sqlite app.db)")
    if not args.yes:
        print(f"Refusing to purge {target!r}.\nPass --yes to confirm.", file=sys.stderr)
        return 2

    print(f"Dropping every table in:\n  {target}")
    Base.metadata.drop_all(bind=engine)
    print("Recreating empty schema ...")
    Base.metadata.create_all(bind=engine)
    print("Database is empty.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
