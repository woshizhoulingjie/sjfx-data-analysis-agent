"""Disconnected preview IPC must not poison later files or imply success."""
import unittest
from unittest.mock import MagicMock, patch

from services.large_package_preview_runner import PreviewRunner


class PreviewTransportTests(unittest.TestCase):
    def test_send_disconnect_is_failed_and_discards_dead_pipe(self):
        runner = PreviewRunner()
        runner.process = MagicMock()
        runner.process.is_alive.return_value = True
        connection = runner.connection = MagicMock()
        connection.send.side_effect = BrokenPipeError(32, 'Broken pipe')
        with patch('services.large_package_preview_runner.terminate_process_tree'):
            summary, _ = runner.run('/tmp', {'path': 'one.pdf'})
        self.assertEqual(summary['status'], 'failed')
        self.assertIn('Broken pipe', summary['error'])
        self.assertIsNone(runner.connection)
        self.assertIsNone(runner.process)
        connection.close.assert_called_once()

    def test_receive_disconnect_is_failed_and_discards_dead_pipe(self):
        runner = PreviewRunner()
        runner.process = MagicMock()
        runner.process.is_alive.return_value = True
        runner.connection = MagicMock()
        runner.connection.poll.return_value = True
        runner.connection.recv.side_effect = EOFError('child exited')
        with patch('services.large_package_preview_runner.terminate_process_tree'):
            summary, _ = runner.run('/tmp', {'path': 'two.pdf'})
        self.assertEqual(summary['status'], 'failed')
        self.assertIsNone(runner.connection)


if __name__ == '__main__':
    unittest.main()
