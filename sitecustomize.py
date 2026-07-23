import io
import json
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path


_ORIGINAL_URLOPEN = urllib.request.urlopen
_CONFIG_PATH = Path(__file__).resolve().with_name("bot_config.json")


def _network_settings():
    defaults = {
        "request_timeout_seconds": 30,
        "get_retry_attempts": 3,
        "get_retry_backoff_seconds": [2, 5, 10],
    }
    try:
        config = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        network = config.get("network", {}) if isinstance(config, dict) else {}
    except (OSError, json.JSONDecodeError):
        network = {}

    timeout = network.get("request_timeout_seconds", defaults["request_timeout_seconds"])
    attempts = network.get("get_retry_attempts", defaults["get_retry_attempts"])
    backoff = network.get("get_retry_backoff_seconds", defaults["get_retry_backoff_seconds"])

    try:
        timeout = max(5, float(timeout))
    except (TypeError, ValueError):
        timeout = defaults["request_timeout_seconds"]

    try:
        attempts = max(1, int(attempts))
    except (TypeError, ValueError):
        attempts = defaults["get_retry_attempts"]

    if not isinstance(backoff, list) or not backoff:
        backoff = defaults["get_retry_backoff_seconds"]

    clean_backoff = []
    for value in backoff:
        try:
            clean_backoff.append(max(0, float(value)))
        except (TypeError, ValueError):
            continue
    if not clean_backoff:
        clean_backoff = defaults["get_retry_backoff_seconds"]

    return timeout, attempts, clean_backoff


class BufferedResponse:
    def __init__(self, body, status, headers, url):
        self._buffer = io.BytesIO(body)
        self.status = status
        self.headers = headers
        self.url = url

    def read(self, *args, **kwargs):
        return self._buffer.read(*args, **kwargs)

    def getcode(self):
        return self.status

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def close(self):
        self._buffer.close()


def _method(request):
    if isinstance(request, urllib.request.Request):
        return request.get_method().upper()
    return "GET"


def _retryable_http(error):
    return error.code == 429 or 500 <= error.code <= 599


def resilient_urlopen(request, data=None, timeout=None, *args, **kwargs):
    method = _method(request)

    # Never retry order submissions or other write operations because a retry
    # could accidentally duplicate a paper-trading action.
    if method not in {"GET", "HEAD"} or data is not None:
        return _ORIGINAL_URLOPEN(request, data=data, timeout=timeout, *args, **kwargs)

    configured_timeout, attempts, backoff = _network_settings()
    effective_timeout = max(configured_timeout, float(timeout or 0))
    last_error = None

    for attempt in range(attempts):
        try:
            response = _ORIGINAL_URLOPEN(
                request,
                data=data,
                timeout=effective_timeout,
                *args,
                **kwargs,
            )
            try:
                body = response.read()
                return BufferedResponse(
                    body=body,
                    status=getattr(response, "status", response.getcode()),
                    headers=getattr(response, "headers", {}),
                    url=response.geturl(),
                )
            finally:
                response.close()
        except urllib.error.HTTPError as error:
            if not _retryable_http(error):
                raise
            last_error = error
        except (urllib.error.URLError, TimeoutError, socket.timeout) as error:
            last_error = error

        if attempt < attempts - 1:
            delay = backoff[min(attempt, len(backoff) - 1)]
            time.sleep(delay)

    if last_error is not None:
        raise last_error
    raise TimeoutError("GET request failed after retries.")


urllib.request.urlopen = resilient_urlopen

# Register the optional evidence-strategy Discord extension before the main
# Discord client defines its event handlers.
try:
    from edge_system.discord_patch import install as _install_edge_discord
    _install_edge_discord()
except Exception as _edge_patch_error:
    print("Evidence strategy extension unavailable: %s" % _edge_patch_error)
