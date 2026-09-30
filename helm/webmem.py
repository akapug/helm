#!/usr/bin/env python3
"""THE ALLOCATOR BOUND EVERY `helm web` RUNS UNDER (task/3715).

THE MEASUREMENT. The owner's console (the helm-web unit) reached its 2 GB
MemoryHigh every hour, in 40+ consecutive hourly lives, and spilled 1.3-1.9
GB more into swap each time. On a console 57 minutes old about
1.35 GB sat in glibc's per-thread arena heaps, most of it swapped: memory
the program had freed and the allocator kept. A lane's ad hoc `helm web`,
which runs under no unit and so under no memory limit at all, reached 4.5 GB
in about 54 minutes behind one open tab.

WHY THE ALLOCATOR KEEPS IT. `helm web` is a ThreadingHTTPServer: a thread
per request. glibc gives threads that allocate at the same time their own
arenas (up to eight per core), and its mmap threshold is DYNAMIC: after a
large mapped block is freed, the threshold rises to that block's size, so
the next buffer of that size (a room file read, a ledger snapshot) comes
from an arena heap instead, and stays there when it is freed. Two knobs
bound that:
  MALLOC_MMAP_THRESHOLD_=131072  a FIXED threshold. Every block of 128 KiB
      or more is its own mapping, handed back to the kernel when it is
      freed. A probe of the pattern went from 527 MB to 226 MB on this alone.
  MALLOC_ARENA_MAX=2             at most two arenas, whatever the thread
      count, so at most two heaps can keep freed memory. Alone it barely
      helped the probe (527 MB to 493 MB).
Refuted, and not built: an RSS cap, which kills a healthy server during its
~1.1 GB warm-up, and MemorySwapMax, which on this zram host would keep cold
pages uncompressed in RAM and throttle the owner's console every hour.

GLIBC READS THEM ONCE, WHEN THE PROCESS STARTS, so they must be wherever a
start of `helm web` finds them. There are three such places, and KNOBS is
the one source for all three:
  * the helm-web unit, through a drop-in (`unit_dropin`) that
    `helm web unit --install` writes. It reloads the user manager and
    restarts nothing: the next start of the unit carries them;
  * this process's environment (`apply`). `os.execv` hands it to the next
    image, so every re-exec onto a changed tree
    (stopfacts_resident.Follower.reexec) starts with them;
  * the life already running, through mallopt(3) (`apply`), because a
    `helm web` started by hand, and the first life after the land that
    shipped this, began without them.
An operator's own value in the process environment wins over helm's in the
second and third. THE UNIT IS THE EXCEPTION: systemd reads a drop-in after
the unit file, and the later `Environment=` of one variable wins, so the
drop-in replaces a value set in the unit file itself. An operator's own value
for the unit goes in a drop-in that sorts after `allocator.conf`
(`systemctl --user edit helm-web` writes `override.conf`).
The variables are inherited by the git and python children a server runs,
which is harmless.
"""
import os
import sys

#: (variable, helm's value, the mallopt(3) parameter that sets it in a
#: running process: M_ARENA_MAX and M_MMAP_THRESHOLD, from glibc's malloc.h).
KNOBS = (("MALLOC_ARENA_MAX", "2", -8),
         ("MALLOC_MMAP_THRESHOLD_", "131072", -3))

#: The drop-in's file name inside the unit's `.service.d` directory. Its
#: own file, so an operator's other drop-ins (the unit's memory limits) are
#: never read or rewritten by the installer.
DROPIN = "allocator.conf"


def _mallopt(param, value):
    """mallopt(3) in this process -> True, False (refused), or None when
    this C library has no mallopt to call."""
    import ctypes
    fn = getattr(ctypes.CDLL(None), "mallopt", None)
    if fn is None:
        return None
    fn.argtypes = (ctypes.c_int, ctypes.c_int)
    fn.restype = ctypes.c_int
    return bool(fn(param, value))


def apply(environ=None, mallopt=None):
    """Bound this process's allocator -> {variable: (value, applied)}.

    Each knob the environment does not already carry is set in it, which is
    what a re-exec inherits; then every knob's value is applied to this life
    through mallopt. `applied` is mallopt's answer: True, False when it
    refused the value, None when there is no mallopt. It never raises: a
    server that could not bound its allocator still serves."""
    env = os.environ if environ is None else environ
    call = _mallopt if mallopt is None else mallopt
    out = {}
    for name, value, param in KNOBS:
        env.setdefault(name, value)
        try:
            done = call(param, int(env[name]))
        except (ValueError, OSError, AttributeError, TypeError):
            done = False
        out[name] = (env[name], done)
    return out


def describe(applied):
    """The one start line naming the bound, for the server's log."""
    return "helm web: allocator bounded (task/3715): " + ", ".join(
        "%s=%s%s" % (name, value, "" if done else
                     " (environment only: mallopt %s)"
                     % ("absent" if done is None else "refused"))
        for name, (value, done) in applied.items())


def unit_dropin(unit_dir=None):
    """(path, text) of the helm-web unit's drop-in carrying KNOBS."""
    from . import rearm, timerhealth
    base = unit_dir or timerhealth.user_unit_dir()
    path = os.path.join(base, rearm.WEB_UNIT + ".service.d", DROPIN)
    text = ("# Written by `helm web unit --install` (helm/webmem.py, "
            "task/3715):\n"
            "# the allocator bound every `helm web` also sets on itself.\n"
            "[Service]\n"
            + "".join("Environment=%s=%s\n" % (name, value)
                      for name, value, _param in KNOBS))
    return path, text


def _unloaded(timeout):
    """True when the user manager says the helm-web unit needs a
    daemon-reload: a unit file or drop-in on disk that it has not loaded.
    Any other answer, or none, is False; this only ever ADDS a reload."""
    import shutil
    import subprocess
    from . import rearm
    if not shutil.which("systemctl"):
        return False
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", rearm.WEB_UNIT + ".service",
             "--property=NeedDaemonReload", "--value"],
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return not result.returncode and (result.stdout or "").strip() == "yes"


def install(timeout=30):
    """(ok, detail): write the drop-in and reload the user manager.

    IT RESTARTS NOTHING. The running console already bounds itself in
    process; the unit's next start (its hourly RuntimeMaxSec expiry, or an
    operator's restart) is the first that reads the drop-in.

    AN UNCHANGED FILE IS NOT A LOADED ONE. An unchanged drop-in is not
    rewritten, and the manager is reloaded for it only when it says it has not
    loaded it (`NeedDaemonReload`): an earlier run whose reload failed (no user
    bus, say) left the file written and the manager on the old unit, and
    "unchanged" would otherwise report that as done."""
    import shutil
    import subprocess
    from . import pk
    path, text = unit_dropin()
    try:
        with open(path, encoding="utf-8") as fh:
            unchanged = fh.read() == text
    except OSError:
        unchanged = False
    if unchanged:
        if not _unloaded(timeout):
            return True, "unchanged: %s" % path
        done = "found %s unchanged but not loaded" % path
    else:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            pk.atomic_write(path, text)
        except OSError as exc:
            return False, "cannot write %s (%s)" % (path, exc)
        done = "wrote %s" % path
    if not shutil.which("systemctl"):
        return False, ("%s, but systemctl is unavailable, so the user "
                       "manager was not reloaded" % done)
    try:
        result = subprocess.run(["systemctl", "--user", "daemon-reload"],
                                capture_output=True, encoding="utf-8",
                                errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, "%s; daemon-reload failed (%s)" % (done, exc)
    if result.returncode:
        return False, "%s; daemon-reload exited %d (%s)" % (
            done, result.returncode, (result.stderr or "").strip()[:200])
    return True, ("%s and reloaded the user manager; the unit's next "
                  "start carries it, and a running `helm web` already bounds "
                  "itself in process" % done)


def cmd_unit(args):
    """web unit [--install] — print the helm-web unit's allocator drop-in,
    or write it and reload the user manager (never a restart)."""
    if list(args) not in ([], ["--install"]):
        print("usage: helm web unit [--install]", file=sys.stderr)
        return 2
    path, text = unit_dropin()
    if not args:
        print("# %s" % path)
        print(text, end="")
        print("# `helm web unit --install` writes it and reloads the user "
              "manager; it restarts nothing.")
        return 0
    ok, detail = install()
    print("helm web unit: %s" % detail, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1
