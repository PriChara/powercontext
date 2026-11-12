"""Small order-report service. Prices are stored in integer cents."""

import re

# ``minTotalCents`` is a string of 1-9 ASCII decimal digits. The explicit
# ``[0-9]`` class is deliberate: ``\d`` and ``str.isdigit()`` also accept
# non-ASCII digits such as the fullwidth form ``１２``.
_MIN_TOTAL_CENTS = re.compile(r'[0-9]{1,9}')


def _parse_min_total_cents(filters):
    """Return the requested minimum total in cents, or ``None`` if not requested.

    Raises ``ValueError`` when ``minTotalCents`` is present but malformed.
    """
    if 'minTotalCents' not in filters:
        return None
    value = filters['minTotalCents']
    if not isinstance(value, str) or not _MIN_TOTAL_CENTS.fullmatch(value):
        raise ValueError(
            'minTotalCents must be a string of 1-9 ASCII decimal digits, '
            f'got {value!r}'
        )
    return int(value)


def list_orders(orders, filters):
    """Return matching orders in input order without mutating them."""
    customer = filters.get('customer')
    status = filters.get('status')
    min_total_cents = _parse_min_total_cents(filters)
    return [
        order for order in orders
        if (customer is None or order['customer'] == customer)
        and (status is None or order['status'] == status)
        and (min_total_cents is None or order['total_cents'] >= min_total_cents)
    ]
