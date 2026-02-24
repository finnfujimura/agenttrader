from agenttrader import BaseStrategy


class SampleStrategy(BaseStrategy):
    def on_start(self):
        self.subscribe(platform="polymarket")

    def on_market_data(self, market, price, orderbook):
        pass
