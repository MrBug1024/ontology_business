from types import SimpleNamespace

from app.services import distillation_capability_service as capability


def test_advisor_long_context_still_calls_default_decision_tool(monkeypatch):
    config = SimpleNamespace(id="synthetic-mcp", connector_revision=1)
    calls = []
    monkeypatch.setattr(capability, "resolve_jev_config", lambda db: config)

    def call_tool(cfg, name, arguments, **kwargs):
        calls.append(arguments)
        return {"status": "success", "text": '{"model":"test-model","results":['
            '{"choice":"核对事实和业务资料","probabilities":{"澄清业务价值与边界":0.1,'
            '"核对事实和业务资料":0.7,"生成业务模型草稿":0.1,"先人工确认取舍":0.1},"confidence":0.7},'
            '{"noul":0.2,"confidence":0.8}]}'}

    monkeypatch.setattr(capability.mcp_service, "call_tool", call_tool)
    receipt = capability.advisor_signal(SimpleNamespace(expunge=lambda row: None),
        "请核对业务资料。" * 2000, "场景上下文。" * 1000)
    assert receipt is not None and receipt.status == "succeeded"
    assert len(calls) == 1
    assert 0 < len(calls[0]["situation"]) <= capability.MAX_ADVISOR_SITUATION
