import json
import re
from typing import Any
from urllib.parse import quote, unquote_plus

from fastapi import HTTPException, status
from groq import Groq

from src.app.core.config import get_settings
from src.app.schemas.voice import InstructionPayload

_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_MAX_ROUTING_ATTEMPTS = 3

_SYSTEM_PROMPT = """
You are a strict API router for a TODO task backend.
You must return only a single valid JSON object and no extra text.
The JSON schema is:
{
  "endpoint": "string",
  "method": "GET|POST|PUT|PATCH|DELETE",
  "params": { "any": "json" }
}
Rules:
- Allowed endpoints: "/tasks", "/tasks/<id>", or "/tasks/title/<title>".
- Use GET /tasks to list tasks.
- Use POST /tasks with params containing title and optional done.
- Use PUT /tasks/<id> with params containing full task fields title and done.
- Use PATCH /tasks/<id> with params containing one or both: title, done.
- Use DELETE /tasks/<id> with params as {}.
- Use DELETE /tasks to delete all tasks when the user explicitly asks to clear/delete all tasks.
- Use /tasks/title/<title> when the user targets a task by title instead of numeric ID.
- For create/add/new commands, always use POST /tasks. Never use PUT/PATCH for creation.
- Do not return /tasks/<id> unless the user explicitly provided a numeric ID in the request text.
- Do not invent task IDs. Use /tasks/<id> only if the user explicitly targets an existing task ID.
- There is no maximum number of tasks. Never enforce or imply task limits.
- Never include markdown, explanations, code fences, or additional keys.
""".strip()


def route_instruction_from_text(transcription: str) -> InstructionPayload:
    settings = get_settings()
    client = Groq(api_key=settings.groq_api_key)
    last_error: HTTPException | None = None
    correction_feedback: str | None = None

    for _ in range(_MAX_ROUTING_ATTEMPTS):
        messages: list[dict[str, str]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": transcription},
        ]
        if correction_feedback:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Your previous JSON was invalid. Return a corrected JSON object only. "
                        f"Fix this issue: {correction_feedback}"
                    ),
                }
            )

        response = client.chat.completions.create(
            model=settings.groq_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=messages,
        )

        content = response.choices[0].message.content if response.choices else None
        if not content:
            last_error = HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Groq returned an empty instruction response.",
            )
            continue

        try:
            payload = _parse_json_object(content)
            instruction = InstructionPayload.model_validate(payload)
            normalized = _normalize_instruction_payload(instruction, transcription)
            method = normalized.method
            if method not in _ALLOWED_METHODS:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Unsupported method from Groq: {normalized.method}",
                )

            if not _is_allowed_endpoint(normalized.endpoint):
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Unsupported endpoint from Groq: {normalized.endpoint}",
                )

            semantic_error = _validate_instruction_semantics(normalized)
            if semantic_error:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=semantic_error,
                )

            return normalized
        except HTTPException as exc:
            last_error = exc
            correction_feedback = exc.detail if isinstance(exc.detail, str) else "Invalid JSON payload."
        except Exception as exc:  # pragma: no cover - validation passthrough
            last_error = HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Groq returned an invalid instruction payload.",
            )
            correction_feedback = "Payload schema mismatch."

    if last_error is not None:
        raise last_error

    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Groq instruction routing failed.",
    )


def transcribe_audio_bytes(
    file_content: bytes,
    filename: str,
    content_type: str | None,
    language: str | None,
) -> str:
    settings = get_settings()
    client = Groq(api_key=settings.groq_api_key)

    transcription = client.audio.transcriptions.create(
        model=settings.groq_transcription_model,
        file=(filename, file_content, content_type or "application/octet-stream"),
        language=language,
    )

    text = getattr(transcription, "text", "")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Groq returned an empty transcription.",
        )

    return text.strip()


def _parse_json_object(content: str) -> dict[str, Any]:
    normalized = _normalize_possible_json(content)

    try:
        parsed = json.loads(normalized)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    extracted = _extract_first_json_object(normalized)
    if not extracted:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Groq response was not valid JSON.",
        )

    try:
        parsed = json.loads(extracted)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Groq response contained malformed JSON.",
        ) from exc

    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Groq JSON response must be an object.",
        )

    return parsed


def _normalize_possible_json(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_first_json_object(content: str) -> str | None:
    start = content.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(content)):
        char = content[index]
        if escaped:
            escaped = False
            continue

        if char == "\\":
            escaped = True
            continue

        if char == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return content[start : index + 1]

    return None


def _is_allowed_endpoint(endpoint: str) -> bool:
    if endpoint == "/tasks":
        return True
    if re.fullmatch(r"/tasks/\d+", endpoint) is not None:
        return True
    return re.fullmatch(r"/tasks/title/[^/]+", endpoint) is not None


def _normalize_instruction_payload(
    instruction: InstructionPayload,
    transcription: str,
) -> InstructionPayload:
    params = dict(instruction.params or {})
    method = instruction.method.strip().upper()
    endpoint = instruction.endpoint.strip().rstrip("/")
    endpoint = endpoint or "/tasks"

    id_candidate = _extract_id_candidate(params, transcription)
    explicit_numeric_id = _has_explicit_numeric_id(transcription)

    # Normalize common model endpoint variants used in prompts/docs.
    if endpoint in {"/tasks/<int:task_id>", "/tasks/{task_id}", "/tasks/<task_id>"}:
        endpoint = f"/tasks/{id_candidate}" if id_candidate is not None else "/tasks"

    # If method requires id and model provided id in params but not path, synthesize /tasks/<id>.
    if method in {"PUT", "PATCH", "DELETE"} and endpoint == "/tasks" and id_candidate is not None:
        endpoint = f"/tasks/{id_candidate}"

    # If caller references title (no id), route to title endpoint for direct task access.
    title_candidate = _extract_title_candidate(params, transcription)
    if method in {"GET", "PUT", "PATCH", "DELETE"} and endpoint == "/tasks" and id_candidate is None and title_candidate:
        endpoint = f"/tasks/title/{quote(title_candidate, safe='')}"

    # If user referenced a title and did not provide an explicit numeric id, prefer title endpoint.
    # This avoids model-invented id routes like /tasks/1 that overwrite/delete unexpected records.
    if method in {"GET", "PUT", "PATCH", "DELETE"} and title_candidate and not explicit_numeric_id:
        endpoint = f"/tasks/title/{quote(title_candidate, safe='')}"

    endpoint = _normalize_title_endpoint(endpoint)

    # PUT requires a full replacement object. If model sends partial fields, treat it as PATCH.
    if method == "PUT":
        title = params.get("title")
        done = params.get("done")
        if not (isinstance(title, str) and title.strip() and isinstance(done, bool)):
            method = "PATCH"

    # For delete commands without explicit id, default to DELETE /tasks (delete all).
    if method == "DELETE" and endpoint == "/tasks" and id_candidate is None:
        params = {}

    if "title" not in params:
        for key in ("name", "task", "task_name", "taskTitle", "task_title"):
            candidate = params.get(key)
            if isinstance(candidate, str) and candidate.strip():
                params["title"] = candidate.strip()
                break

    title = params.get("title")
    if isinstance(title, str):
        params["title"] = title.strip()

    done = params.get("done")
    if isinstance(done, str):
        lowered = done.strip().lower()
        if lowered in {"true", "yes", "1", "done", "completed"}:
            params["done"] = True
        elif lowered in {"false", "no", "0", "pending", "not done"}:
            params["done"] = False

    if method == "POST" and endpoint == "/tasks":
        title = params.get("title")
        if (not isinstance(title, str) or not title.strip()) and transcription.strip():
            params["title"] = transcription.strip()

    return InstructionPayload(endpoint=endpoint, method=method, params=params)


def _validate_instruction_semantics(instruction: InstructionPayload) -> str | None:
    method = instruction.method
    endpoint = instruction.endpoint
    params = instruction.params
    has_id_endpoint = re.fullmatch(r"/tasks/\d+", endpoint) is not None
    has_title_endpoint = re.fullmatch(r"/tasks/title/[^/]+", endpoint) is not None

    if method == "GET":
        if endpoint != "/tasks" and not has_title_endpoint:
            return "GET must target /tasks or /tasks/title/<title>."
        return None

    if method == "POST":
        if endpoint != "/tasks":
            return "POST must target /tasks."
        title = params.get("title")
        if not isinstance(title, str) or not title.strip():
            return "POST /tasks requires params.title as a non-empty string."
        done = params.get("done")
        if done is not None and not isinstance(done, bool):
            return "POST /tasks params.done must be a boolean when provided."
        return None

    if method == "PUT":
        if not has_id_endpoint and not has_title_endpoint:
            return "PUT must target /tasks/<id> or /tasks/title/<title>."
        title = params.get("title")
        done = params.get("done")
        if not isinstance(title, str) or not title.strip():
            return "PUT /tasks/<id> requires params.title as a non-empty string."
        if not isinstance(done, bool):
            return "PUT /tasks/<id> requires params.done as a boolean."
        return None

    if method == "PATCH":
        if not has_id_endpoint and not has_title_endpoint:
            return "PATCH must target /tasks/<id> or /tasks/title/<title>."
        has_title = "title" in params
        has_done = "done" in params
        if not has_title and not has_done:
            return "PATCH /tasks/<id> requires params.title and/or params.done."
        if has_title:
            title = params.get("title")
            if not isinstance(title, str) or not title.strip():
                return "PATCH params.title must be a non-empty string when provided."
        if has_done and not isinstance(params.get("done"), bool):
            return "PATCH params.done must be a boolean when provided."
        return None

    if method == "DELETE":
        if endpoint != "/tasks" and not has_id_endpoint and not has_title_endpoint:
            return "DELETE must target /tasks, /tasks/<id>, or /tasks/title/<title>."
        return None

    return "Unsupported method."


def _has_explicit_numeric_id(transcription: str) -> bool:
    return re.search(r"\b\d+\b", transcription) is not None


def _extract_id_candidate(params: dict[str, Any], transcription: str) -> int | None:
    for key in ("task_id", "id"):
        value = params.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)

    match = re.search(r"\b(\d+)\b", transcription)
    if match:
        return int(match.group(1))

    return None


def _extract_title_candidate(params: dict[str, Any], transcription: str) -> str | None:
    for key in ("title", "name", "task", "task_name", "taskTitle", "task_title"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    quoted = re.search(r"\"([^\"]+)\"|'([^']+)'", transcription)
    if quoted:
        return (quoted.group(1) or quoted.group(2) or "").strip() or None

    title_match = re.search(r"\b(?:titled|title)\s+(.+)$", transcription, re.IGNORECASE)
    if title_match:
        candidate = title_match.group(1).strip(" .,!?:;\"'")
        return candidate or None

    return None


def _normalize_title_endpoint(endpoint: str) -> str:
    prefix = "/tasks/title/"
    if not endpoint.startswith(prefix):
        return endpoint

    raw_title = endpoint[len(prefix) :].strip()
    if not raw_title:
        return "/tasks"

    decoded_once = unquote_plus(raw_title)
    return f"{prefix}{quote(decoded_once, safe='')}"
