# Implementation Plan: assistant-ui as a Proper Custom Dash Component

**Updated:** drawer/modal embedded assistant design added.

**Target stack:** Python Dash + Dash Mantine Components (DMC) + custom React Dash component + FastAPI streaming endpoint + existing self-hosted Onyx agents.

**Primary goal:** add a production-grade agent chat interface to an existing Dash application without turning the whole app into a React/Next.js app. The primary embedding target is now a Dash/DMC drawer or modal popup, with optional inline usage for full-page views.

**Recommended architecture:** Dash remains the application shell and owns the drawer/modal lifecycle; assistant-ui is packaged as an embeddable Dash component; FastAPI provides the streaming agent gateway and conversation state; Onyx remains the agent/search backend.

---

## 1. Outcome

Build a reusable Python package, for example `mimir_dash_assistant`, exposing a Dash component:

```python
from mimir_dash_assistant import AssistantChat

AssistantChat(
    id="mimir-chat",
    apiBaseUrl="/api/assistant",
    conversationId="incident:INC1234567",
    agentId="mimir-default",
    mode="drawer",
    compact=True,
    autofocus=True,
    appContext={
        "app": "mimir",
        "source": "dash-drawer",
        "incident_id": "INC1234567",
    },
    height="100%",
    width="100%",
    showHeader=False,
    showCitations=True,
    showToolTimeline=True,
    showReasoningSummary=True,
)
```

The component should provide:

- streaming assistant messages
- markdown rendering
- code blocks
- citations / source cards
- tool-call timeline
- visible progress / reasoning summary, not raw hidden chain-of-thought
- stop generation
- regenerate
- edit previous prompt
- conversation persistence hooks
- drawer/modal-safe lifecycle handling
- reconnect/resume after drawer close/reopen
- compact citation and tool trace rendering
- Onyx-hosted agent support
- nginx/F5-compatible deployment
- audit-friendly event stream

---

## 2. Key references and constraints

### assistant-ui

assistant-ui supports custom runtimes for arbitrary backends. The most relevant options are:

- `LocalRuntime`: simplest approach; implement a `ChatModelAdapter.run` function and let assistant-ui manage messages, branching, editing, regeneration, and cancellation.
- `AssistantTransport`: better if you want richer agent-state snapshots and bidirectional agent interaction.
- `ExternalStoreRuntime`: useful if you want all message state owned outside assistant-ui.

For this implementation, start with **LocalRuntime** and a custom streaming adapter. Move to **AssistantTransport** later only if the agent UI needs full state synchronisation, collaborative sessions, or richer command/resume semantics.

Reference: <https://www.assistant-ui.com/docs/runtimes/custom/local-runtime>

### Dash custom components

Dash custom components are React components converted into Python classes. Props passed between Dash/Python and React must be JSON-serialisable; functions cannot be passed as props. Dash generates Python wrappers from React metadata and serves the JavaScript/CSS bundles with the app.

Reference: <https://dash.plotly.com/build-your-own-components>

### FastAPI streaming

Use Server-Sent Events (SSE) initially. SSE is native to browsers, reverse-proxy friendly, and a good fit for one-way token/event streaming. Use WebSockets later if you need bidirectional live control beyond normal HTTP requests.

Reference: <https://fastapi.tiangolo.com/tutorial/server-sent-events/>

### Onyx API

Onyx exposes REST APIs under `/api` for self-hosted deployments. API calls should target:

```text
https://your-self-hosted-onyx.com/api
```

Onyx supports API keys and personal access tokens. Basic API keys are suitable for non-admin application development, including Search, Chat, Agents, and Actions. The built-in OpenAPI explorer is available at:

```text
https://your-onyx-domain.com/api/docs
```

Reference: <https://docs.onyx.app/developers/overview>

---

## 3. Proposed architecture

```text
Browser
  |
  | Dash page loads custom AssistantChat component bundle
  v
Dash app / DMC shell
  |
  | React component calls /api/assistant/runs
  v
FastAPI assistant gateway
  |
  | validates user/session/app context
  | maps assistant-ui messages to Onyx agent request
  | calls Onyx Chat/Agent API
  | normalises Onyx response into Mímir event stream
  v
Onyx self-hosted instance
  |
  | RAG / agents / tools / connectors
  v
Indexed knowledge sources
  - curated Mímir markdown/wiki output
  - Confluence-derived content
  - ServiceNow-derived runbooks/incidents, if indexed
  - approved internal docs
```

Recommended URL layout behind nginx/F5:

```text
https://odin.int.refinitiv.com/companion/          -> Dash app
https://odin.int.refinitiv.com/companion/assets/   -> Dash/custom component bundles
https://odin.int.refinitiv.com/companion/api/      -> FastAPI assistant gateway
https://odin.int.refinitiv.com/onyx/               -> Onyx UI, optional
https://odin.int.refinitiv.com/onyx/api/           -> Onyx API, optional direct proxy
```

Prefer keeping browser-to-Onyx traffic indirect:

```text
Browser -> FastAPI gateway -> Onyx API
```

Do **not** expose Onyx API tokens to the browser.

---

## 3.1 Drawer/modal embedding direction

The assistant interface will usually be opened from inside the existing Dash application as a DMC `Drawer` or `Modal`, not as a full-page chat screen. This does not change the main architecture, but it changes the component contract and lifecycle requirements.

### Ownership boundaries

```text
Dash/DMC
  - owns open/close state
  - owns Drawer/Modal/AppShell placement
  - passes selected incident/change/runbook context
  - receives only high-level events from the chat component

AssistantChat React component
  - owns assistant-ui runtime
  - owns token streaming inside the browser component
  - renders transcript, composer, citations, tool trace and progress
  - does not own the Dash drawer/modal state

FastAPI assistant gateway
  - owns conversation state
  - owns run state and event replay
  - owns Onyx API calls and credentials
  - owns audit, persistence and permission enforcement
```

### Important implications

1. **Do not make `AssistantChat` full-screen by default.** It must be layout-neutral and work inside a constrained container.
2. **Let Dash/DMC own the `Drawer` or `Modal`.** The assistant component should not create its own global overlay.
3. **Persist conversation state server-side.** Drawer close/reopen, page refresh and component remount must not lose the conversation.
4. **Do not cancel automatically when the drawer closes.** Prefer explicit user cancellation via a Stop button.
5. **Support reconnect/resume.** When the drawer reopens, reload conversation history and reconnect to any active run stream if needed.
6. **Use compact rendering.** Drawer mode should use inline citation chips, per-message source accordions or tabs rather than a permanent wide side panel.
7. **Inject operational context from Dash.** The chat should know whether it was opened from an incident, change, runbook, service view, or source document.

Recommended default lifecycle:

```text
User opens drawer
  -> Dash passes context and conversationId
  -> AssistantChat loads conversation snapshot
  -> If an active run exists, reconnect to event stream

User submits prompt
  -> AssistantChat POSTs to FastAPI /runs
  -> FastAPI calls Onyx agent
  -> stream events update React state directly

User closes drawer
  -> UI detaches/hides
  -> active run continues unless user pressed Stop

User reopens drawer
  -> component reloads server-side state
  -> active/final messages are visible
```

Recommended conversation IDs for contextual embedding:

```text
incident:INC1234567
change:CHG1234567
runbook:aaa-cts-replication
service:aaa
source:confluence-page-12345
```

---

## 4. Component package structure

Create a dedicated package:

```text
mimir-dash-assistant/
  package.json
  tsconfig.json
  vite.config.ts or webpack.config.js
  pyproject.toml
  MANIFEST.in
  README.md
  src/
    lib/
      components/
        AssistantChat.react.tsx
        AssistantChat.css
      runtime/
        createMimirAdapter.ts
        streamParser.ts
        eventTypes.ts
        messageMapping.ts
      components/
        CitationPanel.tsx
        ToolTimeline.tsx
        ReasoningSummary.tsx
        MarkdownRenderer.tsx
  mimir_dash_assistant/
    __init__.py
    _imports_.py
    AssistantChat.py          # generated by Dash build
    metadata.json             # generated
    package-info.json         # generated
    mimir_dash_assistant.min.js
    mimir_dash_assistant.min.js.map
    async-*.js                # if code splitting enabled
```

Recommended naming:

- npm package: `mimir-dash-assistant`
- Python package: `mimir_dash_assistant`
- Dash component: `AssistantChat`

---

## 5. React/Dash component API

The Dash component props must be JSON-serialisable.

```ts
export type AssistantChatProps = {
  id?: string;

  // Required backend endpoint root.
  apiBaseUrl: string;

  // Optional identifiers.
  conversationId?: string;
  agentId?: string;
  userId?: string;

  // Context supplied by the Dash app.
  appContext?: Record<string, unknown>;

  // Embedding mode.
  mode?: "inline" | "drawer" | "modal";
  compact?: boolean;
  autofocus?: boolean;

  // Display options.
  height?: string;
  width?: string;
  placeholder?: string;
  showHeader?: boolean;
  showThreadList?: boolean;
  showSourcePanel?: boolean;
  showCitations?: boolean;
  showToolTimeline?: boolean;
  showReasoningSummary?: boolean;
  showDebugEvents?: boolean;
  readOnly?: boolean;

  // Feature toggles.
  enableAttachments?: boolean;
  enableRegenerate?: boolean;
  enableEditMessage?: boolean;
  enableStop?: boolean;
  enableFeedback?: boolean;

  // Lifecycle.
  cancelOnUnmount?: boolean;
  reconnectOnMount?: boolean;
  loadConversationOnMount?: boolean;
  activeRunId?: string;

  // Styling.
  className?: string;
  theme?: "light" | "dark" | "auto";

  // Optional event bridge back to Dash.
  lastEvent?: Record<string, unknown>;
  setProps?: (props: Partial<AssistantChatProps>) => void;
};
```

`setProps` is supplied by Dash. Use it sparingly. It can notify Dash of high-level events such as:

```json
{
  "type": "conversation.selected",
  "conversation_id": "conv_123"
}
```

Avoid calling `setProps` on every token. Token streaming should stay inside the React component to avoid flooding Dash callbacks.

---

## 6. Event protocol

Use a structured event stream between FastAPI and the assistant-ui adapter.

### Core event envelope

```json
{
  "id": "evt_01J...",
  "run_id": "run_01J...",
  "conversation_id": "conv_01J...",
  "message_id": "msg_01J...",
  "type": "answer.delta",
  "created_at": "2026-06-24T10:30:00Z",
  "data": {}
}
```

### Event types

```text
run.started
run.completed
run.cancelled
run.error

message.assistant.started
answer.delta
answer.completed

reasoning_summary.started
reasoning_summary.delta
reasoning_summary.completed

tool_call.started
tool_call.delta
tool_call.completed
tool_call.error

citation.added
artifact.added
feedback.recorded
```

### Example answer delta

```json
{
  "type": "answer.delta",
  "data": {
    "delta": "Check CTS replication lag first. "
  }
}
```

### Example citation

```json
{
  "type": "citation.added",
  "data": {
    "citation": {
      "id": "src_1",
      "title": "AAA CTS Replication Runbook",
      "source_type": "onyx_document",
      "url": "https://odin.int.refinitiv.com/onyx/document/abc",
      "chunk_id": "aaa-cts-019",
      "quote": "Replication lag can cause token validation failures.",
      "score": 0.84
    }
  }
}
```

### Example tool-call progress

```json
{
  "type": "tool_call.started",
  "data": {
    "tool_call_id": "tool_1",
    "name": "onyx_agent",
    "label": "Calling Onyx hosted agent",
    "input_summary": "agent=mimir-default, documents=curated wiki"
  }
}
```

---

## 7. Frontend implementation

### 7.1 Install dependencies

```bash
npm install react react-dom
npm install @assistant-ui/react
npm install zod
```

Depending on how you build the component package, you may also need:

```bash
npm install -D typescript vite @vitejs/plugin-react
npm install -D dash-component-boilerplate-related-tooling
```

The exact Dash component build toolchain varies by template. Use the Plotly boilerplate as the starting point, then modernise with TypeScript/Vite if desired.

### 7.2 AssistantChat.react.tsx

```tsx
import React, { useMemo } from "react";
import {
  AssistantRuntimeProvider,
  Thread,
  useLocalRuntime,
  type ChatModelAdapter,
} from "@assistant-ui/react";
import { createMimirAdapter } from "../runtime/createMimirAdapter";
import "./AssistantChat.css";

export type AssistantChatProps = {
  id?: string;
  apiBaseUrl: string;
  conversationId?: string;
  agentId?: string;
  userId?: string;
  appContext?: Record<string, unknown>;
  mode?: "inline" | "drawer" | "modal";
  compact?: boolean;
  autofocus?: boolean;
  height?: string;
  width?: string;
  placeholder?: string;
  showHeader?: boolean;
  showThreadList?: boolean;
  showSourcePanel?: boolean;
  showCitations?: boolean;
  showToolTimeline?: boolean;
  showReasoningSummary?: boolean;
  showDebugEvents?: boolean;
  readOnly?: boolean;
  enableRegenerate?: boolean;
  enableEditMessage?: boolean;
  enableStop?: boolean;
  enableFeedback?: boolean;
  cancelOnUnmount?: boolean;
  reconnectOnMount?: boolean;
  loadConversationOnMount?: boolean;
  activeRunId?: string;
  className?: string;
  theme?: "light" | "dark" | "auto";
  lastEvent?: Record<string, unknown>;
  setProps?: (props: Partial<AssistantChatProps>) => void;
};

export default function AssistantChat(props: AssistantChatProps) {
  const adapter: ChatModelAdapter = useMemo(() => {
    return createMimirAdapter({
      apiBaseUrl: props.apiBaseUrl,
      conversationId: props.conversationId,
      agentId: props.agentId,
      userId: props.userId,
      appContext: props.appContext ?? {},
      onHighLevelEvent: (event) => {
        props.setProps?.({ lastEvent: event });
      },
    });
  }, [
    props.apiBaseUrl,
    props.conversationId,
    props.agentId,
    props.userId,
    JSON.stringify(props.appContext ?? {}),
  ]);

  const runtime = useLocalRuntime(adapter);

  return (
    <div
      id={props.id}
      className={[
        "mimir-assistant-chat",
        `mimir-assistant-chat--${props.mode ?? "inline"}`,
        props.compact ? "mimir-assistant-chat--compact" : "",
        props.className ?? "",
      ].filter(Boolean).join(" ")}
      data-theme={props.theme ?? "auto"}
      data-mode={props.mode ?? "inline"}
      style={{
        height: props.height ?? "100%",
        width: props.width ?? "100%",
      }}
    >
      <AssistantRuntimeProvider runtime={runtime}>
        <Thread />
      </AssistantRuntimeProvider>
    </div>
  );
}
```

### 7.2.1 Drawer/modal-safe CSS

The component must be layout-neutral and safe inside DMC drawers and modals. Avoid viewport-owned positioning in the component itself.

```css
.mimir-assistant-chat {
  height: 100%;
  width: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.mimir-assistant-chat * {
  box-sizing: border-box;
}

.mimir-assistant-chat--drawer,
.mimir-assistant-chat--modal {
  min-height: 0;
}

.mimir-assistant-chat--compact {
  font-size: 0.95rem;
}

.mimir-assistant-thread {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
}

.mimir-assistant-composer {
  flex: 0 0 auto;
}
```

In drawer mode, avoid permanent two-column layouts unless the drawer is very wide. Prefer per-message accordions or a `Chat | Sources | Trace` tab pattern.

### 7.3 createMimirAdapter.ts

This adapter calls the FastAPI gateway and translates the custom SSE event stream into assistant-ui message deltas.

```ts
import type { ChatModelAdapter } from "@assistant-ui/react";
import { parseSseStream } from "./streamParser";

type CreateMimirAdapterArgs = {
  apiBaseUrl: string;
  conversationId?: string;
  agentId?: string;
  userId?: string;
  appContext: Record<string, unknown>;
  onHighLevelEvent?: (event: Record<string, unknown>) => void;
};

export function createMimirAdapter(args: CreateMimirAdapterArgs): ChatModelAdapter {
  return {
    async *run({ messages, abortSignal }) {
      const response = await fetch(`${args.apiBaseUrl}/runs`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        signal: abortSignal,
        body: JSON.stringify({
          conversation_id: args.conversationId,
          agent_id: args.agentId,
          user_id: args.userId,
          app_context: args.appContext,
          messages,
        }),
      });

      if (!response.ok || !response.body) {
        throw new Error(`Assistant request failed: ${response.status}`);
      }

      let text = "";
      const citations: unknown[] = [];
      const toolCalls: unknown[] = [];
      const reasoningSummary: string[] = [];

      for await (const event of parseSseStream(response.body)) {
        switch (event.type) {
          case "run.started":
          case "run.completed":
          case "run.error":
            args.onHighLevelEvent?.(event);
            break;

          case "answer.delta": {
            const delta = event.data?.delta ?? "";
            text += delta;

            yield {
              content: [
                {
                  type: "text",
                  text,
                },
              ],
            };
            break;
          }

          case "citation.added":
            citations.push(event.data?.citation);
            args.onHighLevelEvent?.(event);
            break;

          case "tool_call.started":
          case "tool_call.completed":
          case "tool_call.error":
            toolCalls.push(event.data);
            args.onHighLevelEvent?.(event);
            break;

          case "reasoning_summary.delta":
            reasoningSummary.push(event.data?.delta ?? "");
            args.onHighLevelEvent?.(event);
            break;

          default:
            if (event.type) {
              args.onHighLevelEvent?.(event);
            }
        }
      }
    },
  };
}
```

### 7.4 streamParser.ts

```ts
export type MimirStreamEvent = {
  id?: string;
  run_id?: string;
  conversation_id?: string;
  message_id?: string;
  type: string;
  created_at?: string;
  data?: Record<string, any>;
};

export async function* parseSseStream(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<MimirStreamEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const rawEvent = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);

      const dataLines = rawEvent
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trim());

      if (dataLines.length > 0) {
        const payload = dataLines.join("\n");
        try {
          yield JSON.parse(payload) as MimirStreamEvent;
        } catch {
          yield {
            type: "client.parse_error",
            data: { raw: payload },
          };
        }
      }

      boundary = buffer.indexOf("\n\n");
    }
  }
}
```

---

## 8. Python package integration

### 8.1 pyproject.toml

```toml
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "mimir-dash-assistant"
version = "0.1.0"
description = "assistant-ui based Dash custom component for Mimir/Odin/Forseti agent chat"
requires-python = ">=3.10"
dependencies = [
  "dash>=2.17",
]

[tool.setuptools.packages.find]
include = ["mimir_dash_assistant*"]
```

### 8.2 MANIFEST.in

```text
include mimir_dash_assistant/*.js
include mimir_dash_assistant/*.js.map
include mimir_dash_assistant/*.json
include mimir_dash_assistant/*.css
recursive-include mimir_dash_assistant *.js *.js.map *.json *.css
```

### 8.3 Python import surface

```python
# mimir_dash_assistant/__init__.py
from .AssistantChat import AssistantChat

__all__ = ["AssistantChat"]
```

### 8.4 Build workflow

```bash
# one-time setup
python -m venv .venv
source .venv/bin/activate
pip install -U pip build wheel
npm install

# frontend bundle + generated Python wrappers
npm run build

# package
python -m build

# local install into Dash app
pip install -e .
```

---

## 9. FastAPI assistant gateway

Create a separate Python service or mount FastAPI alongside Dash behind nginx.

Recommended service structure:

```text
assistant_gateway/
  app/
    main.py
    api/
      routes_chat.py
    core/
      config.py
      security.py
      logging.py
    models/
      chat.py
      events.py
      citations.py
    services/
      onyx_client.py
      stream_mapper.py
      conversation_store.py
      audit_log.py
  pyproject.toml
```

### 9.1 Dependencies

```toml
[project]
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.30",
  "httpx>=0.27",
  "pydantic>=2.7",
  "pydantic-settings>=2.2",
  "sqlalchemy>=2.0",
  "psycopg[binary]>=3.2",
  "redis>=5.0",
  "loguru>=0.7",
]
```

### 9.2 Settings

```python
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    onyx_base_url: str = "http://127.0.0.1:3000/api"
    onyx_api_token: str
    default_onyx_agent_id: str | None = None

    cors_allow_origins: list[str] = ["https://odin.int.refinitiv.com"]

    class Config:
        env_prefix = "MIMIR_ASSISTANT_"
        env_file = ".env"


settings = Settings()
```

Example environment:

```bash
export MIMIR_ASSISTANT_ONYX_BASE_URL="http://127.0.0.1:3000/api"
export MIMIR_ASSISTANT_ONYX_API_TOKEN="onyx_pat_or_api_key_here"
export MIMIR_ASSISTANT_DEFAULT_ONYX_AGENT_ID="mimir-default"
```

Use a Basic API key or PAT according to your Onyx permissions model. Avoid admin keys unless genuinely required.

---

## 10. FastAPI schemas

```python
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class AssistantChatRequest(BaseModel):
    conversation_id: str | None = None
    agent_id: str | None = None
    user_id: str | None = None
    app_context: dict[str, Any] = Field(default_factory=dict)
    messages: list[dict[str, Any]]


class Citation(BaseModel):
    id: str
    title: str
    source_type: str = "onyx_document"
    url: str | None = None
    chunk_id: str | None = None
    quote: str | None = None
    score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class StreamEvent(BaseModel):
    id: str = Field(default_factory=lambda: f"evt_{uuid4().hex}")
    run_id: str
    conversation_id: str | None = None
    message_id: str | None = None
    type: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    data: dict[str, Any] = Field(default_factory=dict)
```

---

## 11. FastAPI streaming endpoint

Use `StreamingResponse` or `EventSourceResponse` depending on your installed FastAPI version and compatibility requirements. `StreamingResponse` is simple and broadly understood.

```python
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.core.config import settings
from app.models.chat import AssistantChatRequest, StreamEvent
from app.services.onyx_client import OnyxClient
from app.services.stream_mapper import map_onyx_to_mimir_events

router = APIRouter(prefix="/api/assistant", tags=["assistant"])


def sse(event: StreamEvent) -> str:
    return f"data: {event.model_dump_json()}\n\n"


@router.post("/runs")
async def create_run(request: Request, body: AssistantChatRequest) -> StreamingResponse:
    run_id = f"run_{uuid4().hex}"
    conversation_id = body.conversation_id or f"conv_{uuid4().hex}"
    agent_id = body.agent_id or settings.default_onyx_agent_id

    async def stream() -> AsyncIterator[str]:
        yield sse(StreamEvent(
            run_id=run_id,
            conversation_id=conversation_id,
            type="run.started",
            data={
                "agent_id": agent_id,
                "app_context": body.app_context,
            },
        ))

        yield sse(StreamEvent(
            run_id=run_id,
            conversation_id=conversation_id,
            type="tool_call.started",
            data={
                "tool_call_id": "onyx_agent",
                "name": "onyx_agent",
                "label": "Calling Onyx hosted agent",
                "input_summary": f"agent_id={agent_id}",
            },
        ))

        try:
            onyx = OnyxClient(
                base_url=settings.onyx_base_url,
                token=settings.onyx_api_token,
            )

            async for mapped_event in map_onyx_to_mimir_events(
                onyx=onyx,
                run_id=run_id,
                conversation_id=conversation_id,
                agent_id=agent_id,
                messages=body.messages,
                app_context=body.app_context,
                is_disconnected=request.is_disconnected,
            ):
                yield sse(mapped_event)

            yield sse(StreamEvent(
                run_id=run_id,
                conversation_id=conversation_id,
                type="tool_call.completed",
                data={
                    "tool_call_id": "onyx_agent",
                    "name": "onyx_agent",
                    "summary": "Onyx agent response completed",
                },
            ))

            yield sse(StreamEvent(
                run_id=run_id,
                conversation_id=conversation_id,
                type="run.completed",
            ))

        except asyncio.CancelledError:
            yield sse(StreamEvent(
                run_id=run_id,
                conversation_id=conversation_id,
                type="run.cancelled",
            ))
            raise

        except Exception as exc:
            yield sse(StreamEvent(
                run_id=run_id,
                conversation_id=conversation_id,
                type="run.error",
                data={
                    "message": str(exc),
                },
            ))

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
```


---

## 11.1 Conversation snapshot, active run and cancellation endpoints

Drawer/modal usage requires explicit state reload and run control endpoints. Add these alongside `/runs` before production use.

```text
GET  /api/assistant/conversations/{conversation_id}
GET  /api/assistant/conversations/{conversation_id}/active-run
GET  /api/assistant/runs/{run_id}/events
POST /api/assistant/runs/{run_id}/cancel
```

### Conversation snapshot response

```json
{
  "conversation_id": "incident:INC1234567",
  "agent_id": "mimir-default",
  "title": "INC1234567 assistant",
  "messages": [],
  "citations": [],
  "tool_calls": [],
  "active_run": {
    "run_id": "run_abc",
    "status": "running"
  }
}
```

### Resume event stream

`GET /api/assistant/runs/{run_id}/events` should support replaying stored events from the last seen event ID where possible. This allows the assistant component to recover after drawer close/reopen, browser navigation, or transient stream interruption.

Implementation options:

```text
MVP:
  - store events in Postgres
  - on reconnect, replay all events for the run and let client de-duplicate by event id

Better:
  - accept ?after_event_id=evt_x
  - replay only later events
  - continue streaming live events via Redis pub/sub

Best:
  - maintain durable run state
  - support cancellation, resume, trace inspection and audit replay
```

### Cancellation behaviour

Closing the DMC drawer should not call `/cancel` by default. The user-facing Stop button should call `/runs/{run_id}/cancel`. The backend should then set a cancellation flag that the Onyx adapter checks between streamed events or tool calls.
---

## 12. Onyx client integration

The exact Onyx chat/agent endpoint shape can vary by Onyx version and deployment. Confirm the canonical request/response contract from:

```text
https://your-onyx-domain.com/api/docs
```

Build your client behind an adapter so endpoint changes do not affect the React component or Dash app.

### 12.1 Onyx client skeleton

```python
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx


class OnyxClient:
    def __init__(self, base_url: str, token: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    async def send_agent_message(
        self,
        *,
        agent_id: str | None,
        messages: list[dict[str, Any]],
        app_context: dict[str, Any],
    ) -> dict[str, Any]:
        """Non-streaming fallback call to Onyx.

        Replace the endpoint and payload with the exact contract from your
        Onyx /api/docs explorer.
        """
        payload = {
            "agent_id": agent_id,
            "messages": messages,
            "metadata": app_context,
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/chat/send-message",
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()
            return response.json()

    async def stream_agent_message(
        self,
        *,
        agent_id: str | None,
        messages: list[dict[str, Any]],
        app_context: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """Streaming Onyx call if your deployed Onyx endpoint supports streaming.

        Replace endpoint, payload, and parse logic with your Onyx version's
        OpenAPI contract.
        """
        payload = {
            "agent_id": agent_id,
            "messages": messages,
            "metadata": app_context,
            "stream": True,
        }

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/send-message",
                headers=self._headers(),
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    yield self._parse_onyx_line(line)

    def _parse_onyx_line(self, line: str) -> dict[str, Any]:
        import json
        return json.loads(line)
```

### 12.2 Message mapping

assistant-ui message objects should be normalised before sending to Onyx.

```python
def assistant_ui_messages_to_onyx(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    mapped: list[dict[str, str]] = []

    for message in messages:
        role = message.get("role")
        content = message.get("content", [])

        text_parts: list[str] = []
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(part.get("text", ""))

        if role in {"user", "assistant", "system"}:
            mapped.append({
                "role": role,
                "content": "\n".join(text_parts).strip(),
            })

    return mapped
```

### 12.3 Onyx response mapping

Keep this logic isolated. Onyx may emit answer deltas, final answers, source documents, tool results, or agent events depending on configuration/version.

```python
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from app.models.chat import Citation, StreamEvent
from app.services.onyx_client import OnyxClient
from app.services.message_mapping import assistant_ui_messages_to_onyx


async def map_onyx_to_mimir_events(
    *,
    onyx: OnyxClient,
    run_id: str,
    conversation_id: str,
    agent_id: str | None,
    messages: list[dict[str, Any]],
    app_context: dict[str, Any],
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[StreamEvent]:
    onyx_messages = assistant_ui_messages_to_onyx(messages)

    # Prefer streaming if available in your Onyx version.
    async for onyx_event in onyx.stream_agent_message(
        agent_id=agent_id,
        messages=onyx_messages,
        app_context=app_context,
    ):
        if await is_disconnected():
            yield StreamEvent(
                run_id=run_id,
                conversation_id=conversation_id,
                type="run.cancelled",
            )
            return

        event_type = onyx_event.get("type")

        if event_type in {"answer_delta", "message_delta", "token"}:
            yield StreamEvent(
                run_id=run_id,
                conversation_id=conversation_id,
                type="answer.delta",
                data={"delta": onyx_event.get("delta") or onyx_event.get("text") or ""},
            )

        elif event_type in {"source", "citation", "document"}:
            citation = Citation(
                id=onyx_event.get("id", "src_unknown"),
                title=onyx_event.get("title", "Untitled source"),
                source_type="onyx_document",
                url=onyx_event.get("url"),
                chunk_id=onyx_event.get("chunk_id"),
                quote=onyx_event.get("quote"),
                score=onyx_event.get("score"),
                metadata=onyx_event,
            )
            yield StreamEvent(
                run_id=run_id,
                conversation_id=conversation_id,
                type="citation.added",
                data={"citation": citation.model_dump()},
            )

        elif event_type in {"tool_start", "agent_step_start"}:
            yield StreamEvent(
                run_id=run_id,
                conversation_id=conversation_id,
                type="tool_call.started",
                data={
                    "tool_call_id": onyx_event.get("id"),
                    "name": onyx_event.get("name", "onyx_tool"),
                    "label": onyx_event.get("label", "Running Onyx tool"),
                },
            )

        elif event_type in {"tool_end", "agent_step_end"}:
            yield StreamEvent(
                run_id=run_id,
                conversation_id=conversation_id,
                type="tool_call.completed",
                data={
                    "tool_call_id": onyx_event.get("id"),
                    "name": onyx_event.get("name", "onyx_tool"),
                    "summary": onyx_event.get("summary"),
                },
            )

        else:
            yield StreamEvent(
                run_id=run_id,
                conversation_id=conversation_id,
                type="debug.onyx_event",
                data={"onyx_event": onyx_event},
            )

    yield StreamEvent(
        run_id=run_id,
        conversation_id=conversation_id,
        type="answer.completed",
    )
```

### 12.4 Fallback if Onyx endpoint is non-streaming

If your Onyx hosted agent endpoint only returns a complete response, fake streaming at the gateway for the UI while still storing the original response.

```python
async def stream_final_text_as_deltas(
    *,
    run_id: str,
    conversation_id: str,
    text: str,
) -> AsyncIterator[StreamEvent]:
    for token in text.split(" "):
        yield StreamEvent(
            run_id=run_id,
            conversation_id=conversation_id,
            type="answer.delta",
            data={"delta": token + " "},
        )
```

This gives acceptable UX while you later wire true streaming from Onyx.

---

## 13. Dash app integration

### 13.1 Basic layout

```python
from dash import Dash, html, Input, Output, callback
import dash_mantine_components as dmc
from mimir_dash_assistant import AssistantChat

app = Dash(
    __name__,
    requests_pathname_prefix="/companion/",
    routes_pathname_prefix="/companion/",
)

app.layout = dmc.MantineProvider([
    dmc.AppShell(
        header={"height": 56},
        navbar={"width": 280, "breakpoint": "sm"},
        padding="md",
        children=[
            dmc.AppShellHeader(
                dmc.Group([
                    dmc.Title("Mímir", order=3),
                    dmc.Badge("Onyx Agent Chat"),
                ], p="md")
            ),
            dmc.AppShellNavbar(
                dmc.Stack([
                    dmc.NavLink(label="Chat", active=True),
                    dmc.NavLink(label="Sources"),
                    dmc.NavLink(label="Runs"),
                    dmc.NavLink(label="Settings"),
                ], p="md")
            ),
            dmc.AppShellMain(
                AssistantChat(
                    id="mimir-chat",
                    apiBaseUrl="/companion/api/assistant",
                    conversationId="current",
                    agentId="mimir-default",
                    appContext={
                        "app": "mimir",
                        "knowledge_mode": "curated-wiki",
                    },
                    height="calc(100vh - 96px)",
                    showCitations=True,
                    showToolTimeline=True,
                    showReasoningSummary=True,
                    enableRegenerate=True,
                    enableEditMessage=True,
                    enableStop=True,
                )
            ),
        ],
    )
])
```

### 13.2 Use Dash callbacks only for high-level events

```python
@callback(
    Output("status-panel", "children"),
    Input("mimir-chat", "lastEvent"),
    prevent_initial_call=True,
)
def on_chat_event(event):
    if not event:
        return "No event"
    return f"Last chat event: {event.get('type')}"
```

Do not route token deltas through Dash callbacks.


### 13.3 Drawer integration pattern

For the planned embedded UI, let DMC own the drawer and place `AssistantChat` inside a flex container with explicit height and `minHeight: 0`.

```python
from dash import Dash, Input, Output, State, callback
import dash_mantine_components as dmc
from mimir_dash_assistant import AssistantChat

app = Dash(
    __name__,
    requests_pathname_prefix="/companion/",
    routes_pathname_prefix="/companion/",
)

app.layout = dmc.MantineProvider([
    dmc.Button("Ask Mímir", id="open-agent-chat"),

    dmc.Drawer(
        id="agent-chat-drawer",
        opened=False,
        position="right",
        size="xl",
        title="Mímir Assistant",
        keepMounted=True,
        children=[
            dmc.Box(
                h="calc(100vh - 96px)",
                style={
                    "display": "flex",
                    "flexDirection": "column",
                    "minHeight": 0,
                },
                children=[
                    AssistantChat(
                        id="mimir-agent-chat",
                        apiBaseUrl="/companion/api/assistant",
                        conversationId="current",
                        agentId="mimir-default",
                        mode="drawer",
                        compact=True,
                        autofocus=True,
                        height="100%",
                        width="100%",
                        showHeader=False,
                        showCitations=True,
                        showToolTimeline=True,
                        showReasoningSummary=True,
                        reconnectOnMount=True,
                        loadConversationOnMount=True,
                        cancelOnUnmount=False,
                        appContext={
                            "app": "mimir",
                            "entrypoint": "drawer",
                            "knowledge_mode": "curated-wiki",
                        },
                    )
                ],
            )
        ],
    ),
])


@callback(
    Output("agent-chat-drawer", "opened"),
    Input("open-agent-chat", "n_clicks"),
    State("agent-chat-drawer", "opened"),
    prevent_initial_call=True,
)
def toggle_agent_chat(_, opened):
    return not opened
```

If your DMC version does not support `keepMounted`, use server-side conversation persistence and `loadConversationOnMount=True` so remounting does not lose state.

### 13.4 Contextual drawer launch from a selected incident/change

The best UX is to open the assistant from a selected operational object and pass that object as context.

```python
AssistantChat(
    id="odin-agent-chat",
    apiBaseUrl="/companion/api/assistant",
    conversationId=f"incident:{selected_incident_id}",
    agentId="odin-incident-agent",
    mode="drawer",
    compact=True,
    height="100%",
    appContext={
        "app": "odin",
        "entrypoint": "incident-detail-drawer",
        "incident_id": selected_incident_id,
        "service": selected_service,
        "region": selected_region,
    },
)
```

Recommended context examples:

```text
Odin incident view:
  app=odin, incident_id, service, region, severity, selected_tab

Forseti change view:
  app=forseti, change_id, service, implementation_window, risk_score

Mímir source view:
  app=mimir, source_id, source_type, runbook_id, knowledge_mode

Tyr correlation view:
  app=tyr, incident_id, time_window, candidate_change_ids
```

### 13.5 Modal integration pattern

Use modal mode for short, focused tasks. Use drawer mode for persistent investigation while the user continues using the underlying Dash page.

```python
dmc.Modal(
    id="agent-chat-modal",
    opened=False,
    title="Ask about this change",
    size="80%",
    children=[
        dmc.Box(
            h="70vh",
            style={
                "display": "flex",
                "flexDirection": "column",
                "minHeight": 0,
            },
            children=[
                AssistantChat(
                    id="forseti-agent-chat",
                    apiBaseUrl="/companion/api/assistant",
                    conversationId="change:CHG1234567",
                    agentId="forseti-change-agent",
                    mode="modal",
                    compact=True,
                    height="100%",
                    showSourcePanel=False,
                    showCitations=True,
                    appContext={
                        "app": "forseti",
                        "change_id": "CHG1234567",
                    },
                )
            ],
        )
    ],
)
```
---

## 14. nginx/F5 routing

Since TLS is terminated by F5, nginx can run plain HTTP internally.

Example nginx config:

```nginx
server {
    listen 80;
    server_name odin.int.refinitiv.com;

    # Dash app
    location /companion/ {
        proxy_pass http://127.0.0.1:8020/companion/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
    }

    # FastAPI assistant gateway
    location /companion/api/ {
        proxy_pass http://127.0.0.1:8030/api/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;

        # Critical for SSE streaming
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        add_header X-Accel-Buffering no;
    }

    # Optional Onyx UI/API proxy
    location /onyx/ {
        proxy_pass http://127.0.0.1:3000/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
    }
}
```

If Onyx does not support being served from `/onyx/` cleanly, prefer exposing it on its own internal hostname/subdomain. The Dash chat component should not depend on the Onyx web UI path; it should call your FastAPI gateway.

---

## 15. Conversation persistence

Start with server-side persistence in Postgres.

Minimum tables:

```sql
CREATE TABLE assistant_conversation (
    id TEXT PRIMARY KEY,
    user_id TEXT,
    agent_id TEXT,
    title TEXT,
    app_context JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE assistant_message (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES assistant_conversation(id),
    role TEXT NOT NULL,
    content JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE assistant_event (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    message_id TEXT,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE assistant_citation (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    message_id TEXT,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL,
    url TEXT,
    chunk_id TEXT,
    quote TEXT,
    score DOUBLE PRECISION,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE assistant_feedback (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    rating TEXT,
    comment TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Persist:

- request payload
- Onyx request ID / chat session ID, if available
- all streamed events
- final assistant message
- citation objects
- user feedback
- errors and cancellation events

---

## 16. Security model

### Browser

The browser should receive only:

- Dash session cookie / SSO identity context
- generated conversation IDs
- streamed answer events
- citation metadata safe for display

The browser should never receive:

- Onyx API keys
- PATs
- internal service tokens
- unrestricted document IDs if the user should not access them

### FastAPI gateway

The gateway should:

- derive user identity from SSO/header/session
- enforce per-user agent access
- enforce document/source permissions if Onyx does not already do so for the selected key/token
- inject Onyx API token server-side
- validate `agent_id` against an allow-list
- log all runs
- redact secrets from logs

### Onyx authentication choice

Options:

1. **Single service Basic API key**
   - simplest
   - good for internal MVP
   - requires gateway-side access control

2. **Per-user PAT pass-through**
   - stronger user-level permission inheritance
   - harder token lifecycle
   - may be better if Onyx permissions must exactly match user permissions

3. **Brokered token mapping**
   - gateway maps SSO user to an Onyx token or limited service identity
   - best long-term enterprise model

Recommended first version: service Basic API key with strict gateway allow-lists and audit logging. Move to per-user/PAT or brokered identity after the UI proves value.

---

## 17. Handling citations cleanly

Citations should be separate from markdown. The model/agent may include `[1]` markers in the text, but the gateway should maintain structured source objects.

Recommended rendering model:

```text
Assistant message
  content: markdown text
  metadata:
    citations: [src_1, src_2]
    tool_calls: [tool_1]
```

UI behaviour:

- inline citation chips appear beside cited text
- right-hand source panel shows citation details
- clicking a citation opens the source panel
- source URLs point to Onyx document view, Confluence page, or internal source resolver
- quote snippets are short and source-bound
- show confidence/relevance score only if meaningful and calibrated

---

## 18. Reasoning/thinking display

Do not expose raw private chain-of-thought.

Expose deliberate progress events instead:

```text
Planning search query
Searching curated Mímir wiki
Calling Onyx agent
Reviewing returned source chunks
Building recommendation
```

Example event:

```json
{
  "type": "reasoning_summary.delta",
  "data": {
    "delta": "Searching curated Mímir runbooks for CTS replication symptoms."
  }
}
```

In the UI, label this as:

```text
Activity
```

or:

```text
Agent progress
```

Avoid labelling it as full internal reasoning.

---

## 19. Development phases

### Phase 0: Confirm Onyx API contract

Tasks:

- open `https://your-onyx-domain.com/api/docs`
- identify the exact endpoint for sending a message to a hosted agent
- check whether streaming is supported
- capture example request/response payloads
- capture source/citation response format
- determine how agent IDs are represented
- determine whether Onyx chat session IDs can be reused

Deliverable:

```text
onyx_api_contract.md
```

containing:

- endpoint
- auth header
- request body
- streaming format, if any
- non-streaming response format
- citation/source schema
- error schema

### Phase 1: FastAPI gateway prototype

Tasks:

- create `/api/assistant/runs`
- emit fake SSE events
- test from `curl -N`
- add nginx `proxy_buffering off`
- connect the endpoint to a simple HTML/JS EventSource/fetch-stream test

Acceptance criteria:

- browser receives token deltas without waiting for completion
- stop/cancel disconnect is detected
- nginx does not buffer the stream

### Phase 2: Onyx adapter

Tasks:

- implement `OnyxClient`
- map assistant-ui messages to Onyx payload
- map Onyx responses to Mímir events
- support non-streaming fallback
- normalise citations
- log raw Onyx responses in dev only

Acceptance criteria:

- FastAPI endpoint can call Onyx agent
- answer streams or fake-streams to the client
- citations appear as `citation.added` events
- errors become `run.error`

### Phase 3: Dash component shell

Tasks:

- create `mimir_dash_assistant` package
- wrap basic assistant-ui `Thread`
- expose JSON-serialisable props
- build package
- install into existing Dash app
- render component inside DMC `AppShell`

Acceptance criteria:

- `AssistantChat` imports from Python
- Dash serves the compiled JS bundle
- chat component appears inside existing Dash app

### Phase 4: Streaming adapter in React

Tasks:

- implement `createMimirAdapter`
- implement SSE parser
- translate `answer.delta` to assistant-ui content updates
- handle cancellation with `AbortSignal`
- surface high-level events via `setProps.lastEvent`

Acceptance criteria:

- user can ask a question
- answer appears incrementally
- stop generation works
- Dash callback can receive final/high-level run event

### Phase 4.5: Drawer/modal lifecycle

Tasks:

- render `AssistantChat` inside `dmc.Drawer`
- render `AssistantChat` inside `dmc.Modal`
- confirm layout works with `height="100%"`, `minHeight: 0` and compact mode
- implement conversation snapshot loading on mount
- implement active-run detection and reconnect/resume
- ensure drawer close does not automatically cancel active runs
- ensure explicit Stop button cancels via backend cancellation endpoint

Acceptance criteria:

- drawer opens and closes without losing persisted conversation state
- active run can continue while drawer is closed
- reopened drawer shows current/final state
- modal and drawer layouts do not overflow or break scrolling
- Dash receives high-level events only, not token deltas

### Phase 5: Citations and tool timeline

Tasks:

- create citation store in React component
- create `CitationPanel`
- create `ToolTimeline`
- map `citation.added` and `tool_call.*` events
- optionally add message-level metadata

Acceptance criteria:

- source cards render beside or below answer
- tool progress is visible
- source click opens document URL or source resolver

### Phase 6: Persistence and audit

Tasks:

- add Postgres tables
- persist conversations, messages, events, citations
- add `/api/assistant/conversations`
- add `/api/assistant/conversations/{id}`
- add conversation history picker in Dash or inside the component

Acceptance criteria:

- conversation survives page refresh
- previous messages can be reloaded
- run events can be inspected for debugging/audit

### Phase 7: Enterprise hardening

Tasks:

- SSO identity integration
- per-agent allow-list
- per-user conversation ownership
- rate limiting
- payload size limits
- timeout controls
- structured logs
- OpenTelemetry tracing, optional
- prompt/response redaction rules
- security review

Acceptance criteria:

- no browser-visible Onyx secrets
- users cannot select unauthorised agents
- request volume is controlled
- incident/debug traces are available

---

## 20. Testing strategy

### Frontend tests

Use Vitest/React Testing Library where practical.

Test:

- component renders with required props
- stream parser handles chunk boundaries
- malformed JSON becomes parse error event
- answer deltas accumulate correctly
- citation events are stored
- abort signal cancels fetch

### Backend tests

Use pytest + httpx test client.

Test:

- `/api/assistant/runs` emits valid SSE
- event envelopes validate
- Onyx client maps payloads correctly
- non-streaming fallback works
- disconnection cancels stream
- errors are converted to `run.error`
- secrets are not logged

### Integration tests

Use Playwright.

Test:

- Dash app loads component
- sending a prompt creates a streamed answer
- citations appear
- stop button works
- regenerate works
- nginx route does not buffer stream
- drawer close/reopen preserves or reloads conversation state
- modal layout scrolls correctly
- active run resume works after component remount

---

## 21. Local development commands

Terminal 1: Onyx

```bash
# Existing Onyx stack
# Example only; use your existing Onyx docker compose setup.
docker compose up -d
```

Terminal 2: FastAPI gateway

```bash
cd assistant_gateway
export MIMIR_ASSISTANT_ONYX_BASE_URL="http://127.0.0.1:3000/api"
export MIMIR_ASSISTANT_ONYX_API_TOKEN="..."
uvicorn app.main:app --host 0.0.0.0 --port 8030 --reload
```

Terminal 3: component package

```bash
cd mimir-dash-assistant
npm install
npm run build
pip install -e .
```

Terminal 4: Dash app

```bash
cd companion_dash_app
python app.py
```

Test streaming:

```bash
curl -N \
  -X POST http://127.0.0.1:8030/api/assistant/runs \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":[{"type":"text","text":"How do I check CTS replication?"}]}],"agent_id":"mimir-default"}'
```

---

## 22. Production deployment model

Recommended processes:

```text
onyx-web        : existing Onyx web/API process, port 3000
mimir-dash      : Dash/DMC application, port 8020
assistant-api   : FastAPI assistant gateway, port 8030
postgres        : conversation/audit store
redis           : optional pub/sub, cancellation, rate limit state
nginx           : path routing behind F5
```

Example systemd service for FastAPI:

```ini
[Unit]
Description=Mimir Assistant FastAPI Gateway
After=network.target

[Service]
User=mimir
WorkingDirectory=/opt/mimir/assistant_gateway
EnvironmentFile=/etc/mimir/assistant.env
ExecStart=/opt/mimir/assistant_gateway/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8030
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

## 23. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---:|---|
| Onyx API endpoint shape changes | Medium | Hide Onyx behind `OnyxClient` adapter; use SemVer-aware testing |
| Onyx does not stream hosted agent output | Medium | Gateway fake-streams final response initially |
| nginx buffers SSE | High | `proxy_buffering off`, `X-Accel-Buffering no`, test with `curl -N` |
| Dash callback overload | High | Do not send token deltas through Dash `setProps` |
| assistant-ui API changes | Medium | Pin npm versions; wrap usage behind small local adapter |
| Citation schema mismatch | Medium | Normalise citations in FastAPI gateway |
| Browser receives secrets | High | Browser only calls gateway; gateway injects Onyx token |
| Per-user permission mismatch | High | Start with allow-lists; later broker user-scoped Onyx tokens |
| Subpath deployment breaks assets | Medium | Test under `/companion/`; ensure Dash prefixes and nginx paths match |
| Drawer/modal unmount loses local state | High | Persist state server-side; use `keepMounted` where available; reload snapshot on mount |
| Drawer close accidentally cancels long Onyx run | Medium | Default `cancelOnUnmount=False`; provide explicit Stop button |
| Citation panel too wide for drawer | Medium | Use compact mode, per-message accordions, tabs, or nested source drawer |

---

## 24. Recommended first milestone

Build the smallest version that proves the architecture:

```text
1. Dash page renders custom AssistantChat component inside a DMC drawer.
2. Drawer can open/close without breaking layout or losing persisted state.
3. Component sends prompt to `/companion/api/assistant/runs`.
4. FastAPI calls one Onyx hosted agent.
5. Answer streams back as `answer.delta` events.
6. At least one citation renders in compact drawer mode.
7. Stop generation works.
8. Reopen drawer reloads conversation state.
9. No Onyx credentials are visible in browser dev tools.
```

This is the point where the design becomes clearly better than a Dash-only chatbot.

---

## 25. Longer-term enhancements

After the first production-quality version:

- switch from `LocalRuntime` to `AssistantTransport` if rich agent-state synchronisation becomes necessary
- add conversation search
- add saved prompts
- add file upload and attachment indexing
- add ServiceNow incident context injection
- add approval cards for controlled actions
- add model/agent selector
- add admin trace viewer
- add evaluation capture for answer quality
- add golden test questions for CIAM/AAA runbooks
- add Onyx source health panel
- add explicit “curated wiki only” vs “all indexed sources” mode

---

## 26. Final recommendation

Implement this as a proper custom Dash component, but keep the agent runtime and streaming protocol outside Dash.

The clean boundary is:

```text
Dash/DMC owns application layout, operational workflow, and drawer/modal state.
assistant-ui owns the embedded chat UX.
FastAPI owns streaming, auth, auditing, and Onyx adaptation.
Onyx owns enterprise search, hosted agents, and indexed knowledge.
```

This gives you a production-grade chat client without sacrificing your existing Python/Dash investment.
