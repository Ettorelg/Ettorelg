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

    def test_response_explains_day_open_error(self):
        xml = '<response success="false" code="PRINTER ERROR" status="17"><addInfo><lastCommand>16</lastCommand></addInfo></response>'
        with self.assertRaisesRegex(ValueError, 'giornata fiscale deve essere chiusa'):
            epson_bridge._response(xml, '3016')

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

    def test_header_write_identifies_rejected_line(self):
        payload = {'confirmation': 'SCRIVI CONFIGURAZIONE EPSON', 'sections': ['headers'],
                   'data': {'headers': [{'line': 1, 'text': 'NEGOZIO'}]}}
        with patch.object(epson_bridge, 'direct', side_effect=ValueError('Epson: PRINTER ERROR')):
            with self.assertRaisesRegex(ValueError, 'Intestazione riga 1.*chiusura giornaliera'):
                epson_bridge.write_programming({}, payload)

    def test_header_write_centers_text_and_sets_double_height(self):
        payload = {'confirmation': 'SCRIVI CONFIGURAZIONE EPSON', 'sections': ['headers'],
                   'data': {'headers': [{'line': 1, 'text': 'NEGOZIO', 'centered': True, 'font': 3}]}}
        with patch.object(epson_bridge, 'direct', return_value='') as direct:
            epson_bridge.write_programming({}, payload)
        calls = [call.args[1:] for call in direct.call_args_list]
        self.assertEqual(calls[0], ('3016', '01' + 'NEGOZIO'.center(40)))
        self.assertEqual(calls[1], ('3016', '99' + (' ' * 40)))
        self.assertEqual(calls[2], ('4016', '13'))


if __name__ == '__main__':
    unittest.main()
