import os
from datetime import datetime, timezone
from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.responses import JSONResponse
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from dotenv import load_dotenv

from src.__main__ import thread_manager, send_dm_to_user, get_user_info, expand_macros, CHANNEL, client, get_standard_channel_msg
from src.webhooks import dispatch_event

load_dotenv()

API_KEY = os.getenv("FRAUDPHEUS_API_KEY")
if not API_KEY:
    print("Warning: FRAUDPHEUS_API_KEY not set; API will reject all requests")

app = FastAPI()

_user_cache = {}

def cached_user_info(user_id):
    if not user_id:
        return None
    if user_id in _user_cache:
        return _user_cache[user_id]
    info = get_user_info(user_id)
    if info:
        _user_cache[user_id] = info
    return info

def require_api_key(request: Request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    token = auth.split(" ", 1)[1].strip()
    if token != API_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

@app.get("/api/v1/threads")
async def list_threads(user_slack_id: str = None, _: None = Depends(require_api_key)):
    if not user_slack_id:
        raise HTTPException(400, "user_slack_id required")
    summaries = []
    all_threads = []
    if thread_manager.has_active_thread(user_slack_id):
        t = thread_manager.get_active_thread(user_slack_id)
        all_threads.append((t, "active"))
    for t in thread_manager.get_completed_threads(user_slack_id):
        all_threads.append((t, "completed"))
    for t, status in all_threads:
        thread_ts = t["thread_ts"]
        started_at_iso = None
        snippet = ""
        try:
            resp = client.conversations_replies(channel=CHANNEL, ts=thread_ts, limit=1, inclusive=True)
            for m in resp.get("messages", []):
                if m.get("ts") == thread_ts:
                    ts_float = float(m["ts"]) if m.get("ts") else 0
                    started_at_iso = datetime.fromtimestamp(ts_float, tz=timezone.utc).isoformat().replace("+00:00","Z")
                    text = m.get("text", "")
                    snippet = text
                    break
        except SlackApiError:
            pass
        summaries.append({
            "thread_ts": thread_ts,
            "started_at": started_at_iso,
            "initial_message_snippet": snippet,
            "status": status
        })
    summaries.sort(key=lambda x: float(x["thread_ts"]), reverse=True)
    return summaries

@app.post("/api/v1/threads", status_code=201)
async def start_thread(body: dict, _: None = Depends(require_api_key)):
    user_slack_id = body.get("user_slack_id")
    initial_message = body.get("initial_message")
    author_slack_id = body.get("author_slack_id")
    if not user_slack_id or not initial_message or not author_slack_id:
        raise HTTPException(400, "Missing required fields")
    user_info = cached_user_info(user_slack_id)
    if not user_info:
        raise HTTPException(404, "User not found")
    dm_ts = send_dm_to_user(user_slack_id, expand_macros(initial_message))
    if not dm_ts:
        raise HTTPException(500, "Failed to send DM")
    try:
        channel_text = f"*<@{author_slack_id}> started a message to <@{user_slack_id}>:*\n{initial_message}"
        response = client.chat_postMessage(
            channel=CHANNEL,
            text=channel_text,
            blocks=get_standard_channel_msg(user_slack_id, channel_text),
            username=user_info["display_name"],
            icon_url=user_info["avatar"]
        )
        thread_manager.create_active_thread(user_slack_id, CHANNEL, response["ts"], response["ts"])
        dispatch_event("thread.created", {
            "thread_ts": response["ts"],
            "user_slack_id": user_slack_id,
            "started_at": datetime.fromtimestamp(float(response["ts"]), tz=timezone.utc).isoformat().replace("+00:00","Z"),
            "initial_message": initial_message
        })
        return {"thread_ts": response["ts"]}
    except SlackApiError as err:
        raise HTTPException(500, f"Slack error: {err.response['error'] if err.response else str(err)}")

@app.get("/api/v1/threads/{thread_ts}")
async def get_thread_history(thread_ts: str, _: None = Depends(require_api_key)):
    try:
        replies = client.conversations_replies(channel=CHANNEL, ts=thread_ts)
        messages = replies.get("messages", [])
        filtered = []
        target_user_id = thread_manager.get_user_by_thread_ts(thread_ts)
        for m in messages:
            if m.get("ts") == thread_ts:
                continue
            text = m.get("text", "")
            user = m.get("user")
            is_bot = m.get("bot_id") is not None
            is_from_user = user == target_user_id and not is_bot
            if is_from_user or (text and (text.startswith("!") or any(text.startswith(k) for k in ["$final","$ban","$deduct","$noevidence","$dm","$alt"]))):
                if text.startswith("!"):
                    text = text[1:]
                    ts_float = float(m["ts"]) if m.get("ts") else 0
                filtered.append({
                    "id": m.get("ts"),
                    "content": text,
                    "timestamp": datetime.fromtimestamp(ts_float, tz=timezone.utc).isoformat().replace("+00:00","Z"),
                    "is_from_user": is_from_user,
            "author": {"name": cached_user_info(user)["name"] if user else "Unknown"}
                })
        filtered.sort(key=lambda x: x["id"])  # Slack ts are sortable lexicographically when same channel
        return filtered
    except SlackApiError as err:
        if err.response and err.response.get("error") == "thread_not_found":
            raise HTTPException(404, "Thread not found")
        raise HTTPException(500, f"Slack error: {err.response['error'] if err.response else str(err)}")

@app.post("/api/v1/threads/{thread_ts}/messages", status_code=201)
async def send_message(thread_ts: str, body: dict, _: None = Depends(require_api_key)):
    content = body.get("content")
    author_slack_id = body.get("author_slack_id")
    if not content or not author_slack_id:
        raise HTTPException(400, "Missing required fields")
    target_user_id = thread_manager.get_user_by_thread_ts(thread_ts)
    if not target_user_id:
        raise HTTPException(404, "Thread not found")
    expanded = expand_macros(content)
    dm_ts = send_dm_to_user(target_user_id, expanded)
    if not dm_ts:
        raise HTTPException(500, "Failed to send DM")
    try:
        post = client.chat_postMessage(
            channel=CHANNEL,
            thread_ts=thread_ts,
            text=f"*<@{author_slack_id}>:*\n{content}",
            blocks=[
                {
                    "type": "markdown",
                    "text": f"*<@{author_slack_id}>:*\n{content}"
                }
            ]
        )
        thread_manager.update_thread_activity(target_user_id)
        ts_float = float(post["ts"]) if post.get("ts") else 0
        thread_manager.store_message_mapping(post["ts"], target_user_id, dm_ts, expanded, thread_ts)
        dispatch_event("message.staff.new", {
            "thread_ts": thread_ts,
            "message": {
                "id": post["ts"],
                "content": content,
                "timestamp": datetime.fromtimestamp(ts_float, tz=timezone.utc).isoformat().replace("+00:00","Z"),
                "is_from_user": False,
                "author": {"name": cached_user_info(author_slack_id)["name"] if author_slack_id else "Unknown"}
            }
        })
        return {
            "id": post["ts"],
            "content": content,
            "timestamp": datetime.fromtimestamp(ts_float, tz=timezone.utc).isoformat().replace("+00:00","Z"),
            "is_from_user": False,
            "author": {"name": cached_user_info(author_slack_id)["name"] if author_slack_id else "Unknown"}
        }
    except SlackApiError as err:
        raise HTTPException(500, f"Slack error: {err.response['error'] if err.response else str(err)}")

@app.post("/api/v1/threads/{thread_ts}/internal_note", status_code=201)
async def post_internal_note(thread_ts: str, body: dict, _: None = Depends(require_api_key)):
    content = body.get("content")
    author_name = body.get("author_name")
    attachments = body.get("attachments") or []
    if not content and not attachments:
        raise HTTPException(400, "Missing required fields")
    if not author_name:
        raise HTTPException(400, "Missing required fields")

    display_text = content or "(attachments)"

    main_text = f"**[Internal Note from {author_name}]:**"
    if content:
        main_text += f" {content}"

    blocks = [
        {
            "type": "markdown",
            "text": main_text
        }
    ]

    for att in attachments:
        if isinstance(att, dict) and att.get("image_url"):
            blocks.append({
                "type": "image",
                "image_url": att["image_url"],
                "alt_text": att.get("alt_text", "attachment")
            })

    print(blocks)
    try:
        client.chat_postMessage(
            channel=CHANNEL,
            thread_ts=thread_ts,
            text=f"[Internal Note from {author_name}]: {display_text}",
            blocks=blocks
        )
        return {"success": True}
    except SlackApiError as err:
        raise HTTPException(500, f"Slack error: {err.response['error'] if err.response else str(err)}")