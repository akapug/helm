#!/usr/bin/env python3
"""One proxy-runtime PROOF constructor, shared by every suite that needs one.

A proxywatch runtime entry is only authority when its immutable proof
re-derives to the recorded runtime for that exact session — that is what
`seats_runtime._verified_exact_runtime` checks and what
`proxywatch._proxy_proof_runtime` computes. So any suite asserting anything
about runtime authority needs a proof of THE SHAPE PRODUCTION WRITES, and a
hand-written one is the classic wrong-richness fixture: it passes its own
arms while the live reader rejects it.

This module exists so those suites share ONE constructor without importing
each other. It is a PURE FUNCTION — no environment, no filesystem, no
import-time side effects — so importing it cannot couple a suite to another
suite's temp home, roster or collection order.
"""


def runtime_proof(session="session-ds4pro", observed=1000, route=None):
    """The proof shape production stamps, defaulting to a ds4pro route.

    `route` is the catalog route the proof claims; a caller binding a
    specific family passes that family's own route so the derived runtime
    resolves to it and to nothing else.
    """
    return {"v": 2, "session": session, "agent_harness": "claude",
            "agent_pid": 4101,
            "agent_starttime": 701, "model": (route or {}).get("alias", "ds4-pro"),
            "local_base_url": "http://127.0.0.1:8360",
            "proxy_pid": 4201, "proxy_identity": "proc:702",
            "proxy_config": "/safe/config.yaml", "config_sha256": "a" * 64,
            "route": route or {"alias": "ds4-pro", "provider": "opencode-go",
                                "upstream_model": "deepseek-v4-pro",
                                "base_url": "https://opencode.ai/zen/go/v1"},
            "observed_at": observed,
            "canary": {"state": "HEALTHY", "status": 200}}
