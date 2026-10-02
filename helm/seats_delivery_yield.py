"""The pair-meld YIELD retirement, split out of seats_delivery (task/3743).

meld.recv reads a room independently of delivery, so a YIELD it has answered
must not replay at the next tool boundary. `mark_meld_yield_seen` retires that
one physical row on the wake cursor and leaves the delivery offset alone.

Every name it uses is resolved on seats_delivery AT CALL TIME, as it was when
the function lived there, so a patch of seats_delivery.X (or of seats.X, which
the facade fans out to seats_delivery) still reaches it.
"""
import json
import os


def mark_meld_yield_seen(room, seat, row):
    """Retire only the addressed physical YIELD this session saw in meld.recv.

    recv reads the room independently of delivery, so its answered YIELD must
    not replay at the next tool boundary or block stop. Keep the delivery
    offset untouched: older obligations and the next YIELD still belong to
    the boundary. A missing session, unreadable cursor or unlocatable row
    proves no consumption and leaves delivery unchanged.
    """
    from . import seats_delivery as sd     # at call time: no import cycle,
    #                                        and a patch of seats_delivery.X reaches it
    session = sd.home.session_id()
    if not session or not sd.recipient_matches(sd.seat_for_session(session), seat) \
            or not isinstance(row.get("id"), str) or not row["id"] \
            or not sd.deliverable(row, seat, room, ambient=False, beacon=True) \
            or not sd.recover_room_rotation(room):
        return False
    paths = sd._cursor_pair(room, seat, session)
    for path in paths:
        if sd._cursor(room, seat, session, beacon=path == paths[1]) is None \
                and os.path.exists(path):
            return False                 # unreadable is not a new cursor
    sd._init_cursor(room, seat, session, at_start=True)
    with sd._cursor_locks(paths, estate=False):
        delivery = sd._cursor(room, seat, session)
        wake = sd._cursor(room, seat, session, beacon=True)
        if delivery is None or wake is None:
            return False
        try:
            with open(sd.chat.room_path(room), "rb") as f:
                st = os.fstat(f.fileno())
                start = max(0, st.st_size - sd.SCAN_CAP)
                f.seek(start)
                if start:
                    f.readline()          # discard a possible partial first row
                matches = []
                while f.tell() < st.st_size:
                    off = f.tell()
                    data = f.readline(sd.SCAN_CAP)
                    if not data.endswith(b"\n"):
                        break
                    if json.loads(data) == row:
                        matches.append(off)
        except (OSError, ValueError):
            return False
        if len(matches) != 1:
            return False                 # ambiguous or outside the bounded tail
        token = sd._occurrence(st.st_dev, st.st_ino, matches[0])
        wake = dict(wake)
        wake["done"] = sorted(set(wake.get("done") or ()) | {token})
        held = set(wake.get("held") or ()) - {token}
        if held:
            wake["held"] = sorted(held)
        else:
            wake.pop("held", None)
        return sd._commit_cursor_updates({paths[1]: wake})
