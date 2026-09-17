import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import epson_bridge


class EpsonProgrammingTests(unittest.TestCase):
    def test_response_rejects_wrong_command(self):
        xml = '<response success="true"><addInfo><responseCommand>4202</responseCommand><responseData>ok</responseData></addInfo></response>'
        self.assertEqual(epson_bridge._response(xml, '4202'), 'ok')
        with self.assertRaisesRegex(ValueError, 'corrispondente'):
            epson_bridge._response(xml, '4205')

    def test_write_requires_explicit_confirmation(self):
        with self.assertRaisesRegex(ValueError, 'Conferma'):
            epson_bridge.write_programming({}, {'sections': ['logo'], 'data': {'logo': {}}})

    def test_logo_write_is_bounded_and_uses_documented_parameters(self):
        payload = {'confirmation': 'SCRIVI CONFIGURAZIONE EPSON', 'sections': ['logo'],
                   'data': {'logo': {'header': 1, 'footer': 0, 'alignment': 2}}}
        with patch.object(epson_bridge, 'direct', return_value='') as direct:
            result = epson_bridge.write_programming({}, payload)
        self.assertEqual([call.args[1:] for call in direct.call_args_list],
                         [('4015', '09001'), ('4015', '10000'), ('4015', '22002')])
        self.assertTrue(result['ok'])


if __name__ == '__main__':
    unittest.main()
