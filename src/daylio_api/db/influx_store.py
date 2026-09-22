"""InfluxDB 2.x store placeholder.

=============================== MAINTENANCE NOTES ===============================
Module   : src/daylio_api/db/influx_store.py
Scope    : Fase 0 placeholder (Task 0.2/0.4). Wires the official
           ``influxdb-client`` using connection values resolved by
           ``config.load()``/env (org `tanadelgigante`, bucket `daylio`, D18).
           Real daily-point writes with full overwrite (D3/§5.4), Flux queries
           for the analytics endpoints and the SQLite->Influx resync job are
           implemented in Phase 3. Importing this module must never connect.
================================================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from influxdb_client import (
    InfluxDBClient,  # noqa: F401  (kept so the dependency is part of the public surface)
)

from daylio_api.logger import get_logger

log = get_logger("db.influx")


@dataclass
class InfluxConfig:
    """Connection parameters resolved from application config/env."""

    url: str
    token: str = ""
    org: str = "tanadelgigante"
    bucket: str = "daylio"
    measurement: str = "daylio_entry"


class InfluxStore:
    """InfluxDB client wrapper (not connected at import/construction time)."""

    def __init__(self, config: InfluxConfig | dict[str, Any] | None = None) -> None:
        self.config = config if isinstance(config, InfluxConfig) else InfluxConfig(**(config or {}))
        self._client: InfluxDBClient | None = None

    def client(self) -> InfluxDBClient:
        """Lazily create (and cache) the underlying InfluxDBClient."""
        if self._client is None:
            self._client = InfluxDBClient(
                url=self.config.url,
                token=self.config.token,
                org=self.config.org,
            )
            log.debug("InfluxDB client created for url=%s", self.config.url)
        return self._client

    def write(self, *args: Any, **kwargs: Any) -> Any:
        """Placeholder: real write_point/overwrite implemented in Phase 3."""
        raise NotImplementedError("InfluxStore.write() is implemented in Phase 3 (WORKPLAN §7)")

    def query(self, *args: Any, **kwargs: Any) -> Any:
        """Placeholder: real Flux analytics queries implemented in Phase 3."""
        raise NotImplementedError("InfluxStore.query() is implemented in Phase 3 (WORKPLAN §7)")

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
