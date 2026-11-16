from src.models.hmm_regimes import get_feature_names


def test_model_features_exclude_raw_levels_and_targets() -> None:
    raw_columns = {
        "open",
        "high",
        "low",
        "close",
        "volume",
        "CapRealUSD",
        "PriceUSD",
        "AdrActCnt",
        "HashRate",
        "FeeTotUSD",
        "TxCnt",
        "walcl",
        "dtwexbgs",
        "dgs10",
        "quote_asset_volume",
        "number_of_trades",
        "taker_buy_base_volume",
        "taker_buy_quote_volume",
    }
    feature_names = get_feature_names()

    assert not raw_columns.intersection(feature_names)
    assert not any(name.startswith("target_") for name in feature_names)
