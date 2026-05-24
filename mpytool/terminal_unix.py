"""MicroPython tool: Unix terminal (tty/termios/select)"""

import os as _os
import sys as _sys
import select as _select
import tty as _tty
import termios as _termios

from mpytool.terminal import TerminalBase


class Terminal(TerminalBase):

    def __init__(self, conn, log):
        super().__init__(conn, log)
        self._stdin_fd = _sys.stdin.fileno()
        self._orig_attr = _termios.tcgetattr(self._stdin_fd)

    def __del__(self):
        if self._orig_attr:
            _termios.tcsetattr(
                self._stdin_fd, _termios.TCSANOW, self._orig_attr)

    def _setup(self):
        _tty.setraw(self._stdin_fd)

    def _restore(self):
        if self._orig_attr:
            _termios.tcsetattr(
                self._stdin_fd, _termios.TCSANOW, self._orig_attr)
            self._orig_attr = None

    def read(self):
        return _sys.stdin.buffer.raw.read(1)

    def _flush_tx(self):
        """Write a partial chunk without blocking.

        Called only when the connection fd is writable, so os.write() writes
        as much as fits and returns immediately; the rest stays buffered for
        the next writable event.
        """
        if not self._tx_buf:
            return
        try:
            count = _os.write(self._conn.fd, self._tx_buf)
        except BlockingIOError:
            return
        if count:
            self._log.debug('TX: %r', bytes(self._tx_buf[:count]))
            del self._tx_buf[:count]

    def _loop(self):
        rd_fds = [self._stdin_fd, self._conn.fd]
        while self._running:
            # Only watch for writability while there is something to send,
            # otherwise select() would busy-loop on the always-writable fd.
            wr_fds = [self._conn.fd] if self._tx_buf else []
            readable, writable, _ = _select.select(rd_fds, wr_fds, [], 1)
            if self._stdin_fd in readable:
                self._read_event_terminal()
            if self._conn.fd in readable:
                self._read_event_device()
            if self._conn.fd in writable:
                self._flush_tx()
