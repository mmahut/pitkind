import pytest
from pydantic import ValidationError

from pitkind.config import Config, ModelEntry, NCLConfig
from pitkind.schemas import ModelOutput

_MODEL = {"openrouter_id": "vendor/model-1", "decoding_params": {"temperature": 0}}


def _config_with_n_models(n: int) -> dict:
    return {
        "models": [dict(_MODEL, openrouter_id=f"vendor/model-{i}") for i in range(n)],
        "ballot_categories": ["YES", "NO", "ABSTAIN"],
    }


def test_odd_model_count_accepted():
    config = Config.model_validate(_config_with_n_models(5))
    assert len(config.models) == 5


def test_provider_schema_replaces_stale_yaml_and_requires_all_deliberation_fields():
    params = {"response_format": {"type": "json_schema", "json_schema": {"schema": {"obsolete": True}}}}
    entry = ModelEntry(openrouter_id="test", decoding_params=params)
    response_format = entry.decoding_params["response_format"]
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert set(schema["required"]) == set(ModelOutput.model_fields)
    for definition in [schema, *schema["$defs"].values()]:
        assert definition["additionalProperties"] is False
        assert set(definition["required"]) == set(definition["properties"])
        assert all("default" not in field for field in definition["properties"].values())
    provision = schema["$defs"]["ProvisionAssessment"]
    assert "evidence" in provision["required"]
    assert "cannot_determine" in provision["properties"]["assessment"]["enum"]
    assert params["response_format"]["json_schema"]["schema"] == {"obsolete": True}


def test_even_model_count_rejected():
    with pytest.raises(ValidationError, match="odd number"):
        Config.model_validate(_config_with_n_models(4))


def test_empty_model_list_rejected():
    with pytest.raises(ValidationError):
        Config.model_validate(_config_with_n_models(0))


def test_non_binary_ballot_rejected():
    raw = _config_with_n_models(3)
    raw["ballot_categories"] = ["YES"]
    with pytest.raises(ValidationError, match="exactly"):
        Config.model_validate(raw)


def test_unknown_ballot_category_rejected():
    raw = _config_with_n_models(3)
    raw["ballot_categories"] = ["YES", "NO", "MAYBE"]
    with pytest.raises(ValidationError, match="exactly"):
        Config.model_validate(raw)


def test_ncl_is_optional_and_accepts_integer_lovelace():
    raw = _config_with_n_models(3)
    assert Config.model_validate(raw).ncl is None
    raw["ncl"] = {"limit_lovelace": 42604371000000}
    assert Config.model_validate(raw).ncl.model_dump() == raw["ncl"]


@pytest.mark.parametrize("change", [
    {"limit_lovelace": -1}, {"limit_lovelace": 1.5}, {"limit_lovelace": True},
    {"limit_lovelace": "42604371000000"}, {"start_epoch": 613},
    {"end_epoch": 713}, {"limit_lovelace": 0}, {"limit_ada": 42604371},
])
def test_invalid_ncl_is_rejected(change):
    with pytest.raises(ValidationError):
        NCLConfig.model_validate({"limit_lovelace": 42604371000000, **change})
