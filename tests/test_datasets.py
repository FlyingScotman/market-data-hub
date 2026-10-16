from datetime import date

import pytest

from mdhub.datasets import DatasetSpec, all_datasets, register_dataset, unregister_dataset


def _spec(**kw: object) -> DatasetSpec:
    base: dict[str, object] = {
        "name": "t_ds",
        "table": "md.t",
        "date_column": "d",
        "calendar": "MOEX_STOCK",
        "frequency": "business_daily",
        "max_lag_days": 1,
        "description": "test",
        "history_start": date(2024, 1, 2),
    }
    base.update(kw)
    return DatasetSpec(**base)  # type: ignore[arg-type]


def test_register_idempotent_and_conflict() -> None:
    try:
        register_dataset(_spec())
        register_dataset(_spec())
        assert any(s.name == "t_ds" for s in all_datasets())
        with pytest.raises(ValueError, match="different spec"):
            register_dataset(_spec(max_lag_days=3))
    finally:
        unregister_dataset("t_ds")


def test_validation() -> None:
    with pytest.raises(ValueError, match="schema-qualified"):
        _spec(table="t")
    with pytest.raises(ValueError, match="series_column"):
        _spec(series=("A",))
