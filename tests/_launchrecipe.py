"""A fixture launch.sh that states a real launch recipe (task/3695).

`helm seat resume` restores the recipe a seat's launch states, and REFUSES a
launch that states none, so a fixture's launch.sh must be one a helm mint
could have written: a stub such as `exec env FAKE=1 claude "$@"` states no
model, window or permission and is refused. This renders the line
`launch_line` itself mints for the seat, with the endpoint pinned (a
fixture's project seat holds none allocated yet), so every such arm resumes
exactly what helm would have launched.
"""
from unittest import mock

from helm import seat, seat_launch_assets

FIXTURE_PORT = 18317


def launch_sh(family, seat_name, room=None, multi=False, room_source=None):
    """The text of a helm-minted launch.sh for `seat_name`, today's recipe."""
    with mock.patch.object(seat_launch_assets, "_launch_endpoint",
                           return_value=FIXTURE_PORT):
        line = seat.launch_line(family, None, room, seat_name, multi=multi,
                                room_source=room_source)
    return "#!/bin/sh\nexec %s \"$@\"\n" % line
