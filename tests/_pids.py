"""The suite's dead-pid fixture, shared by every test that plants one.

A "dead" pid a test plants must be one the kernel can never hand out:
below pid_max a live process may hold it, and the asserting test then
flakily loses — the exact failure the train300 land gate hit on
test_a_DEAD_pid_cannot_wedge_the_room_shut.
"""

# Above the kernel's PID_MAX_LIMIT (2**22), so no live process can hold one.
DEAD_PID = 4194305
