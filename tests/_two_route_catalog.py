#!/usr/bin/env python3
"""The TWO-ROUTE pool entry the multi-route arms are written against.

The catalog declares one token source per seat: ds4pro's pool lists the
DeepSeek direct key and the OpenCode Go subscription, and a seat is minted
with ONE of them. The machinery for a config carrying both still ships
(eligibility over every catalogued route, the canary-selected credential, the
per-block cost rung), so the arms that pin it run against this entry: ds4pro
as one alias served by two vendors with the flat one default, patched into
FAMILIES under the same name for the life of one test.

NO BILLING WINDOW on either row, deliberately: the off-peak gate reads the
wall clock, and these arms are about routes, not about the hour they run at.
`pin_off_peak` is the other half of that: an arm that plans a ds4pro config
and is not about the hour pins the gate's clock to a Saturday. Pure functions
plus two patch helpers — no import-time side effects.
"""
from unittest import mock

FAMILY = "ds4pro"  # noqa: SEAT_NAME — the catalog family whose entry is patched

OPENCODE_GO = {"base_url": "https://opencode.ai/zen/go/v1",
               "upstream_model": "deepseek-v4-pro",
               "rung": "free", "authstore": "opencode-go"}
DEEPSEEK_DIRECT = {"proxy_provider": "deepseek-direct",
                   "base_url": "https://api.deepseek.com/v1",
                   "upstream_model": "deepseek-v4-pro",
                   "rung": "paid", "authstore": "deepseek"}


def two_route_entry(families):
    """ds4pro's live entry with both vendors in its pool, flat one default."""
    return dict(families[FAMILY], pool_default="opencode-go",
                pool_providers={"opencode-go": dict(OPENCODE_GO),
                                "deepseek": dict(DEEPSEEK_DIRECT)})


def patch_two_routes(test):
    """Patch the two-route entry into FAMILIES until ``test`` cleans up."""
    from helm import seat, seat_catalog  # noqa: F401 — the facade seeds the impl
    patch = mock.patch.dict(seat_catalog.FAMILIES, {
        FAMILY: two_route_entry(seat_catalog.FAMILIES)})
    patch.start()
    test.addCleanup(patch.stop)


#: A Saturday noon, UTC: off-peak for every declared billing window.
OFF_PEAK = 1790424000.0


def pin_off_peak(test, at=OFF_PEAK):
    """Pin the off-peak gate's clock for an arm that is not about the hour.

    ds4pro's only route is gated by DeepSeek's peak window, so any arm that
    plans a ds4pro config against the WALL clock would change its answer
    seven hours of every weekday — a test that reads live state it did not
    plant."""
    patch = mock.patch("helm.offpeak.now", return_value=at)
    clock = mock.patch("helm.offpeak.clock_error", return_value=None)
    patch.start()
    clock.start()
    test.addCleanup(clock.stop)
    test.addCleanup(patch.stop)
