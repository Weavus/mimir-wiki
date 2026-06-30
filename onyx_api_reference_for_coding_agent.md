# Onyx API Reference for Offline Coding Agents

_Last refreshed from official Onyx documentation: 2026-06-24._

This file is a concise implementation reference for integrating a Python/Dash/FastAPI application with Onyx-hosted agents. It is intended for a coding agent that does not have live web access.

Primary source docs:

- `https://docs.onyx.app/developers/overview`
- `https://docs.onyx.app/developers/core_concepts`
- `https://docs.onyx.app/developers/guides/chat_new_guide`
- `https://docs.onyx.app/developers/guides/index_files_ingestion_api`
- `https://docs.onyx.app/developers/guides/create_connector`
- `https://docs.onyx.app/developers/api_reference/...`

---

## 1. Base URL and Authentication

Onyx API base URL:

```text
https://cloud.onyx.app/api
https://<your-self-hosted-onyx-domain>/api
```

For the user's current environment, behind nginx/F5, this will likely be something like:

```text
https://odin.int.refinitiv.com/onyx/api
```

or, if nginx maps `/onyx/` to the Onyx web/backend service while stripping the prefix:

```text
https://odin.int.refinitiv.com/api
```

Use whichever path actually reaches the Onyx backend API.

All documented endpoints require Bearer authentication:

```http
Authorization: Bearer <ONYX_API_KEY_OR_PAT>
Content-Type: application/json
```

Token types:

| Token type | Scope | Use case |
|---|---|---|
| Admin API Key | Full access, including `/admin/` and `/manage/admin/` endpoints | Provisioning connectors, credentials, users, agents, tools |
| Basic API Key | Non-admin endpoints such as Chat, Search, Agents, Actions | Application integration, chat UI, normal agent use |
| Limited API Key | Read-only agent access; can post chat messages but cannot read chat history | Restricted integrations |
| Personal Access Token | Authenticates as a real user and inherits that user's permissions | User-specific integrations |

Recommended for app integration: use a **Basic API Key** for chat/search unless you need admin operations. Use Admin API Keys only from trusted backend services, never from browser code.

Python client baseline:

```python
import requests

ONYX_API_BASE_URL = "https://<onyx-domain>/api"
ONYX_API_KEY = "..."

headers = {
    "Authorization": f"Bearer {ONYX_API_KEY}",
    "Content-Type": "application/json",
}
```

---

## 2. Core Concepts

### Agents / Personas / Assistants

Onyx uses the terms **Agent**, **Persona**, and **Assistant** interchangeably in much of the API. Many endpoints still use `persona` in the URL.

Built-in agent IDs:

| ID | Agent | Purpose |
|---:|---|---|
| `0` | Search Agent | Uses internal search over indexed knowledge |
| `-1` | General Agent | Basic LLM chat, no tools |
| `-2` | Paraphrase Agent | Uses search and quotes exact snippets |
| `-3` | Art Agent | Image generation |

Most chat calls require a `persona_id` either via `chat_session_info.persona_id` or by continuing an existing session that already has one.

### Actions / Tools

Actions are tools an agent may use: internal search, web search, code execution, image generation, custom OpenAPI tools, MCP tools, etc.

Use:

```http
GET /tool
```

to list tool IDs. Chat calls can restrict tools with `allowed_tool_ids` or force one with `forced_tool_id`.

### Connectors, Credentials, and CC-pairs

- `Connector`: defines what to index and how, for example Confluence, Jira, web, file, SharePoint.
- `Credential`: authentication details for the source system.
- `ConnectorCredentialPair` / `cc_pair`: active combination of connector + credential.

For API-created connectors, creating a Connector alone is not enough. It must be associated with a Credential to become active/visible.

### Documents

Documents are indexed items. Important fields:

```json
{
  "id": "optional-stable-id",
  "semantic_identifier": "Title shown in UI",
  "title": "Optional search title",
  "sections": [
    {"text": "section text", "link": "https://source/link"}
  ],
  "source": "file",
  "metadata": {"key": "value"},
  "doc_updated_at": "2025-09-19T08:20:00Z"
}
```

Valid `source` values include: `ingestion_api`, `web`, `slack`, `google_drive`, `gmail`, `github`, `gitlab`, `confluence`, `jira`, `file`, `notion`, `zendesk`, `sharepoint`, `teams`, `salesforce`, `s3`, `r2`, `google_cloud_storage`, `wikipedia`, `freshdesk`, `airtable`, `imap`, `bitbucket`, and more.

Common `input_type` values:

| Value | Meaning |
|---|---|
| `load_state` | One-time load |
| `poll` | Recurring sync/polling |
| `event` | Event-driven, not implemented for most connectors |
| `slim_retrieval` | Permission-sync retrieval mode |

Common `access_type` values:

| Value | Meaning |
|---|---|
| `public` | All Onyx users may access data |
| `private` | Creator and specified groups only |
| `sync` | Source-system permission sync, Enterprise only / connector-dependent |

---

## 3. Chat API

### 3.1 Send Chat Message

Endpoint:

```http
POST /chat/send-chat-message
```

This is the primary endpoint for sending messages to an Onyx agent. It is the same API family used by the Onyx frontend. It can return either:

- SSE streaming packets when `stream: true`.
- A complete JSON response when `stream: false`.

Minimal non-streaming request:

```bash
curl --request POST \
  --url "$ONYX_API_BASE_URL/chat/send-chat-message" \
  --header "Authorization: Bearer $ONYX_API_KEY" \
  --header "Content-Type: application/json" \
  --data '{
    "message": "What is Onyx?",
    "stream": false,
    "include_citations": true,
    "chat_session_info": {
      "persona_id": 0
    }
  }'
```

Typical streaming request:

```json
{
  "message": "Summarise the AAA CTS replication runbook",
  "chat_session_info": {
    "persona_id": 0,
    "project_id": null,
    "description": "Mimir drawer chat"
  },
  "chat_session_id": null,
  "parent_message_id": -1,
  "stream": true,
  "include_citations": true,
  "origin": "api",
  "additional_context": "User is viewing incident INC1234567 in Odin.",
  "internal_search_filters": {
    "source_type": ["confluence", "file"],
    "document_set": ["Mimir Runbooks"],
    "time_cutoff": null,
    "tags": [
      {"tag_key": "service", "tag_value": "AAA"}
    ]
  },
  "allowed_tool_ids": null,
  "forced_tool_id": null,
  "llm_override": null,
  "deep_research": false,
  "file_descriptors": []
}
```

Important request fields:

| Field | Type | Notes |
|---|---|---|
| `message` | string | User message to send |
| `chat_session_id` | UUID/null | Existing chat session to continue. Omit/null to create a new session |
| `chat_session_info.persona_id` | int | Agent ID to use when creating a session |
| `chat_session_info.project_id` | int/null | Optional project scope |
| `parent_message_id` | int/null | Previous message ID. Default `-1`. If explicitly `null`, history is reset and the new message becomes the first message |
| `stream` | bool | `true` for SSE packets; `false` for full JSON |
| `include_citations` | bool | Include source citation metadata |
| `additional_context` | string/null | Ephemeral request-scoped context injected into the LLM call but not stored in chat history |
| `internal_search_filters` | object | Filters for internal search results |
| `allowed_tool_ids` | int[]/null | `null` = allow agent configured tools; `[]` = disable all tools |
| `forced_tool_id` | int/null | Force use of a specific tool |
| `llm_override` | object/null | Optional `model_provider`, `model_version`, `temperature` |
| `deep_research` | bool | Expensive deep research mode |
| `file_descriptors` | array | Files to include with the request |

`internal_search_filters` shape:

```json
{
  "source_type": ["confluence", "web", "file"],
  "document_set": ["Runbooks"],
  "time_cutoff": "2023-11-07T05:31:56Z",
  "tags": [
    {"tag_key": "service", "tag_value": "AAA"}
  ]
}
```

Non-streaming response shape:

```json
{
  "answer": "Full answer with citations",
  "answer_citationless": "Full answer without citation marks",
  "pre_answer_reasoning": null,
  "tool_calls": [],
  "top_documents": [],
  "citation_info": [],
  "message_id": 123,
  "chat_session_id": "3c90c3cc-0d44-4b50-8888-8dd25736052a",
  "error_msg": null
}
```

### 3.2 Streaming Response Model

When `stream: true`, Onyx returns an SSE stream of packets. Packet content is Onyx-specific. The docs describe a packet model like:

```python
class Packet(BaseModel):
    ind: int        # sequential index
    obj: PacketObj  # object with type-specific payload
```

Common packet types include:

| Packet type | Meaning | Frontend mapping |
|---|---|---|
| `message_start` | Assistant message begins | Create assistant message bubble |
| `message_delta` | Incremental answer text | Append text delta |
| `reasoning_start` | Reasoning/progress section begins | Show collapsible "thinking/progress" panel |
| `reasoning_delta` | Incremental reasoning/progress token | Append to progress panel, not raw hidden CoT unless approved by product policy |
| `reasoning_done` | Reasoning/progress complete | Mark progress panel complete |
| `search_tool_start` | Internal search begins | Tool timeline entry |
| `search_tool_queries_delta` | Search query info | Show query progress |
| `search_tool_documents_delta` | Search results/docs | Add source candidates |
| `open_url_start` | URL-opening tool starts | Tool timeline entry |
| `open_url_urls` | URLs selected/opened | Add URL list |
| `open_url_documents` | URL contents returned | Add retrieved docs |
| `python_tool_start` | Code execution begins | Tool timeline entry |
| `python_tool_delta` | Code execution output | Append output |
| `custom_tool_start` | Custom/OpenAPI/MCP tool begins | Tool timeline entry |
| `custom_tool_delta` | Tool output | Append output/result |
| `citation_info` | Citation/source metadata | Add citation cards/chips |
| `section_end` | Section boundary | Mark current section complete |
| `stop` | Overall stop/completion | Finalise message |
| `error` | Error packet | Display error state |
| `image_generation_*` | Image generation updates | Render image generation progress/final output |
| `deep_research_*` / `research_agent_*` | Deep research events | Render research plan/intermediate report |

SSE parser pattern in Python/FastAPI adapter:

```python
import json
import httpx
from collections.abc import AsyncIterator

async def stream_onyx_chat(payload: dict, api_base_url: str, token: str) -> AsyncIterator[dict]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream(
            "POST",
            f"{api_base_url}/chat/send-chat-message",
            headers=headers,
            json=payload,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                if line.startswith("data: "):
                    raw = line[len("data: "):]
                else:
                    raw = line
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    yield {"type": "raw", "raw": raw}
```

Adapter mapping to assistant-ui-style events:

```python
def map_onyx_packet(packet: dict) -> list[dict]:
    obj = packet.get("obj", packet)
    packet_type = obj.get("type")

    if packet_type == "message_delta":
        return [{"type": "answer.delta", "delta": obj.get("content", "") or obj.get("delta", "")}]

    if packet_type == "citation_info":
        return [{"type": "citation.added", "citation": obj}]

    if packet_type in {"search_tool_start", "custom_tool_start", "python_tool_start"}:
        return [{"type": "tool_call.started", "tool_type": packet_type, "payload": obj}]

    if packet_type in {"search_tool_documents_delta", "custom_tool_delta", "python_tool_delta"}:
        return [{"type": "tool_call.delta", "tool_type": packet_type, "payload": obj}]

    if packet_type == "reasoning_delta":
        return [{"type": "reasoning.delta", "delta": obj.get("content", "") or obj.get("delta", "")}]

    if packet_type == "stop":
        return [{"type": "answer.completed", "payload": obj}]

    if packet_type == "error":
        return [{"type": "run.error", "payload": obj}]

    return [{"type": "onyx.raw", "packet_type": packet_type, "payload": obj}]
```

Note: field names inside `obj` can vary by packet type and Onyx version. Keep a raw packet capture/log during development.

### 3.3 Stop Chat Session

Endpoint:

```http
POST /chat/stop-chat-session/{chat_session_id}
```

Used by the frontend stop button. It sets a stop signal in Redis.

```bash
curl --request POST \
  --url "$ONYX_API_BASE_URL/chat/stop-chat-session/$CHAT_SESSION_ID" \
  --header "Authorization: Bearer $ONYX_API_KEY"
```

### 3.4 Get Chat Session

Endpoint:

```http
GET /chat/get-chat-session/{session_id}
```

Query params:

| Param | Type | Default |
|---|---:|---:|
| `is_shared` | bool | `false` |
| `include_deleted` | bool | `false` |

Returns session metadata, messages, context docs, citations, packets, persona ID, etc.

Use this to reload chat state after a Dash drawer/modal closes and reopens.

### 3.5 Search Chats

Endpoint:

```http
GET /chat/search
```

Query params:

| Param | Type | Default |
|---|---:|---:|
| `query` | string/null | null |
| `page` | int | 1 |
| `page_size` | int | 10 |

Returns grouped chat session summaries plus `has_more` and `next_page`.

### 3.6 Other Chat Endpoints

The API reference also lists:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/chat/create-chat-session` | Create a new chat session |
| `POST` | `/chat/seed-chat` | Seed chat session |
| `GET` | `/chat/get-user-chat-sessions` | List current user's sessions |
| `GET` | `/chat/file/{...}` or equivalent Fetch Chat File page | Fetch chat file |
| `DELETE` | `/chat/delete-chat-session/{session_id}` | Delete one session |
| `DELETE` | `/chat/delete-all-chat-sessions` | Delete all sessions |

Check `/api/docs` on the running instance for exact path variants if generated code needs these.

---

## 4. Agents API

### 4.1 List Agents Available to User

Endpoint:

```http
GET /agents
```

Query params:

| Param | Type | Default | Notes |
|---|---:|---:|---|
| `page_num` | int | 0 | 0-indexed |
| `page_size` | int | 10 | Range 1–1000 |
| `include_deleted` | bool | false | Include deleted agents |
| `get_editable` | bool | false | Only editable agents |
| `include_default` | bool | true | Include built-in/default agents |

Example:

```bash
curl --request GET \
  --url "$ONYX_API_BASE_URL/agents?page_num=0&page_size=100" \
  --header "Authorization: Bearer $ONYX_API_KEY"
```

Response shape:

```json
{
  "items": [
    {
      "id": 123,
      "name": "Agent name",
      "description": "Agent description",
      "tools": [],
      "starter_messages": [],
      "llm_relevance_filter": true,
      "llm_filter_extraction": true,
      "document_sets": [],
      "llm_model_version_override": null,
      "llm_model_provider_override": null,
      "is_public": true,
      "is_visible": true,
      "display_priority": 123,
      "is_default_persona": false,
      "builtin_persona": false,
      "labels": [],
      "owner": {"id": "uuid", "email": "user@example.com"}
    }
  ],
  "total_items": 123
}
```

### 4.2 Create Agent

Endpoint:

```http
POST /persona
```

Despite being documented as **Create Agent**, the path uses `persona`.

Important body fields:

```json
{
  "name": "Mimir Runbook Agent",
  "description": "Answers questions from indexed runbooks",
  "document_set_ids": [123],
  "num_chunks": 10,
  "is_public": true,
  "recency_bias": "auto",
  "llm_filter_extraction": true,
  "llm_relevance_filter": true,
  "tool_ids": [123],
  "system_prompt": "You are a runbook assistant...",
  "task_prompt": "Answer with citations and operational next steps.",
  "datetime_aware": true,
  "llm_model_provider_override": null,
  "llm_model_version_override": null,
  "starter_messages": [
    {"name": "Find runbook", "message": "Find the relevant runbook for this incident."}
  ],
  "users": [],
  "groups": [],
  "search_start_date": null,
  "label_ids": [],
  "is_default_persona": false,
  "display_priority": 100,
  "user_file_ids": [],
  "replace_base_system_prompt": false
}
```

`recency_bias` options:

```text
favor_recent | base_decay | no_decay | auto
```

### 4.3 Get/Update/Delete Agent

The API reference lists:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/persona/{persona_id}` | Get one agent/persona |
| `PATCH` | `/persona/{persona_id}` | Update an agent/persona |
| `DELETE` | `/persona/{persona_id}` | Delete an agent/persona |
| `GET` | `/admin/personas` or `/admin/persona` variants | Admin listing; check `/api/docs` for exact path |
| `PATCH` | `/admin/persona/{persona_id}/undelete` | Admin undelete |

Use `/agents` for normal app-side discovery and `/persona` endpoints for agent management.

---

## 5. Tools / Actions API

### 5.1 List Tools

Endpoint:

```http
GET /tool
```

Example:

```bash
curl --request GET \
  --url "$ONYX_API_BASE_URL/tool" \
  --header "Authorization: Bearer $ONYX_API_KEY"
```

Response item shape:

```json
{
  "id": 123,
  "name": "internal_search",
  "description": "Search indexed documents",
  "definition": {},
  "display_name": "Internal Search",
  "in_code_tool_id": "internal_search",
  "custom_headers": null,
  "passthrough_auth": false,
  "mcp_server_id": null,
  "user_id": null,
  "oauth_config_id": null,
  "oauth_config_name": null,
  "enabled": true,
  "chat_selectable": true,
  "agent_creation_selectable": true,
  "default_enabled": false
}
```

Use this endpoint to discover `allowed_tool_ids` / `forced_tool_id` for chat calls and `tool_ids` for agent creation.

### 5.2 Create Custom Tool

Endpoint:

```http
POST /admin/tool/custom
```

Admin token required.

Body:

```json
{
  "name": "service_now_lookup",
  "definition": {},
  "passthrough_auth": true,
  "description": "Look up ServiceNow incident/change data",
  "custom_headers": [
    {"key": "X-Internal-App", "value": "mimir"}
  ],
  "oauth_config_id": null
}
```

The `definition` should contain the OpenAPI/tool schema expected by Onyx. You can validate tools with the Validate Tool endpoint.

Other Actions endpoints listed:

| Method | Path | Purpose |
|---|---|---|
| `PUT` | `/admin/tool/custom/{tool_id}` | Update custom tool |
| `DELETE` | `/admin/tool/custom/{tool_id}` | Delete custom tool |
| `POST` | `/admin/tool/validate` | Validate custom tool definition |
| `GET` | `/tool` | List tools |
| `GET` | `/tool/openapi` or page-specific variant | List OpenAPI tools |
| `GET` | `/tool/{tool_id}` | Get custom tool |

Check local `/api/docs` for exact path variants.

---

## 6. Ingestion API

Use the ingestion API for unsupported sources, supplemental content, programmatic data pipelines, and simple document upserts. If you need advanced scheduling/credential handling, create a connector instead.

Base path:

```http
/onyx-api/ingestion
```

### 6.1 Upsert Ingestion Document

Endpoint:

```http
POST /onyx-api/ingestion
```

Minimal payload:

```json
{
  "document": {
    "id": "runbook-aaa-cts-replication",
    "semantic_identifier": "AAA CTS Replication Runbook",
    "sections": [
      {
        "text": "CTS replication lag can cause token validation failures...",
        "link": "https://confluence.example/runbooks/aaa-cts"
      }
    ],
    "source": "file",
    "metadata": {
      "service": "AAA",
      "category": "runbook"
    }
  },
  "cc_pair_id": 243
}
```

Full-ish payload shape:

```json
{
  "document": {
    "id": "stable-doc-id",
    "semantic_identifier": "Title shown in UI",
    "title": "Optional title for search",
    "sections": [
      {"text": "text section", "link": "https://source/link"},
      {"image_file_id": "uuid", "text": "caption", "link": "https://source/image"}
    ],
    "source": "file",
    "metadata": {"category": "faq", "tags": ["runbook", "aaa"]},
    "doc_updated_at": "2025-09-19T08:20:00Z",
    "chunk_count": 15,
    "primary_owners": [
      {"display_name": "Alex Chen", "first_name": "Alex", "last_name": "Chen", "email": "alex@example.com"}
    ],
    "secondary_owners": [],
    "from_ingestion_api": true,
    "additional_info": null,
    "external_access": {
      "external_user_emails": [],
      "external_user_group_ids": [],
      "is_public": true
    }
  },
  "cc_pair_id": 243
}
```

Response:

```json
{
  "document_id": "stable-doc-id",
  "already_existed": true
}
```

Important notes:

- If you want ingestion API documents to appear under a connector in the Connectors page, include `cc_pair_id`.
- The upsert call returns once the document is accepted; actual indexing happens asynchronously.
- Use stable document IDs so repeated runs update existing docs rather than creating duplicates.

### 6.2 List Ingestion Docs

Endpoint:

```http
GET /onyx-api/ingestion
```

Response:

```json
[
  {
    "document_id": "doc-id",
    "semantic_id": "Title shown in UI",
    "link": "https://source/link"
  }
]
```

### 6.3 Delete Ingestion Doc

Endpoint:

```http
DELETE /onyx-api/ingestion/{document_id}
```

Example:

```bash
curl --request DELETE \
  --url "$ONYX_API_BASE_URL/onyx-api/ingestion/runbook-aaa-cts-replication" \
  --header "Authorization: Bearer $ONYX_API_KEY"
```

---

## 7. Connectors and Credentials API

Connector creation is more complex than ingestion. Prefer the Admin Panel or Ingestion API unless automation is required.

Admin API key is usually required.

### 7.1 Create Connector

Endpoint:

```http
POST /manage/admin/connector
```

Example payload:

```json
{
  "name": "jira-TECH",
  "source": "jira",
  "input_type": "poll",
  "access_type": "public",
  "connector_specific_config": {
    "jira_base_url": "https://your-company.atlassian.net",
    "project_key": "TECH",
    "comment_email_blacklist": ["legal@company.com"]
  },
  "refresh_freq": 3600,
  "prune_freq": 86400,
  "indexing_start": null,
  "groups": []
}
```

Response includes connector `id`. Save it.

Connector-specific config depends on connector type and maps to the fields in the Admin Panel. If unsure, inspect the relevant implementation in the Onyx repository under `backend/onyx/connectors/` or test via `/api/docs`.

### 7.2 List Credentials

Endpoint used by guide:

```http
GET /manage/admin/credential
```

Use to find an existing credential ID for a source.

### 7.3 Associate Credential with Connector

Endpoint:

```http
PUT /manage/admin/connector/{connector_id}/credential/{credential_id}
```

Example:

```bash
curl --request PUT \
  --url "$ONYX_API_BASE_URL/manage/admin/connector/$CONNECTOR_ID/credential/$CREDENTIAL_ID" \
  --header "Authorization: Bearer $ONYX_ADMIN_API_KEY"
```

If this step is skipped, the connector is not fully active/visible in the Admin Panel.

### 7.4 Connector Management Endpoints

The API reference lists endpoints for:

| Area | Endpoint family |
|---|---|
| Create/read/delete connectors | `/manage/admin/connector` |
| Connector indexing status | `/manage/admin/connector/indexing-status...` variants |
| Run connector once | connector run-once endpoint |
| Upload/update connector files | file connector upload/update endpoints |
| CC-pair full info | CC-pair endpoint |
| CC-pair index attempts/errors | index attempts/errors endpoints |
| Prune CC-pair | prune endpoint |
| Docs by CC-pair | docs-by-connector-credential-pair endpoint |
| Credentials CRUD | `/manage/admin/credential...` variants |

For exact path details, use the running instance OpenAPI explorer at:

```text
https://<onyx-domain>/api/docs
```

---

## 8. Projects and Files API

Projects scope chat sessions and files.

### 8.1 Create Project

Endpoint:

```http
POST /user/projects/create?name=<project-name>
```

Response includes:

```json
{
  "id": 123,
  "name": "Project name",
  "description": null,
  "created_at": "2023-11-07T05:31:56Z",
  "user_id": "uuid",
  "chat_sessions": [],
  "instructions": null
}
```

### 8.2 Upload User Files

Endpoint:

```http
POST /user/projects/file/upload
Content-Type: multipart/form-data
```

Form fields:

| Field | Type | Required |
|---|---|---:|
| `files` | file[] | yes |
| `project_id` | int/null | no |
| `temp_id_map` | string/null | no |

Curl:

```bash
curl --request POST \
  --url "$ONYX_API_BASE_URL/user/projects/file/upload" \
  --header "Authorization: Bearer $ONYX_API_KEY" \
  --form 'files=@runbook.pdf' \
  --form 'project_id=123'
```

Response:

```json
{
  "user_files": [
    {
      "id": "uuid",
      "name": "runbook.pdf",
      "user_id": "uuid",
      "file_id": "file-store-id",
      "created_at": "2023-11-07T05:31:56Z",
      "last_accessed_at": "2023-11-07T05:31:56Z",
      "file_type": "pdf",
      "token_count": 123,
      "chunk_count": 12,
      "temp_id": null,
      "project_id": 123
    }
  ],
  "rejected_files": []
}
```

Use returned user file IDs in chat `file_descriptors` or agent `user_file_ids` where appropriate.

### 8.3 Other Project/File Endpoints

The API reference lists:

| Method | Path family | Purpose |
|---|---|---|
| `GET` | `/user/projects` | List projects |
| `GET` | `/user/projects/{project_id}` | Get project |
| `PATCH` | `/user/projects/{project_id}` | Update project |
| `DELETE` | `/user/projects/{project_id}` | Delete project |
| `GET` | project details/instructions endpoints | Read details/instructions |
| `POST` | upsert project instructions | Set project instructions |
| `GET` | project files endpoints | List files |
| `GET` | user file endpoint | Fetch file |
| `DELETE` | user file endpoint | Delete file |
| `POST`/`DELETE` | link/unlink file to project | Manage project file association |

Use `/api/docs` for exact generated paths when implementing less common operations.

---

## 9. Search API

API reference lists:

| Method | Path | Purpose |
|---|---|---|
| `POST` | search request endpoint | Internal Onyx search |
| `POST` | web search endpoint | Web search plus content fetch |
| `POST` | web search lite endpoint | Search snippets/URLs without fetching page contents |
| `POST` | open URLs endpoint | Fetch content for selected URLs, intended with web-search-lite |

The official API page for `Handle Search Request` is sparse in the rendered docs. For implementation, prefer using chat with the Search Agent/tool unless you specifically need a raw search endpoint. For raw search exact request/response schema, inspect:

```text
https://<onyx-domain>/api/docs
```

or the OpenAPI JSON for your running Onyx instance.

---

## 10. User Management, Token Limits, Miscellaneous

The API reference lists endpoints for:

### User Management

| Operation | Purpose |
|---|---|
| `GET /auth/type` or page-specific equivalent | Get auth type |
| verify logged in | Validate current token/session |
| list users | Admin user listing |
| list accepted/invited users | Admin user state |
| bulk invite users | Invite users |
| activate/deactivate/delete user | User lifecycle |
| get/set user role | Role management |

Use Admin API key where required.

### Token Limits

Endpoints exist to create, get, update, and delete global token limit settings.

### Miscellaneous

| Endpoint | Purpose |
|---|---|
| healthcheck | Service health |
| backend version | Current Onyx backend version |
| latest app version tags | Docker image version discovery |

Use these for deployment checks and monitoring.

---

## 11. FastAPI Adapter for Dash / assistant-ui Integration

Recommended local integration pattern:

```text
Dash + DMC drawer/modal
  -> custom React Dash component wrapping assistant-ui
  -> FastAPI endpoint in your app
  -> Onyx /chat/send-chat-message streaming API
```

Never expose the Onyx API key to the browser. The browser calls your FastAPI adapter; your backend calls Onyx.

### 11.1 Request Model

```python
from pydantic import BaseModel, Field
from typing import Any

class ChatRunRequest(BaseModel):
    conversation_id: str | None = None
    onyx_chat_session_id: str | None = None
    parent_message_id: int | None = -1
    persona_id: int = 0
    message: str
    app_context: dict[str, Any] = Field(default_factory=dict)
    stream: bool = True
    include_citations: bool = True
    document_sets: list[str] = Field(default_factory=list)
    source_types: list[str] = Field(default_factory=list)
    tags: dict[str, str] = Field(default_factory=dict)
    allowed_tool_ids: list[int] | None = None
    forced_tool_id: int | None = None
```

### 11.2 FastAPI Streaming Proxy

```python
import json
import os
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api/chat")

ONYX_API_BASE_URL = os.environ["ONYX_API_BASE_URL"].rstrip("/")
ONYX_API_KEY = os.environ["ONYX_API_KEY"]


def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def build_additional_context(app_context: dict) -> str | None:
    if not app_context:
        return None
    return "Runtime application context:\n" + json.dumps(app_context, ensure_ascii=False, indent=2)


def build_onyx_payload(req: ChatRunRequest) -> dict:
    tags = [
        {"tag_key": key, "tag_value": value}
        for key, value in req.tags.items()
    ]

    payload = {
        "message": req.message,
        "chat_session_id": req.onyx_chat_session_id,
        "parent_message_id": req.parent_message_id,
        "chat_session_info": {
            "persona_id": req.persona_id,
            "description": req.conversation_id,
            "project_id": None,
        },
        "stream": True,
        "include_citations": req.include_citations,
        "origin": "api",
        "additional_context": build_additional_context(req.app_context),
        "internal_search_filters": {
            "source_type": req.source_types,
            "document_set": req.document_sets,
            "time_cutoff": None,
            "tags": tags,
        },
        "allowed_tool_ids": req.allowed_tool_ids,
        "forced_tool_id": req.forced_tool_id,
        "deep_research": False,
        "file_descriptors": [],
        "llm_override": None,
    }
    return payload


def map_onyx_packet(packet: dict) -> list[dict]:
    obj = packet.get("obj", packet)
    packet_type = obj.get("type")

    if packet_type == "message_delta":
        return [{"type": "answer.delta", "delta": obj.get("content") or obj.get("delta") or ""}]
    if packet_type == "message_start":
        return [{"type": "answer.started", "payload": obj}]
    if packet_type == "citation_info":
        return [{"type": "citation.added", "citation": obj}]
    if packet_type in {"search_tool_start", "custom_tool_start", "python_tool_start", "open_url_start"}:
        return [{"type": "tool_call.started", "tool_type": packet_type, "payload": obj}]
    if packet_type.endswith("_delta") or packet_type in {"search_tool_documents_delta", "open_url_documents"}:
        return [{"type": "tool_call.delta", "tool_type": packet_type, "payload": obj}]
    if packet_type == "reasoning_delta":
        return [{"type": "reasoning.delta", "delta": obj.get("content") or obj.get("delta") or ""}]
    if packet_type == "reasoning_done":
        return [{"type": "reasoning.completed", "payload": obj}]
    if packet_type == "stop":
        return [{"type": "answer.completed", "payload": obj}]
    if packet_type == "error":
        return [{"type": "run.error", "payload": obj}]

    return [{"type": "onyx.raw", "packet_type": packet_type, "payload": obj}]


async def iter_onyx_events(onyx_payload: dict, request: Request) -> AsyncIterator[str]:
    headers = {
        "Authorization": f"Bearer {ONYX_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    yield sse({"type": "run.started"})

    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream(
            "POST",
            f"{ONYX_API_BASE_URL}/chat/send-chat-message",
            headers=headers,
            json=onyx_payload,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if await request.is_disconnected():
                    break
                if not line:
                    continue

                raw = line[len("data: "):] if line.startswith("data: ") else line
                try:
                    packet = json.loads(raw)
                except json.JSONDecodeError:
                    yield sse({"type": "onyx.raw", "raw": raw})
                    continue

                for mapped in map_onyx_packet(packet):
                    yield sse(mapped)

    yield sse({"type": "run.finished"})


@router.post("/runs")
async def create_chat_run(req: ChatRunRequest, request: Request):
    payload = build_onyx_payload(req)
    return StreamingResponse(
        iter_onyx_events(payload, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )
```

### 11.3 Stop Endpoint in Adapter

```python
from fastapi import HTTPException

@router.post("/onyx-sessions/{chat_session_id}/stop")
async def stop_onyx_chat_session(chat_session_id: str):
    headers = {"Authorization": f"Bearer {ONYX_API_KEY}"}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{ONYX_API_BASE_URL}/chat/stop-chat-session/{chat_session_id}",
            headers=headers,
        )
    if response.status_code >= 400:
        raise HTTPException(response.status_code, response.text)
    return response.json() if response.content else {}
```

### 11.4 Rehydrate Session Endpoint in Adapter

```python
@router.get("/onyx-sessions/{chat_session_id}")
async def get_onyx_chat_session(chat_session_id: str):
    headers = {"Authorization": f"Bearer {ONYX_API_KEY}"}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{ONYX_API_BASE_URL}/chat/get-chat-session/{chat_session_id}",
            headers=headers,
        )
    response.raise_for_status()
    return response.json()
```

---

## 12. Implementation Notes for Dash Drawer/Modal Chat

For a Dash app embedding assistant-ui in a DMC Drawer or Modal:

- Let Dash/DMC own opening/closing the drawer/modal.
- The custom React component should own only the chat rendering and assistant-ui runtime.
- Persist state server-side or in Onyx chat sessions; do not rely only on React state.
- On close, do not automatically stop Onyx unless the user clicked Stop.
- On reopen, call `GET /chat/get-chat-session/{session_id}` through your adapter to restore messages/citations/packets.
- Use `additional_context` for ephemeral Dash context such as current incident/change/runbook, selected filters, and visible page state.
- Use `internal_search_filters.document_set`, `source_type`, and `tags` to scope search to the currently relevant knowledge base.

Example `additional_context`:

```json
{
  "app": "odin",
  "entrypoint": "incident_drawer",
  "incident_id": "INC1234567",
  "service": "AAA",
  "region": "emea2",
  "selected_tab": "similar_incidents"
}
```

---

## 13. Recommended Error Handling

Handle these cases explicitly:

| Case | Handling |
|---|---|
| 401/403 | Invalid token or insufficient token type; surface as auth/config error |
| 404 | Wrong base URL/path, missing session, missing document, or deleted resource |
| 422 | Request schema validation failed; log full request/response body |
| Stream disconnect | Let browser reconnect/reload session state rather than assuming failure |
| Onyx `error` packet | Render error in chat transcript and keep raw packet in debug log |
| Empty `message_delta` fields | Preserve raw packet; Onyx packet schema may vary by version |
| Missing citations | Check `include_citations: true`, search/tool use, and agent/tool configuration |

Always log raw Onyx packets during development:

```python
logger.debug("onyx_packet", packet=packet)
```

---

## 14. Minimum Integration Checklist

For the coding agent implementing Mímir/Odin/Forseti chat:

1. Configure backend env vars:
   - `ONYX_API_BASE_URL`
   - `ONYX_API_KEY`
   - optional default `ONYX_PERSONA_ID`
2. Implement backend FastAPI adapter:
   - `POST /api/chat/runs`
   - `POST /api/chat/onyx-sessions/{id}/stop`
   - `GET /api/chat/onyx-sessions/{id}`
3. Implement Onyx SSE parser.
4. Log raw packets and mapped events.
5. Map Onyx packets to assistant-ui events:
   - answer deltas
   - citations
   - tool calls
   - reasoning/progress
   - errors
   - completion
6. In Dash, pass app context into the custom assistant-ui component.
7. Use `additional_context` for runtime context that should not persist in Onyx history.
8. Use `chat_session_id` to continue Onyx sessions.
9. Use `parent_message_id` from Onyx response/session history when appending sequential messages.
10. Use Stop endpoint for explicit cancellation.
11. Use `GET /agents` to discover persona IDs instead of hard-coding where possible.
12. Use `GET /tool` to discover tool IDs for `allowed_tool_ids` and `forced_tool_id`.
13. For custom content indexing, prefer `POST /onyx-api/ingestion` with stable IDs.
14. For source-system sync, create connector + credential + association.
15. Validate all exact paths against the local `/api/docs` for the deployed Onyx version.

---

## 15. Known Naming/Version Caveats

- The product name is **Onyx**, but some older code/docs may reference Danswer.
- The terms Agent, Persona, and Assistant are used interchangeably. API paths still commonly use `/persona`.
- The docs note migration away from old `/chat/send-message` and `/chat/send-message-simple-api` APIs toward `/chat/send-chat-message` by February 1, 2026.
- Onyx is moving to group-based permissions; Curator and Global Curator roles are being removed. Prefer groups-aware designs for new integrations.
- The public docs are a curated subset. The definitive endpoint list for the deployed version is the instance's OpenAPI explorer at `/api/docs`.

