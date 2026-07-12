import unittest
from unittest.mock import MagicMock, patch

from PySide6.QtNetwork import QAbstractSocket

from clipsave_app import app


class _QueryResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, row):
        self.row = row
        self.queries = []

    def execute(self, query):
        self.queries.append(query)
        return _QueryResult(self.row)


class _Database:
    def __init__(self, row, needs_library_rescan=False):
        self.connection = _Connection(row)
        self.needs_library_rescan = needs_library_rescan


class AppTests(unittest.TestCase):
    def test_server_name_is_scoped_to_current_user(self):
        with patch("clipsave_app.app._current_user_identity", return_value="domain\\user"):
            first = app._instance_server_name()
        with patch("clipsave_app.app._current_user_identity", return_value="domain\\other"):
            second = app._instance_server_name()

        self.assertTrue(first.startswith(f"{app.INSTANCE_SERVER}."))
        self.assertNotEqual(first, second)
        self.assertNotIn("domain", first)

    def test_show_message_requires_exact_protocol_bytes(self):
        self.assertTrue(app._is_show_message(app.SHOW_MESSAGE))
        self.assertFalse(app._is_show_message(b"show"))
        self.assertFalse(app._is_show_message(b"show\nextra"))
        self.assertFalse(app._is_show_message(b"SHOW\n"))

    def test_failed_notification_does_not_remove_endpoint(self):
        socket = MagicMock()
        socket.waitForConnected.return_value = False
        with patch("clipsave_app.app.QLocalSocket", return_value=socket):
            with patch.object(app.QLocalServer, "removeServer") as remove_server:
                notified = app.SingleInstance("test.endpoint").notify_existing()

        self.assertFalse(notified)
        remove_server.assert_not_called()

    def test_server_is_restricted_to_current_user(self):
        server = MagicMock()

        app.SingleInstance._configure_server(server)

        server.setSocketOptions.assert_called_once_with(app.QLocalServer.SocketOption.UserAccessOption)

    def test_listen_does_not_remove_active_endpoint(self):
        server = MagicMock()
        server.listen.return_value = False
        server.serverError.return_value = QAbstractSocket.SocketError.AddressInUseError
        with patch("clipsave_app.app.QLocalServer", return_value=server) as server_class:
            single = app.SingleInstance("test.endpoint")
            with patch.object(single, "_endpoint_is_active", return_value=True):
                self.assertFalse(single.listen(MagicMock()))

        server_class.removeServer.assert_not_called()

    def test_listen_cleans_only_confirmed_stale_endpoint_and_retries(self):
        first_server = MagicMock()
        first_server.listen.return_value = False
        first_server.serverError.return_value = QAbstractSocket.SocketError.AddressInUseError
        second_server = MagicMock()
        second_server.listen.return_value = True
        with patch("clipsave_app.app.QLocalServer", side_effect=[first_server, second_server]) as server_class:
            server_class.removeServer.return_value = True
            single = app.SingleInstance("test.endpoint")
            with patch.object(single, "_endpoint_is_active", return_value=False):
                self.assertTrue(single.listen(MagicMock()))

        server_class.removeServer.assert_called_once_with("test.endpoint")
        second_server.newConnection.connect.assert_called_once()

    def test_scan_runs_after_actual_migration(self):
        database = _Database((1,))

        self.assertTrue(app._should_scan_library({"pictures": 1, "markdown": 0, "data": 0}, database))
        self.assertEqual(database.connection.queries, [])

    def test_scan_runs_when_database_has_no_valid_records(self):
        database = _Database(None)

        self.assertTrue(app._should_scan_library({"pictures": 0, "markdown": 0, "data": 0}, database))

    def test_scan_is_skipped_when_database_has_valid_records(self):
        database = _Database((1,))

        self.assertFalse(app._should_scan_library({"pictures": 0, "markdown": 0, "data": 0}, database))
        self.assertIn("missing = 0", database.connection.queries[0])

    def test_scan_runs_after_database_recovery(self):
        database = _Database((1,), needs_library_rescan=True)

        self.assertTrue(app._should_scan_library({"pictures": 0, "markdown": 0, "data": 0}, database))
        self.assertEqual(database.connection.queries, [])


if __name__ == "__main__":
    unittest.main()
