import unittest
from unittest.mock import Mock, patch
import main


class WindowsAppTests(unittest.TestCase):
    def test_update_origin_and_path_are_fixed(self):
        valid = {'version': '1.0.1', 'path': '/static/windows/AlphaMenu-Setup-1.0.1.exe', 'sha256': 'a' * 64}
        self.assertEqual(main.validate_update(valid), main.BASE + valid['path'])
        for change in [{'path': 'https://example.com/setup.exe'}, {'path': '/static/windows/../evil.exe'}, {'sha256': 'bad'}, {'version': '1.1'}]:
            with self.assertRaises(ValueError):
                main.validate_update(valid | change)

    def test_numeric_versions(self):
        self.assertGreater(main.version_tuple('1.10.0'), main.version_tuple('1.9.9'))

    def test_incompatible_bridge_is_not_replaced(self):
        with patch.object(main.bridge, 'ThreadingHTTPServer', side_effect=OSError), patch.object(main, 'bridge_health', side_effect=ValueError('Old bridge')):
            with self.assertRaisesRegex(ValueError, 'Old bridge'):
                main.start_bridge()

    def test_existing_compatible_bridge_is_reused(self):
        with patch.object(main.bridge, 'ThreadingHTTPServer', side_effect=OSError), patch.object(main, 'bridge_health'):
            self.assertIsNone(main.start_bridge())

    def test_local_api_health(self):
        result = main.LocalApi().fiscal_request('/health')
        self.assertTrue(result['ok'])
        self.assertEqual(result['data']['version'], main.bridge.BRIDGE_VERSION)

    def test_local_api_rejects_unknown_routes(self):
        result = main.LocalApi().fiscal_request('/not-allowed')
        self.assertFalse(result['ok'])
        self.assertIn('non consentita', result['message'])

    def test_local_api_dispatches_fiscal_request(self):
        fiscal = Mock()
        fiscal.dispatch.return_value = {'message': 'ok'}
        with patch.object(main.bridge, 'epson_bridge', fiscal):
            result = main.LocalApi().fiscal_request('/fiscal/config/read', {'config': {'host': 'printer'}})
        self.assertEqual(result, {'ok': True, 'data': {'message': 'ok'}})
        fiscal.dispatch.assert_called_once_with('/fiscal/config/read', {'config': {'host': 'printer'}})


if __name__ == '__main__':
    unittest.main()
