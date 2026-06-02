import unittest
from unittest import mock

import settings


class DeviceIpExpansionTests(unittest.TestCase):
    def test_expands_valid_range(self):
        ips = settings._expand_device_ips('10.20.141.21-10.20.141.29')
        self.assertEqual(len(ips), 9)
        self.assertEqual(ips[0], '10.20.141.21')
        self.assertEqual(ips[-1], '10.20.141.29')

    def test_preserves_invalid_range(self):
        ips = settings._expand_device_ips('10.20.141.29-10.20.141.21')
        self.assertEqual(ips, ['10.20.141.29-10.20.141.21'])

    def test_supports_mix_of_tokens(self):
        ips = settings._expand_device_ips('10.20.141.21-10.20.141.22,10.20.141.99')
        self.assertEqual(ips, ['10.20.141.21', '10.20.141.22', '10.20.141.99'])


class NotificationToggleTests(unittest.TestCase):
    def test_stale_toggle_setters_write_expected_values(self):
        with mock.patch.object(settings, '_save') as mocked_save:
            settings.set_notify_device_stale(True)
            settings.set_notify_mdb_stale(False)
        self.assertEqual(settings._cfg['notifications']['notify_device_stale'], '1')
        self.assertEqual(settings._cfg['notifications']['notify_mdb_stale'], '0')
        self.assertEqual(mocked_save.call_count, 2)

    def test_toggle_getters_read_booleans(self):
        settings._cfg.setdefault('notifications', {})
        settings._cfg['notifications']['notify_device_status'] = '0'
        settings._cfg['notifications']['notify_device_stale'] = '1'
        settings._cfg['notifications']['notify_mdb_stale'] = '0'
        self.assertFalse(settings.get_notify_device_status())
        self.assertTrue(settings.get_notify_device_stale())
        self.assertFalse(settings.get_notify_mdb_stale())


if __name__ == '__main__':
    unittest.main()
