import unittest
from unittest.mock import MagicMock, patch

from flask import session

import app as menu


class OrderNotificationTests(unittest.TestCase):
    def test_push_endpoint_is_restricted_to_browser_services(self):
        self.assertTrue(menu.valid_push_endpoint('https://fcm.googleapis.com/fcm/send/abc'))
        self.assertTrue(menu.valid_push_endpoint('https://web.push.apple.com/abc'))
        self.assertFalse(menu.valid_push_endpoint('http://fcm.googleapis.com/abc'))
        self.assertFalse(menu.valid_push_endpoint('https://fcm.googleapis.com.evil.example/abc'))
        self.assertFalse(menu.valid_push_endpoint('https://[invalid/abc'))

    def test_order_notification_query_is_scoped_to_employee_shop(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [(12, 'asporto'), (13, 'tavolo')]
        with menu.app.test_request_context('/api/ordini/notifiche?dopo=11'):
            session['employee_id'] = 4
            session['employee_shop_id'] = 7
            with patch.object(menu.psycopg2, 'connect', return_value=connection):
                response = menu.api_ordini_notifiche()
        self.assertEqual(response.json['ultimo_id'], 13)
        self.assertEqual(response.json['nuovi'], [{'id': 12, 'tipo': 'asporto'}, {'id': 13, 'tipo': 'tavolo'}])
        self.assertEqual(cursor.execute.call_args.args[1], (7, 11))

    def test_anonymous_user_cannot_read_order_notifications(self):
        with menu.app.test_request_context('/api/ordini/notifiche'):
            response, status = menu.api_ordini_notifiche()
        self.assertEqual(status, 403)
        self.assertIn('error', response.json)


if __name__ == '__main__':
    unittest.main()
