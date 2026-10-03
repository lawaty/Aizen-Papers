from __future__ import annotations

from typing import Mapping

import requests

from sender.domain.models import Customer, Invoice, Payment
from sender.domain.ports import CustomerSource, InvoiceSource, PaymentSource
from sender.domain.errors import DaftraApiError
from sender.infrastructure.config import DAFTRA_BASE_URL
from sender.infrastructure.util import json_or_none
from .mapper import DaftraCustomerMapper, DaftraInvoiceMapper, DaftraPaymentMapper


def _total_results(payload: dict) -> int | None:
    """Daftra's own count of the rows a listing matched, or ``None`` if it did not say.

    The count is the one piece of *source-side* truth a listing carries, and it is
    the only thing that can tell "the account has no new payments" apart from "the
    endpoint is showing me less of the account than it used to". The rows themselves
    cannot: a paginated page looks identical whether the account shrank or not.
    ``None`` rather than ``0`` when the key is missing, because an absent count must
    not read as "there is nothing there".
    """
    pagination = payload.get("pagination") if isinstance(payload, dict) else None
    if not isinstance(pagination, dict):
        return None
    total = pagination.get("total_results")
    return total if isinstance(total, int) and not isinstance(total, bool) else None


class DaftraClient:
    """One Daftra account, read three ways: invoices, payments and clients.

    Implements :class:`~sender.domain.ports.InvoiceSource`,
    :class:`~sender.domain.ports.PaymentSource` and
    :class:`~sender.domain.ports.CustomerSource`.

    One client per tenant serves all three pipelines: the credentials, base url,
    timeout and country code are identical, and keeping them in a single object is
    what lets the composition root build one adapter per app and hand it to
    whichever pollers are configured.
    """

    def __init__(
        self,
        api_key: str = "",
        base_url: str = DAFTRA_BASE_URL,
        timeout: float = 15.0,
        session: requests.Session | None = None,
        country_code: str = "20",
        payments_status: str | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._session = session if session is not None else requests.Session()
        self._api_key = api_key
        if getattr(self._session, "headers", None) is None:
            self._session.headers = {}
        self._remove_auth_headers()
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self._api_key:
            headers["apikey"] = self._api_key
        self._session.headers.update(headers)
        # The mappers normalize customer phones, so they need the deployment's
        # country code, not a baked-in 20.
        self._mapper = DaftraInvoiceMapper(country_code)
        self._payment_mapper = DaftraPaymentMapper(country_code)
        self._customer_mapper = DaftraCustomerMapper(country_code)
        # Server-side narrowing of the payment listing. ``None`` means "no filter",
        # which is a real mode (see the payments guide), not a missing value.
        self._payments_status = payments_status or None
        #: The source's own count from the most recent ``list_payments`` call, read
        #: by the poller's "did the source go quiet?" tripwire. ``None`` until a
        #: listing has run, and ``None`` forever for a source that does not report it.
        self._last_listing_total: int | None = None

    @property
    def last_listing_total(self) -> int | None:
        """How many payments the source says matched the last listing, if it said.

        Read through ``getattr`` by the poller rather than declared on the port, so
        that a source which cannot report a total (the offline stub, a test double)
        simply does not get asked instead of having to implement a method that
        means nothing to it. ``None`` means "no opinion", never "none exist".
        """
        return self._last_listing_total

    def get_invoice(self, invoice_id: int | str) -> Invoice:
        return self._mapper.to_invoice(self._fetch_invoice(invoice_id))

    def get_raw_invoice(self, invoice_id: int | str) -> dict:
        return self._fetch_invoice(invoice_id)

    def list_invoices(self, limit: int = 10, page: int = 1, filters: Mapping | None = None) -> list[Invoice]:
        params = {"page": page, "limit": limit, **(filters or {})}
        payload = self._request("GET", "/invoices.json", params=params)
        return self._mapper.to_invoices(payload)

    def get_payment(self, payment_id: int | str) -> Payment:
        """The payment, resolved far enough to actually address the customer.

        Two requests, and that is the price of reaching the customer at all: a
        Daftra payment record names no client, so the payer only exists on the
        invoice the payment settles. Doing the second hop *here* rather than in
        the poller keeps the application layer free of Daftra's join order, and
        keeps ``PaymentSource`` stating what a caller needs rather than how many
        calls it costs.

        A payment whose invoice cannot be read raises
        :class:`DaftraApiError`, which the poller classifies like any other fetch
        failure: a transient one is retried with backoff, a 404 (the invoice was
        deleted) is abandoned once and left visible for an operator.
        """
        raw = self._fetch_payment(payment_id)
        payment = self._payment_mapper.to_payment(raw)
        if not payment.invoice_id:
            # No linked invoice means no identifiable payer. Hand it back as-is so
            # the poller records the honest outcome (skipped, no usable phone)
            # instead of inventing a second request that cannot help.
            return payment
        return self._payment_mapper.to_payment(raw, self._fetch_invoice(payment.invoice_id))

    def get_raw_payment(self, payment_id: int | str) -> dict:
        """Just the payment row, un-joined — what ``show-payment --raw`` prints."""
        return self._fetch_payment(payment_id)

    def list_payments(self, limit: int = 10, page: int = 1) -> list[Payment]:
        """One page of payments, newest first (the endpoint's own default order).

        The status filter is applied server-side so a page is never spent on
        payments that are not going to be announced — which matters because a
        page is exactly the unit the poller's paging logic reasons about.

        ``include_client_credit=1`` is **not** optional and is the whole reason
        this pipeline can see payments at all. ``/invoice_payments.json`` excludes
        ``client_credit`` rows by default, and on a real account that is nearly
        every payment: of 125 payments on one tenant, 124 were ``client_credit``
        and exactly one was ``cash``. Left to its default the endpoint answered
        ``total_results=1`` — that single ``cash`` row from five months earlier —
        on every request regardless of ``limit``, ``page``, ``sort``, ``status``,
        ``invoice_id``, ``treasury_id`` or ``branch_id``. The 124 others were
        demonstrably there, reachable individually via
        ``/invoice_payments/{id}.json`` and embedded in each invoice's detail, so
        the pipeline was not failing: it was being handed one old row it had
        already announced, forever, and reporting ``listed 1, new 0`` with a zero
        exit code and no warning.

        A ``client_credit`` row is money received against an invoice — it settles
        one, and the template tells the customer their balance was updated — so it
        is exactly what this pipeline exists to announce. :meth:`_invoice_id`
        already recorded that these rows exist; this is the request that makes them
        visible. The flag is passed unconditionally rather than exposed as a knob
        because omitting it does not narrow the announcement, it *silences* it.
        """
        params: dict = {"page": page, "limit": limit, "include_client_credit": 1}
        if self._payments_status:
            params["status"] = self._payments_status
        payload = self._request("GET", "/invoice_payments.json", params=params)
        self._last_listing_total = _total_results(payload)
        return self._payment_mapper.to_payments(payload)

    def get_customer(self, customer_id: int | str) -> Customer:
        """The client record, resolved far enough to address the customer.

        One request, unlike :meth:`get_payment`'s two. A client row *is* the
        customer: it carries the name and the phone itself, so there is nothing to
        join to. The poller still treats the listing row as possibly too thin and
        falls back to this when a phone is missing, but that is the inherited
        "no reachable phone" rule rather than a property of this endpoint.
        """
        return self._customer_mapper.to_customer(self._fetch_customer(customer_id))

    def get_raw_customer(self, customer_id: int | str) -> dict:
        """Just the client row, un-normalized — what ``show-customer --raw`` prints."""
        return self._fetch_customer(customer_id)

    def list_customers(self, limit: int = 10, page: int = 1) -> list[Customer]:
        """One page of clients, newest first.

        ``sort=created&direction=desc`` is **not** decoration and must not be
        dropped, tuned away, or "simplified" — it is the only combination this
        endpoint honours that yields true newest-first order (verified live
        against three accounts: ``sort=created`` alone gives *oldest* first,
        ``order=desc`` is silently ignored, and the parameterless default is a
        stable but arbitrary permutation such as ``[5,1,2,6,4,3]``).

        The poller walks pages forward and stops at the first already-seen record,
        so an arbitrary order makes it stop early on a saturated page and silently
        never announce a new client that sits further back. It is a silent failure
        — no error, no warning, a pipeline that just quietly stops working — which
        is why the request params are pinned by a test.

        There is deliberately **no** ``created_from`` narrowing here, unlike
        ``list_payments``'s ``status``. The endpoint does accept one, but its
        boundary is date-granular and inclusive (the time component is truncated),
        so it cannot express a precise watermark, and Daftra *silently ignores*
        every other date-filter spelling — answering 200 with the unfiltered set.
        A misspelled filter would therefore look like it worked while scanning
        everything. The seen-set already is the watermark, and the accounts hold a
        handful of clients each, so there is nothing to gain.
        """
        params = {"page": page, "limit": limit, "sort": "created", "direction": "desc"}
        payload = self._request("GET", "/clients.json", params=params)
        return self._customer_mapper.to_customers(payload)

    def _fetch_customer(self, customer_id: int | str) -> dict:
        sid = str(customer_id)
        if not sid.isdigit():
            raise ValueError(f"Invalid customer id: {customer_id!r}")
        return self._request("GET", f"/clients/{sid}.json")

    def _fetch_invoice(self, invoice_id: int | str) -> dict:
        sid = str(invoice_id)
        if not sid.isdigit():
            raise ValueError(f"Invalid invoice id: {invoice_id!r}")
        return self._request("GET", f"/invoices/{sid}.json")

    def _fetch_payment(self, payment_id: int | str) -> dict:
        sid = str(payment_id)
        if not sid.isdigit():
            raise ValueError(f"Invalid payment id: {payment_id!r}")
        return self._request("GET", f"/invoice_payments/{sid}.json")

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self._base}{path}"
        request_kwargs = dict(kwargs)
        headers = self._clean_auth_headers(request_kwargs.get("headers"))
        if self._api_key:
            headers["apikey"] = self._api_key
        if headers:
            request_kwargs["headers"] = headers

        try:
            response = self._send(method, url, request_kwargs)
        except requests.RequestException as exc:
            raise DaftraApiError(None, f"network error: {exc}") from exc

        status = getattr(response, "status_code", None)
        response_ok = getattr(response, "ok", None)
        if response_ok is None:
            response_ok = status is not None and 200 <= status < 300
        if not response_ok:
            response_text = getattr(response, "text", "") or ""
            raise DaftraApiError(status, response_text[:500], json_or_none(response))
        payload = json_or_none(response)
        if payload is None:
            # A 2xx whose body is not JSON is a proxy/captive-portal/gateway
            # hiccup, not a genuine "no invoices" answer. Reported with a None
            # status (the same shape as a network error) so the poller
            # classifies it as transient and retries, instead of silently
            # looking like an empty listing or being abandoned as permanent.
            raise DaftraApiError(
                None,
                f"response was not JSON (HTTP {status}): "
                f"{getattr(response, 'text', '')[:200]}",
            )
        if isinstance(payload, dict) and payload.get("result") not in (None, "successful", "success"):
            raise DaftraApiError(
                status, str(payload.get("message") or payload)[:200], payload
            )
        return payload or {}

    def _send(self, method: str, url: str, kwargs: dict):
        request = getattr(self._session, "request", None)
        if callable(request):
            return request(method, url, timeout=self._timeout, **kwargs)
        if method.upper() == "GET":
            return self._session.get(url, timeout=self._timeout, **kwargs)
        return self._session.post(url, timeout=self._timeout, **kwargs)

    @staticmethod
    def _is_auth_header(key) -> bool:
        normalized = str(key).lower().replace("-", "").replace("_", "")
        return normalized in {"apikey", "authorization"}

    @classmethod
    def _clean_auth_headers(cls, headers) -> dict:
        return {
            key: value
            for key, value in (headers or {}).items()
            if not cls._is_auth_header(key)
        }

    def _remove_auth_headers(self) -> None:
        headers = getattr(self._session, "headers", None)
        if headers is None:
            return
        for key in list(headers):
            if self._is_auth_header(key):
                del headers[key]
