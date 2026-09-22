"""Database stores and migrations package.

MAINTENANCE NOTES:
    - Phase 0 keeps the store modules as thin placeholders: SQLite connect
      path is configurable, InfluxDB client reads its config from env. Real
      schemas/upsert/analytics are implemented in Phase 3 (WORKPLAN §7).
"""
