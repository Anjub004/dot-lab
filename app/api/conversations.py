"""Conversation endpoints: chat threads whose messages start tasks.

Posting a message creates a task with the message as its goal. Earlier messages
of the conversation are given to the planner as short-term context, and the
task's result is added to the conversation as the assistant's reply.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.agent.state import TaskStatus
from app.api.deps import get_ctx
from app.api.tasks import answer_and_resume, check_can_create_task
from app.context import AppContext
from app.services.conversation_service import (
    Conversation,
    ConversationMessage,
    ConversationNotFoundError,
)

router = APIRouter(prefix="/conversations", tags=["conversations"])


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class ConversationDetail(BaseModel):
    conversation: Conversation
    messages: list[ConversationMessage]


class MessageCreate(BaseModel):
    content: str = Field(min_length=3, max_length=5000)

    @field_validator("content")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("must be at least 3 characters")
        return value


class MessagePosted(BaseModel):
    message: ConversationMessage
    task_id: str
    task_status: TaskStatus


def _get(ctx: AppContext, conversation_id: str) -> Conversation:
    try:
        return ctx.conversations.get(conversation_id)
    except ConversationNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found") from exc


@router.post("", response_model=Conversation, status_code=status.HTTP_201_CREATED)
def create_conversation(
    body: ConversationCreate, ctx: AppContext = Depends(get_ctx)
) -> Conversation:
    return ctx.conversations.create(body.title)


@router.get("", response_model=list[Conversation])
def list_conversations(ctx: AppContext = Depends(get_ctx)) -> list[Conversation]:
    return ctx.conversations.list()


@router.get("/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    conversation_id: str, ctx: AppContext = Depends(get_ctx)
) -> ConversationDetail:
    conversation = _get(ctx, conversation_id)
    return ConversationDetail(
        conversation=conversation, messages=ctx.conversations.messages(conversation_id)
    )


@router.post(
    "/{conversation_id}/messages",
    response_model=MessagePosted,
    status_code=status.HTTP_201_CREATED,
)
async def post_message(
    conversation_id: str, body: MessageCreate, ctx: AppContext = Depends(get_ctx)
) -> MessagePosted:
    """Add a user message and start a task for it.

    If a task of this conversation is waiting for an answer (WAITING_INPUT), the
    message is that answer and the waiting task continues instead.

    ``async`` on purpose: starting the background task needs the event loop,
    which a plain ``def`` endpoint (run in a worker thread) does not have.
    """
    _get(ctx, conversation_id)
    waiting = ctx.tasks.list_tasks(
        status=TaskStatus.WAITING_INPUT, conversation_id=conversation_id, limit=1
    )
    if waiting:
        task = answer_and_resume(ctx, waiting[0].id, body.content)
        message = ctx.conversations.add_message(conversation_id, "user", body.content, task.id)
        return MessagePosted(message=message, task_id=task.id, task_status=task.status)
    check_can_create_task(ctx, body.content)
    task = ctx.tasks.create_task(body.content, conversation_id=conversation_id)
    message = ctx.conversations.add_message(conversation_id, "user", body.content, task.id)
    ctx.runner.submit(task.id)
    return MessagePosted(message=message, task_id=task.id, task_status=task.status)


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(conversation_id: str, ctx: AppContext = Depends(get_ctx)) -> None:
    """Delete the conversation and its messages. Its tasks and their history are kept."""
    if not ctx.conversations.delete(conversation_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
