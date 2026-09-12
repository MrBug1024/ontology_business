from app.services import assistant_research_service
from app.services import assistant_orchestrator


def test_research_source_requires_https_and_preserves_provenance():
    item = assistant_research_service._source(
        {
            "title": "行业标准",
            "url": "https://example.com/standard",
            "snippet": "公开摘要",
            "published_at": "2026-01-01",
        },
        1,
        "2026-09-11T00:00:00+00:00",
    )

    assert item is not None
    assert item["citation_id"] == "W1"
    assert item["retrieved_at"].startswith("2026-09-11")
    assert item["kind"] == "web_research"


def test_research_source_rejects_non_https():
    assert assistant_research_service._source(
        {"title": "不安全", "url": "http://example.com"},
        1,
        "now",
    ) is None


def test_semantic_planner_routes_research_to_read_only_capability():
    decision = assistant_orchestrator.AssistantSemanticDecision(
        goal="research",
        scope="research",
        confidence="high",
        reason="需要行业知识",
    )
    state = {
        "decision": decision,
        "source": "model",
        "mode": "ask",
    }

    route = assistant_orchestrator._govern(state)

    assert route["intent"] == "research"
    assert route["branch"] == "answer"
