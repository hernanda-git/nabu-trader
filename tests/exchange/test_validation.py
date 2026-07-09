"""Task 5 — pre-submission validation gate. No order that violates an
exchange filter may pass validate_order.
"""

from src.exchange.validation import validate_order

APE_FILTERS = {"tickSize": 0.0001, "minPrice": 0.001, "maxPrice": 1000,
               "stepSize": 1, "minQty": 1, "minNotional": 5.0}


def test_validate_accepts_good_ape_order():
    err = validate_order(symbol="APEUSDT", side="BUY", price=0.162, qty=31,
                         filters=APE_FILTERS)
    assert err is None, err


def test_validate_rejects_price_above_max():
    # A price above the symbol's maxPrice is an -1111 class rejection.
    err = validate_order(symbol="APEUSDT", side="BUY", price=1500.0, qty=31,
                         filters=APE_FILTERS)
    assert err is not None, "price 1500 > maxPrice 1000 must be rejected"


def test_validate_rejects_price_below_min():
    err = validate_order(symbol="APEUSDT", side="BUY", price=0.0005, qty=31,
                         filters=APE_FILTERS)
    assert err is not None, "price 0.0005 < minPrice 0.001 must be rejected"


def test_validate_rejects_below_min_notional():
    err = validate_order(symbol="APEUSDT", side="BUY", price=0.162, qty=10,  # 1.62 < 5
                         filters=APE_FILTERS)
    assert err is not None, "notional 1.62 < 5.0 must be rejected"


def test_validate_rejects_bad_qty_precision():
    # APE stepSize=1 -> qty 30.5 is not an integer lot
    err = validate_order(symbol="APEUSDT", side="BUY", price=0.162, qty=30.5,
                         filters=APE_FILTERS)
    assert err is not None, "non-integer qty 30.5 must be rejected for APE"
