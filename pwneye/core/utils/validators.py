import re

def validate_ip_or_domain(value: str) -> str:
    """
    Validate that the provided value is a valid IP address, fully qualified domain name (FQDN),
    or short hostname. Trailing dots are not allowed.

    :param value: The input string to validate.
    :return: The validated IP or domain.
    """
    ip_regex = re.compile(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$")
    hostname_regex = re.compile(
        r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.[A-Za-z0-9-]{1,63})*$"
    )

    if value.endswith("."):
        raise ValueError("Trailing dots are not allowed.")

    if ip_regex.match(value) or hostname_regex.match(value):
        return value

    raise ValueError("Must be a valid IP address or hostname.")

def validate_port(value: str) -> int:
    """
    Validate that the provided value is a valid TCP port.

    :param value: The input string to validate.
    :return: The validated port as an integer.
    """
    try:
        port = int(value)
    except ValueError:
        raise ValueError("must be an integer between 1 and 65535")

    if 1 <= port <= 65535:
        return port

    raise ValueError("must be an integer between 1 and 65535")