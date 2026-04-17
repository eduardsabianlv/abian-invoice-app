"""
Azure Functions v2 — PAX8 ↔ Moneo Bridge
All routes defined with @app.route decorators (Python v2 model).
"""

import json
import logging
import os
from datetime import date, datetime

import azure.functions as func

from pax8_client import PAX8Client, PAX8Error
from moneo_client import MoneoClient, MoneoError
from invoice_mapper import (
    map_pax8_to_moneo_lines,
    merge_duplicate_lines,
    calculate_profit,
    apply_split_subscriptions,
    build_invoice_comment,
    get_month_date_range,
    excl_vat,
)
import secrets_store
import manual_links_store
import mappings_store

logger = logging.getLogger(__name__)

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


# Company mappings are served by mappings_store (Azure Tables or JSON fallback).
load_mappings = mappings_store.load_mappings


# ------------------------------------------------------------------
# Response helpers
# ------------------------------------------------------------------

def json_response(data, status_code: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        body=json.dumps(data, ensure_ascii=False, default=str),
        status_code=status_code,
        mimetype="application/json",
    )


def error_response(message: str, status_code: int = 500) -> func.HttpResponse:
    return json_response({"error": message}, status_code)


# ------------------------------------------------------------------
# GET /api/companies
# ------------------------------------------------------------------

@app.route(route="companies", methods=["GET"])
def get_companies(req: func.HttpRequest) -> func.HttpResponse:
    """
    Returns all PAX8 companies merged with their Moneo customer mappings.
    """
    try:
        pax8 = PAX8Client()
        mappings = load_mappings()
        companies = pax8.get_all_companies()

        result = []
        for c in companies:
            cid = c.get("id", c.get("companyId", ""))
            mapping = mappings.get(cid, {})
            result.append({
                "pax8_id": cid,
                "pax8_name": c.get("name", c.get("companyName", "")),
                "pax8_status": c.get("status", ""),
                "moneo_code": mapping.get("moneo_code"),
                "moneo_name": mapping.get("moneo_name"),
                "has_mapping": bool(mapping.get("moneo_code")),
                "split_subscriptions": mapping.get("split_subscriptions", {}),
            })

        return json_response(result)
    except PAX8Error as e:
        logger.error(f"PAX8 error in get_companies: {e}")
        return error_response(str(e), e.status_code or 502)
    except Exception as e:
        logger.exception("Unexpected error in get_companies")
        return error_response(str(e))


# ------------------------------------------------------------------
# GET /api/billing?pax8_company_id=X&year=2026&month=4
# ------------------------------------------------------------------

@app.route(route="billing", methods=["GET"])
def get_billing(req: func.HttpRequest) -> func.HttpResponse:
    """
    Returns PAX8 billing line items for a company in a given month.
    """
    company_id = req.params.get("pax8_company_id")
    year = req.params.get("year")
    month = req.params.get("month")

    if not all([company_id, year, month]):
        return error_response("pax8_company_id, year and month are required", 400)

    try:
        year = int(year)
        month = int(month)
    except ValueError:
        return error_response("year and month must be integers", 400)

    try:
        pax8 = PAX8Client()
        billing_lines = pax8.get_billing_for_month(company_id, year, month)

        # Add Moneo item code mapping
        for line in billing_lines:
            from invoice_mapper import pax8_sku_to_moneo_itemcode
            line["moneo_itemcode"] = pax8_sku_to_moneo_itemcode(
                line.get("sku", ""), line.get("billing_term", "Monthly")
            )

        total_cost = sum(l.get("total_cost", 0) for l in billing_lines)

        return json_response({
            "pax8_company_id": company_id,
            "year": year,
            "month": month,
            "total_cost": round(total_cost, 2),
            "lines": billing_lines,
        })
    except PAX8Error as e:
        logger.error(f"PAX8 error in get_billing: {e}")
        return error_response(str(e), e.status_code or 502)
    except Exception as e:
        logger.exception("Unexpected error in get_billing")
        return error_response(str(e))


# ------------------------------------------------------------------
# GET /api/status?year=2026&month=4
# ------------------------------------------------------------------

@app.route(route="status", methods=["GET"])
def get_status(req: func.HttpRequest) -> func.HttpResponse:
    """
    Full dashboard data. For each mapped PAX8 company returns:
    - PAX8 billing amount
    - Moneo invoice (if exists): nr, date, total, payment status
    - Profit
    - Status: invoiced_paid | invoiced_unpaid | not_invoiced
    """
    year = req.params.get("year")
    month = req.params.get("month")

    if not all([year, month]):
        return error_response("year and month are required", 400)

    try:
        year = int(year)
        month = int(month)
    except ValueError:
        return error_response("year and month must be integers", 400)

    start_date, end_date = get_month_date_range(year, month)

    try:
        pax8 = PAX8Client()
        moneo = MoneoClient()
        mappings = load_mappings()
        manual_links = manual_links_store.get_for_period(year, month)

        # Fetch all PAX8 companies
        pax8_companies = pax8.get_all_companies()
        pax8_by_id = {
            c.get("id", c.get("companyId", "")): c for c in pax8_companies
        }

        # Fetch Moneo invoices for the month (all customers)
        moneo_invoices_raw = moneo.get_invoices(date_from=start_date, date_to=end_date)
        moneo_invoices_enriched = moneo.get_invoice_payment_status(moneo_invoices_raw)

        # Index Moneo invoices by customer code
        moneo_by_customer: dict = {}
        for inv in moneo_invoices_enriched:
            cc = inv.get("custcode", inv.get("customerCode", ""))
            if cc not in moneo_by_customer:
                moneo_by_customer[cc] = []
            moneo_by_customer[cc].append(inv)

        dashboard = []

        for pax8_id, mapping in mappings.items():
            moneo_code = mapping.get("moneo_code")
            if not moneo_code:
                continue

            pax8_company = pax8_by_id.get(pax8_id, {})
            pax8_name = pax8_company.get("name", pax8_company.get("companyName", pax8_id))

            # PAX8 billing for this company/month
            pax8_cost = 0.0
            try:
                billing_lines = pax8.get_billing_for_month(pax8_id, year, month)
                pax8_cost = sum(l.get("total_cost", 0) for l in billing_lines)
            except PAX8Error as e:
                logger.warning(f"PAX8 billing fetch failed for {pax8_id}: {e}")
                billing_lines = []

            # Moneo invoices for this customer this month
            customer_invoices = moneo_by_customer.get(moneo_code, [])

            # Apply manual link override: user linked an existing Moneo invoice
            # (often for an invoice in a different month, or a partial amount)
            link = manual_links.get(pax8_id)
            manual = False
            if link and not customer_invoices:
                manual = True
                customer_invoices = [{
                    "invnr": link.get("invoice_nr", ""),
                    "invdate": link.get("invoice_date", ""),
                    "total": float(link.get("amount", 0)),
                    "paid": float(link.get("amount", 0)) if link.get("payment_status") == "paid" else 0.0,
                    "payment_status": link.get("payment_status", "unpaid"),
                    "_manual": True,
                    "_original_total": float(link.get("original_total", link.get("amount", 0))),
                }]

            moneo_total = sum(
                float(inv.get("total", inv.get("invsum", 0)) or 0)
                for inv in customer_invoices
            )

            # Determine overall status
            if not customer_invoices:
                status = "not_invoiced"
                payment_status = None
            else:
                statuses = {inv["payment_status"] for inv in customer_invoices}
                if "paid" in statuses and len(statuses) == 1:
                    status = "invoiced_paid"
                    payment_status = "paid"
                elif "unpaid" in statuses or "partial" in statuses:
                    status = "invoiced_unpaid"
                    payment_status = "unpaid"
                else:
                    status = "invoiced_unpaid"
                    payment_status = "unpaid"

            # Profit
            profit_data = calculate_profit(pax8_cost, moneo_total) if moneo_total > 0 else {
                "moneo_excl_vat": 0.0,
                "pax8_cost": pax8_cost,
                "profit": 0.0,
                "profit_pct": 0.0,
            }

            dashboard.append({
                "pax8_id": pax8_id,
                "pax8_name": pax8_name,
                "moneo_code": moneo_code,
                "moneo_name": mapping.get("moneo_name", ""),
                "pax8_cost": round(pax8_cost, 2),
                "moneo_total": round(moneo_total, 2),
                "moneo_total_excl_vat": profit_data["moneo_excl_vat"],
                "profit": profit_data["profit"],
                "profit_pct": profit_data["profit_pct"],
                "status": status,
                "payment_status": payment_status,
                "manual_linked": manual,
                "invoices": [
                    {
                        "invnr": inv.get("invnr", inv.get("invoiceNr", "")),
                        "invdate": inv.get("invdate", inv.get("invoiceDate", "")),
                        "total": float(inv.get("total", inv.get("invsum", 0)) or 0),
                        "paid": float(inv.get("paid", inv.get("paidsum", 0)) or 0),
                        "payment_status": inv["payment_status"],
                        "_original_total": inv.get("_original_total"),
                        "_manual": inv.get("_manual", False),
                    }
                    for inv in customer_invoices
                ],
            })

        # Sort: not_invoiced first, then unpaid, then paid
        status_order = {"not_invoiced": 0, "invoiced_unpaid": 1, "invoiced_paid": 2}
        dashboard.sort(key=lambda x: (status_order.get(x["status"], 9), x["pax8_name"]))

        return json_response({
            "year": year,
            "month": month,
            "companies": dashboard,
            "summary": {
                "total_pax8_cost": round(sum(c["pax8_cost"] for c in dashboard), 2),
                "total_moneo": round(sum(c["moneo_total"] for c in dashboard), 2),
                "total_profit": round(sum(c["profit"] for c in dashboard), 2),
                "not_invoiced_count": sum(1 for c in dashboard if c["status"] == "not_invoiced"),
                "unpaid_count": sum(1 for c in dashboard if c["status"] == "invoiced_unpaid"),
                "paid_count": sum(1 for c in dashboard if c["status"] == "invoiced_paid"),
            },
        })

    except PAX8Error as e:
        logger.error(f"PAX8 error in get_status: {e}")
        return error_response(str(e), e.status_code or 502)
    except MoneoError as e:
        logger.error(f"Moneo error in get_status: {e}")
        return error_response(str(e), e.status_code or 502)
    except Exception as e:
        logger.exception("Unexpected error in get_status")
        return error_response(str(e))


# ------------------------------------------------------------------
# GET /api/invoices?moneo_customer_code=X
# ------------------------------------------------------------------

@app.route(route="invoices", methods=["GET"])
def get_invoices(req: func.HttpRequest) -> func.HttpResponse:
    """
    Moneo invoices for a customer with payment status.
    Optional: date_from, date_to query params.
    """
    customer_code = req.params.get("moneo_customer_code")
    date_from = req.params.get("date_from")
    date_to = req.params.get("date_to")

    if not customer_code:
        return error_response("moneo_customer_code is required", 400)

    try:
        moneo = MoneoClient()
        invoices = moneo.get_invoices(
            customer_code=customer_code,
            date_from=date_from,
            date_to=date_to,
        )
        enriched = moneo.get_invoice_payment_status(invoices)

        # Optionally fetch line items for each invoice
        include_lines = req.params.get("include_lines", "false").lower() == "true"
        if include_lines:
            for inv in enriched:
                invnr = inv.get("invnr", inv.get("invoiceNr", ""))
                if invnr:
                    try:
                        inv["lines"] = moneo.get_invoice_items(invnr)
                    except MoneoError as e:
                        logger.warning(f"Failed to fetch lines for invoice {invnr}: {e}")
                        inv["lines"] = []

        return json_response(enriched)
    except MoneoError as e:
        logger.error(f"Moneo error in get_invoices: {e}")
        return error_response(str(e), e.status_code or 502)
    except Exception as e:
        logger.exception("Unexpected error in get_invoices")
        return error_response(str(e))


# ------------------------------------------------------------------
# POST /api/generate-invoice
# ------------------------------------------------------------------

@app.route(route="generate-invoice", methods=["POST"])
def generate_invoice(req: func.HttpRequest) -> func.HttpResponse:
    """
    Creates a Moneo invoice from PAX8 billing data.

    Body:
    {
      "pax8_company_id": "...",
      "moneo_customer_code": "1013",
      "year": 2026,
      "month": 4,
      "comment": "optional override"   // optional
    }

    Returns: { "invoice_nr": "...", "total": ..., "lines_count": ... }
    """
    try:
        body = req.get_json()
    except Exception:
        return error_response("Invalid JSON body", 400)

    pax8_company_id = body.get("pax8_company_id")
    moneo_customer_code = body.get("moneo_customer_code")
    year = body.get("year")
    month = body.get("month")

    if not all([pax8_company_id, moneo_customer_code, year, month]):
        return error_response(
            "pax8_company_id, moneo_customer_code, year and month are required", 400
        )

    try:
        year = int(year)
        month = int(month)
    except (TypeError, ValueError):
        return error_response("year and month must be integers", 400)

    try:
        pax8 = PAX8Client()
        moneo = MoneoClient()
        mappings = load_mappings()

        # Fetch PAX8 company name
        try:
            pax8_company = pax8.get_company(pax8_company_id)
            pax8_name = pax8_company.get("name", pax8_company.get("companyName", pax8_company_id))
        except PAX8Error:
            pax8_name = pax8_company_id

        # Fetch billing lines
        billing_lines = pax8.get_billing_for_month(pax8_company_id, year, month)

        if not billing_lines:
            return error_response(
                f"No billing data found for company {pax8_company_id} in {year}-{month:02d}",
                404,
            )

        # Check for split subscriptions
        mapping = mappings.get(pax8_company_id, {})
        split_config = mapping.get("split_subscriptions", {})

        if split_config:
            # Apply split: some items go to a different Moneo customer
            split_groups = apply_split_subscriptions(
                billing_lines, split_config, moneo_customer_code
            )
            # Only process the items for the requested moneo_customer_code
            billing_lines = split_groups.get(moneo_customer_code, billing_lines)

        # Map PAX8 lines → Moneo lines
        moneo_lines = map_pax8_to_moneo_lines(billing_lines)
        moneo_lines = merge_duplicate_lines(moneo_lines)

        if not moneo_lines:
            return error_response("No invoice lines generated from PAX8 data", 422)

        # Build comment
        inv_date = f"{year}-{month:02d}-01"
        comment = body.get("comment") or build_invoice_comment(year, month, pax8_name)

        # Create invoice in Moneo
        result = moneo.create_invoice(
            customer_code=moneo_customer_code,
            inv_date=inv_date,
            lines=moneo_lines,
            comment=comment,
        )

        # Extract invoice number from Moneo response
        invoice_nr = (
            result.get("invnr")
            or result.get("invoiceNr")
            or result.get("id")
            or result.get("data", {}).get("invnr")
            or "unknown"
        )

        total = sum(l["rowsum"] for l in moneo_lines)

        logger.info(
            f"Created Moneo invoice {invoice_nr} for customer {moneo_customer_code}, "
            f"total {total:.2f} EUR ({len(moneo_lines)} lines)"
        )

        return json_response({
            "invoice_nr": invoice_nr,
            "moneo_customer_code": moneo_customer_code,
            "pax8_company_id": pax8_company_id,
            "year": year,
            "month": month,
            "total": round(total, 2),
            "lines_count": len(moneo_lines),
            "lines": moneo_lines,
            "comment": comment,
        }, status_code=201)

    except PAX8Error as e:
        logger.error(f"PAX8 error in generate_invoice: {e}")
        return error_response(str(e), e.status_code or 502)
    except MoneoError as e:
        logger.error(f"Moneo error in generate_invoice: {e}")
        return error_response(str(e), e.status_code or 502)
    except Exception as e:
        logger.exception("Unexpected error in generate_invoice")
        return error_response(str(e))


# ------------------------------------------------------------------
# POST /api/manual-links
# ------------------------------------------------------------------

@app.route(route="manual-links", methods=["POST"])
def save_manual_link(req: func.HttpRequest) -> func.HttpResponse:
    """
    Link an existing Moneo invoice to a PAX8 company for a given month,
    optionally overriding the amount (when one invoice spans multiple months).

    Body: { year, month, pax8_id, invoice_nr, invoice_date,
            amount, original_total, payment_status }
    """
    try:
        body = req.get_json()
    except Exception:
        return error_response("Invalid JSON body", 400)

    required = ("year", "month", "pax8_id", "invoice_nr", "amount")
    missing = [k for k in required if body.get(k) in (None, "")]
    if missing:
        return error_response(f"Missing fields: {', '.join(missing)}", 400)

    try:
        year = int(body["year"])
        month = int(body["month"])
        amount = float(body["amount"])
        original_total = float(body.get("original_total") or body["amount"])
    except (TypeError, ValueError):
        return error_response("year, month and amounts must be numeric", 400)

    link = {
        "invoice_nr": str(body["invoice_nr"]),
        "invoice_date": body.get("invoice_date", ""),
        "amount": amount,
        "original_total": original_total,
        "payment_status": body.get("payment_status", "unpaid"),
    }
    manual_links_store.set_link(year, month, body["pax8_id"], link)
    return json_response({"saved": True, "link": link})


# ------------------------------------------------------------------
# DELETE /api/manual-links
# ------------------------------------------------------------------

@app.route(route="manual-links", methods=["DELETE"])
def delete_manual_link(req: func.HttpRequest) -> func.HttpResponse:
    """Body: { year, month, pax8_id }"""
    try:
        body = req.get_json()
    except Exception:
        return error_response("Invalid JSON body", 400)

    for key in ("year", "month", "pax8_id"):
        if body.get(key) in (None, ""):
            return error_response(f"Missing field: {key}", 400)

    try:
        year = int(body["year"])
        month = int(body["month"])
    except (TypeError, ValueError):
        return error_response("year and month must be integers", 400)

    removed = manual_links_store.delete_link(year, month, body["pax8_id"])
    return json_response({"removed": removed})


# ------------------------------------------------------------------
# GET /api/moneo-customers
# ------------------------------------------------------------------

@app.route(route="moneo-customers", methods=["GET"])
def get_moneo_customers(req: func.HttpRequest) -> func.HttpResponse:
    """Return all Moneo customers (custcode + companyname + vatno) for autocomplete."""
    try:
        moneo = MoneoClient()
        customers = moneo.get_customers()
        result = [
            {
                "custcode": c.get("code") or c.get("custcode") or "",
                "companyname": c.get("name") or c.get("companyname") or "",
                "vatno": c.get("vatno") or "",
            }
            for c in customers
            if c.get("code") or c.get("custcode")
        ]
        result.sort(key=lambda x: (x["companyname"] or "").lower())
        return json_response(result)
    except MoneoError as e:
        logger.error(f"Moneo error in get_moneo_customers: {e}")
        return error_response(str(e), e.status_code or 502)
    except Exception as e:
        logger.exception("Unexpected error in get_moneo_customers")
        return error_response(str(e))


# ------------------------------------------------------------------
# GET /api/secrets
# ------------------------------------------------------------------

@app.route(route="secrets", methods=["GET"])
def get_secrets(req: func.HttpRequest) -> func.HttpResponse:
    """Return masked previews of configured API credentials."""
    try:
        return json_response(secrets_store.get_masked_all())
    except Exception as e:
        logger.exception("Error loading secrets")
        return error_response(str(e))


# ------------------------------------------------------------------
# POST /api/secrets
# ------------------------------------------------------------------

@app.route(route="secrets", methods=["POST"])
def save_secrets(req: func.HttpRequest) -> func.HttpResponse:
    """
    Update API credentials.

    Body: partial dict, any subset of:
      pax8_client_id, pax8_client_secret, moneo_api_key, moneo_company_id
    - Non-empty string → replace
    - Empty string or omitted key → leave unchanged
    - null → clear
    """
    try:
        body = req.get_json()
    except Exception:
        return error_response("Invalid JSON body", 400)

    if not isinstance(body, dict):
        return error_response("Body must be a JSON object", 400)

    allowed = set(secrets_store.KEYS)
    unknown = set(body.keys()) - allowed
    if unknown:
        return error_response(f"Unknown keys: {', '.join(sorted(unknown))}", 400)

    try:
        secrets_store.update_secrets(body)
        secrets_store.invalidate_caches()
        return json_response({"saved": True, "secrets": secrets_store.get_masked_all()})
    except Exception as e:
        logger.exception("Error saving secrets")
        return error_response(str(e))


# ------------------------------------------------------------------
# GET /api/config
# ------------------------------------------------------------------

@app.route(route="config", methods=["GET"])
def get_config(req: func.HttpRequest) -> func.HttpResponse:
    """Returns the company mappings config (PAX8 id → Moneo customer code)."""
    try:
        mappings = load_mappings()
        return json_response(mappings)
    except Exception as e:
        logger.exception("Error loading config")
        return error_response(str(e))


# ------------------------------------------------------------------
# POST /api/config
# ------------------------------------------------------------------

@app.route(route="config", methods=["POST"])
def save_config(req: func.HttpRequest) -> func.HttpResponse:
    """
    Save/update company mapping config.

    Body: full mappings object or partial update.
    {
      "PAX8_UUID": {
        "moneo_code": "1013",
        "moneo_name": "TOLMETS SIA",
        "split_subscriptions": {}
      }
    }
    """
    try:
        body = req.get_json()
    except Exception:
        return error_response("Invalid JSON body", 400)

    if not isinstance(body, dict):
        return error_response("Body must be a JSON object", 400)

    try:
        mappings = mappings_store.upsert_many(body)
        return json_response({"saved": True, "mappings": mappings})
    except Exception as e:
        logger.exception("Error saving config")
        return error_response(str(e))
