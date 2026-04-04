import socket
from ping3 import ping


def is_host_reachable(
    target: str,
    timeout: float = 2.0,
    icmp_attempts: int = 2,
) -> bool:
    """
    Check whether a target is valid and appears reachable.

    Logic:
    1. Validate target (DNS/IP resolution)
    2. Attempt ICMP ping (best effort)

    Returns:
        True  -> target is valid AND either responds to ICMP
        False -> target is invalid OR clearly unreachable

    Notes:
        - ICMP may be blocked by firewalls
        - A False result does NOT guarantee the host is offline
    """

    # 1. Validate target (DNS or IP)
    try:
        socket.getaddrinfo(target, None)
    except socket.gaierror:
        return False

    # 2. Best-effort ICMP check
    for _ in range(icmp_attempts):
        try:
            if ping(target, timeout=timeout) is not None:
                return True
        except Exception:
            pass

    # Target is valid but ICMP is blocked or host unreachable
    return False


def is_tcp_port_open(
    target: str,
    port: int,
    timeout: float = 1.0,
) -> bool:
    """
    Check whether a TCP port accepts connections.
    """
    try:
        with socket.create_connection((target, port), timeout=timeout):
            return True
    except OSError:
        return False
