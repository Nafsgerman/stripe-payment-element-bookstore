"""Typed application config, loaded once from the environment.

Secrets live only in `.env` (never committed). `.env.example` documents the
keys with placeholder values. Importing this module reads the environment a
single time and exposes an immutable `config` object.
"""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    stripe_secret_key: str
    stripe_publishable_key: str
    stripe_webhook_secret: str  # required only once the webhook route lands
    domain: str                 # public origin used to build the return_url
    database_path: str


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


def _load() -> Config:
    return Config(
        stripe_secret_key=_require("STRIPE_SECRET_KEY"),
        stripe_publishable_key=_require("STRIPE_PUBLISHABLE_KEY"),
        stripe_webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET", ""),
        domain=os.environ.get("DOMAIN", "http://localhost:4242"),
        database_path=os.environ.get("DATABASE_PATH", "orders.db"),
    )


config = _load()