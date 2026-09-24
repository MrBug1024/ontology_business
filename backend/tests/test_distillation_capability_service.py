from types import SimpleNamespace

from app.distillation_conversation_schemas import JevDecisionArguments
from app.services import distillation_capability_service as capability


def _snapshot():
    return {
        "version": 4,
        "capability_mcps": [{
            "id": "jev-id",
            "name": "jev_decide",
            "name_key": "jev_decide",
            "transport": "streamable_http",
            "connector_revision": 1,
        }],
    }


def _config():
    return SimpleNamespace(
        id="jev-id",
        name="jev_decide",
        name_key="jev_decide",
        connector_revision=1,
        transport="streamable_http",
        url="https://jev.invalid/mcp",
        headers={},
        env={},
        args=[],
        command="",
    )


class _Db:
    def expunge(self, _row):
        return None


def test_jev_result_normalizes_choice_score_and_noul(monkeypatch):
    config = _config()
    turn = SimpleNamespace(id="turn-id", context={"resource_selection": _snapshot()})
    monkeypatch.setattr(capability, "_selected_config", lambda _db, _turn: config)
    monkeypatch.setattr(capability.mcp_service, "call_tool", lambda *args, **kwargs: {
        "status": "success",
        "text": '{"model":"open-jev-deberta-v3-large","results":['
        '{"choice":"人工复核","probabilities":{"自动处理":0.2,"人工复核":0.7,"暂不处理":0.1},"confidence":0.7},'
        '{"score":1.25,"probabilities":{"低":0.2,"中":0.3,"高":0.5},"confidence":0.5},'
        '{"noul":0.82,"confidence":0.82}]}',
    })

    result = capability.execute(_Db(), {
        "situation": "订单已完成审核",
        "questions": [
            {"type": "choice", "question": "下一步", "options": ["自动处理", "人工复核", "暂不处理"]},
            {"type": "score", "question": "准备度", "options": ["低", "中", "高"]},
            {"type": "yes_no", "question": "是否完成审核"},
        ],
    }, turn)

    assert result.content["status"] == "success"
    assert result.content["results"][0]["choice_index"] == 1
    assert result.content["results"][1]["score"] == 1.25
    assert result.content["results"][2]["true_probability"] == 0.82
    assert result.capability_receipt is not None
    assert result.capability_receipt.status == "succeeded"


def test_jev_error_status_is_blocked_and_not_a_success_receipt(monkeypatch):
    config = _config()
    turn = SimpleNamespace(id="turn-id", context={"resource_selection": _snapshot()})
    monkeypatch.setattr(capability, "_selected_config", lambda _db, _turn: config)
    monkeypatch.setattr(capability.mcp_service, "call_tool", lambda *args, **kwargs: {
        "status": "error", "text": "model unavailable",
    })

    result = capability.execute(_Db(), {
        "situation": "需要判断",
        "questions": [{"type": "yes_no", "question": "是否继续"}],
    }, turn)

    assert result.content["status"] == "blocked"
    assert result.capability_receipt is not None
    assert result.capability_receipt.status == "failed"
    assert result.capability_receipt.results == []


def test_new_snapshot_exposes_jev_as_capability_not_mcp_material():
    assert capability.effective_keys(_snapshot()) == ("jev_decide",)
    assert "mcps" not in _snapshot()
    payload = JevDecisionArguments(
        situation="bounded",
        questions=[{"type": "yes_no", "question": "continue?"}],
    )
    assert payload.questions[0].options is None
