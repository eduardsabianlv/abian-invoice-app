"""
PAX8 API Client
OAuth 2.0 client credentials flow, token cached in memory for 24h.
"""

import os
import time
import logging
import requests
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

PAX8_TOKEN_URL = "https://login.pax8.com/oauth/token"
PAX8_AUDIENCE = "api://p8p.client/pax8.app.api.partner.v1"
PAX8_BASE_URL = "https://api.pax8.com/v1"

# In-memory token cache
_token_cache = {
    "access_token": None,
    "expires_at": 0,
}


class PAX8Error(Exception):
    """Raised when the PAX8 API returns an error."""
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class PAX8Client:
    def __init__(self):
        self.client_id = os.environ.get("PAX8_CLIENT_ID")
        self.client_secret = os.environ.get("PAX8_CLIENT_SECRET")
        if not self.client_id or not self.client_secret:
            raise PAX8Error("PAX8_CLIENT_ID and PAX8_CLIENT_SECRET must be set")
        self.session = requests.Session()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def get_token(self) -> str:
        """Return a valid OAuth access token, fetching a new one if needed."""
        now = time.time()
        if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
            return _token_cache["access_token"]

        logger.info("Fetching new PAX8 OAuth token")
        resp = self.session.post(
            PAX8_TOKEN_URL,
            json={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "audience": PAX8_AUDIENCE,
            },
            timeout=30,
        )
        if not resp.ok:
            raise PAX8Error(
                f"PAX8 token fetch failed: {resp.status_code} {resp.text}",
                status_code=resp.status_code,
            )
        data = resp.json()
        _token_cache["access_token"] = data["access_token"]
        # PAX8 tokens are valid 24 h; fall back to 23 h if not specified
        expires_in = data.get("expires_in", 82800)
        _token_cache["expires_at"] = now + expires_in
        return _token_cache["access_token"]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.get_token()}"}

    def _get(self, path: str, params: dict = None) -> dict:
        url = f"{PAX8_BASE_URL}{path}"
        resp = self.session.get(url, headers=self._headers(), params=params, timeout=30)
        if not resp.ok:
            raise PAX8Error(
                f"PAX8 GET {path} failed: {resp.status_code} {resp.text}",
                status_code=resp.status_code,
            )
        return resp.json()

    # ------------------------------------------------------------------
    # Companies
    # ------------------------------------------------------------------

    def list_companies(self, page: int = 0, size: int = 200) -> dict:
        """GET /companies — paginated list of partner's managed companies."""
        return self._get("/companies", params={"page": page, "size": size})

    def get_company(self, company_id: str) -> dict:
        """GET /companies/{id}"""
        return self._get(f"/companies/{company_id}")

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    def list_subscriptions(self, company_id: str, page: int = 0, size: int = 200) -> dict:
        """GET /companies/{company_id}/subscriptions"""
        return self._get(
            f"/companies/{company_id}/subscriptions",
            params={"page": page, "size": size},
        )

    # ------------------------------------------------------------------
    # Usage
    # ------------------------------------------------------------------

    def get_usage_summary(
        self,
        company_id: str,
        resource_group: str,
        start_date: str,
        end_date: str,
    ) -> dict:
        """
        GET /usage-summaries
        start_date / end_date: ISO date strings, e.g. '2026-04-01'
        """
        return self._get(
            "/usage-summaries",
            params={
                "companyId": company_id,
                "resourceGroup": resource_group,
                "startDate": start_date,
                "endDate": end_date,
            },
        )

    def get_detailed_usage_summary(
        self,
        company_id: str,
        start_date: str,
        end_date: str,
        page: int = 0,
        size: int = 200,
    ) -> dict:
        """
        GET /usage-summaries with detailed breakdown.
        Used to fetch all usage items for a company in a date range.
        """
        return self._get(
            "/usage-summaries",
            params={
                "companyId": company_id,
                "startDate": start_date,
                "endDate": end_date,
                "page": page,
                "size": size,
            },
        )

    # ------------------------------------------------------------------
    # Invoices
    # ------------------------------------------------------------------

    def list_invoices(
        self,
        start_date: str,
        end_date: str,
        page: int = 0,
        size: int = 200,
    ) -> dict:
        """
        GET /invoices — partner-level invoice list.
        start_date / end_date: ISO date strings.
        """
        return self._get(
            "/invoices",
            params={
                "startDate": start_date,
                "endDate": end_date,
                "page": page,
                "size": size,
            },
        )

    def get_invoice(self, invoice_id: str) -> dict:
        """GET /invoices/{id}"""
        return self._get(f"/invoices/{invoice_id}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def get_all_companies(self) -> list:
        """Fetch all companies, handling pagination automatically."""
        companies = []
        page = 0
        size = 200
        while True:
            data = self.list_companies(page=page, size=size)
            items = data.get("content", data.get("data", []))
            companies.extend(items)
            # PAX8 pagination: check if there are more pages
            total_pages = data.get("totalPages", data.get("page", {}).get("totalPages", 1))
            if page + 1 >= total_pages or len(items) < size:
                break
            page += 1
        return companies

    def get_all_subscriptions(self, company_id: str) -> list:
        """Fetch all subscriptions for a company, handling pagination."""
        subscriptions = []
        page = 0
        size = 200
        while True:
            data = self.list_subscriptions(company_id, page=page, size=size)
            items = data.get("content", data.get("data", []))
            subscriptions.extend(items)
            total_pages = data.get("totalPages", data.get("page", {}).get("totalPages", 1))
            if page + 1 >= total_pages or len(items) < size:
                break
            page += 1
        return subscriptions

    def get_billing_for_month(self, company_id: str, year: int, month: int) -> list:
        """
        Returns billing line items for a company for a given month.
        Combines subscription data with usage summaries.
        """
        from calendar import monthrange
        start_date = f"{year}-{month:02d}-01"
        last_day = monthrange(year, month)[1]
        end_date = f"{year}-{month:02d}-{last_day:02d}"

        billing_lines = []

        # Get subscriptions
        try:
            subscriptions = self.get_all_subscriptions(company_id)
            for sub in subscriptions:
                if sub.get("status") in ("Active", "Cancelled"):
                    sku = sub.get("sku", sub.get("productId", "UNKNOWN"))
                    billing_lines.append({
                        "type": "subscription",
                        "sku": sku,
                        "name": sub.get("productName", sub.get("name", sku)),
                        "quantity": sub.get("quantity", 1),
                        "unit_cost": float(sub.get("price", sub.get("unitCost", 0))),
                        "total_cost": float(sub.get("price", sub.get("unitCost", 0)))
                        * float(sub.get("quantity", 1)),
                        "billing_term": sub.get("billingTerm", "Monthly"),
                        "subscription_id": sub.get("id"),
                        "commitment": sub.get("commitmentTerm", ""),
                    })
        except PAX8Error as e:
            logger.warning(f"Failed to fetch subscriptions for {company_id}: {e}")

        # Get usage summaries
        try:
            usage_data = self.get_detailed_usage_summary(
                company_id, start_date, end_date
            )
            usage_items = usage_data.get("content", usage_data.get("data", []))
            for item in usage_items:
                sku = item.get("sku", item.get("resourceId", "USAGE"))
                billing_lines.append({
                    "type": "usage",
                    "sku": sku,
                    "name": item.get("resourceName", item.get("name", sku)),
                    "quantity": float(item.get("quantity", 0)),
                    "unit_cost": float(item.get("unitCost", item.get("pricePerUnit", 0))),
                    "total_cost": float(item.get("totalCost", item.get("cost", 0))),
                    "billing_term": "Usage",
                    "resource_group": item.get("resourceGroup", ""),
                })
        except PAX8Error as e:
            logger.warning(f"Failed to fetch usage for {company_id}: {e}")

        return billing_lines
