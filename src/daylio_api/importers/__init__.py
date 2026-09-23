"""Importers package: unified CSV + .daylio parsing and normalization (D7, D8).

MAINTENANCE NOTES:
    - ``base.py``           : ``ImportResult`` (audit/import_jobs contract) and
      ``NormalizedEntry`` (canonical daily record) dataclasses + ``detect_format``.
    - ``csv_importer.py``   : stdlib CSV parser (``utf-8-sig`` BOM, D8.b).
    - ``daylio_importer.py``: ZIP -> ``backup.daylio`` (Base64) -> JSON (D7);
      month 0-based, customMoods, tags/tag_groups, scales, scaleValues,
      assets ignored (D11).
    - ``normalizer.py``     : mood 1-5 (D1), activities list (D8.d), scales
      value+label (D8.c), Scala salute from activities/tags (D2), daily
      dedup "vince la più recente" (D3).
"""
