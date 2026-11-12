"""Small order-report service. Prices are stored in integer cents."""


def list_orders(orders, filters):
    """Return matching orders in input order without mutating them."""
    customer = filters.get('customer')
    status = filters.get('status')
    return [
        order for order in orders
        if (customer is None or order['customer'] == customer)
        and (status is None or order['status'] == status)
    ]
