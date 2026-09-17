"""Minimal NSE HTTP client.

NSE's endpoints are not a public API. Two things are required to get anything
back at all:

1. **A cookie handshake.** Hit ``https://www.nseindia.com/`` with a browser
   User-Agent first and reuse the cookie jar. A bare request to an ``/api/``
   path returns 401/403.
2. **A plausible Referer**, pointing at the page that would normally issue the
   call.

Cookies expire, so :meth:`NSEClient.get_json` re-handshakes once on an auth
failure before giving up.

NSE also blocks datacenter IP ranges outright at its Akamai edge. That is not
something headers can work around -- see :class:`NSEBlocked`.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

import requests

BASE = "https://www.nseindia.com"
API = f"{BASE}/api"
ARCHIVES = "https://nsearchives.nseindia.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

TIMEOUT = 45


class NSEBlocked(RuntimeError):
    """NSE's edge refused the request because of where it came from.

    Raised on the Akamai "Access Denied" response, which is returned to cloud
    and CI IP ranges regardless of headers or cookies. Retrying will not help
    and neither will a different User-Agent.
    """


def _looks_blocked(resp: requests.Response) -> bool:
    if resp.status_code not in (401, 403):
        return False
    body = resp.text[:2000].lower()
    return "access denied" in body or "errors.edgesuite.net" in body


class NSEClient:
    """Session-managing client for NSE's JSON endpoints and archive files."""

    def __init__(self, timeout: int = TIMEOUT, retries: int = 3) -> None:
        self.timeout = timeout
        self.retries = retries
        self._session: requests.Session | None = None

    def _handshake(self) -> requests.Session:
        s = requests.Session()
        s.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive",
            }
        )
        resp = s.get(BASE, timeout=self.timeout)
        if _looks_blocked(resp):
            raise NSEBlocked(
                "NSE refused the connection from this network (Akamai 'Access "
                "Denied'). NSE blocks datacenter and CI IP ranges; run this from "
                "a residential/India IP, or use the BSE-backed sector master and "
                "the NSDL flow data, which do answer cloud IPs."
            )
        resp.raise_for_status()
        self._session = s
        return s

    @property
    def session(self) -> requests.Session:
        return self._session or self._handshake()

    def get_json(
        self, path: str, params: Mapping[str, Any] | None = None, referer: str = BASE
    ) -> Any:
        """GET an ``/api/`` path and decode JSON, handshaking as needed."""
        url = path if path.startswith("http") else f"{API}/{path.lstrip('/')}"
        last: Exception | None = None

        for attempt in range(self.retries):
            try:
                resp = self.session.get(
                    url,
                    params=params,
                    timeout=self.timeout,
                    headers={"Referer": referer},
                )
                if _looks_blocked(resp):
                    raise NSEBlocked(
                        f"NSE refused {url} from this network (Akamai 'Access "
                        "Denied'). This is an IP-range block, not a header issue."
                    )
                if resp.status_code in (401, 403):
                    # Most likely a stale cookie; re-handshake and try again.
                    self._session = None
                    last = requests.HTTPError(f"{resp.status_code} for {url}")
                    continue
                resp.raise_for_status()
                return resp.json()
            except NSEBlocked:
                raise
            except (requests.RequestException, ValueError) as exc:
                last = exc
                self._session = None
            if attempt < self.retries - 1:
                time.sleep(2**attempt)

        raise RuntimeError(f"Failed to fetch {url} after {self.retries} attempts: {last}")
