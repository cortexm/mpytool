"""MicroPython tool: terminal connection

Base class with common terminal functionality.
Platform-specific implementations in terminal_unix.py and terminal_win.py.
"""

import sys as _sys

AVAILABLE = False


class TerminalBase:
    """Common terminal functionality for interactive REPL"""

    def __init__(self, conn, log):
        self._log = log
        self._conn = conn
        self._running = None
        # Pending keystrokes to send to device. Buffered here and flushed only
        # when the connection fd is writable, so a blocking write (e.g. device
        # not draining USB-CDC) never stalls the loop and breaks Ctrl+] exit.
        self._tx_buf = bytearray()

    def _setup(self):
        """Set terminal to raw mode (platform-specific)"""
        raise NotImplementedError

    def _restore(self):
        """Restore original terminal mode (platform-specific)"""
        raise NotImplementedError

    def read(self):
        """Read input from keyboard (platform-specific)"""
        raise NotImplementedError

    def _loop(self):
        """Main event loop (platform-specific)"""
        raise NotImplementedError

    def write(self, buf):
        _sys.stdout.buffer.raw.write(buf)

    def _read_event_terminal(self):
        data = self.read()
        self._log.debug('TRM: %s', data)
        if 0x1d in data:  # CTRL + ]
            self._running = False
            return
        if data:
            self._tx_buf += data

    def _flush_tx(self):
        """Send buffered keystrokes to device (blocking write).

        Platforms that gate this on fd writability avoid blocking; the base
        implementation is used where writability can't be polled.
        """
        if self._tx_buf:
            self._conn.write(bytes(self._tx_buf))
            del self._tx_buf[:]

    def _read_event_device(self):
        data = self._conn.read()
        self._log.debug('DEV: %s', data)
        if data:
            self.write(data)

    def _flush_device(self):
        data = self._conn.flush()
        if data:
            self.write(data)

    def run(self):
        self._setup()
        self._running = True
        try:
            self._flush_device()
            self._loop()
        except OSError as err:
            self._log.error(err)
        finally:
            self._restore()
            self.write(b'\r\n')


try:
    from mpytool.terminal_unix import Terminal
    AVAILABLE = True
except ImportError:
    try:
        from mpytool.terminal_win import Terminal
        AVAILABLE = True
    except ImportError:
        pass
