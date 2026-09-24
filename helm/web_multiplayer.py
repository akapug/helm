"""Multiplayer projections for :mod:`helm.web`."""
import sys
# EXPLICIT, NOT INHERITED — the rule web_core already states one name at a
# time, applied to the whole family. Each name below reached this module
# ONLY through the globals() splice under this block, so it was bound when
# web.py had already been imported and ABSENT on a direct `from helm import
# <this module>`: a NameError at the first call, or a NameError swallowed by
# a fail-open. Measured in web_common, whose code_drift answered "no drift"
# from an unbound `time` — the half-live detector silenced by an import
# order. The binding is identical either way (web.py imports the same module
# object, and the facade fanout rebinds it over this one), so naming it here
# costs nothing and removes the ordering dependency.
import json
import os

_web = sys.modules.get(__package__ + ".web")
if _web is not None:
    globals().update({name: value for name, value in vars(_web).items()
                      if not (name.startswith("__") and name.endswith("__"))})



def _mp_actor():
    """The owner seat for cave writes/heartbeats — the same identity the chat
    post signs under (HELM_CELL_PROFILE else the derived owner handle), never
    the agent default."""
    return _chat_profile()



def _mp_caves():
    """Best-effort plaintext cave list. Cave dirs are hashed keys, but every
    relay doc header carries its cave name in cleartext — read one per dir. A
    presence-only cave (no doc yet) stays invisible; the demo always writes a
    doc, so a live cave is always discoverable."""
    from . import multiplayer
    root = multiplayer.multiplayer_dir()
    caves = set()
    try:
        cave_dirs = os.listdir(root)
    except OSError:
        return []
    for cd in cave_dirs:
        p = os.path.join(root, cd)
        try:
            names = os.listdir(p)
        except OSError:
            continue
        for n in names:
            if not n.endswith(".updates.jsonl"):
                continue
            try:
                with open(os.path.join(p, n), "rb") as f:
                    head = json.loads(f.readline().decode("utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(head, dict) and isinstance(head.get("cave"), str):
                caves.add(head["cave"])
                break  # one header names the cave; no need to read the rest
    return sorted(caves)



def _mp_pub(row, fields):
    """A copy of `row` with each identity/display field in `fields` display-
    laundered through chat._dsan — this wire's analog of chat.public_rows. A
    peer actor/connection/state or an envelope actor rides the BLIND relay from
    any client, so a bidi/control-char payload must never reach a consumer."""
    from . import chat
    c = dict(row)
    for k in fields:
        if isinstance(c.get(k), str):
            c[k] = chat._dsan(c[k])
    return c



def _api_mp_state(qs):
    """The relay read (open on loopback, like every GET): the blind relay's
    opaque update log after ?after=CURSOR + the live TTL peers + the discoverable
    caves. This endpoint NEVER decodes an update — it hands the CALLER the raw
    envelopes and lets that client fold the CRDT, exactly as the contract says."""
    try:
        from . import multiplayer
        relay, presence = multiplayer.adapters()
    except Exception:
        return {"unavailable": True}, 200
    cave = _q1(qs, "cave", "main") or "main"
    doc = _q1(qs, "doc", "board") or "board"
    after = _q1(qs, "after", "0")
    reset = False
    try:
        state = relay.updates(cave, doc, after)
    except ValueError as e:
        # a stale/mismatched cursor (the cave doc's generation rolled, or the
        # tmpfs was wiped) — restart the client from the head so its accumulated
        # log cannot silently desync. A bad cave/doc NAME is a real 400.
        if "cursor" not in str(e):
            return {"error": str(e)}, 400
        try:
            state = relay.updates(cave, doc, 0)
            reset = True
        except ValueError as e2:
            return {"error": str(e2)}, 400
    try:
        peers = presence.peers(cave)
    except ValueError:
        peers = []
    # DISPLAY-launder every identity/display field the SERVER emits, mirroring the
    # chat wire (public_rows/_dsan): peer actor/connection/state, the demo cell's
    # envelope actor, and the discoverable cave names all ride the BLIND relay
    # from any client. The opaque `update` stays RAW — the relay never decodes it,
    # and the browser folds the CRDT and launders the decoded cell at its render
    # seam (the server cannot, without breaking the blind-relay contract).
    from . import chat
    peers = [_mp_pub(p, ("actor", "connection", "state")) for p in peers]
    updates = [_mp_pub({"id": u.get("id"), "actor": u.get("actor"),
                        "ts": u.get("ts"),
                        "bytes": len(str(u.get("update", "")).encode("utf-8")),
                        "update": u.get("update")}, ("actor",))
               for u in state["updates"]]
    caves = [chat._dsan(c) for c in _mp_caves()]
    return {"cave": cave, "doc": doc, "cursor": state["cursor"], "reset": reset,
            "updates": updates, "peers": peers, "caves": caves}, 200



def _api_mp_publish(payload):
    """The one write: set a demo LWW cell as the owner. The reference encoder
    (helm.multiplayer_demo) builds the opaque string HERE so the CLI and this
    seam stay bit-identical, then hands the BLIND relay an undecoded update."""
    from . import multiplayer, multiplayer_demo
    cave = str(payload.get("cave") or "main")
    doc = str(payload.get("doc") or "board")
    key, value = payload.get("key"), payload.get("value")
    if not isinstance(key, str) or not key.strip():
        return {"error": "key is required"}, 400
    if not isinstance(value, str):
        return {"error": "value must be a string"}, 400
    relay, presence = multiplayer.adapters()
    actor = _mp_actor()
    try:
        update = multiplayer_demo.encode(key, value, actor)
        # relay.publish raises ValueError on the update/doc size caps — that is
        # bad INPUT (400), not a server fault (uncaught it would 500). (gate LOW)
        ack = relay.publish(cave, doc, actor, update)
    except ValueError as e:
        return {"error": str(e)}, 400
    # writing keeps the owner present without waiting for the next poll tick
    presence.heartbeat(cave, actor, "editing", multiplayer.DEFAULT_TTL,
                       MP_OWNER_CONNECTION)
    return {"ok": True, "ack": ack}, 200



def _api_mp_presence(payload):
    """The owner's presence heartbeat into the relay's TTL channel (mutation →
    bearer), for a bridge or a script that wants the OWNER to show up as a peer
    beside terminal actors. Presence is decoupled from the doc — this never
    touches the update log. NOTE the cockpit's OWN owner presence does not ride
    this: it is _owner_row, derived in-process from the cockpit's poll, and it
    lands on the ROSTER where the rest of the fleet already is."""
    from . import multiplayer
    cave = str(payload.get("cave") or "main")
    state = str(payload.get("state") or "watching")
    _relay, presence = multiplayer.adapters()
    row = presence.heartbeat(cave, _mp_actor(), state, multiplayer.DEFAULT_TTL,
                             MP_OWNER_CONNECTION)
    return {"ok": True, "peer": row}, 200
del _web
