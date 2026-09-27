"""LiveKit runtime adapter for durable onboarding state."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterable
from datetime import UTC, datetime

from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    ChatContext,
    ChatMessage,
    JobContext,
    RunContext,
    TurnHandlingOptions,
    cli,
    function_tool,
    llm,
    room_io,
)
from livekit.plugins import elevenlabs, openai, silero

from state import StateError, Store

load_dotenv(os.path.join(os.path.dirname(__file__), ".env.local"))
server = AgentServer()


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} must be configured")
    return value


class OnboardingAgent(Agent):
    def __init__(
        self, store: Store, onboarding_id: str, connection_id: str, executor_id: str
    ):
        self.store = store
        self.onboarding_id = onboarding_id
        self.connection_id = connection_id
        self.executor_id = executor_id
        super().__init__(
            instructions=(
                "Interpret the customer's latest utterance for onboarding. "
                "Call exactly one available business tool with all supported facts. "
                "For a correction, include only fields explicitly corrected by the user. "
                "Use no facts absent from the utterance. An empty save_answers is valid. "
                "Do not write a reply; application code selects the next question."
            )
        )

    async def on_user_turn_completed(
        self, turn_ctx: ChatContext, new_message: ChatMessage
    ) -> None:
        action = await asyncio.to_thread(self.store.next_action, self.onboarding_id)
        context = {
            "action": action,
            "state_revision": (
                await asyncio.to_thread(self.store.get, self.onboarding_id)
            )["revision"],
            "captured_at": datetime.now(UTC).isoformat(),
        }
        await asyncio.to_thread(
            self.store.capture,
            self.onboarding_id,
            self.connection_id,
            self.executor_id,
            new_message.id,
            new_message.text_content,
            context,
        )

    async def llm_node(
        self,
        chat_ctx: ChatContext,
        tools: list[llm.Tool],
        model_settings,
    ) -> AsyncIterable[llm.ChatChunk | str]:
        latest = next(
            (
                item
                for item in reversed(chat_ctx.items)
                if isinstance(item, ChatMessage) and item.role == "user"
            ),
            None,
        )
        if latest is None:
            yield (await asyncio.to_thread(self.store.next_action, self.onboarding_id))[
                "text"
            ]
            return
        source_id = latest.id
        captured = await asyncio.to_thread(
            self.store.rows, "transcript_events", self.onboarding_id
        )
        input_event = next(
            (
                item
                for item in reversed(captured)
                if item["source_id"] == source_id and item["kind"] == "final_turn"
            ),
            None,
        )
        if input_event is None:
            raise StateError("Input persistence barrier was not reached")
        context = json.loads(input_event["context_json"])
        complete = False
        for kind in ("answers", "booking", "proposal"):
            if await asyncio.to_thread(
                self.store.operation, self.onboarding_id, source_id, kind
            ):
                complete = True
                break
        if complete:
            yield (await asyncio.to_thread(self.store.next_action, self.onboarding_id))[
                "text"
            ]
            return

        @function_tool(
            name="save_answers",
            description="Save all facts from this turn. answers_json maps field IDs to objects with value and optional status. correction_ids identifies explicit corrections.",
        )
        async def save_answers(
            run_context: RunContext, answers_json: str, correction_ids: list[str]
        ) -> str:
            current_revision = (
                await asyncio.to_thread(self.store.get, self.onboarding_id)
            )["revision"]
            result = await asyncio.to_thread(
                self.store.apply_answers,
                self.onboarding_id,
                self.connection_id,
                self.executor_id,
                source_id,
                json.loads(answers_json),
                current_revision,
                correction_ids,
            )
            return json.dumps(result)

        @function_tool(
            name="confirm_followup",
            description="Confirm or decline only the exact durable proposal presented for this turn after explicit customer approval or refusal.",
        )
        async def confirm_followup(run_context: RunContext, approved: bool) -> str:
            action = context["action"]
            if action["kind"] != "approval":
                raise StateError("No proposal was presented for this turn")
            proposal = next(
                row
                for row in self.store.rows("followups", self.onboarding_id)
                if row["id"] == action["id"]
            )
            result = await asyncio.to_thread(
                self.store.confirm,
                self.onboarding_id,
                self.connection_id,
                self.executor_id,
                source_id,
                action["id"],
                proposal["proposal_revision"],
                approved,
            )
            return json.dumps(result)

        @function_tool(
            name="propose_followup",
            description="Propose an exact future date and time supplied by the customer. start_local is ISO local date and time; timezone is an IANA timezone.",
        )
        async def propose_followup(
            run_context: RunContext, start_local: str, timezone: str
        ) -> str:
            result = await asyncio.to_thread(
                self.store.propose,
                self.onboarding_id,
                self.connection_id,
                self.executor_id,
                source_id,
                start_local,
                timezone,
                int(os.getenv("FOLLOWUP_SLOT_MINUTES", "30")),
            )
            return json.dumps(result)

        selected = {
            "approval": [confirm_followup],
            "proposal": [propose_followup],
        }.get(context["action"]["kind"], [save_answers])
        current = await asyncio.to_thread(self.store.get, self.onboarding_id)
        policy_ctx = chat_ctx.copy()
        policy_ctx.add_message(
            role="system",
            content=json.dumps(
                {
                    "original_turn_context": context,
                    "workflow_fields": current["workflow"],
                    "canonical_state": current["state"],
                    "rule": "Interpret the last user turn only. Call the provided business tool exactly once. Do not invent facts or claim a booking.",
                }
            ),
        )
        async for chunk in Agent.default.llm_node(
            self, policy_ctx, selected, model_settings
        ):
            if isinstance(chunk, str):
                continue
            if chunk.delta is not None:
                chunk.delta.content = None
            if chunk.delta is None or chunk.delta.tool_calls:
                yield chunk


@server.rtc_session(
    agent_name=os.getenv("ONBOARDING_AGENT_NAME", "resumable-onboarding")
)
async def entrypoint(ctx: JobContext) -> None:
    store = Store()
    await asyncio.to_thread(store.migrate)
    if ctx.is_fake_job:
        onboarding_id = required("ONBOARDING_CONSOLE_ID")
        credential = required("ONBOARDING_CONSOLE_CREDENTIAL")
        connection_id, _ = await asyncio.to_thread(
            store.connect_attempt, onboarding_id, credential, "console", "local-console"
        )
        participant = "local-console"
    else:
        metadata = json.loads(ctx.job.metadata)
        onboarding_id = metadata["onboarding_id"]
        connection_id = metadata["connection_id"]
        connection = next(
            row
            for row in store.rows("connections", onboarding_id)
            if row["id"] == connection_id
        )
        if connection["room"] != ctx.room.name:
            raise StateError("Dispatch room does not match authorized connection")
        participant = connection["participant"]
    executor_id = uuid.uuid4().hex
    await asyncio.to_thread(store.claim, connection_id, executor_id)

    async def renew_lease() -> None:
        while True:
            await asyncio.sleep(20)
            try:
                await asyncio.to_thread(store.claim, connection_id, executor_id)
            except StateError:
                ctx.shutdown("Connection ownership ended")
                return

    lease_task = asyncio.create_task(renew_lease())

    async def stop_lease() -> None:
        lease_task.cancel()
        try:
            await lease_task
        except asyncio.CancelledError:
            pass

    ctx.add_shutdown_callback(stop_lease)
    agent = OnboardingAgent(store, onboarding_id, connection_id, executor_id)
    session = AgentSession(
        stt=elevenlabs.STT(
            model=os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2_realtime")
        ),
        llm=openai.LLM.with_openrouter(
            model=required("OPENROUTER_MODEL"), tool_choice="required"
        ),
        tts=elevenlabs.TTS(
            voice_id=required("ELEVENLABS_VOICE_ID"),
            model=os.getenv("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5"),
        ),
        vad=silero.VAD.load(),
        turn_handling=TurnHandlingOptions(preemptive_generation={"enabled": False}),
    )

    @session.on("conversation_item_added")
    def observe(event) -> None:
        item = event.item
        if (
            isinstance(item, ChatMessage)
            and item.role == "assistant"
            and item.text_content
        ):
            asyncio.create_task(
                asyncio.to_thread(
                    store.observe_assistant,
                    onboarding_id,
                    connection_id,
                    item.id,
                    item.text_content,
                    bool(item.interrupted),
                )
            )

    await session.start(
        agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(participant_identity=participant),
    )
    await ctx.connect()
    pending = await asyncio.to_thread(store.pending_inputs, onboarding_id)
    for item in pending:
        recovered_context = ChatContext(
            items=[
                ChatMessage(id=item["source_id"], role="user", content=[item["text"]])
            ]
        )
        speech = session.generate_reply(chat_ctx=recovered_context)
        await speech.wait_for_playout()
        if await asyncio.to_thread(store.pending_inputs, onboarding_id):
            if (
                not await asyncio.to_thread(
                    store.operation, onboarding_id, item["source_id"], "answers"
                )
                and not await asyncio.to_thread(
                    store.operation, onboarding_id, item["source_id"], "booking"
                )
                and not await asyncio.to_thread(
                    store.operation, onboarding_id, item["source_id"], "proposal"
                )
            ):
                raise StateError("Pending input was not committed during recovery")
    if not pending:
        action = await asyncio.to_thread(store.next_action, onboarding_id)
        await session.say(action["text"])


if __name__ == "__main__":
    cli.run_app(server)
