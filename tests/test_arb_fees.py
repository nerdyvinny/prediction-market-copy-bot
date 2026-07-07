"""Fee + edge math (pure functions, no I/O)."""

from pmbot.arb.fees import kalshi_taker_fee, pair_cost, pair_net_edge, size_pair


class TestKalshiTakerFee:
    def test_known_value_at_half(self):
        # Kalshi's published max: 1.75c/contract at P=0.50.
        assert kalshi_taker_fee(100, 0.50) == 1.75

    def test_rounds_order_total_up_to_cent(self):
        # 1 contract @ 0.50 -> $0.0175 -> $0.02
        assert kalshi_taker_fee(1, 0.50) == 0.02
        # 1 contract @ 0.99 -> $0.000693 -> $0.01 (never free)
        assert kalshi_taker_fee(1, 0.99) == 0.01

    def test_no_double_rounding_across_sizes(self):
        # 200 @ 0.50 -> exactly $3.50, no extra cent from rounding.
        assert kalshi_taker_fee(200, 0.50) == 3.50

    def test_degenerate_inputs(self):
        assert kalshi_taker_fee(0, 0.50) == 0.0
        assert kalshi_taker_fee(-5, 0.50) == 0.0
        assert kalshi_taker_fee(10, 0.0) == 0.0
        assert kalshi_taker_fee(10, 1.0) == 0.0


class TestPairMath:
    def test_pair_cost_includes_fee(self):
        # 100 pairs: PM leg $45 + K leg $50 + fee $1.75
        assert pair_cost(100, 0.45, 0.50) == 45 + 50 + 1.75

    def test_net_edge_subtracts_fee_and_buffer(self):
        # gross gap = 0.05; fee/contract = 0.0175; buffer = 0.005
        edge = pair_net_edge(100, 0.45, 0.50, slippage_buffer=0.005)
        assert abs(edge - (0.05 - 0.0175 - 0.005)) < 1e-9

    def test_net_edge_zero_for_no_size(self):
        assert pair_net_edge(0, 0.45, 0.50) == 0.0


class TestSizePair:
    def test_fits_budget_including_fee(self):
        sized = size_pair(0.45, 0.50, max_usd=50)
        assert sized is not None
        assert sized.cost_usd <= 50
        assert sized.contracts == 51          # 51 * 0.95 + fee(51, .5) = 49.35
        assert sized.profit_usd > 0

    def test_respects_depth_cap(self):
        sized = size_pair(0.45, 0.50, max_usd=500, max_contracts=10)
        assert sized is not None
        assert sized.contracts == 10

    def test_rejects_negative_edge_pairs(self):
        # 0.60 + 0.45 = 1.05 > 1: never profitable.
        assert size_pair(0.60, 0.45, max_usd=100) is None

    def test_rejects_when_fee_eats_tiny_edge(self):
        # gross edge/pair = 0.005 but fee at 0.50 is 0.0175 -> loss.
        assert size_pair(0.495, 0.50, max_usd=100) is None

    def test_rejects_zero_budget(self):
        assert size_pair(0.45, 0.50, max_usd=0) is None
