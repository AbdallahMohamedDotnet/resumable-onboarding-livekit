import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from livekit.agents import Agent, ChatContext, ChatMessage

from agent import OnboardingAgent, end_call_after_playout
from state import WORKFLOWS, Store


# Verify text input is saved before the model provider is called.
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

    # Check for persisted input when the fake provider is invoked.
    async def observe_provider_call(self, chat_ctx, tools, model_settings):
        pending = store.pending_inputs(onboarding_id)
        assert len(pending) == 1
        assert pending[0]["source_id"] == message.id
        assert [tool.id for tool in tools] == ["save_answers"]
        yield "ignored model prose"

    monkeypatch.setattr(Agent.default, "llm_node", observe_provider_call)
    tools = []
    assert [
        chunk
        async for chunk in agent.llm_node(ChatContext(items=[message]), tools, None)
    ] == []
    assert [tool.id for tool in tools] == ["save_answers"]
    assert len(store.pending_inputs(onboarding_id)) == 1


# Verify the native turn hook saves input before generating a reply.
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
    with pytest.raises(PermissionError):
        [
            chunk
            async for chunk in agent.llm_node(ChatContext(items=[message]), [], None)
        ]


@pytest.mark.asyncio
async def test_call_ends_only_after_completed_reply(tmp_path):
    store = Store(tmp_path / "agent.sqlite3")
    store.migrate()
    onboarding_id, credential = store.create()
    connection_id, _ = store.connect_attempt(
        onboarding_id, credential, "room", "person"
    )
    store.claim(connection_id, "executor")

    class Speech:
        interrupted = False

        def __init__(self):
            self.played = False
            self.release = asyncio.Event()

        async def wait_for_playout(self):
            await self.release.wait()
            self.played = True

        def exception(self):
            return None

    ctx = Mock()
    ctx.is_fake_job.return_value = False
    ctx.delete_room = AsyncMock()
    speech = Speech()
    speech.release.set()
    args = (store, onboarding_id, connection_id, "executor", ctx, set())
    assert not await end_call_after_playout(speech, *args)
    assert speech.played
    ctx.delete_room.assert_not_awaited()

    answers = {
        field["id"]: {"value": field["id"]}
        for field in WORKFLOWS[1]
        if field["required"] and field["id"] != "followup.customer_timezone"
    }
    answers["followup.availability"] = {"status": "declined", "value": None}
    store.capture(
        onboarding_id,
        connection_id,
        "executor",
        "final-turn",
        "I do not want a follow-up",
        {"action": store.next_action(onboarding_id)},
    )
    store.apply_answers(
        onboarding_id, connection_id, "executor", "final-turn", answers, 0
    )
    assert store.next_action(onboarding_id)["kind"] == "review_offer"
    store.capture(
        onboarding_id,
        connection_id,
        "executor",
        "skip-review",
        "No, thanks",
        {"action": store.next_action(onboarding_id)},
    )
    store.review_decision(
        onboarding_id, connection_id, "executor", "skip-review", "skip"
    )
    assert store.next_action(onboarding_id)["kind"] == "complete"
    closing_speech = Speech()
    closing = asyncio.create_task(end_call_after_playout(closing_speech, *args))
    await asyncio.sleep(0)
    ctx.delete_room.assert_not_awaited()
    closing_speech.release.set()
    assert await closing
    ctx.delete_room.assert_awaited_once()
    ctx.shutdown.assert_called_once_with("Onboarding complete")
