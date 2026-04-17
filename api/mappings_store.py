"""
Company mappings — PAX8 company UUID → Moneo customer code, name, and
split-subscription rules.

Backend: Azure Table "companyMappings" if available, else api/company_mappings.json.
Schema:
  PartitionKey = "default"
  RowKey       = pax8 company UUID
  Fields: moneo_code, moneo_name, split_subscriptions (JSON string in Azure)
"""

import json
import os
import logging

import storage

logger = logging.getLogger(__name__)

MAPPINGS_FILE = os.path.join(os.path.dirname(__file__), "company_mappings.json")
TABLE_NAME = "companyMappings"
PARTITION = "default"


# ---------- file helpers --------------------------------------------------

def _load_file_raw() -> dict:
    try:
        with open(MAPPINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"company_mappings.json parse error: {e}")
        return {}


def _save_file(data: dict) -> None:
    with open(MAPPINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _strip_comments(data: dict) -> dict:
    return {k: v for k, v in data.items() if not k.startswith("_")}


# ---------- public API ----------------------------------------------------

def load_mappings() -> dict:
    """Return {pax8_id: {moneo_code, moneo_name, split_subscriptions}}."""
    table = storage.get_table(TABLE_NAME)
    if table is not None:
        result = {}
        try:
            for e in table.query_entities(f"PartitionKey eq '{PARTITION}'"):
                rk = e.get("RowKey")
                if not rk:
                    continue
                split = e.get("split_subscriptions")
                if isinstance(split, str):
                    try:
                        split = json.loads(split)
                    except Exception:
                        split = {}
                result[rk] = {
                    "moneo_code": e.get("moneo_code", ""),
                    "moneo_name": e.get("moneo_name", ""),
                    "split_subscriptions": split or {},
                }
        except Exception as ex:
            logger.error(f"Failed to query companyMappings: {ex}")
        return result
    return _strip_comments(_load_file_raw())


def upsert_mapping(pax8_id: str, mapping: dict) -> None:
    table = storage.get_table(TABLE_NAME)
    if table is not None:
        entity = {
            "PartitionKey": PARTITION,
            "RowKey": pax8_id,
            "moneo_code": mapping.get("moneo_code", ""),
            "moneo_name": mapping.get("moneo_name", ""),
            "split_subscriptions": json.dumps(
                mapping.get("split_subscriptions") or {}, ensure_ascii=False
            ),
        }
        table.upsert_entity(entity)
        return
    data = _load_file_raw()
    data[pax8_id] = mapping
    _save_file(data)


def delete_mapping(pax8_id: str) -> None:
    table = storage.get_table(TABLE_NAME)
    if table is not None:
        try:
            table.delete_entity(partition_key=PARTITION, row_key=pax8_id)
        except Exception:
            pass
        return
    data = _load_file_raw()
    data.pop(pax8_id, None)
    _save_file(data)


def upsert_many(patch: dict) -> dict:
    """Apply partial update; value=None removes the mapping. Returns refreshed mappings."""
    for pax8_id, mapping in patch.items():
        if mapping is None:
            delete_mapping(pax8_id)
        else:
            upsert_mapping(pax8_id, mapping)
    return load_mappings()
