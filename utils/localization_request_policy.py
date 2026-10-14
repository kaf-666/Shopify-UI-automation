"""Narrow permission for Mondressy's verified Webrex localization constructor.

Public source inspected 2026-10-14: currency-converter-42-183/assets/
ws-currencyconverter.js, function u(), line 1, column 36622. It posts FormData
to the literal /localization?isCallFromWB=true. The flag is static app metadata;
country and optional language are form fields. No request values are retained.
"""

from __future__ import annotations

from email import policy
from email.parser import BytesParser
import re
from urllib.parse import urlsplit


MONDRESSY_ORIGIN = "https://mondressy.com"
WEBREX_LOCALIZATION_REASON = "MONDRESSY_WEBREX_LOCALIZATION_CONTEXT"
_QUERY = "isCallFromWB=true"
_CONTENT_TYPE = re.compile(
    r'multipart/form-data;[ \t]*boundary=(?:[A-Za-z0-9_-]{1,70}|"[A-Za-z0-9_-]{1,70}")',
    re.IGNORECASE,
)
_REQUIRED_FIELDS = frozenset({"form_type", "_method", "return_to", "country_code", "utf8"})
_FORM_DISPOSITION = re.compile(r'form-data;[ \t]*name="([a-z0-9_]+)"')
_STOREFRONT_PATH = re.compile(
    r"/(?:|search/?|cart/?|products/[A-Za-z0-9-]+/?|"
    r"collections/[A-Za-z0-9-]+(?:/products/[A-Za-z0-9-]+)?/?)"
)


def _is_storefront_return(value: str) -> bool:
    # The inspected script uses window.location.href. Never authorize an
    # external redirect, credentials, alternate port, or checkout/account path.
    if re.search(r"[\\\x00-\x20\x7f]", value):
        return False
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "mondressy.com"
        and _STOREFRONT_PATH.fullmatch(parsed.path) is not None
    )


def _safe_form(request) -> bool:
    content_type = request.header_value("content-type")
    body = request.post_data_buffer
    if (
        not isinstance(content_type, str)
        or _CONTENT_TYPE.fullmatch(content_type) is None
        or not isinstance(body, bytes)
        or not 0 < len(body) <= 16384
    ):
        return False
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + body
    )
    if message.defects or not message.is_multipart():
        return False
    fields = {}
    for part in message.iter_parts():
        headers = list(part.raw_items())
        # Reject ambiguous MIME parameters before the email parser can
        # normalize or discard duplicates differently from the server.
        disposition = (
            _FORM_DISPOSITION.fullmatch(headers[0][1])
            if len(headers) == 1 and headers[0][0].lower() == "content-disposition"
            else None
        )
        name = disposition.group(1) if disposition else None
        if (
            part.defects
            or part.is_multipart()
            or name not in _REQUIRED_FIELDS | {"language_code"}
            or name in fields
        ):
            return False
        fields[name] = part.get_payload(decode=True).decode("utf-8", errors="strict")
    return (
        _REQUIRED_FIELDS <= fields.keys()
        and fields["form_type"] == "localization"
        and fields["_method"] == "put"
        and fields["utf8"] == "\u2713"
        and re.fullmatch(r"[A-Z]{2}", fields["country_code"]) is not None
        and ("language_code" not in fields or re.fullmatch(
            r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", fields["language_code"]
        ) is not None)
        and _is_storefront_return(fields["return_to"])
    )


def is_verified_webrex_localization(url, method, canonical_origin, request) -> bool:
    """Validate the exact observed request in memory; fail closed on any error."""
    try:
        parsed = urlsplit(url)
        canonical = urlsplit(canonical_origin or "")
        return (
            method == "POST"
            and canonical.scheme == "https"
            and canonical.netloc == "mondressy.com"
            and canonical.path in {"", "/"}
            and not canonical.query
            and not canonical.fragment
            and parsed.scheme == "https"
            and parsed.netloc == "mondressy.com"
            and parsed.path == "/localization"
            # Exact wire structure: one fixed literal key/value, no alternate
            # encoding, duplicates, extra keys, empty components or fragment.
            and parsed.query == _QUERY
            and "#" not in url
            and _safe_form(request)
        )
    except Exception:
        # Parsing/header/body errors must never leak raw payloads or permit a
        # request whose shape could not be verified.
        return False
