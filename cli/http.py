"""Authenticated HTTP helpers for the Drift Assist API."""

import json
import sys
import urllib.error
import urllib.request
import uuid

from cli.config import resolve


RED = "\033[31m"
RESET = "\033[0m"


class ApiError(Exception):
    """An API failure with enough context for CLI-safe reporting."""

    def __init__(
        self,
        status: int | None,
        body: str,
        *,
        method: str | None = None,
        path: str | None = None,
        api: str | None = None,
        exit_code: int = 1,
        kind: str = "http",
    ) -> None:
        self.status = status
        self.body = body
        self.method = method
        self.path = path
        self.api = api
        self.exit_code = exit_code
        self.kind = kind
        super().__init__(self._message())

    def _message(self) -> str:
        if self.kind == "server":
            return f"Server not running on {self.api}. Start it with: assist start"
        if self.kind == "http":
            return (
                f"{self.method} {self.path} → HTTP {self.status}: "
                f"{self.body[:300]}"
            )
        if self.kind == "request":
            return f"Request to {self.api}{self.path} failed: {self.body}"
        return self.body


def server_not_running(api: str) -> ApiError:
    return ApiError(None, "", api=api, kind="server")


def report_error(error: ApiError) -> int:
    if error.kind in {"http", "request", "server"}:
        print(f"{RED}ERROR:{RESET} {error}", file=sys.stderr)
    else:
        print(error, file=sys.stderr)
    return error.exit_code


def _request(
    method: str,
    path: str,
    data: dict | None,
    timeout: int,
    raw_body=None,
    extra_headers: dict | None = None,
) -> dict:
    resolved = resolve()
    if resolved.token is None:
        token_path = resolved.home / "auth_token"
        raise ApiError(
            None,
            f"assist: auth_token not found at {token_path} — is the server installed?",
            exit_code=2,
            kind="auth",
            api=resolved.api,
            path=path,
        )

    headers = {"X-Assist-Token": resolved.token, **(extra_headers or {})}
    body = None
    if raw_body is not None:
        body = raw_body
    elif method == "POST":
        headers["Content-Type"] = "application/json"
        body = json.dumps({} if data is None else data).encode("utf-8")

    request = urllib.request.Request(
        resolved.api + path,
        data=body,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            response_body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise ApiError(
            exc.code,
            response_body,
            method=method,
            path=path,
            api=resolved.api,
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise server_not_running(resolved.api) from exc

    if not 200 <= status < 300:
        raise ApiError(
            status,
            response_body,
            method=method,
            path=path,
            api=resolved.api,
        )

    try:
        return json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise ApiError(
            None,
            f"Invalid JSON response: {exc}",
            method=method,
            path=path,
            api=resolved.api,
            kind="request",
        ) from exc


def get(path: str) -> dict:
    return _request("GET", path, None, timeout=10)


def post(path: str, data: dict | None = None) -> dict:
    return _request("POST", path, data, timeout=30)


def post_multipart(path: str, fields: dict, stream, size: int, timeout: int = 300) -> dict:
    """POST `fields` and one file part named "file", streaming the file in 1 MB chunks."""
    boundary = "assist-" + uuid.uuid4().hex
    head = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8")
        for key, value in fields.items()
    )
    # The part's filename is a constant: names travel in `fields`, so nothing a
    # caller chose (a quote, a trailing backslash) can break this header.
    head += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="upload.bin"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")

    # Exactly `size` bytes of the stream, never more, so the body always matches
    # Content-Length; a stream that ends early is an error, not a short body
    # the server would wait on.
    def body():
        yield head
        left = size
        while left > 0:
            chunk = stream.read(min(1024 * 1024, left))
            if not chunk:
                raise ValueError(f"upload stream ended {left} bytes short of {size}")
            left -= len(chunk)
            yield chunk
        yield tail

    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(head) + size + len(tail)),
    }
    return _request("POST", path, None, timeout, raw_body=body(), extra_headers=headers)
