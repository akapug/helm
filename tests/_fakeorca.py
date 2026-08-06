"""A one-shot orca runtime for transport tests: writes orca-runtime.json and
answers newline-delimited JSON on a real AF_UNIX socket, exactly as the daemon
does. A mock of the socket cannot show that the socket code degrades, so every
consumer (test_orcaadopt, test_codexhomes) drives this REAL transport. Callers
must pin ORCA_USER_DATA_PATH at the directory handed here — without it the
adapter falls back to $HOME/.config/orca and a unit test talks to the owner's
LIVE daemon."""
import json
import os
import socket
import threading


class FakeDaemon:
    """reply= the ok result body. raw= exact bytes instead (b"" = close
    without ever replying — the silent-drop shape). error= a {message, code}
    envelope answered as ok:false under the CALLER's request id (a raw blob
    cannot know the id, so id-correct error replies need this mode).
    hang=True reads the request then neither replies nor closes until the
    client gives up — the slow/wedged-daemon shape that must surface as a
    client-side timeout, not a connect error."""

    def __init__(self, tmp, reply=None, raw=None, token="tok", hang=False,
                 error=None):
        self.dir = tmp
        self.sock_path = os.path.join(tmp, "d.sock")
        self.reply, self.raw, self.token = reply, raw, token
        self.hang, self.error = hang, error
        self.seen = []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.sock_path)
        self.srv.listen(4)
        with open(os.path.join(tmp, "orca-runtime.json"), "w") as f:
            json.dump({"runtimeId": "rt1", "authToken": token,
                       "transports": [{"kind": "unix",
                                       "endpoint": self.sock_path}]}, f)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            with conn:
                try:
                    data = conn.recv(65536)
                    if not data:
                        continue
                    req = json.loads(data.decode().splitlines()[0])
                    self.seen.append(req)
                    if self.hang:
                        conn.recv(1)   # blocks until the CLIENT times out
                        continue
                    if self.raw is not None:
                        conn.sendall(self.raw)
                        continue
                    if self.error is not None:
                        conn.sendall((json.dumps({
                            "id": req.get("id"), "ok": False,
                            "error": self.error,
                            "_meta": {"runtimeId": "rt1"}}) + "\n").encode())
                        continue
                    conn.sendall((json.dumps({
                        "id": req.get("id"), "ok": True,
                        "result": self.reply or {},
                        "_meta": {"runtimeId": "rt1"}}) + "\n").encode())
                except (OSError, ValueError):
                    continue

    def close(self):
        """Closed means UNREACHABLE: srv.close() alone does not cut off new
        connects while _serve blocks in accept() (the fd's kernel listener
        survives the in-syscall reference — measured 2026-08-04: a client
        connected and got answered after close), so the socket PATH is
        unlinked too, the shape a vanished daemon leaves behind."""
        try:
            self.srv.close()
        except OSError:
            pass
        try:
            os.unlink(self.sock_path)
        except OSError:
            pass
