from agenttrader import BaseStrategy


MARKET_ID = "5031084282167950494806674428243037744881029417420880897305642929037077494331"


class LivePaperSmokeStrategy(BaseStrategy):
    def on_start(self):
        self.subscribe(platform="polymarket", market_ids=[MARKET_ID])

    def on_market_data(self, market, price, orderbook):
        position = self.get_position(market.id)
        if position is None and price < 0.5:
            self.buy(market.id, 1)
        elif position is not None and price > 0.9:
            self.sell(market.id)
