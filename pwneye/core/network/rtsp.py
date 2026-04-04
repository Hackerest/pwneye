import base64
import hashlib
import os
import re
import subprocess
import threading
import time
import socket

from typing import Optional, Dict
from dataclasses import dataclass

from urllib.parse import urlparse

from pwneye.core.types import RtspProbeResult


@dataclass(frozen=True)
class RtspResponse:
    status_code: int
    reason: str
    headers: Dict[str, str]
    body: str

def build_rtsp_url(
    host: str,
    port: int = 554,
    path: str = "/",
    username: Optional[str] = None,
    password: Optional[str] = None,
    use_tcp: bool = True
) -> str:
    """
    Build RTSP URL from components.
    
    Args:
        host: Target host/IP
        port: RTSP port (default 554)
        path: Stream path
        username: Optional username
        password: Optional password
        use_tcp: Use TCP transport (more reliable)
        
    Returns:
        Complete RTSP URL
    """
    if (
        username is not None or password is not None
    ) and not (username == "" and password == ""):
        auth_username = username or ""
        auth_password = password or ""
        auth = f"{auth_username}:{auth_password}@"
    else:
        auth = ""
    
    return f"rtsp://{auth}{host}:{port}{path}"


def parse_rtsp_url(url: str) -> Dict[str, Optional[str]]:
    """
    Extract RTSP URL components without validation.
    """
    parsed = urlparse(url)

    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    return {
        "host": parsed.hostname,
        "port": parsed.port,
        "path": path,
        "username": parsed.username,
        "password": parsed.password,
    }

def add_rtsp_auth(
    rtsp_url: str,
    username: str,
    password: str,
) -> str:
    """
    Inject authentication credentials into an RTSP URL.

    Args:
        rtsp_url: Original RTSP URL (without credentials)
        username: RTSP username
        password: RTSP password

    Returns:
        RTSP URL with embedded credentials
    """
    parsed = urlparse(rtsp_url)

    host = parsed.hostname
    port = parsed.port or 554

    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    return f"rtsp://{username}:{password}@{host}:{port}{path}"

def _read_rtsp_response(sock: socket.socket) -> RtspResponse:
    """
    Read a complete RTSP response from an already connected socket.
    """
    data = b""

    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk

    if not data:
        raise OSError("Empty RTSP response")

    header_bytes, _, remaining = data.partition(b"\r\n\r\n")
    header_text = header_bytes.decode("utf-8", errors="ignore")
    header_lines = header_text.split("\r\n")

    if not header_lines:
        raise OSError("Invalid RTSP response")

    match = re.match(r"RTSP/\d+\.\d+\s+(\d+)\s*(.*)", header_lines[0])
    if not match:
        raise OSError("Malformed RTSP status line")

    headers: Dict[str, str] = {}
    for line in header_lines[1:]:
        if ":" not in line:
            continue

        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()

    content_length = int(headers.get("content-length", "0") or "0")
    body = remaining

    while len(body) < content_length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk

    return RtspResponse(
        status_code=int(match.group(1)),
        reason=match.group(2).strip(),
        headers=headers,
        body=body[:content_length].decode("utf-8", errors="ignore"),
    )

def _ensure_not_cancelled(stop_event: threading.Event | None) -> None:
    """
    Abort the current RTSP operation if a stop has been requested.
    """
    if stop_event is not None and stop_event.is_set():
        raise InterruptedError("RTSP probe interrupted")

def _recv_with_deadline(
    sock: socket.socket,
    size: int,
    deadline: float,
    stop_event: threading.Event | None = None,
    poll_interval: float = 0.2,
) -> bytes:
    """
    Receive a chunk while periodically checking whether the probe was cancelled.
    """
    while True:
        _ensure_not_cancelled(stop_event)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("RTSP receive timed out")

        sock.settimeout(min(poll_interval, remaining))

        try:
            return sock.recv(size)
        except socket.timeout:
            continue

def _read_rtsp_response_with_deadline(
    sock: socket.socket,
    deadline: float,
    stop_event: threading.Event | None = None,
) -> RtspResponse:
    """
    Read a complete RTSP response while supporting cooperative interruption.
    """
    data = b""

    while b"\r\n\r\n" not in data:
        chunk = _recv_with_deadline(sock, 4096, deadline, stop_event=stop_event)
        if not chunk:
            break
        data += chunk

    if not data:
        raise OSError("Empty RTSP response")

    header_bytes, _, remaining = data.partition(b"\r\n\r\n")
    header_text = header_bytes.decode("utf-8", errors="ignore")
    header_lines = header_text.split("\r\n")

    if not header_lines:
        raise OSError("Invalid RTSP response")

    match = re.match(r"RTSP/\d+\.\d+\s+(\d+)\s*(.*)", header_lines[0])
    if not match:
        raise OSError("Malformed RTSP status line")

    headers: Dict[str, str] = {}
    for line in header_lines[1:]:
        if ":" not in line:
            continue

        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()

    content_length = int(headers.get("content-length", "0") or "0")
    body = remaining

    while len(body) < content_length:
        chunk = _recv_with_deadline(sock, 4096, deadline, stop_event=stop_event)
        if not chunk:
            break
        body += chunk

    return RtspResponse(
        status_code=int(match.group(1)),
        reason=match.group(2).strip(),
        headers=headers,
        body=body[:content_length].decode("utf-8", errors="ignore"),
    )

def _build_rtsp_request(
    method: str,
    url: str,
    cseq: int,
    headers: Optional[Dict[str, str]] = None,
) -> bytes:
    """
    Build a raw RTSP request.
    """
    lines = [
        f"{method} {url} RTSP/1.0",
        f"CSeq: {cseq}",
        "User-Agent: pwneye/0.1.0",
    ]

    if headers:
        for key, value in headers.items():
            lines.append(f"{key}: {value}")

    lines.append("")
    lines.append("")

    return "\r\n".join(lines).encode()

def _send_rtsp_request(
    host: str,
    port: int,
    method: str,
    url: str,
    timeout: int,
    headers: Optional[Dict[str, str]] = None,
    stop_event: threading.Event | None = None,
) -> RtspResponse:
    """
    Send a single RTSP request and return the parsed response.
    """
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None

    while True:
        _ensure_not_cancelled(stop_event)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if last_error is not None:
                raise last_error
            raise socket.timeout("RTSP connection timed out")

        try:
            sock = socket.create_connection(
                (host, port),
                timeout=min(0.2, remaining),
            )
            break
        except socket.timeout as exc:
            last_error = exc
            continue

    with sock:
        _ensure_not_cancelled(stop_event)
        sock.sendall(_build_rtsp_request(method, url, cseq=1, headers=headers))
        return _read_rtsp_response_with_deadline(
            sock,
            deadline,
            stop_event=stop_event,
        )

def _parse_www_authenticate(header: str) -> tuple[str | None, Dict[str, str]]:
    """
    Parse a WWW-Authenticate header.
    """
    if not header:
        return None, {}

    scheme, _, rest = header.partition(" ")
    scheme = scheme.strip().lower() or None

    params: Dict[str, str] = {}
    for key, quoted, bare in re.findall(r'(\w+)=(?:"([^"]*)"|([^,]+))', rest):
        params[key.lower()] = (quoted or bare).strip()

    return scheme, params

def _build_basic_authorization(username: str, password: str) -> str:
    """
    Build a Basic authorization header value.
    """
    token = f"{username}:{password}".encode()
    return "Basic " + base64.b64encode(token).decode()

def _hash_md5(value: str) -> str:
    return hashlib.md5(value.encode()).hexdigest()

def _build_digest_authorization(
    username: str,
    password: str,
    method: str,
    uri: str,
    params: Dict[str, str],
) -> str | None:
    """
    Build a Digest authorization header value.
    """
    realm = params.get("realm")
    nonce = params.get("nonce")

    if not realm or not nonce:
        return None

    qop = params.get("qop")
    opaque = params.get("opaque")
    algorithm = params.get("algorithm", "MD5")

    if algorithm.upper() != "MD5":
        return None

    ha1 = _hash_md5(f"{username}:{realm}:{password}")
    ha2 = _hash_md5(f"{method}:{uri}")

    parts = [
        f'username="{username}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
        'algorithm="MD5"',
    ]

    if qop:
        qop_token = qop.split(",")[0].strip()
        if qop_token != "auth":
            return None

        cnonce = hashlib.md5(os.urandom(16)).hexdigest()[:16]
        nc = "00000001"
        response = _hash_md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop_token}:{ha2}")

        parts.extend([
            f'response="{response}"',
            f'qop="{qop_token}"',
            f'nc={nc}',
            f'cnonce="{cnonce}"',
        ])
    else:
        response = _hash_md5(f"{ha1}:{nonce}:{ha2}")
        parts.append(f'response="{response}"')

    if opaque:
        parts.append(f'opaque="{opaque}"')

    return "Digest " + ", ".join(parts)

def probe_rtsp_url(
    rtsp_url: str,
    *,
    method: str = "DESCRIBE",
    timeout: int = 5,
    stop_event: threading.Event | None = None,
) -> RtspProbeResult:
    """
    Probe an RTSP URL using a lightweight socket-based request.

    Authentication is handled inline when the target challenges with
    WWW-Authenticate Basic or Digest.
    """
    parsed = parse_rtsp_url(rtsp_url)
    host = parsed["host"]
    port = parsed["port"] or 554
    username = parsed["username"] or ""
    password = parsed["password"] or ""

    if not host:
        return RtspProbeResult(
            url=rtsp_url,
            status_code=None,
            reason="Invalid target",
            auth_scheme=None,
            credentials_valid=False,
            path_valid=False,
            stream_available=False,
            error="Missing RTSP host",
        )

    try:
        response = _send_rtsp_request(
            host=host,
            port=port,
            method=method,
            url=rtsp_url,
            timeout=timeout,
            headers={"Accept": "application/sdp"},
            stop_event=stop_event,
        )
    except (socket.timeout, socket.error, OSError) as exc:
        return RtspProbeResult(
            url=rtsp_url,
            status_code=None,
            reason="Connection error",
            auth_scheme=None,
            credentials_valid=False,
            path_valid=False,
            stream_available=False,
            error=str(exc),
        )

    if response.status_code == 200:
        return RtspProbeResult(
            url=rtsp_url,
            status_code=200,
            reason=response.reason,
            auth_scheme=None,
            credentials_valid=True,
            path_valid=True,
            stream_available=True,
        )

    if response.status_code == 401:
        auth_header = response.headers.get("www-authenticate", "")
        scheme, params = _parse_www_authenticate(auth_header)

        if not username and not password:
            return RtspProbeResult(
                url=rtsp_url,
                status_code=401,
                reason=response.reason,
                auth_scheme=scheme,
                credentials_valid=False,
                path_valid=False,
                stream_available=False,
            )

        if scheme == "basic":
            authorization = _build_basic_authorization(username, password)
        elif scheme == "digest":
            authorization = _build_digest_authorization(
                username=username,
                password=password,
                method=method,
                uri=rtsp_url,
                params=params,
            )
        else:
            authorization = None

        if authorization is None:
            return RtspProbeResult(
                url=rtsp_url,
                status_code=401,
                reason=response.reason,
                auth_scheme=scheme,
                credentials_valid=False,
                path_valid=False,
                stream_available=False,
                error="Unsupported WWW-Authenticate challenge",
            )

        try:
            authenticated = _send_rtsp_request(
                host=host,
                port=port,
                method=method,
                url=rtsp_url,
                timeout=timeout,
                headers={
                    "Accept": "application/sdp",
                    "Authorization": authorization,
                },
                stop_event=stop_event,
            )
        except (socket.timeout, socket.error, OSError) as exc:
            return RtspProbeResult(
                url=rtsp_url,
                status_code=None,
                reason="Connection error",
                auth_scheme=scheme,
                credentials_valid=False,
                path_valid=False,
                stream_available=False,
                error=str(exc),
            )

        if authenticated.status_code == 200:
            return RtspProbeResult(
                url=rtsp_url,
                status_code=200,
                reason=authenticated.reason,
                auth_scheme=scheme,
                credentials_valid=True,
                path_valid=True,
                stream_available=True,
            )

        if authenticated.status_code in {404, 454}:
            return RtspProbeResult(
                url=rtsp_url,
                status_code=authenticated.status_code,
                reason=authenticated.reason,
                auth_scheme=scheme,
                credentials_valid=True,
                path_valid=False,
                stream_available=False,
            )

        return RtspProbeResult(
            url=rtsp_url,
            status_code=authenticated.status_code,
            reason=authenticated.reason,
            auth_scheme=scheme,
            credentials_valid=False,
            path_valid=False,
            stream_available=False,
        )

    return RtspProbeResult(
        url=rtsp_url,
        status_code=response.status_code,
        reason=response.reason,
        auth_scheme=None,
        credentials_valid=response.status_code not in {401, 403},
        path_valid=response.status_code not in {404, 454},
        stream_available=response.status_code == 200,
    )

def probe_rtsp_url_with_ffprobe(
    rtsp_url: str,
    *,
    protocol: str = "tcp",
    timeout: int = 5,
) -> RtspProbeResult:
    """
    Probe an RTSP URL using ffprobe as a compatibility fallback.
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-rtsp_transport", protocol,
        "-timeout", str(timeout * 1_000_000),
        "-i", rtsp_url,
        "-show_entries", "stream=index",
    ]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout + 1,
        )
    except subprocess.TimeoutExpired:
        return RtspProbeResult(
            url=rtsp_url,
            status_code=None,
            reason="ffprobe timeout",
            auth_scheme=None,
            credentials_valid=False,
            path_valid=False,
            stream_available=False,
            error="ffprobe timeout",
        )

    stderr = (proc.stderr or "").lower()

    if proc.returncode == 0:
        return RtspProbeResult(
            url=rtsp_url,
            status_code=200,
            reason="OK",
            auth_scheme=None,
            credentials_valid=True,
            path_valid=True,
            stream_available=True,
        )

    if "401" in stderr and "unauthorized" in stderr:
        return RtspProbeResult(
            url=rtsp_url,
            status_code=401,
            reason="Unauthorized",
            auth_scheme=None,
            credentials_valid=False,
            path_valid=False,
            stream_available=False,
        )

    if "404" in stderr or "454" in stderr or "not found" in stderr:
        return RtspProbeResult(
            url=rtsp_url,
            status_code=404 if "404" in stderr else 454 if "454" in stderr else None,
            reason="Path not found",
            auth_scheme=None,
            credentials_valid=True,
            path_valid=False,
            stream_available=False,
        )

    return RtspProbeResult(
        url=rtsp_url,
        status_code=None,
        reason="ffprobe fallback",
        auth_scheme=None,
        credentials_valid=True,
        path_valid=False,
        stream_available=False,
        error=stderr.strip() or "ffprobe failed",
    )

def is_rtsp_port(host: str, port: int, timeout: int = 3) -> bool:
    """
    Check if a port supports RTSP protocol.
    
    Sends an RTSP OPTIONS request and checks for valid response.
    
    Args:
        host: Target host/IP
        port: Port to check
        timeout: Connection timeout in seconds
        
    Returns:
        True if port responds to RTSP, False otherwise
    """
    try:
        # Create socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        
        # Connect
        sock.connect((host, port))
        
        # Send RTSP OPTIONS request
        request = (
            f"OPTIONS rtsp://{host}:{port}/ RTSP/1.0\r\n"
            f"CSeq: 1\r\n"
            f"\r\n"
        )
        sock.sendall(request.encode())
        
        # Read response
        response = sock.recv(1024).decode('utf-8', errors='ignore')
        
        sock.close()
        
        # Check if response is RTSP
        return response.startswith('RTSP/1.0') or response.startswith('RTSP/2.0')
    except (socket.timeout, socket.error, ConnectionRefusedError, OSError):
        return False
    
def rtsp_credentials_valid(
    rtsp_url: str,
    protocol: str = "tcp",
    timeout: int = 5,
    retries: int = 1,
    retry_delay: float = 0.3,
) -> bool:
    """
    Determine whether RTSP credentials are valid.

    Logic:
    - 401 Unauthorized  -> credentials are invalid
    - timeout           -> retry (up to `retries`)
    - any other response -> credentials are likely valid
    """

    attempts = retries + 1

    for attempt in range(attempts):
        try:
            result = probe_rtsp_url(rtsp_url, timeout=timeout)
            if result.stream_available:
                return True

            if result.status_code == 401:
                return False

            if result.credentials_valid and result.path_valid is False:
                return True

            if result.error is None:
                return False

        except Exception:
            if attempt < attempts - 1:
                time.sleep(retry_delay)
                continue

    # All attempts timed out
    return False

def detect_banner(
    host: str,
    port: int = 554,
    timeout: int = 3,
) -> Optional[str]:
    """
    Send an RTSP OPTIONS request and try to extract the Server banner.

    Args:
        host: Target IP or hostname
        port: RTSP port
        timeout: Socket timeout in seconds

    Returns:
        Banner string if found, None otherwise
    """
    request = (
        f"OPTIONS rtsp://{host}:{port}/ RTSP/1.0\r\n"
        f"CSeq: 1\r\n"
        f"\r\n"
    )

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((host, port))
            sock.sendall(request.encode())
            response = sock.recv(2048).decode(errors="ignore")

        # Not an RTSP response at all
        if not response.startswith("RTSP/"):
            return None

        for line in response.splitlines():
            if line.lower().startswith("server:"):
                return line.split(":", 1)[1].strip()

        return None

    except (socket.timeout, socket.error, OSError):
        return None
