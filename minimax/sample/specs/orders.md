# ORD-17 · Order report minimum total filter

`list_orders(orders, filters)` must support optional `minTotalCents`. Its value is a string of 1–9 ASCII decimal digits. Reject empty, signed, decimal, non-ASCII, numeric and null values with `ValueError`, even for an empty orders list. Leading zeros are valid. Include an order when `order['total_cents']` is greater than or equal to the parsed minimum.

Without `minTotalCents`, preserve current behavior. Combine the minimum with existing exact `customer` and `status` filters using AND. Preserve input ordering and never mutate the input orders or filters. Implement only in `sample/orders.py`; keep `sample/test_orders.py` unchanged.
