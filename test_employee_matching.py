import unittest

import mdb_reader


class EmployeeMatchingTests(unittest.TestCase):
    def test_prefers_startswith_over_contains(self):
        employees = [
            {'uid': '10', 'badge': '1111', 'name': 'SUNDARI SUBRAMANIAN SUND', 'dept': 'X', 'active': True},
            {'uid': '11', 'badge': '1295', 'name': 'AMAN P FAIZAL', 'dept': 'Y', 'active': True},
        ]
        ranked = mdb_reader.rank_employee_matches('aman', employees=employees)
        self.assertGreater(len(ranked), 0)
        self.assertEqual(ranked[0]['name'], 'AMAN P FAIZAL')

    def test_numeric_query_prefers_badge_exact(self):
        employees = [
            {'uid': '1295', 'badge': '21295', 'name': 'Wrong', 'dept': 'X', 'active': True},
            {'uid': '500', 'badge': '1295', 'name': 'Right', 'dept': 'Y', 'active': True},
        ]
        ranked = mdb_reader.rank_employee_matches('1295', employees=employees)
        self.assertEqual(ranked[0]['badge'], '1295')
        self.assertEqual(ranked[0]['name'], 'Right')

    def test_token_startswith_beats_contains(self):
        employees = [
            {'uid': '1', 'badge': '1001', 'name': 'ALI RISHAL', 'dept': 'X', 'active': True},
            {'uid': '2', 'badge': '1002', 'name': 'FARISHAL KHAN', 'dept': 'Y', 'active': True},
        ]
        ranked = mdb_reader.rank_employee_matches('rishal', employees=employees)
        self.assertEqual(ranked[0]['name'], 'ALI RISHAL')


if __name__ == '__main__':
    unittest.main()
