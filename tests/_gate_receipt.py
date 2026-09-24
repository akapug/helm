"""Test seams for receipts consumed as whole-suite authority."""
import contextlib
from unittest import mock

from helm import gate, gateauthority


@contextlib.contextmanager
def serial_process(ran=1):
    """Replace child execution without turning authority into a custom command.

    A caller-supplied argv records no interpreter and therefore cannot bind;
    this seam keeps the frozen serial argv and Helm-chosen interpreter intact.
    """
    noun = "test" if ran == 1 else "tests"
    stderr = "Ran %d %s in 0.0s\n\nOK\n" % (ran, noun)
    with mock.patch.object(gate, "SUITE", gateauthority.SERIAL_ARGV), \
            mock.patch.object(
                gate, "_queued_process", return_value=("", stderr, 0, None)):
        yield
