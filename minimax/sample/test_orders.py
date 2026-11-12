"""Run the same suite against either the baseline or candidate module."""

import importlib.util
import os
from pathlib import Path
import unittest


module_path = Path(os.environ.get('ORDERS_MODULE', Path(__file__).with_name('orders.py')))
spec = importlib.util.spec_from_file_location('orders_under_test', module_path)
orders_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(orders_module)
list_orders = orders_module.list_orders

ORDERS = [
    {'id': 'A', 'customer': 'Alex', 'status': 'paid', 'total_cents': 250},
    {'id': 'B', 'customer': 'Alex', 'status': 'paid', 'total_cents': 125},
    {'id': 'C', 'customer': 'Kim', 'status': 'paid', 'total_cents': 250},
    {'id': 'D', 'customer': 'Alex', 'status': 'open', 'total_cents': 400},
    {'id': 'E', 'customer': 'Alex', 'status': 'paid', 'total_cents': 300},
    {'id': 'F', 'customer': 'Alex', 'status': 'paid', 'total_cents': 0},
]


def ids(filters):
    return [o['id'] for o in list_orders(ORDERS, filters)]


class Feature(unittest.TestCase):
    def test_inclusive_minimum(self):
        self.assertEqual(ids({'minTotalCents': '0250'}), ['A', 'C', 'D', 'E'])

    def test_filter_intersection_and_order(self):
        self.assertEqual(ids({'customer': 'Alex', 'status': 'paid', 'minTotalCents': '250'}), ['A', 'E'])

    def test_zero(self):
        self.assertEqual(ids({'minTotalCents': '0'}), ['A', 'B', 'C', 'D', 'E', 'F'])

    def test_invalid_values_even_when_no_orders(self):
        for value in ('', '-1', '+1', '1.5', '１２', '1000000000', ' 10', 10, None, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                list_orders([], {'minTotalCents': value})


class Regression(unittest.TestCase):
    def test_unfiltered(self):
        self.assertEqual(ids({}), ['A', 'B', 'C', 'D', 'E', 'F'])

    def test_original_customer_status(self):
        self.assertEqual(ids({'customer': 'Alex', 'status': 'paid'}), ['A', 'B', 'E', 'F'])

    def test_no_mutation(self):
        filters = {'status': 'open'}
        snapshot = [order.copy() for order in ORDERS]
        self.assertEqual(ids(filters), ['D'])
        self.assertEqual(ORDERS, snapshot)
        self.assertEqual(filters, {'status': 'open'})
