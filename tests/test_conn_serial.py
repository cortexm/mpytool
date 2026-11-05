"""Tests for ConnSerial (serial connection / reconnect handling)"""

import unittest
from unittest.mock import patch, MagicMock

import mpytool.conn as conn
from mpytool.conn_serial import ConnSerial

try:
    import termios
except ImportError:  # Windows
    termios = None


@unittest.skipIf(termios is None, "termios unavailable (non-Unix)")
class TestReconnectTermiosError(unittest.TestCase):
    """After a reset the device may re-enumerate under a new port name;
    reopening the stale node leaks a termios.error from pyserial. It must
    surface as a clean ConnError, not a raw traceback."""

    @patch('mpytool.conn_serial._serial.Serial')
    def test_reconnect_termios_error_becomes_connerror(self, mock_serial):
        mock_serial.return_value = MagicMock()  # initial open succeeds
        c = ConnSerial(port='/dev/ttyTEST')
        # every reopen now fails like a lingering, unconfigured USB-CDC node
        mock_serial.side_effect = termios.error(6, 'Device not configured')
        with self.assertRaises(conn.ConnError):
            c.reconnect(timeout=0.2)

    @patch('mpytool.conn_serial._serial.Serial')
    def test_initial_open_termios_error_becomes_connerror(self, mock_serial):
        mock_serial.side_effect = termios.error(6, 'Device not configured')
        with self.assertRaises(conn.ConnError):
            ConnSerial(port='/dev/ttyTEST')


class TestCloseThenReconnect(unittest.TestCase):
    """A delayed machine reset closes the port itself, before reconnecting."""

    @patch('mpytool.conn_serial._serial.Serial')
    def test_close_tolerates_port_already_gone(self, mock_serial):
        mock_serial.return_value.close.side_effect = OSError(6, 'gone')
        c = ConnSerial(port='/dev/ttyTEST')
        c.close()
        self.assertIsNone(c.fd)

    @patch('mpytool.conn_serial._time.sleep')
    @patch('mpytool.conn_serial._serial.Serial')
    def test_reconnect_reopens_closed_port(self, mock_serial, _sleep):
        c = ConnSerial(port='/dev/ttyTEST')
        self.assertTrue(c.can_reconnect)
        c.close()
        mock_serial.reset_mock()
        self.assertTrue(c.reconnect(timeout=1))
        mock_serial.assert_called_once_with(port='/dev/ttyTEST')
        self.assertIsNotNone(c.fd)


if __name__ == "__main__":
    unittest.main()
