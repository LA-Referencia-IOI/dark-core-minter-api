"""Database command-line helpers."""

import argparse
import logging

from app.database.connection import run_migrations


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


def main() -> None:
    """Run database maintenance commands."""
    parser = argparse.ArgumentParser(description="dARK Core Minter database commands")
    parser.add_argument(
        "command",
        choices=["migrate"],
        help="database command to run",
    )
    args = parser.parse_args()

    if args.command == "migrate":
        run_migrations()


if __name__ == "__main__":
    main()
