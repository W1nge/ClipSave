import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtNetwork import QAbstractSocket

from clipsave_app import app, constants


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


class _Signal:
    def __init__(self):
        self.callback = None

    def connect(self, callback):
        self.callback = callback


class _LocalConnection:
    def __init__(self):
        self.readyRead = _Signal()
        self.disconnected = _Signal()
        self.buffer = b""
        self.disconnected_called = False
        self.delete_later_called = False

    def bytesAvailable(self):
        return len(self.buffer)

    def readAll(self):
        value = self.buffer
        self.buffer = b""
        return value

    def disconnectFromServer(self):
        self.disconnected_called = True

    def deleteLater(self):
        self.delete_later_called = True

    def waitForReadyRead(self, _timeout):
        raise AssertionError("GUI-thread blocking read must not be used")


class AppTests(unittest.TestCase):
    def test_smoke_profile_override_requires_ready_file(self):
        fallback = Path("C:/fallback")
        with patch.object(constants, "_local_appdata", return_value=fallback), patch.object(
            constants.sys, "argv", ["clipsave", "--smoke-profile", "C:/override"]
        ):
            self.assertEqual(constants._configured_local_root(), fallback / "ClipSave")
        with patch.object(constants.sys, "argv", [
            "clipsave",
            "--smoke-profile",
            "C:/override",
            "--smoke-ready-file",
            "C:/ready.txt",
        ]):
            self.assertEqual(constants._configured_local_root(), Path("C:/override"))

    def test_release_version_is_0_3_0(self):
        self.assertEqual(constants.APP_VERSION, "0.3.0")

    @unittest.skipUnless(os.name == "nt", "Windows SID lookup is Windows-only")
    def test_windows_user_sid_uses_real_process_token(self):
        self.assertTrue(app._windows_user_sid().startswith("S-1-"))

    @unittest.skipUnless(os.name == "nt", "Windows mutex is Windows-only")
    def test_windows_mutex_rejects_second_owner_for_same_server_name(self):
        name = f"ClipSave.Test.{id(self)}"
        first = app.SingleInstance(name)
        second = app.SingleInstance(name)
        try:
            self.assertTrue(first._acquire_mutex())
            self.assertFalse(second._acquire_mutex())
        finally:
            second.close()
            first.close()

    def test_server_name_is_scoped_to_current_user(self):
        with patch("clipsave_app.app._current_user_identity", return_value="domain\\user"):
            first = app._instance_server_name()
        with patch("clipsave_app.app._current_user_identity", return_value="domain\\other"):
            second = app._instance_server_name()

        self.assertTrue(first.startswith(f"{app.INSTANCE_SERVER}."))
        self.assertNotEqual(first, second)
        self.assertNotIn("domain", first)

    def test_windows_user_identity_ignores_mutable_environment_names(self):
        with patch("clipsave_app.app.os.name", "nt"), patch(
            "clipsave_app.app._windows_user_sid", return_value="S-1-5-21-123-456-789-1001"
        ), patch.dict("clipsave_app.app.os.environ", {"USERDOMAIN": "FIRST"}):
            first = app._current_user_identity()
        with patch("clipsave_app.app.os.name", "nt"), patch(
            "clipsave_app.app._windows_user_sid", return_value="S-1-5-21-123-456-789-1001"
        ), patch.dict("clipsave_app.app.os.environ", {"USERDOMAIN": "SECOND"}):
            second = app._current_user_identity()

        self.assertEqual(first, second)

    def test_show_message_requires_exact_protocol_bytes(self):
        self.assertTrue(app._is_show_message(app.SHOW_MESSAGE))
        self.assertFalse(app._is_show_message(b"show"))
        self.assertFalse(app._is_show_message(b"show\nextra"))
        self.assertFalse(app._is_show_message(b"SHOW\n"))

    def test_instance_claim_retries_while_mutex_owner_starts(self):
        single = MagicMock()
        single.notify_existing.side_effect = [False, False, True]
        single.listen.return_value = False
        with patch("clipsave_app.app.time.sleep") as sleep:
            self.assertFalse(
                app._claim_or_notify_instance(single, MagicMock(), timeout=1.0)
            )

        self.assertEqual(single.notify_existing.call_count, 3)
        self.assertEqual(single.listen.call_count, 2)
        self.assertEqual(sleep.call_count, 2)

    def test_instance_contender_can_take_over_after_owner_exits(self):
        single = MagicMock()
        single.notify_existing.return_value = False
        single.listen.side_effect = [False, True]
        callback = MagicMock()
        with patch("clipsave_app.app.time.sleep"):
            self.assertTrue(
                app._claim_or_notify_instance(single, callback, timeout=1.0)
            )

        self.assertEqual(single.listen.call_count, 2)
        self.assertEqual(single.listen.call_args.args, (callback,))

    def test_session_commit_cancels_logoff_when_clean_shutdown_fails(self):
        window = MagicMock()
        manager = MagicMock()
        window.quit_application_for_session_end.return_value = False

        app._commit_session_data(window, manager)

        manager.cancel.assert_called_once_with()
        window.quit_application_for_session_end.assert_called_once_with(timeout=2.0)
        window.quit_application.assert_not_called()

    def test_session_commit_allows_logoff_after_clean_shutdown(self):
        window = MagicMock()
        manager = MagicMock()
        window.quit_application_for_session_end.return_value = True

        app._commit_session_data(window, manager)

        manager.cancel.assert_not_called()

    def test_session_commit_cancels_when_noninteractive_api_is_missing(self):
        class Window:
            pass

        manager = MagicMock()
        app._commit_session_data(Window(), manager)
        manager.cancel.assert_called_once_with()

    def test_session_commit_cancels_when_shutdown_raises_any_exception(self):
        window = MagicMock()
        manager = MagicMock()
        window.quit_application_for_session_end.side_effect = KeyboardInterrupt()

        app._commit_session_data(window, manager)

        manager.cancel.assert_called_once_with()

    def test_failed_notification_does_not_remove_endpoint(self):
        socket = MagicMock()
        socket.waitForConnected.return_value = False
        with patch("clipsave_app.app.QLocalSocket", return_value=socket):
            with patch.object(app.QLocalServer, "removeServer") as remove_server:
                notified = app.SingleInstance("test.endpoint").notify_existing()

        self.assertFalse(notified)
        remove_server.assert_not_called()

    def test_closed_and_rejected_connections_are_scheduled_for_deletion(self):
        instance = app.SingleInstance("test.endpoint")
        accepted = MagicMock()
        instance._connections[accepted] = bytearray()
        instance._close_connection(accepted)
        accepted.disconnectFromServer.assert_called_once_with()
        accepted.deleteLater.assert_called_once_with()

        instance._connections = {MagicMock(): bytearray() for _ in range(instance.MAX_CLIENTS)}
        rejected = MagicMock()
        instance._accept_connection(rejected, MagicMock())
        rejected.disconnectFromServer.assert_called_once_with()
        rejected.deleteLater.assert_called_once_with()

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
            single.close()

        server_class.removeServer.assert_called_once_with("test.endpoint")
        second_server.newConnection.connect.assert_called_once()

    def test_client_messages_are_read_without_blocking_the_gui_thread(self):
        single = app.SingleInstance("test.endpoint")
        connection = _LocalConnection()
        callbacks = []

        single._accept_connection(connection, lambda: callbacks.append(True))
        self.assertEqual(callbacks, [])
        self.assertFalse(connection.disconnected_called)

        connection.buffer = app.SHOW_MESSAGE
        connection.readyRead.callback()

        self.assertEqual(callbacks, [True])
        self.assertTrue(connection.disconnected_called)
        self.assertTrue(connection.delete_later_called)
        self.assertNotIn(connection, single._connections)

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
