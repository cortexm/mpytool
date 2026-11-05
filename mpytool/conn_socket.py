"""MicroPython tool: socket connector"""

import socket as _socket
import ssl as _ssl
import mpytool.conn as _conn
import mpytool.utils as _utils


class ConnSocket(_conn.Conn):
    def __init__(self, address, log=None, tls=None):
        """Connect to a device over TCP, optionally wrapped in TLS

        Arguments:
            address: host or host:port (default port 23)
            log: logger
            tls: None for plain TCP, True for TLS with defaults, or a dict
                with any of: profile, cert, key, ca, insecure,
                check_hostname, key_password
        """
        super().__init__(log)
        self._socket = None
        if ':' in address:
            host, port = address.split(':')
            port = int(port)
        else:
            host = address
            port = 23
        if tls is not None:
            tls = _utils.resolve_tls(tls, log=self._log)
        proto = "tls" if tls is not None else "tcp"
        if log:
            log.info(f"Connecting to: {host}:{port} [{proto}]")
        sock = self._connect(host, port)
        if tls is not None:
            sock = self._handshake(sock, host, port, tls)
        sock.settimeout(None)
        sock.setblocking(False)
        self._socket = sock
        if log:
            log.info("connected")

    @staticmethod
    def _connect(host, port):
        """Open a blocking TCP connection"""
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        sock.settimeout(5)
        try:
            sock.connect((host, port))
        except _socket.timeout as err:
            sock.close()
            raise _conn.ConnError(f"Timeout connecting to {host}:{port}") from err
        except _socket.error as err:
            sock.close()
            raise _conn.ConnError(f"Cannot connect to {host}:{port}: {err}") from err
        return sock

    @staticmethod
    def _wrap_tls(sock, host, tls, verify=True):
        """Perform the TLS handshake on a connected socket

        An explicit CA replaces the system trust store rather than adding
        to it (create_default_context() skips load_default_certs() when
        cafile is given) - against a private PKI only that CA should be
        able to vouch for the device.
        """
        context = _ssl.create_default_context(cafile=tls.get('ca'))
        if not verify:
            context.check_hostname = False
            context.verify_mode = _ssl.CERT_NONE
        elif not tls.get('check_hostname', True):
            context.check_hostname = False
        if tls.get('cert'):
            context.load_cert_chain(
                tls['cert'], tls.get('key'), tls.get('key_password'))
        return context.wrap_socket(sock, server_hostname=host)

    def _handshake(self, sock, host, port, tls):
        """TLS handshake, downgrading verification errors when insecure

        The certificate is always verified. With 'insecure' a failure is
        reported as a warning and the connection is retried unverified -
        unlike a plain CERT_NONE handshake this still says what was wrong
        (expired, hostname mismatch, unknown CA).
        """
        try:
            return self._wrap_tls(sock, host, tls)
        except _ssl.SSLCertVerificationError as err:
            sock.close()
            reason = err.verify_message or str(err)
            if not tls.get('insecure'):
                raise _conn.ConnError(
                    f"TLS certificate verification failed: {reason}") from err
            self._log.warning(
                "TLS certificate verification failed: %s "
                "(continuing, --tls-insecure)", reason)
        except (_ssl.SSLError, OSError) as err:
            sock.close()
            raise _conn.ConnError(f"TLS error: {err}") from err
        # Verification failed but was downgraded - reconnect unverified,
        # the failed handshake already tore down the TCP connection
        sock = self._connect(host, port)
        try:
            return self._wrap_tls(sock, host, tls, verify=False)
        except (_ssl.SSLError, OSError) as err:
            sock.close()
            raise _conn.ConnError(f"TLS error: {err}") from err

    def close(self):
        super().close()
        if self._socket:
            self._socket.close()
            self._socket = None

    def __del__(self):
        self.close()

    @property
    def fd(self):
        return self._socket.fileno() if self._socket else None

    def _read_available(self):
        """Read available data from socket"""
        if self._socket is None:
            raise _conn.ConnError("Not connected")
        try:
            data = self._socket.recv(4096)
            if data:
                self._log.debug("RX: %r", data)
                return data
            # recv() returned b'' = peer closed connection
            raise _conn.ConnError("Connection closed")
        except (BlockingIOError, _ssl.SSLWantReadError):
            pass
        except _conn.ConnError:
            raise
        except OSError as err:
            raise _conn.ConnError(f"Connection lost: {err}") from err
        return None

    def _write_raw(self, data):
        """Write data to socket"""
        if self._socket is None:
            raise _conn.ConnError("Not connected")
        self._log.debug("TX: %r", data)
        try:
            return self._socket.send(data)
        except _ssl.SSLWantWriteError:
            return 0
        except OSError as err:
            raise _conn.ConnError(f"Connection lost: {err}") from err
