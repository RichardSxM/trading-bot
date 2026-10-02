"""Which socket peers may name the client for the lockout (spec 0004, lockout key).

Docker's published port on the host's ``127.0.0.1`` delivers every host connection (``tailscale
serve``, a laptop browser) from the container's default gateway, not from loopback.
"""

import ipaddress
from typing import Final

_DEFAULT_ROUTE: Final = "00000000"


def default_gateway(route_table: str) -> str | None:
    """The IPv4 default gateway in ``/proc/net/route`` text, or None when there is none.

    Each row is ``Iface Destination Gateway ...`` with addresses as little endian hex, so the
    default route (destination ``00000000``) with gateway ``010012AC`` means ``172.18.0.1``.
    """
    for line in route_table.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 3 or fields[1] != _DEFAULT_ROUTE:
            continue
        try:
            packed = int(fields[2], 16).to_bytes(4, "little")
        except ValueError, OverflowError:
            return None
        gateway = ipaddress.IPv4Address(packed)
        return None if gateway.is_unspecified else str(gateway)
    return None
