"""Resources owned by one appliance-tool invocation; no disk or boot policy."""
from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path


def start_server_thread(server):
    try:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
    except BaseException:
        server.server_close()
        raise
    return server, thread


class ApplianceSession:
    """Release only handles and temporary credentials created by this scope."""

    def __init__(self):
        self.process = self.client = self.server = self.thread = None
        self.stderr_stream = None
        self.key_paths = ()
        self.seed_to_remove = None

    def __enter__(self):
        return self

    def create_builder_key(self, seed_dir: Path, generate, *, remove_seed=False):
        private = seed_dir / "builder_ed25519"
        public = private.with_suffix(".pub")
        # A failure before generate returns may still create a partial key.
        # Refuse existing paths before claiming ownership of either filename.
        if any(path.exists() or path.is_symlink() for path in (private, public)):
            raise RuntimeError(f"refusing to reuse builder credentials: {seed_dir}")
        if remove_seed:
            try:
                seed_dir.mkdir(mode=0o700)
            except FileExistsError:
                # Existing seed content belongs to the caller.  Only the new
                # credentials below are ours to remove in that directory.
                if not seed_dir.is_dir():
                    raise
            else:
                self.seed_to_remove = seed_dir
        self.key_paths = (private, public)
        return generate(seed_dir)

    def start_server(self, factory, *args):
        self.server, self.thread = factory(*args)
        return self.server, self.thread

    def open_stderr(self, path: Path):
        self.stderr_stream = path.open("wb")
        return self.stderr_stream

    def start_process(self, argv, **kwargs):
        self.process = subprocess.Popen(argv, **kwargs)
        return self.process

    def own_client(self, client):
        self.client = client
        return client

    def close(self):
        errors = []

        def attempt(action):
            try:
                action()
            except BaseException as error:
                errors.append(error)

        if self.client is not None:
            attempt(self.client.close)
        if self.process is not None:
            def stop():
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=15)
            attempt(stop)
        # A single-threaded package server can be blocked writing to QEMU.
        # Stop this invocation's process before waiting for its server thread.
        if self.server is not None:
            attempt(self.server.shutdown)
            attempt(self.server.server_close)
        if self.thread is not None:
            attempt(lambda: self.thread.join(timeout=5))
        if self.stderr_stream is not None:
            attempt(self.stderr_stream.close)
        for path in self.key_paths:
            attempt(lambda path=path: path.unlink(missing_ok=True))
        if self.seed_to_remove is not None:
            attempt(lambda: shutil.rmtree(self.seed_to_remove, ignore_errors=True))
        if errors:
            raise errors[0]

    def __exit__(self, _type, error, _traceback):
        try:
            self.close()
        except BaseException as cleanup_error:
            if error is None:
                raise
            # Keep the original operation failure, but retain cleanup evidence.
            if hasattr(error, "add_note"):
                error.add_note(f"appliance resource cleanup failed: {cleanup_error}")
            else:
                raise cleanup_error from error
        return False
