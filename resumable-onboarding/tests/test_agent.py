import json

import pytest
from livekit.agents import Agent, ChatContext, ChatMessage

from agent import OnboardingAgent
from state import Store, Unauthorized


@pytest.mark.asyncio
async def test_text_input_is_durable_before_llm_request(tmp_path, monkeypatch):
    store = Store(tmp_path / "agent.sqlite3")
    store.migrate()
    onboarding_id, credential = store.create()
    connection_id, _ = store.connect_attempt(
        onboarding_id, credential, "console", "local-console"
    )
    store.claim(connection_id, "executor")
    agent = OnboardingAgent(store, onboarding_id, connection_id, "executor")
    message = ChatMessage(role="user", content=["My name is Alice Example."])

    async def observe_provider_call(self, chat_ctx, tools, model_settings):
        pending = store.pending_inputs(onboarding_id)
        assert len(pending) == 1
        assert pending[0]["source_id"] == message.id
        assert [tool.id for tool in tools] == ["save_answers"]
        yield "ignored model prose"

    monkeypatch.setattr(Agent.default, "llm_node", observe_provider_call)
    tools = []
    assert [
        chunk async for chunk in agent.llm_node(ChatContext(items=[message]), tools, None)
    ] == []
    assert [tool.id for tool in tools] == ["save_answers"]
    assert len(store.pending_inputs(onboarding_id)) == 1


@pytest.mark.asyncio
async def test_native_turn_hook_persists_before_reply(tmp_path):
    store = Store(tmp_path / "agent.sqlite3")
    store.migrate()
    onboarding_id, credential = store.create()
    connection_id, _ = store.connect_attempt(
        onboarding_id, credential, "room", "person"
    )
    store.claim(connection_id, "executor")
    agent = OnboardingAgent(store, onboarding_id, connection_id, "executor")
    message = ChatMessage(role="user", content=["I am Ahmed from Atlas"])
    await agent.on_user_turn_completed(ChatContext(), message)
    pending = store.pending_inputs(onboarding_id)
    assert len(pending) == 1
    assert pending[0]["source_id"] == message.id
    assert json.loads(pending[0]["context_json"])["action"]["id"] == "customer.name"
    store.apply_answers(
        onboarding_id,
        connection_id,
        "executor",
        message.id,
        {"customer.name": {"value": "Ahmed"}, "company.name": {"value": "Atlas"}},
        0,
    )
    reply = [
        chunk async for chunk in agent.llm_node(ChatContext(items=[message]), [], None)
    ]
    assert reply == ["What is the best email or phone number to reach you?"]
    assert store.pending_inputs(onboarding_id) == []
    store.connect_attempt(onboarding_id, credential, "new-room", "new-person")
    with pytest.raises(Unauthorized):
        [
            chunk
            async for chunk in agent.llm_node(ChatContext(items=[message]), [], None)
        ]
