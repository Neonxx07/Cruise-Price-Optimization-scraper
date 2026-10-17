"""MSC rows carry the booked price and today's price, 2026-10-15.

Neon: "it is not showing old price and new price and it is not collecting
data and prices at all". The scan WAS collecting them - 3001059 in
data/msc_control/rate_check_data.jsonl: current_value 3654.79,
today_price_same_category 3,516.26 - but MscBookingResult had nowhere to
put them, msc_results had no column, and the GUI hard-coded both cells to
"—". Every MSC price ever read evaporated before the row.
"""
from core.calculator_msc import evaluate_msc_booking
from models.database import MscResultRecord


def test_3001059_both_prices_reach_the_result():
    r = evaluate_msc_booking(
        booking_id="3001059", category="BR1", current_total_price=3654.79,
        today_base_price=3516.26, due_amount=2000.0, today_price_tab_confirmed=True,
        current_discounts=[], today_discount_options=[], club_discount_offered=False)
    assert (r.current_total_price, r.today_price, r.due_amount) == (3654.79, 3516.26, 2000.0)


def test_prices_survive_every_early_return():
    paid = evaluate_msc_booking(booking_id="3001068", category="INTERIOR",
                                is_paid_in_full=True, due_amount=5.48,
                                current_total_price=544.48, today_base_price=520.0)
    assert paid.checks == [] and paid.current_total_price == 544.48 and paid.today_price == 520.0
    over = evaluate_msc_booking("3000071", "BP", is_overpayment=True,
                                current_total_price=1000.0)
    assert over.current_total_price == 1000.0 and over.today_price is None


def test_unread_prices_stay_none_never_zero():
    r = evaluate_msc_booking("X", "BP", current_discounts=[], today_discount_options=[])
    assert r.current_total_price is None and r.today_price is None


def test_the_table_has_somewhere_to_keep_them():
    cols = {c.name for c in MscResultRecord.__table__.columns}
    assert {"current_total_price", "today_price", "due_amount"} <= cols
    for name in ("current_total_price", "today_price", "due_amount"):
        assert MscResultRecord.__table__.columns[name].nullable    # NULL = not recorded
