"""Open-access resolution and guarded PDF transfer for lit-downloader."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import ssl
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener, urlopen

MAX_PDF_BYTES = 100 * 1024 * 1024
USER_AGENT = "endnotex9-mcp-lit-downloader/0.1"


def _tls_context() -> ssl.SSLContext:
    """Verify certificates the way the operating system does.

    Python's own verification cannot fetch an intermediate certificate a
    server forgot to send, which browsers do silently: an institutional
    repository copy that opened fine in Edge failed here with
    CERTIFICATE_VERIFY_FAILED. truststore hands verification to the OS.
    """
    try:
        import truststore
    except ImportError:
        return ssl.create_default_context()
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


_TLS = _tls_context()


@dataclass(frozen=True)
class PdfCandidate:
    provider: str
    url: str


def _is_public_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def validate_public_url(url: str, *, resolve_dns: bool = True) -> str:
    """Reject local/metadata targets before an OA URL is fetched."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only public HTTP(S) PDF URLs are allowed")
    if parsed.username or parsed.password:
        raise ValueError("PDF URLs containing credentials are not allowed")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid PDF URL port") from exc
    if port not in {None, 80, 443}:
        raise ValueError("Non-standard PDF URL ports are not allowed")

    host = parsed.hostname.casefold().rstrip(".")
    if host in {"localhost", "metadata.google.internal"} or host.endswith(".localhost"):
        raise ValueError("Local or cloud-metadata PDF URL rejected")
    try:
        if not _is_public_ip(host):
            raise ValueError("Private or reserved PDF address rejected")
        return url
    except ValueError as exc:
        if "address rejected" in str(exc):
            raise
        # Not an IP literal: resolve every address before connecting.
    if resolve_dns:
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(host, port or 443)}
        except OSError as exc:
            raise ValueError(f"Could not resolve PDF host: {host}") from exc
        if not addresses or any(not _is_public_ip(address) for address in addresses):
            raise ValueError("PDF host resolved to a private or reserved address")
    return url


class _SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _json_get(url: str, timeout: int) -> dict:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout, context=_TLS) as response:
        payload = response.read(5 * 1024 * 1024 + 1)
    if len(payload) > 5 * 1024 * 1024:
        raise RuntimeError("Resolver response exceeded 5 MB")
    return json.loads(payload.decode("utf-8"))


def _unique(candidates: Iterable[PdfCandidate]) -> list[PdfCandidate]:
    output: list[PdfCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = candidate.url.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(candidate)
    return output


def resolve_unpaywall(doi: str, timeout: int = 20) -> list[PdfCandidate]:
    email = os.environ.get("UNPAYWALL_EMAIL", "").strip()
    if not email:
        return []
    endpoint = f"https://api.unpaywall.org/v2/{quote(doi, safe='')}?email={quote(email)}"
    payload = _json_get(endpoint, timeout)
    locations = [payload.get("best_oa_location") or {}, *(payload.get("oa_locations") or [])]
    return _unique(
        PdfCandidate("unpaywall", str(location.get("url_for_pdf") or "")) for location in locations
    )


def resolve_openalex(doi: str, timeout: int = 20) -> list[PdfCandidate]:
    endpoint = f"https://api.openalex.org/works/https://doi.org/{quote(doi, safe='')}"
    mailto = os.environ.get("OPENALEX_EMAIL", "").strip()
    if mailto:
        endpoint += f"?mailto={quote(mailto)}"
    payload = _json_get(endpoint, timeout)
    locations = [
        payload.get("best_oa_location") or {},
        payload.get("primary_location") or {},
        *(payload.get("locations") or []),
    ]
    return _unique(
        PdfCandidate("openalex", str(location.get("pdf_url") or "")) for location in locations
    )


def resolve_oa_candidates(doi: str, timeout: int = 20) -> tuple[list[PdfCandidate], list[str]]:
    """Run independent OA resolvers; one unavailable service must not stop the chain."""
    candidates: list[PdfCandidate] = []
    errors: list[str] = []
    for name, resolver in (("unpaywall", resolve_unpaywall), ("openalex", resolve_openalex)):
        try:
            candidates.extend(resolver(doi, timeout))
        except (
            HTTPError,
            URLError,
            OSError,
            ValueError,
            RuntimeError,
            json.JSONDecodeError,
        ) as exc:
            errors.append(f"{name}: {exc}")
    return _unique(candidates), errors


def fetch_public(url: str, headers: dict[str, str], timeout: int = 60) -> tuple[bytes, str]:
    """GET a public URL with redirect checks and a hard cap; the body may be any content."""
    validate_public_url(url)
    opener = build_opener(HTTPSHandler(context=_TLS), _SafeRedirectHandler())
    with opener.open(Request(url, headers=headers), timeout=timeout) as response:
        final_url = response.geturl()
        validate_public_url(final_url)
        length = response.headers.get("Content-Length")
        if length and int(length) > MAX_PDF_BYTES:
            raise RuntimeError("Response exceeds the 100 MB transfer limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(min(1024 * 1024, MAX_PDF_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_PDF_BYTES:
                raise RuntimeError("Response exceeds the 100 MB transfer limit")
    return b"".join(chunks), final_url


def download_pdf(candidate: PdfCandidate, timeout: int = 60) -> tuple[bytes, str]:
    """Fetch one public OA candidate with redirect checks, magic bytes, and a hard cap."""
    data, final_url = fetch_public(
        candidate.url, {"Accept": "application/pdf", "User-Agent": USER_AGENT}, timeout
    )
    if not data.startswith(b"%PDF-"):
        raise RuntimeError("OA candidate returned non-PDF content")
    return data, final_url
