"""
Invoice Mapper
Core business logic: transforms PAX8 billing data into Moneo invoice lines.

SKU naming convention:
  - Monthly billing: PAX8_SKU + "-M"
  - Yearly billing:  PAX8_SKU + "-Y"

Profit calculation:
  - PAX8 costs are excl. VAT
  - Moneo amounts include 21% VAT
  - profit = moneo_amount_excl_vat - pax8_cost
  - moneo_amount_excl_vat = moneo_total / 1.21
"""

from typing import Optional
from collections import defaultdict
import logging
from calendar import monthrange
from datetime import date

logger = logging.getLogger(__name__)

VAT_RATE = 0.21  # Latvian standard VAT rate


# ------------------------------------------------------------------
# SKU helpers
# ------------------------------------------------------------------

def pax8_sku_to_moneo_itemcode(sku: str, billing_term: str = "Monthly") -> str:
    """
    Convert a PAX8 SKU to a Moneo item code.
    Appends -M for monthly, -Y for yearly/annual billing terms.
    """
    if not sku:
        return "UNKNOWN"
    term_lower = billing_term.lower() if billing_term else ""
    if "annual" in term_lower or "year" in term_lower:
        suffix = "-Y"
    elif "monthly" in term_lower or "month" in term_lower:
        suffix = "-M"
    else:
        # Default to monthly for usage-based items
        suffix = "-M"
    return f"{sku}{suffix}"


def moneo_itemcode_to_pax8_sku(itemcode: str) -> str:
    """Strip the -M / -Y suffix to recover the original PAX8 SKU."""
    for suffix in ("-M", "-Y"):
        if itemcode.endswith(suffix):
            return itemcode[: -len(suffix)]
    return itemcode


# ------------------------------------------------------------------
# Price helpers
# ------------------------------------------------------------------

def excl_vat(amount_incl_vat: float) -> float:
    """Convert a VAT-inclusive amount to excl. VAT (Latvian 21%)."""
    return round(amount_incl_vat / (1 + VAT_RATE), 4)


def incl_vat(amount_excl_vat: float) -> float:
    """Convert an excl-VAT amount to incl. VAT."""
    return round(amount_excl_vat * (1 + VAT_RATE), 4)


def calculate_profit(pax8_cost: float, moneo_total_incl_vat: float) -> dict:
    """
    Calculate profit from PAX8 cost and Moneo invoice total.

    PAX8 costs are excl. VAT.
    Moneo totals include 21% VAT.

    Returns dict with:
      - moneo_excl_vat: Moneo amount without VAT
      - pax8_cost: cost from PAX8
      - profit: absolute profit (EUR)
      - profit_pct: profit as percentage of PAX8 cost
    """
    moneo_excl = excl_vat(moneo_total_incl_vat)
    profit = round(moneo_excl - pax8_cost, 4)
    profit_pct = round((profit / pax8_cost * 100), 2) if pax8_cost > 0 else 0.0
    return {
        "moneo_excl_vat": moneo_excl,
        "pax8_cost": pax8_cost,
        "profit": profit,
        "profit_pct": profit_pct,
    }


# ------------------------------------------------------------------
# Core mapper
# ------------------------------------------------------------------

def map_pax8_to_moneo_lines(pax8_items: list) -> list:
    """
    Convert a list of PAX8 billing items (from pax8_client.get_billing_for_month)
    into Moneo invoice line dicts.

    Each Moneo line has:
      itemcode  - PAX8 SKU + suffix (e.g. "CFQ7TTC0LH18:0001-M")
      itemname  - product name
      quant     - quantity
      price     - unit price excl. VAT (same as PAX8 cost — reseller adds markup separately)
      rowsum    - quant * price

    Note: The price here is the PAX8 unit cost. The reseller typically adjusts
    prices manually in Moneo or via a price list. This mapper maps the structure;
    the actual sell price can be overridden.
    """
    moneo_lines = []

    for item in pax8_items:
        sku = item.get("sku", "UNKNOWN")
        billing_term = item.get("billing_term", "Monthly")
        itemcode = pax8_sku_to_moneo_itemcode(sku, billing_term)

        quant = float(item.get("quantity", 1))
        unit_cost = float(item.get("unit_cost", 0))
        total_cost = float(item.get("total_cost", quant * unit_cost))

        # Avoid zero-quantity lines
        if quant <= 0:
            continue

        moneo_lines.append({
            "itemcode": itemcode,
            "itemname": item.get("name", sku),
            "quant": quant,
            "price": round(unit_cost, 4),
            "rowsum": round(total_cost, 2),
            # Metadata (not sent to Moneo, used for UI)
            "_pax8_sku": sku,
            "_billing_term": billing_term,
            "_type": item.get("type", "subscription"),
        })

    return moneo_lines


def merge_duplicate_lines(lines: list) -> list:
    """
    Merge Moneo invoice lines with the same itemcode.
    Useful when the same SKU appears multiple times (e.g. usage + subscription).
    """
    merged: dict = {}
    for line in lines:
        key = line["itemcode"]
        if key in merged:
            existing = merged[key]
            existing["quant"] += line["quant"]
            existing["rowsum"] = round(existing["rowsum"] + line["rowsum"], 2)
            # Recalculate unit price as weighted average
            if existing["quant"] > 0:
                existing["price"] = round(existing["rowsum"] / existing["quant"], 4)
        else:
            merged[key] = dict(line)  # copy
    return list(merged.values())


# ------------------------------------------------------------------
# Grouping helpers
# ------------------------------------------------------------------

def group_by_month(pax8_data: list, date_field: str = "date") -> dict:
    """
    Group a list of PAX8 records by year-month (YYYY-MM).
    date_field: the key in each record that contains the date string.
    Returns: { "YYYY-MM": [records...] }
    """
    groups: dict = defaultdict(list)
    for record in pax8_data:
        raw_date = record.get(date_field, "")
        if raw_date and len(raw_date) >= 7:
            month_key = raw_date[:7]  # "YYYY-MM"
        else:
            month_key = "unknown"
        groups[month_key].append(record)
    return dict(groups)


def get_month_date_range(year: int, month: int) -> tuple:
    """Return (start_date, end_date) ISO strings for the given year/month."""
    last_day = monthrange(year, month)[1]
    start = f"{year}-{month:02d}-01"
    end = f"{year}-{month:02d}-{last_day:02d}"
    return start, end


# ------------------------------------------------------------------
# Split-subscription helper
# ------------------------------------------------------------------

def apply_split_subscriptions(
    pax8_items: list,
    split_config: dict,
    default_moneo_code: str,
) -> dict:
    """
    Some Azure subscriptions in PAX8 should be billed to a different Moneo customer.
    split_config: { "Azure-TL": "1014", ... }  — maps PAX8 subscription name/tag to Moneo code.

    Returns:
    {
      moneo_code: [pax8_items_for_that_customer, ...]
    }
    """
    result: dict = defaultdict(list)
    for item in pax8_items:
        sku = item.get("sku", "")
        name = item.get("name", "")
        matched = False
        for tag, moneo_code in split_config.items():
            if tag in sku or tag in name:
                result[moneo_code].append(item)
                matched = True
                break
        if not matched:
            result[default_moneo_code].append(item)
    return dict(result)


# ------------------------------------------------------------------
# Invoice comment builder
# ------------------------------------------------------------------

def build_invoice_comment(year: int, month: int, pax8_company_name: str) -> str:
    """Generate a default Moneo invoice comment for a PAX8 billing period."""
    month_names_lv = [
        "", "janvāris", "februāris", "marts", "aprīlis", "maijs", "jūnijs",
        "jūlijs", "augusts", "septembris", "oktobris", "novembris", "decembris"
    ]
    return (
        f"Microsoft pakalpojumi — {month_names_lv[month]} {year} "
        f"({pax8_company_name})"
    )
