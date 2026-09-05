"""MicroPython tool: Unix terminal (tty/termios/selectors)"""

import os as _os
import sys as _sys
import selectors as _selectors
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
        conn_fd = self._conn.fd
        with _selectors.DefaultSelector() as selector:
            selector.register(self._stdin_fd, _selectors.EVENT_READ)
            selector.register(conn_fd, _selectors.EVENT_READ)
            watch_write = False
            while self._running:
                # Only watch for writability while there is something to send,
                # otherwise select() would busy-loop on the always-writable fd.
                if bool(self._tx_buf) != watch_write:
                    watch_write = not watch_write
                    events = _selectors.EVENT_READ
                    if watch_write:
                        events |= _selectors.EVENT_WRITE
                    selector.modify(conn_fd, events)
                ready = {key.fd: mask for key, mask in selector.select(1)}
                if ready.get(self._stdin_fd, 0) & _selectors.EVENT_READ:
                    self._read_event_terminal()
                conn_events = ready.get(conn_fd, 0)
                if conn_events & _selectors.EVENT_READ:
                    self._read_event_device()
                if conn_events & _selectors.EVENT_WRITE:
                    self._flush_tx()
