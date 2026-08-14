import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server


class SafetyTests(unittest.TestCase):
    def test_blocks_env_read(self):
        with self.assertRaises(ValueError):
            server._validate_read_path('/home/ubuntu/openclaw_workspace/qcc-chatgpt-ops/chatgpt-trigger.env')

    def test_blocks_private_key_read(self):
        with self.assertRaises(ValueError):
            server._validate_read_path('/home/ubuntu/openclaw_workspace/qcc-chatgpt-ops/temp.key')

    def test_blocks_outside_root(self):
        with self.assertRaises(ValueError):
            server._validate_read_path('/etc/passwd')

    def test_write_restricted_to_ops(self):
        with self.assertRaises(ValueError):
            server._validate_write_path('/home/ubuntu/openclaw_workspace/quant_command_centre/qcc/core.py')

    def test_service_allowlist(self):
        self.assertEqual(server._validate_service('qcc-chatgpt-trigger.service'), 'qcc-chatgpt-trigger.service')
        with self.assertRaises(ValueError):
            server._validate_service('ssh.service')

    def test_run_uses_no_shell(self):
        fake = mock.Mock(returncode=0, stdout='ok', stderr='')
        with mock.patch('server.subprocess.run', return_value=fake) as run:
            result = server._run(['echo', 'ok'])
            self.assertEqual(result['returncode'], 0)
            kwargs = run.call_args.kwargs
            self.assertNotIn('shell', kwargs)


if __name__ == '__main__':
    unittest.main()
