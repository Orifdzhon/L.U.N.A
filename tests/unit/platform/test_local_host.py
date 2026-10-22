import pytest

from luna.platform.browser import is_local_host


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST.",
        "api.localhost",
        "127.0.0.1",
        "127.1.2.3",
        "0.0.0.0",
        "10.0.0.1",
        "172.16.5.4",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",  # CGNAT
        "198.18.0.1",
        "224.0.0.1",  # multicast
        "239.255.255.250",
        "240.0.0.1",  # reserved
        "255.255.255.255",
        "2130706433",
        "0x7f.1",
        "::",
        "::1",
        "::127.0.0.1",  # IPv4-compatible
        "::10.0.0.1",
        "::ffff:127.0.0.1",  # IPv4-mapped
        "::ffff:7f00:1",
        "::ffff:10.0.0.1",
        "ff02::1",  # multicast
        "ff0e::1",
        "fec0::1",  # site-local
        "fe80::1",
        "fe80::1%eth0",
        "fc00::1",
        "fd12:3456::1",  # ULA
        "64:ff9b::7f00:1",  # NAT64 -> 127.0.0.1
        "64:ff9b::a00:1",  # NAT64 -> 10.0.0.1
        "64:ff9b::a9fe:a9fe",  # NAT64 -> 169.254.169.254
        "2002:7f00:1::",  # 6to4 -> 127.0.0.1
        "2002:c0a8:101::",  # 6to4 -> 192.168.1.1
    ],
)
def test_non_public_hosts_are_local(host: str) -> None:
    assert is_local_host(host)


@pytest.mark.parametrize(
    "host",
    [
        "example.com",
        "habr.com.",
        "93.184.216.34",
        "8.8.8.8",
        "2606:2800:220:1:248:1893:25c8:1946",
        "::ffff:8.8.8.8",
    ],
)
def test_public_hosts_are_not_local(host: str) -> None:
    assert not is_local_host(host)
