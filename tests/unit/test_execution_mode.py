from agenttrader.data.models import ExecutionMode, DataProvenance


def test_execution_mode_default_is_strict():
    assert ExecutionMode.STRICT_PRICE_ONLY.value == "strict_price_only"
    assert ExecutionMode.OBSERVED_ORDERBOOK.value == "observed_orderbook"
    assert ExecutionMode.SYNTHETIC_EXECUTION_MODEL.value == "synthetic_execution_model"


def test_data_provenance_fields():
    p = DataProvenance(source="parquet", observed=True, granularity="trade")
    assert p.source == "parquet"
    assert p.observed is True
    assert p.granularity == "trade"
