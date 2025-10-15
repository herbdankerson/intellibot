# 0001 – Centralised ParadeDB Access

## Context

Historically modules opened ParadeDB connections ad hoc (`psycopg.connect`, SQLAlchemy engines, raw DSNs). This produced divergent hostnames (`localhost` vs `paradedb`), duplicate pools, and inconsistent behaviour between host machines and containers.

## Decision

Introduce `src/my_agentic_chatbot/storage/connection.py` as the single import for ParadeDB access. It resolves DSNs from environment settings, coerces SQLAlchemy URLs to the `psycopg` driver, exposes context-managed psycopg connections, and caches SQLAlchemy engines. All runtime components, Prefect tasks, and scripts must import from this module.

## Consequences

- Container and host environments stay aligned (`paradedb` DNS works everywhere).
- Connection pooling is centralised, avoiding idle connections from multiple engine factories.
- Future DSN tweaks (TLS, failover) happen in one place.
- Any direct `psycopg.connect` calls outside the helper are now considered regressions and flagged during reviews.
