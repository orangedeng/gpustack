import pytest
from pydantic import ValidationError

from gpustack.schemas.models import (
    CategoryEnum,
    Model,
    ModelCreate,
    is_decision_model,
)
from gpustack.schemas.model_routes import ModelRouteUpdate


def test_decision_category_in_enum():
    assert CategoryEnum.DECISION.value == "decision"


def test_route_accepts_decision_category():
    route = ModelRouteUpdate(name="d3-route", categories=["decision"])
    assert route.categories == ["decision"]


def test_route_still_rejects_unknown_category():
    with pytest.raises(ValidationError):
        ModelRouteUpdate(name="bad-route", categories=["vqa"])


def test_is_decision_model():
    assert is_decision_model(Model(id=1, name="d3", categories=["decision"]))
    assert not is_decision_model(Model(id=2, name="qwen", categories=["llm"]))
    assert not is_decision_model(Model(id=3, name="plain"))


def _model_payload(**overrides):
    payload = {
        "name": "d3",
        "source": "huggingface",
        "huggingface_repo_id": "vllm-sr/d3-flash",
    }
    payload.update(overrides)
    return payload


def test_model_write_rejects_unknown_category():
    # The write boundary is ModelCreate/ModelUpdate (non-table SQLModel —
    # table classes skip validators on construction). A typo'd decision
    # category would silently create a model that is neither a decision
    # provider nor filterable as one: the identity IS the string, and
    # nothing auto-corrects it.
    with pytest.raises(ValidationError, match="Invalid category"):
        ModelCreate.model_validate(_model_payload(categories=["Decision"]))


def test_model_write_accepts_every_enum_category():
    for value in CategoryEnum:
        payload = ModelCreate.model_validate(_model_payload(categories=[value]))
        assert value in payload.categories
