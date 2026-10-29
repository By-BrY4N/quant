"""Verificação focal do caso contábil manual definido para F0."""

import unittest
from datetime import UTC, datetime
from decimal import Decimal

from quantlab.domain import PortfolioState, Side
from quantlab.ledger import apply_fill


class LedgerRoundTripTest(unittest.TestCase):
    def test_fixed_one_unit_round_trip_matches_manual_accounting(self) -> None:
        state = PortfolioState(cash_usdt=Decimal("1000.00"), base_qty=Decimal("0"))
        fee_rate = Decimal("0.001")
        buy_at = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        sell_at = datetime(2026, 1, 1, 0, 1, tzinfo=UTC)

        state, buy = apply_fill(
            state,
            timestamp_utc=buy_at,
            side=Side.BUY,
            quantity_base=Decimal("1"),
            price_usdt=Decimal("100.00"),
            fee_rate=fee_rate,
        )
        state, sell = apply_fill(
            state,
            timestamp_utc=sell_at,
            side=Side.SELL,
            quantity_base=Decimal("1"),
            price_usdt=Decimal("110.00"),
            fee_rate=fee_rate,
        )

        self.assertEqual(state.cash_usdt, Decimal("1009.790000"))
        self.assertEqual(state.base_qty, Decimal("0"))
        self.assertEqual(state.fees_paid_usdt, Decimal("0.210000"))
        self.assertEqual(buy.fee_usdt, Decimal("0.100000"))
        self.assertEqual(sell.fee_usdt, Decimal("0.110000"))


if __name__ == "__main__":
    unittest.main()
