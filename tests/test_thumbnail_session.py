import unittest
from unittest.mock import Mock

from clipsave_app.thumbnail_session import ThumbnailSession


class ThumbnailSessionTests(unittest.TestCase):
    def test_invalidate_advances_generation_and_cancels_queued_work(self):
        queue = Mock()
        session = ThumbnailSession(queue)

        self.assertEqual(session.invalidate(), 1)

        queue.cancel_queued.assert_called_once_with()
        self.assertTrue(session.is_current(1))
        self.assertFalse(session.is_current(0))

    def test_request_uses_current_generation(self):
        queue = Mock()
        queue.request.return_value = True
        session = ThumbnailSession(queue)
        session.invalidate(cancel_queued=False)
        key = object()

        self.assertTrue(session.request(key))

        queue.request.assert_called_once_with(key, 1)

    def test_close_and_pause_invalidate_without_double_cancelling(self):
        queue = Mock()
        queue.close.return_value = True
        queue.pause_and_wait.return_value = True
        session = ThumbnailSession(queue)

        self.assertTrue(session.pause_and_wait(10))
        self.assertTrue(session.close(20))

        self.assertEqual(session.generation, 2)
        queue.cancel_queued.assert_not_called()
        queue.pause_and_wait.assert_called_once_with(10)
        queue.close.assert_called_once_with(20)
