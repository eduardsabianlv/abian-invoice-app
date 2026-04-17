"""
Azure Table Storage service helper.

If AZURE_TABLES_CONNECTION_STRING (or non-empty AzureWebJobsStorage) is set,
returns a TableServiceClient. Otherwise returns None, and each store falls
back to its JSON file.

Works with:
  - Azurite emulator: AzureWebJobsStorage="UseDevelopmentStorage=true"
  - Real Azure: full DefaultEndpointsProtocol=... connection string
  - Not set / empty: returns None, file fallback kicks in
"""

import os
import logging

logger = logging.getLogger(__name__)

_service = None
_checked = False


def _conn_str():
    conn = os.environ.get("AZURE_TABLES_CONNECTION_STRING") or os.environ.get("AzureWebJobsStorage")
    return conn or None


def get_service():
    """Return a cached TableServiceClient or None if Azure isn't configured."""
    global _service, _checked
    if _checked:
        return _service
    _checked = True
    conn = _conn_str()
    if not conn:
        return None
    try:
        from azure.data.tables import TableServiceClient
        _service = TableServiceClient.from_connection_string(conn)
        logger.info("Using Azure Table Storage for persistence")
        return _service
    except Exception as e:
        logger.warning(f"Azure Tables unavailable ({e}); using JSON file fallback")
        _service = None
        return None


def get_table(name: str):
    """Return a TableClient (auto-creates the table). None if Azure isn't configured."""
    svc = get_service()
    if svc is None:
        return None
    try:
        svc.create_table_if_not_exists(table_name=name)
    except Exception as e:
        logger.debug(f"create_table {name}: {e}")
    return svc.get_table_client(name)


def using_azure() -> bool:
    return get_service() is not None


def reset_cache() -> None:
    """For tests / after connection-string changes."""
    global _service, _checked
    _service = None
    _checked = False
