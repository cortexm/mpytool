"""Unit tests for ConnSocket (plain TCP and SSL)

Run tests:
    python -m unittest tests.test_conn_socket -v
"""

import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from mpytool.conn_socket import ConnSocket
from mpytool.conn import ConnError


# Check if openssl is available
OPENSSL_AVAILABLE = shutil.which('openssl') is not None


def create_test_certs(tmpdir):
    """Create self-signed certificate and key for testing"""
    cert_file = os.path.join(tmpdir, 'server.pem')
    key_file = os.path.join(tmpdir, 'server.key')
    # Generate self-signed certificate (valid for 1 day)
    subprocess.run([
        'openssl', 'req', '-x509', '-newkey', 'rsa:2048',
        '-keyout', key_file, '-out', cert_file,
        '-days', '1', '-nodes',
        '-subj', '/CN=localhost'
    ], check=True, capture_output=True)
    return cert_file, key_file


def create_test_pki(tmpdir):
    """Create a CA plus a server and a client certificate signed by it

    Returns:
        (ca_cert, (server_cert, server_key), (client_cert, client_key))
    """
    ca_cert = os.path.join(tmpdir, 'ca.pem')
    ca_key = os.path.join(tmpdir, 'ca.key')
    # OpenSSL 3.x refuses a CA without explicit basicConstraints/keyUsage
    subprocess.run([
        'openssl', 'req', '-x509', '-newkey', 'rsa:2048',
        '-keyout', ca_key, '-out', ca_cert, '-days', '1', '-nodes',
        '-subj', '/CN=mpytool-test-CA',
        '-addext', 'basicConstraints=critical,CA:TRUE',
        '-addext', 'keyUsage=critical,keyCertSign,cRLSign',
    ], check=True, capture_output=True)
    san_file = os.path.join(tmpdir, 'san.ext')
    with open(san_file, 'w') as file:
        file.write('subjectAltName=DNS:localhost,IP:127.0.0.1\n')

    def sign(name, extfile=None):
        cert = os.path.join(tmpdir, f'{name}.crt')
        key = os.path.join(tmpdir, f'{name}.key')
        csr = os.path.join(tmpdir, f'{name}.csr')
        subprocess.run([
            'openssl', 'req', '-newkey', 'rsa:2048', '-keyout', key,
            '-out', csr, '-nodes', '-subj', f'/CN={name}',
        ], check=True, capture_output=True)
        cmd = [
            'openssl', 'x509', '-req', '-in', csr, '-CA', ca_cert,
            '-CAkey', ca_key, '-CAcreateserial', '-out', cert, '-days', '1']
        if extfile:
            cmd += ['-extfile', extfile]
        subprocess.run(cmd, check=True, capture_output=True)
        return cert, key

    return ca_cert, sign('localhost', san_file), sign('client')


class SSLEchoServer:
    """Simple SSL echo server for testing

    With client_ca set the server requires a client certificate signed by
    that CA and records the common name it saw in peer_cn.
    """

    def __init__(self, cert_file, key_file, port=0, client_ca=None):
        self.cert_file = cert_file
        self.key_file = key_file
        self.port = port
        self.client_ca = client_ca
        self.peer_cn = None
        self._server = None
        self._thread = None
        self._running = False

    def start(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert_file, self.key_file)
        if self.client_ca:
            context.verify_mode = ssl.CERT_REQUIRED
            context.load_verify_locations(self.client_ca)

        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(('127.0.0.1', self.port))
        self._server.listen(5)
        self.port = self._server.getsockname()[1]
        self._running = True

        def serve():
            while self._running:
                try:
                    self._server.settimeout(0.5)
                    try:
                        client, addr = self._server.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        break  # Server closed
                    try:
                        ssl_client = context.wrap_socket(client, server_side=True)
                        try:
                            peer = ssl_client.getpeercert()
                            if peer:
                                self.peer_cn = dict(
                                    item[0] for item in peer['subject']
                                )['commonName']
                            data = ssl_client.recv(1024)
                            if data:
                                ssl_client.send(data)
                        finally:
                            ssl_client.close()
                    except ssl.SSLError:
                        # Client disconnected or handshake failed
                        client.close()
                except Exception:
                    pass  # Ignore errors, keep serving

        self._thread = threading.Thread(target=serve, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._server:
            self._server.close()
        if self._thread:
            self._thread.join(timeout=2)


class PlainEchoServer:
    """Simple plain TCP echo server for testing"""

    def __init__(self, port=0):
        self.port = port
        self._server = None
        self._thread = None
        self._running = False

    def start(self):
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(('127.0.0.1', self.port))
        self._server.listen(5)
        self.port = self._server.getsockname()[1]
        self._running = True

        def serve():
            while self._running:
                try:
                    self._server.settimeout(0.5)
                    try:
                        client, addr = self._server.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    try:
                        data = client.recv(1024)
                        if data:
                            client.send(data)
                    finally:
                        client.close()
                except Exception:
                    pass

        self._thread = threading.Thread(target=serve, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._server:
            self._server.close()
        if self._thread:
            self._thread.join(timeout=2)


# =============================================================================
# Plain TCP tests (no SSL)
# =============================================================================

class TestConnSocketPlain(unittest.TestCase):
    """Test plain TCP connection (no SSL)"""

    @classmethod
    def setUpClass(cls):
        cls.server = PlainEchoServer()
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def test_plain_connection(self):
        """Plain TCP connection works"""
        conn = ConnSocket(f'127.0.0.1:{self.server.port}')
        try:
            conn.write(b'hello')
            # Give server time to echo back
            time.sleep(0.1)
            data = conn._read_available()
            self.assertEqual(data, b'hello')
        finally:
            conn.close()

    def test_connection_refused(self):
        """Connection to closed port raises ConnError"""
        with self.assertRaises(ConnError) as ctx:
            ConnSocket('127.0.0.1:1')  # Port 1 should be closed
        self.assertIn('Cannot connect', str(ctx.exception))


# =============================================================================
# SSL tests
# =============================================================================

@unittest.skipUnless(OPENSSL_AVAILABLE, "openssl not available")
class TestConnSocketSSL(unittest.TestCase):
    """Test SSL/TLS connection"""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        cls.cert_file, cls.key_file = create_test_certs(cls.tmpdir)
        cls.server = SSLEchoServer(cls.cert_file, cls.key_file)
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_tls_insecure(self):
        """Verification failure is downgraded to a warning"""
        conn = ConnSocket(
            f'127.0.0.1:{self.server.port}', tls={'insecure': True})
        try:
            conn.write(b'hello ssl')
            time.sleep(0.1)
            data = conn._read_available()
            self.assertEqual(data, b'hello ssl')
        finally:
            conn.close()

    def test_tls_with_ca(self):
        """TLS connection with custom CA certificate"""
        # Use localhost to match CN in certificate
        conn = ConnSocket(
            f'localhost:{self.server.port}', tls={'ca': self.cert_file})
        try:
            conn.write(b'hello ca')
            time.sleep(0.1)
            data = conn._read_available()
            self.assertEqual(data, b'hello ca')
        finally:
            conn.close()

    def test_tls_verify_fails_without_ca(self):
        """Self-signed certificate is refused without --tls-insecure"""
        with self.assertRaises(ConnError) as ctx:
            ConnSocket(f'127.0.0.1:{self.server.port}', tls=True)
        self.assertIn('certificate verification failed', str(ctx.exception))

    def test_tls_true_and_empty_dict_equivalent(self):
        """tls={} means TLS with defaults, not plain TCP"""
        with self.assertRaises(ConnError) as ctx:
            ConnSocket(f'127.0.0.1:{self.server.port}', tls={})
        self.assertIn('certificate verification failed', str(ctx.exception))

    def test_tls_invalid_ca_file(self):
        """TLS connection with invalid CA file raises error"""
        with self.assertRaises(ConnError) as ctx:
            ConnSocket(
                f'127.0.0.1:{self.server.port}',
                tls={'ca': '/nonexistent/ca.pem'})
        self.assertIn('TLS error', str(ctx.exception))

    def test_tls_no_hostname_check(self):
        """Certificate is verified but the hostname is not"""
        # Use 127.0.0.1 but cert has CN=localhost
        conn = ConnSocket(
            f'127.0.0.1:{self.server.port}',
            tls={'ca': self.cert_file, 'check_hostname': False})
        try:
            conn.write(b'hello no-hostname')
            time.sleep(0.1)
            data = conn._read_available()
            self.assertEqual(data, b'hello no-hostname')
        finally:
            conn.close()

    def test_unknown_tls_option_rejected(self):
        """A typo in the tls dict must not pass silently"""
        with self.assertRaises(ValueError):
            ConnSocket(
                f'127.0.0.1:{self.server.port}', tls={'insecrue': True})


# =============================================================================
# Mutual TLS tests
# =============================================================================

@unittest.skipUnless(OPENSSL_AVAILABLE, "openssl not available")
class TestConnSocketMutualTLS(unittest.TestCase):
    """Test client certificate authentication"""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        cls.ca, server, client = create_test_pki(cls.tmpdir)
        cls.client_cert, cls.client_key = client
        cls.server = SSLEchoServer(*server, client_ca=cls.ca)
        cls.server.start()
        # Certificate store: a profile directory and a combined bundle
        cls.store = os.path.join(cls.tmpdir, 'cfg', 'certs')
        os.makedirs(os.path.join(cls.store, 'board'))
        for src, name in (
                (cls.client_cert, 'cert.pem'),
                (cls.client_key, 'key.pem'),
                (cls.ca, 'ca.pem')):
            dst = os.path.join(cls.store, 'board', name)
            shutil.copyfile(src, dst)
            os.chmod(dst, 0o600)
        bundle = os.path.join(cls.store, 'lab.pem')
        with open(bundle, 'wb') as out:
            for src in (cls.client_cert, cls.client_key):
                with open(src, 'rb') as file:
                    out.write(file.read())
        os.chmod(bundle, 0o600)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        self.server.peer_cn = None
        patcher = patch.dict(
            os.environ,
            {'MPYTOOL_CONFIG_DIR': os.path.join(self.tmpdir, 'cfg')})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _echo(self, conn):
        conn.write(b'ping')
        time.sleep(0.15)
        return conn._read_available()

    def test_mtls_explicit_paths(self):
        """Client certificate given as plain paths"""
        conn = ConnSocket(
            f'localhost:{self.server.port}',
            tls={'cert': self.client_cert, 'key': self.client_key,
                 'ca': self.ca})
        try:
            self.assertEqual(self._echo(conn), b'ping')
            self.assertEqual(self.server.peer_cn, 'client')
        finally:
            conn.close()

    def test_mtls_profile_directory(self):
        """Profile directory supplies cert, key and CA"""
        conn = ConnSocket(
            f'localhost:{self.server.port}', tls={'profile': 'board'})
        try:
            self.assertEqual(self._echo(conn), b'ping')
            self.assertEqual(self.server.peer_cn, 'client')
        finally:
            conn.close()

    def test_mtls_bundle(self):
        """Combined cert+key bundle, CA given separately"""
        conn = ConnSocket(
            f'localhost:{self.server.port}',
            tls={'profile': 'lab', 'ca': self.ca})
        try:
            self.assertEqual(self._echo(conn), b'ping')
            self.assertEqual(self.server.peer_cn, 'client')
        finally:
            conn.close()

    def test_unknown_profile(self):
        """Unknown profile name fails before connecting"""
        with self.assertRaises(ConnError) as ctx:
            ConnSocket(
                f'localhost:{self.server.port}', tls={'profile': 'nope'})
        self.assertIn("'nope' not found", str(ctx.exception))

    @unittest.skipIf(sys.platform == 'win32', "POSIX permissions only")
    def test_world_readable_key_refused(self):
        """A private key other users can read is refused"""
        key = os.path.join(self.store, 'board', 'key.pem')
        os.chmod(key, 0o644)
        try:
            with self.assertRaises(ConnError) as ctx:
                ConnSocket(
                    f'localhost:{self.server.port}', tls={'profile': 'board'})
            self.assertIn('chmod 600', str(ctx.exception))
        finally:
            os.chmod(key, 0o600)

    def test_server_ca_is_exclusive(self):
        """An explicit CA replaces the system trust store"""
        # The test CA is not a public one, so a system-trusted host must
        # fail to verify against it
        with self.assertRaises(ConnError) as ctx:
            ConnSocket(
                f'localhost:{self.server.port}',
                tls={'profile': 'lab', 'ca': self.client_cert})
        self.assertIn('certificate verification failed', str(ctx.exception))


# =============================================================================
# CLI argument tests
# =============================================================================

class TestTlsCliArgs(unittest.TestCase):
    """Test TLS CLI argument parsing"""

    def setUp(self):
        from mpytool.mpytool import _build_main_parser
        self.parser = _build_main_parser()

    def _parse(self, *argv):
        return self.parser.parse_args(['-a', 'host:123', *argv, 'ls'])

    def test_tls_flags_in_help(self):
        """New TLS flags appear in help output"""
        result = subprocess.run(
            ['mpytool', '--help'], capture_output=True, text=True)
        self.assertIn('--tls', result.stdout)
        self.assertIn('--tls-ca', result.stdout)
        self.assertIn('--tls-insecure', result.stdout)
        self.assertIn('--tls-no-hostname', result.stdout)
        self.assertIn('--mtls', result.stdout)

    def test_deprecated_flags_hidden_from_help(self):
        """Pre-2.8.0 names still work but are not advertised"""
        result = subprocess.run(
            ['mpytool', '--help'], capture_output=True, text=True)
        self.assertNotIn('--ssl-ca', result.stdout)
        self.assertNotIn('--ssl-no-verify', result.stdout)
        self.assertNotIn('--ssl-no-hostname', result.stdout)

    def test_deprecated_aliases(self):
        """Old flag names map onto the new destinations"""
        self.assertTrue(self._parse('--ssl').tls)
        self.assertEqual(self._parse('--ssl-ca', 'ca.pem').tls_ca, 'ca.pem')
        self.assertTrue(self._parse('--ssl-no-verify').tls_insecure)
        self.assertTrue(self._parse('--tls-no-verify').tls_insecure)
        self.assertTrue(self._parse('--ssl-no-hostname').tls_no_hostname)

    def test_mtls_takes_profile_name(self):
        self.assertEqual(self._parse('--mtls', 'board').mtls, 'board')

    def test_flags_imply_tls(self):
        """--mtls, --tls-ca, --tls-insecure, --tls-no-hostname imply --tls"""
        for argv in (
                ['--mtls', 'board'], ['--tls-ca', 'ca.pem'],
                ['--tls-insecure'], ['--tls-no-hostname']):
            args = self._parse(*argv)
            use_tls = (
                args.tls or args.mtls or args.tls_ca or args.tls_insecure
                or args.tls_no_hostname)
            self.assertTrue(use_tls, argv)


if __name__ == '__main__':
    unittest.main()
