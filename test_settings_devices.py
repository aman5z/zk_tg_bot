import unittest

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


if __name__ == '__main__':
    unittest.main()
