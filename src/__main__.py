import os
import re
import time
import threading
import json
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from pyairtable import Api

from src.thread_manager import ThreadManager
from src.webhooks import dispatch_event

load_dotenv()

app = App(token=os.getenv("SLACK_BOT_TOKEN"))
client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))
user_client = WebClient(token=os.getenv("SLACK_USER_TOKEN"))

CHANNEL = os.getenv("CHANNEL_ID")

airtable_api = Api(os.getenv("AIRTABLE_API_KEY"))
airtable_base = airtable_api.base(os.getenv("AIRTABLE_BASE_ID"))

thread_manager = ThreadManager(airtable_base, client)

TRUST_EMOJI = {
    0: "🔵",
    1: "🔴",
    2: "🟢",
    3: "🟡",
    4: "⚠️"
}

TRUST_LABELS = {
    0: "Blue (Normal)",
    1: "Red (Banned/Convicted)",
    2: "Green (Trusted)",
    3: "Yellow (Suspicious)",
    4: "Unknown"
}

# we might need to change these soon
MACROS = {
    "$final": "Ban decisions are final. Thank you for your attention to this matter!",
    "$ban": "Hi, after reviewing your account, we have found evidence of substantial botting/hour inflation. As a result, you have been banned from hackatime, and future Hack Club events. You can appeal this decision by sending appropriate proof to this thread.",
    "$deduct": "Hi, after reviewing your account for SoM we found evidence of significant botting/hour inflation for your project(s). As a result, you will receive a payout deduction. Please note that continuing to log fraudulent time on projects will result in a ban from hackatime, SoM, and potentially future Hack Club events.",
    "$noevidence": "We cannot share our evidence for a ban due to the reasons outlined in the hackatime ban banner.",
    "$dm": "We aren't able to share details on bans for the reasons outlined on hackatime:\n```\nWe do not disclose the patterns that were detected. Releasing this information would only benefit fraudsters. The fraud team regularly investigates claims of false bans to increase the effectiveness of our detection systems to combat fraud.\n```\nWhat I can tell you:\nYou were banned because your hackatime data matched patterns strongly indicative of fraud, and this was verified by human reviewers. Ban decisions are final and will not be lifted. If you were banned in error, the ban will automatically be lifted.",
    "$alt": "Hi, we've determined that your account is/has an alt. Alting/ban evasion are not allowed. As a result, you've been banned from hackatime, SoM, and future Hack Club events."
}

def expand_macros(text):
    """Expand macros in text"""
    if not text:
        return text
    
    for macro, replacement in MACROS.items():
        if macro in text:
            text = text.replace(macro, replacement)
    
    return text
# ty miguel for code :3
def get_user_trust_level(slack_id):
    """Get user's trust level from hackatime API"""
    try:
        api_url = "https://hackatime.hackclub.com/api/admin/v1/execute"
        api_token = os.getenv("HACKATIME_API_KEY")

        if not api_token:
            print("HACKATIME_API_KEY not found in environment")
            return 4

        query = f"""
            SELECT trust_level
            FROM users
            WHERE slack_uid = '{slack_id}'
            LIMIT 1
        """

        response = requests.post(api_url,
            headers={
                'authorization': f'Bearer {api_token}',
                'content-type': 'application/json',
            },
            json={'query': query}
        )

        if response.status_code == 200:
            data = response.json()
            rows = data.get('rows', [])
            if rows and len(rows) > 0:
                trust_level = rows[0].get('trust_level')
                if trust_level is not None:
                    return int(trust_level[1]) if isinstance(trust_level, list) else int(trust_level)

        return 4
    except Exception as err:
        print(f"Error fetching trust level for {slack_id}: {err}")
        return 4

def get_past_threads_info(user_id):
    """Get formatted info about user's past threads"""
    completed_threads = thread_manager.get_completed_threads(user_id)

    if not completed_threads:
        return "No previous threads"

    thread_links = []
    for thread in completed_threads[:5]:
        thread_ts = thread.get("thread_ts", "")
        if thread_ts:
            thread_url = f"https://hackclub.slack.com/archives/{CHANNEL}/p{thread_ts.replace('.', '')}"
            thread_links.append(f"• <{thread_url}|Thread from {thread_ts}>")

    result = f"*Past threads ({len(completed_threads)} total):*\n" + "\n".join(thread_links)
    if len(completed_threads) > 5:
        result += f"\n_...and {len(completed_threads) - 5} more_"

    return result

def check_inactive_threads():
    """Check for inactive threads and send close message after 5 days"""
    while True:
        try:
            time.sleep(3600 * 12)
            inactive_threads = thread_manager.get_inactive_threads(120)

            for thread in inactive_threads:
                user_id = thread["user_id"]
                thread_info = thread["thread_info"]
                hours_inactive = thread["hours_inactive"]

                if hours_inactive >= 120:
                    try:
                        dm_response = client.conversations_open(users=[user_id])
                        dm_channel = dm_response["channel"]["id"]

                        client.chat_postMessage(
                            channel=dm_channel,
                            text="Heyo, it seems like this thread has gone quiet for a while. There is a good chance that this case is now resolved! If not, please open a new thread! :ohneheart:",
                            username="Arnav",
                            icon_emoji=":ban:"
                        )

                        thread_manager.complete_thread(user_id)
                        print(f"Auto-closed thread for user {user_id} after {hours_inactive} hours of inactivity")

                    except Exception as err:
                        print(f"Error auto-closing thread for user {user_id}: {err}")

        except Exception as err:
            print(f"Error in inactive thread checker: {err}")

def get_user_info_for_backup(user_id):
    """Get user info for backup export"""
    try:
        response = client.users_info(user=user_id)
        user = response["user"]
        return {
            "id": user_id,
            "name": user.get("name", ""),
            "real_name": user.get("real_name", ""),
            "display_name": user.get("profile", {}).get("display_name", ""),
            "email": user.get("profile", {}).get("email", ""),
            "is_bot": user.get("is_bot", False),
            "avatar": user.get("profile", {}).get("image_72", "")
        }
    except SlackApiError as e:
        print(f"Error getting user info for {user_id}: {e}")
        return {
            "id": user_id,
            "name": "unknown",
            "real_name": "Unknown User",
            "display_name": "Unknown",
            "email": "",
            "is_bot": False,
            "avatar": ""
        }

def create_backup_export():
    """Export all thread data to JSON with full message history"""
    try:
        backup_data = {
            "export_timestamp": datetime.now(timezone.utc).isoformat(),
            "channel_id": CHANNEL,
            "fraud_cases": [],
            "users": {},
            "statistics": {}
        }

        all_threads = []

        # Collect active threads
        for user_id, thread_info in thread_manager.active_cache.items():
            thread_ts = thread_info.get("thread_ts")
            if thread_ts:
                all_threads.append({
                    "user_id": user_id,
                    "thread_ts": thread_ts,
                    "status": "active"
                })

        # Collect completed threads
        for user_id, threads in thread_manager.completed_cache.items():
            for thread in threads:
                thread_ts = thread.get("thread_ts")
                if thread_ts:
                    all_threads.append({
                        "user_id": user_id,
                        "thread_ts": thread_ts,
                        "status": "completed"
                    })

        # Extract full message data for each thread
        for thread_data in all_threads:
            user_id = thread_data["user_id"]
            thread_ts = thread_data["thread_ts"]
            status = thread_data["status"]

            try:
                response = client.conversations_replies(
                    channel=CHANNEL,
                    ts=thread_ts,
                    limit=1000
                )
                messages = response.get("messages", [])

                if not messages:
                    continue

                # Get user info
                if user_id not in backup_data["users"]:
                    backup_data["users"][user_id] = get_user_info_for_backup(user_id)

                case_data = {
                    "case_id": thread_ts,
                    "reported_user_id": user_id,
                    "status": status,
                    "thread_ts": thread_ts,
                    "messages": [],
                    "created_at": None,
                    "last_activity": None,
                    "total_messages": len(messages)
                }

                for i, message in enumerate(messages):
                    msg_user_id = message.get("user")

                    if msg_user_id and msg_user_id not in backup_data["users"]:
                        backup_data["users"][msg_user_id] = get_user_info_for_backup(msg_user_id)

                    message_data = {
                        "ts": message.get("ts"),
                        "user": msg_user_id,
                        "text": message.get("text", ""),
                        "timestamp": datetime.fromtimestamp(float(message.get("ts", 0))).isoformat() if message.get("ts") else None,
                        "is_bot": message.get("bot_id") is not None,
                        "bot_id": message.get("bot_id"),
                        "username": message.get("username"),
                        "is_from_reported_user": msg_user_id == user_id and not message.get("bot_id")
                    }

                    case_data["messages"].append(message_data)

                    if i == 0:
                        case_data["created_at"] = message_data["timestamp"]

                    case_data["last_activity"] = message_data["timestamp"]

                backup_data["fraud_cases"].append(case_data)

            except Exception as err:
                print(f"Error fetching thread {thread_ts}: {err}")

        backup_data["statistics"] = {
            "total_cases": len(backup_data["fraud_cases"]),
            "active_cases": len([c for c in backup_data["fraud_cases"] if c["status"] == "active"]),
            "completed_cases": len([c for c in backup_data["fraud_cases"] if c["status"] == "completed"]),
            "total_users": len(backup_data["users"]),
            "total_messages": sum(c["total_messages"] for c in backup_data["fraud_cases"])
        }

        return backup_data

    except Exception as err:
        print(f"Error creating backup export: {err}")
        return None


def get_standard_channel_msg(user_id, message_text):
    """Get blocks for a standard message uploaded into channel with 2 buttons"""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"<@{user_id}> (User ID: `{user_id}`)"
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": message_text
            }
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Reply in this thread to send a response to the user"
                }
            ]
        },
        {
            "type": "actions",
            "elements": [
                { # Complete this pain of a thread
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Mark as Completed"
                    },
                    "style": "primary",
                    "action_id": "mark_completed",
                    "value": user_id,
                    "confirm": {
                        "title": {
                            "type": "plain_text",
                            "text": "Are you sure?"
                        },
                        "text": {
                            "type": "mrkdwn",
                            "text": "This will mark the thread as complete."
                        },
                        "confirm": {
                            "type": "plain_text",
                            "text": "Mark as Completed"
                        },
                        "deny": {
                            "type": "plain_text",
                            "text": "Cancel"
                        }
                    }

                },
                { # Delete it pls
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Delete thread"
                    },
                    "style": "danger",
                    "action_id": "delete_thread",
                    "value": user_id,
                    "confirm": {
                        "title": {
                            "type": "plain_text",
                            "text": "Are you sure?"
                        },
                        "text": {
                            "type": "mrkdwn",
                            "text": "This will delete the entire thread and new replies will go into a new thread"
                        },
                        "confirm": {
                            "type": "plain_text",
                            "text": "Delete"
                        },
                        "deny": {
                            "type": "plain_text",
                            "text": "Cancel"
                        }
                    }
                }
            ]
        }
    ]

def get_user_info(user_id):
    """Get user's profile info"""
    try:
        response = client.users_info(user=user_id)
        user = response["user"]
        return {
            "name": user["real_name"] or user["name"],
            "avatar": user["profile"].get("image_72", ""),
            "display_name": user["profile"].get("display_name", user["name"])
        }

    except SlackApiError as err:
        print(f"Error during user info collection: {err}")
        return None

def post_message_to_channel(user_id, message_text, user_info, files=None):
    """Post user's message to the given channel, either as new message or new reply"""
    # Slack is kinda weird and must have message text even when only file is shared
    if not message_text or message_text.strip() == "":
        return None

    file_yes = False
    if message_text == "[Shared a file]":
        file_yes = True
    if thread_manager.has_active_thread(user_id):
        thread_info = thread_manager.get_active_thread(user_id)

        try:
            response = client.chat_postMessage(
                channel=CHANNEL,
                thread_ts=thread_info["thread_ts"],
                text=f"{message_text}",
                username=user_info["display_name"],
                icon_url=user_info["avatar"]
            )

            if file_yes and files:
                download_reupload_files(files, CHANNEL, thread_info["thread_ts"])

            thread_manager.update_thread_activity(user_id)
            dispatch_event("message.user.new", {
                "thread_ts": thread_info["thread_ts"],
                "message": {
                    "id": response["ts"],
                    "content": message_text,
                    "timestamp": datetime.fromtimestamp(float(response["ts"])) .astimezone(timezone.utc).isoformat().replace("+00:00","Z"),
                    "is_from_user": True,
                    "author": {"name": user_info["display_name"]}
                }
            })
            return True

        except SlackApiError as err:
            print(f"Error writing to a thread: {err}")
            return False
    else:
        return create_new_thread(user_id, message_text, user_info)

def create_new_thread(user_id, message_text, user_info, files=None):
    """Create new thread in the channel"""
    try:
        response = client.chat_postMessage(
            channel=CHANNEL,
            text=f"*{user_id}*:\n{message_text}",
            username=user_info["display_name"],
            icon_url=user_info["avatar"],
            blocks=get_standard_channel_msg(user_id, message_text)
        )

        if files:
            download_reupload_files(files, CHANNEL, response["ts"])

        success = thread_manager.create_active_thread(
            user_id,
            CHANNEL,
            response["ts"],
            response["ts"]
        )
        if success:
            trust_level = get_user_trust_level(user_id)
            trust_emoji = TRUST_EMOJI.get(trust_level, TRUST_EMOJI[4])
            trust_label = TRUST_LABELS.get(trust_level, TRUST_LABELS[4])
            past_threads = get_past_threads_info(user_id)

            client.chat_postMessage(
                channel=CHANNEL,
                thread_ts=response["ts"],
                text=f"*User Info:*\n{trust_emoji} Trust Level: {trust_label}\n\n{past_threads}",
                username="Thread Info",
                icon_emoji=":information_source:"
            )

            dispatch_event("message.user.new", {
                "thread_ts": response["ts"],
                "message": {
                    "id": response["ts"],
                    "content": message_text,
                    "timestamp": datetime.fromtimestamp(float(response["ts"])) .astimezone(timezone.utc).isoformat().replace("+00:00","Z"),
                    "is_from_user": True,
                    "author": {"name": user_info["display_name"]}
                }
            })

        return success

    except SlackApiError as err:
        print(f"Error creating new thread: {err}")
        return False

def send_dm_to_user(user_id, reply_text, files=None):
    """Send a reply back to the user"""
    try:
        dm_response = client.conversations_open(users=[user_id])
        dm_channel = dm_response["channel"]["id"]

        if files or reply_text == "[Shared file]":
            return None

        response = client.chat_postMessage(
            channel=dm_channel,
            text=reply_text,
            username="Fraud Department",
            icon_emoji=":ban:"
        )

        return response["ts"] if response.get("ok") else None

    except SlackApiError as err:
        print(f"Error sending reply to user {user_id}: {err}")
        print(f"Error response: {err.response}")
        return None

def extract_user_id(text):
    """Extracts user ID from a mention text <@U000000> or from a direct ID"""
    mention_format = re.search(r"<@([A-Z0-9]+)>", text)
    if mention_format:
        return mention_format.group(1)

    id_match = re.search(r"\b(U[A-Z0-9]{8,})\b", text)
    if id_match:
        return id_match.group(1)

    return None


@app.command("/fdchat")
def handle_fdchat_cmd(ack, respond, command):
    """Handle conversations started by staff"""
    ack()
# for the leekers
    if command.get("channel_id") != CHANNEL:
        respond({
            "response_type": "ephemeral",
            "text": f"This command can only be used in one place. If you don't know it, don't even try"
        })
        return

    command_text = command.get("text", "").strip()

    if not command_text:
        respond({
            "response_type": "ephemeral",
            "text": "Usage: /fdchat @user your message' or '/fdchat U000000 your message'"
        })
        return

    requester_id = command.get("user_id")

    parts = command_text.split(" ", 1)
    user_id = parts[0]
    staff_message = expand_macros(parts[1])

    target_user_id = extract_user_id(user_id)
    if not target_user_id:
        respond({
            "response_type": "ephemeral",
            "text": "Provide a valid user ID: U000000 or a mention: @name"
        })
        return

    user_info = get_user_info(target_user_id)
    if not user_info:
        respond({
            "response_type": "ephemeral",
            "text": f"Couldn't find user info for {target_user_id}"
        })
        return

    if thread_manager.has_active_thread(target_user_id):
        thread_info = thread_manager.get_active_thread(target_user_id)

        try:
            response = client.chat_postMessage(
                channel=CHANNEL,
                thread_ts=thread_info["thread_ts"],
                text=f"*<@{requester_id}> continued:*\n{staff_message}"
            )
            dm_ts = send_dm_to_user(target_user_id, staff_message)
            thread_manager.update_thread_activity(target_user_id)

            if dm_ts:
                expanded_text = expand_macros(staff_message)
                if expanded_text != staff_message:
                    client.chat_postMessage(
                        channel=CHANNEL,
                        thread_ts=thread_info["thread_ts"],
                        text=f"📨 *Sent to user:*\n{expanded_text}",
                        username="Macro Echo",
                        icon_emoji=":outbox_tray:"
                    )
                thread_manager.store_message_mapping(response["ts"], target_user_id, dm_ts, staff_message, thread_info["thread_ts"])
                dispatch_event("message.staff.new", {
                    "thread_ts": thread_info["thread_ts"],
                    "message": {
                        "id": response["ts"],
                        "content": staff_message,
                        "timestamp": datetime.fromtimestamp(float(response["ts"])) .astimezone(timezone.utc).isoformat().replace("+00:00","Z"),
                        "is_from_user": False,
                        "author": {"name": get_user_info(requester_id)["name"] if requester_id else "Unknown"}
                    }
                })

            if dm_ts:
                respond({
                    "response_type": "ephemeral",
                    "text": f"Message sent in some older thread to {user_info['display_name']}"
                })
            else:
                respond({
                    "response_type": "ephemeral",
                    "text": f"It sucks, couldn't add a message to older thread for {user_info['display_name']}"
                })
            return
        except SlackApiError as err:
            respond({
                "response_type": "ephemeral",
                "text": f"Something broke, awesome - couldn't add a message to an existing thread"
            })
            return
    try: # Try, not trying. It was standing out a lot, I had to fix it a little
        dm_ts = send_dm_to_user(target_user_id, staff_message)
        if not dm_ts:
            respond({
                "response_type": "ephemeral",
                "text": f"Failed to send DM to {target_user_id}"
            })
            return
        original_sent_text = staff_message
        staff_message = f"*<@{requester_id}> started a message to <@{target_user_id}>:*\n" + staff_message

        response = client.chat_postMessage(
            channel=CHANNEL,
            text=f"*<@{user_id}> started a message to <@{target_user_id}>:*\n {staff_message}",
            username=user_info["display_name"],
            icon_url=user_info["avatar"],
            blocks=get_standard_channel_msg(target_user_id, staff_message)
        )

        thread_manager.create_active_thread(
            target_user_id,
            CHANNEL,
            response["ts"],
            response["ts"]
        )

        trust_level = get_user_trust_level(target_user_id)
        trust_emoji = TRUST_EMOJI.get(trust_level, TRUST_EMOJI[4])
        trust_label = TRUST_LABELS.get(trust_level, TRUST_LABELS[4])
        past_threads = get_past_threads_info(target_user_id)

        client.chat_postMessage(
            channel=CHANNEL,
            thread_ts=response["ts"],
            text=f"*User Info:*\n{trust_emoji} Trust Level: {trust_label}\n\n{past_threads}",
            username="Thread Info",
            icon_emoji=":information_source:"
        )

        thread_manager.store_message_mapping(response["ts"], target_user_id, dm_ts, original_sent_text, response["ts"])
        dispatch_event("thread.created", {
            "thread_ts": response["ts"],
            "user_slack_id": target_user_id,
            "started_at": datetime.fromtimestamp(float(response["ts"])) .astimezone(timezone.utc).isoformat().replace("+00:00","Z"),
            "initial_message": original_sent_text
        })
        dispatch_event("message.staff.new", {
            "thread_ts": response["ts"],
            "message": {
                "id": response["ts"],
                "content": original_sent_text,
                "timestamp": datetime.fromtimestamp(float(response["ts"])) .astimezone(timezone.utc).isoformat().replace("+00:00","Z"),
                "is_from_user": False,
                "author": {"name": get_user_info(requester_id)["name"] if requester_id else "Unknown"}
            }
        })

        expanded_text = expand_macros(staff_message)
        if expanded_text != staff_message:
            client.chat_postMessage(
                channel=CHANNEL,
                thread_ts=response["ts"],
                text=f"📨 *Sent to user:*\n{expanded_text}",
                username="Macro Echo",
                icon_emoji=":outbox_tray:"
            )

        respond({
            "response_type": "ephemeral",
            "text": f"Started conversation with {user_info['display_name']}, good luck"
        })

        print(f"Successfully started conversation with {target_user_id} via slash command")

    except SlackApiError as err:
        respond({
            "response_type": "ephemeral",
            "text": f"Error starting conversation: {err}"
        })

def handle_dms(user_id, message_text, files, say):
    """Receive and react to messages sent to the bot"""
    user_info = get_user_info(user_id)
    if not user_info:
        say("Hiya! Couldn't process your message, try again another time")
        return
    success = post_message_to_channel(user_id, message_text, user_info, files)
    if not success:
        say("There was some error during processing of your message, try again another time")

@app.message("")
def handle_all_messages(message, say, client, logger):
    """Handle all messages related to the bot"""
    user_id = message["user"]
    message_text = message["text"]
    channel_type = message.get("channel_type", '')
    files = message.get("files", [])
    channel_id = message.get("channel")

    print(f"Message received - Channel: {channel_id}, Type: {channel_type}")

    if message.get("bot_id"):
        return

    if channel_type == "im":
        handle_dms(user_id, message_text, files, say)
    elif channel_id == CHANNEL:
        if message_text and message_text.strip() == "!backup":
            handle_backup_command(message, client)
        elif message_text and message_text.strip() == "!bulkresolve":
            handle_bulkresolve_command(message, client)
        elif "thread_ts" in message:
            handle_channel_reply(message, client)

def handle_channel_reply(message, client):
    """Handle replies in channel to send them to users"""
    thread_ts = message["thread_ts"]
    reply_text = message["text"]
    files = message.get("files", [])
    fraud_dept_ts = message["ts"]

    if reply_text and reply_text.strip() == "!backup":
        handle_backup_command(message, client)
        return

    if reply_text and reply_text.strip() == "!bulkresolve":
        handle_bulkresolve_command(message, client)
        return

    is_macro = reply_text and any(reply_text.startswith(macro) for macro in MACROS.keys())

    if not reply_text or (not is_macro and len(reply_text) > 0 and reply_text[0] != '!'):
        return

    if reply_text and reply_text[0] == '!' and not is_macro:
        reply_text = reply_text[1:]

    original_text = reply_text
    reply_text = expand_macros(reply_text)

    #if reply_text and files:
    #    return

    # Find user's active thread by TS (look in cache -> look at TS)
    target_user_id = None
    for user_id in thread_manager.active_cache:
        thread_info = thread_manager.get_active_thread(user_id)

        # Check the TS
        if thread_info and thread_info["thread_ts"] == thread_ts:
            target_user_id = user_id
            break

    if target_user_id:
        dm_ts = send_dm_to_user(target_user_id, reply_text, files)

        # Some logging
        if dm_ts:
            thread_manager.store_message_mapping(fraud_dept_ts, target_user_id, dm_ts, reply_text, thread_ts)
            dispatch_event("message.staff.new", {
                "thread_ts": thread_ts,
                "message": {
                    "id": fraud_dept_ts,
                    "content": reply_text,
                    "timestamp": datetime.fromtimestamp(float(fraud_dept_ts)).astimezone(timezone.utc).isoformat().replace("+00:00","Z"),
                    "is_from_user": False,
                    "author": {"name": get_user_info(message["user"]) ["name"] if message.get("user") else "Unknown"}
                }
            })
            thread_manager.update_thread_activity(target_user_id)
            
            # Only echo if macros were used
            if original_text != reply_text:
                client.chat_postMessage(
                    channel=CHANNEL,
                    thread_ts=thread_ts,
                    text=f"📨 *Sent to user:*\n{reply_text}",
                    username="Macro Echo",
                    icon_emoji=":outbox_tray:"
                )
            
            try:
                client.reactions_add(
                    channel=CHANNEL,
                    timestamp=message["ts"],
                    name="done"
                )
            except SlackApiError as err:
                print(f"Failed to add done reaction: {err}")
        else:
            print(f"Failed to send reply to user {target_user_id}")
            try:
                client.reactions_add(
                    channel=CHANNEL,
                    timestamp=message["ts"],
                    name="x"
                )
            except SlackApiError as err:
                print(f"Failed to add X reaction: {err}")
    else:
        print(f"Could not find user for thread {thread_ts}")

def handle_bulkresolve_command(message, client):
    """Handle !bulkresolve command to auto-resolve threads inactive for 2+ days"""
    try:
        thread_ts = message.get("thread_ts")
        user_id = message.get("user")

        # for the goobers who try to run this elsewhere
        if thread_ts:
            client.chat_postMessage(
                channel=CHANNEL,
                thread_ts=thread_ts,
                text="⚠️ The `!bulkresolve` command can only be used in the main channel, not in threads.",
                username="Bulk Resolve Bot"
            )
            return

        # Get inactive threads (2+ days = 48+ hours)
        inactive_threads = thread_manager.get_inactive_threads(48)

        if not inactive_threads:
            client.chat_postMessage(
                channel=CHANNEL,
                text="✅ No threads have been inactive for 2+ days. All threads are active!",
                username="Bulk Resolve Bot"
            )
            return

        initial_message = f"🔄 **Bulk Resolve Started**\n\nFound {len(inactive_threads)} thread(s) inactive for 2+ days.\nResolving threads and notifying users..."

        initial_response = client.chat_postMessage(
            channel=CHANNEL,
            text=initial_message,
            username="Bulk Resolve Bot"
        )

        initial_msg_ts = initial_response["ts"]

        # Process in background
        def run_bulkresolve():
            resolved_count = 0
            failed_count = 0

            for thread_data in inactive_threads:
                target_user_id = thread_data["user_id"]
                thread_info = thread_data["thread_info"]
                hours_inactive = thread_data["hours_inactive"]

                try:
                    dm_response = client.conversations_open(users=[target_user_id])
                    dm_channel = dm_response["channel"]["id"]

                    # Use Slack's mrkdwn formatting with small text via context block
                    client.chat_postMessage(
                        channel=dm_channel,
                        text="Heyo, it looks like this thread has gone quiet!",
                        blocks=[
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": "Heyo, it looks like this thread has gone quiet!"
                                }
                            },
                            {
                                "type": "context",
                                "elements": [
                                    {
                                        "type": "mrkdwn",
                                        "text": "_This action has been performed automatically by a friendly bot_"
                                    }
                                ]
                            }
                        ],
                        username="Fraud Department",
                        icon_emoji=":ban:"
                    )

                    # Complete the thread
                    success = thread_manager.complete_thread(target_user_id)

                    if success:
                        resolved_count += 1
                        # Dispatch event for tracking
                        dispatch_event("thread.status.changed", {
                            "thread_ts": thread_info.get("thread_ts"),
                            "user_slack_id": target_user_id,
                            "new_status": "completed",
                            "timestamp": datetime.utcnow().replace(tzinfo=timezone.utc).isoformat().replace("+00:00","Z"),
                            "reason": "bulk_auto_resolve"
                        })
                        print(f"Auto-resolved thread for user {target_user_id} (inactive for {hours_inactive:.1f} hours)")
                    else:
                        failed_count += 1

                except Exception as e:
                    print(f"Error resolving thread for {target_user_id}: {e}")
                    failed_count += 1

            # Send completion report
            report = f"✅ **Bulk Resolve Complete**\n\n"
            report += f"• Resolved: {resolved_count}\n"
            report += f"• Failed: {failed_count}\n"
            report += f"• Total: {len(inactive_threads)}"

            client.chat_postMessage(
                channel=CHANNEL,
                text=report,
                username="Bulk Resolve Bot"
            )

            # Add checkmark to initial message
            try:
                client.reactions_add(
                    channel=CHANNEL,
                    timestamp=initial_msg_ts,
                    name="white_check_mark"
                )
            except SlackApiError as err:
                print(f"Failed to add reaction to initial message: {err}")

        # Run in background thread
        bulkresolve_thread = threading.Thread(target=run_bulkresolve, daemon=True)
        bulkresolve_thread.start()

        print(f"Bulk resolve command initiated by user {user_id} for {len(inactive_threads)} threads")

    except Exception as err:
        print(f"Error in bulkresolve command handler: {err}")
        try:
            client.chat_postMessage(
                channel=CHANNEL,
                text=f"❌ **Bulk Resolve Error**\n\nFailed to execute: {str(err)[:200]}",
                username="Bulk Resolve Bot"
            )
        except:
            pass

def handle_backup_command(message, client):
    """Handle !backup command to start fraud case extraction"""
    try:
        thread_ts = message.get("thread_ts")
        user_id = message.get("user")

        initial_message = "🔄 **Backup Started**\n\nCreating backup of all thread data..."

        if thread_ts:
            client.chat_postMessage(
                channel=CHANNEL,
                thread_ts=thread_ts,
                text=initial_message,
                username="Backup Bot"
            )
        else:
            client.chat_postMessage(
                channel=CHANNEL,
                text=initial_message,
                username="Backup Bot"
            )

        def run_backup():
            try:
                backup_data = create_backup_export()
                if not backup_data:
                    error_msg = "❌ **Backup Failed**\n\nCould not create export data"
                    if thread_ts:
                        client.chat_postMessage(
                            channel=CHANNEL,
                            thread_ts=thread_ts,
                            text=error_msg,
                            username="Backup Bot"
                        )
                    else:
                        client.chat_postMessage(
                            channel=CHANNEL,
                            text=error_msg,
                            username="Backup Bot"
                        )
                    return

                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"fraudpheus_messages_{timestamp}.json"
                json_content = json.dumps(backup_data, indent=2, ensure_ascii=False)

                success_msg = f"✅ **Message Export Complete!**\n\n**Statistics:**\n• {backup_data['statistics']['total_cases']} fraud cases\n• {backup_data['statistics']['total_messages']} total messages\n• {backup_data['statistics']['total_users']} users"

                if thread_ts:
                    client.chat_postMessage(
                        channel=CHANNEL,
                        thread_ts=thread_ts,
                        text=success_msg,
                        username="Backup Bot"
                    )
                else:
                    client.chat_postMessage(
                        channel=CHANNEL,
                        text=success_msg,
                        username="Backup Bot"
                    )

                client.files_upload_v2(
                    channel=CHANNEL,
                    content=json_content.encode('utf-8'),
                    filename=filename,
                    title=f"Fraudpheus Message Export - {timestamp}",
                    initial_comment="**Backup file attached below** 📎",
                    thread_ts=thread_ts
                )

            except Exception as e:
                error_msg = f"❌ **Backup Error**\n\nFailed to run backup: {str(e)[:500]}"
                if thread_ts:
                    client.chat_postMessage(
                        channel=CHANNEL,
                        thread_ts=thread_ts,
                        text=error_msg,
                        username="Backup Bot"
                    )
                else:
                    client.chat_postMessage(
                        channel=CHANNEL,
                        text=error_msg,
                        username="Backup Bot"
                    )

        backup_thread = threading.Thread(target=run_backup, daemon=True)
        backup_thread.start()

        print(f"Backup command initiated by user {user_id}")

    except Exception as err:
        print(f"Error in backup command handler: {err}")

@app.action("mark_completed")
def handle_mark_completed(ack, body, client):
    """Complete the thread"""
    ack()

    user_id = body["actions"][0]["value"]
    messages_ts = body["message"]["ts"]

    # Give a nice checkmark
    try:
        client.reactions_add(
            channel=CHANNEL,
            timestamp=messages_ts,
            name="white_check_mark"
        )

        success = thread_manager.complete_thread(user_id)
        if success:
            print(f"Marked thread for user {user_id} as completed")
            thread_info = thread_manager.get_active_thread(user_id) or {}
            dispatch_event("thread.status.changed", {
                "thread_ts": body["message"]["ts"],
                "user_slack_id": user_id,
                "new_status": "completed",
                "timestamp": datetime.utcnow().replace(tzinfo=timezone.utc).isoformat().replace("+00:00","Z")
            })
        else:
            print(f"Failed to mark {user_id}'s thread as completed")

    except SlackApiError as err:
        print(f"Error marking thread as completed: {err}")

@app.action("delete_thread")
def handle_delete_thread(ack, body, client):
    """Handle deleting thread"""
    ack()

    user_id = body["actions"][0]["value"]
    message_ts = body["message"]["ts"]

    try:
        thread_info = {}

        # Check if user has an active thread - get its info
        if user_id in thread_manager.active_cache and thread_manager.active_cache[user_id]["message_ts"] == message_ts:
            thread_info = thread_manager.active_cache[user_id]
        # Else, if he has a completed thread - get that info
        elif user_id in thread_manager.completed_cache:
            for i, thread in enumerate(thread_manager.completed_cache[user_id]):
                if thread["message_ts"] == message_ts:
                    thread_info = thread
                    break

        if not thread_info:
            print(f"Couldn't find thread info for {user_id} (messages ts {message_ts})")
            return

        thread_ts = thread_info["thread_ts"]

        # Try deleting
        try:
            cursor = None
            while True:
                api_args = {
                    "channel": CHANNEL,
                    "ts": thread_ts,
                    "inclusive": True,
                    "limit": 100
                }

                if cursor:
                    api_args["cursor"] = cursor

                response = client.conversations_replies(**api_args)
                messages = response["messages"]

                for message in messages:
                    try:
                        user_client.chat_delete(
                            channel=CHANNEL,
                            ts=message["ts"],
                            as_user=True
                        )
                        time.sleep(0.3)

                    except SlackApiError as err:
                        try:
                            client.chat_delete(
                                channel=CHANNEL,
                                ts=message["ts"]
                            )
                            time.sleep(0.3)

                        except SlackApiError as err:
                            print(f"Couldn't delete messages {message['ts']}: {err}")
                            time.sleep(0.2)
                            continue

                if response.get("has_more", False) and response.get("response_metadata", {}).get("next_cursor"):
                    cursor = response["response_metadata"]["next_cursor"]
                else:
                    break

        except SlackApiError as err:
            print(f"Error deleting thread: {err}")

        thread_manager.delete_thread(user_id, message_ts)

    except SlackApiError as err:
        print(f"Error deleting thread: {err}")

@app.event("file_shared")
def handle_file_shared(event, client, logger):
    """Handle files being shared"""
    try:
        file_id = event["file_id"]
        user_id = event["user_id"]
        file_info = client.files_info(file=file_id)
        file_data = file_info["file"]

        channels = file_data.get("channels", [])
        groups = file_data.get("groups", [])
        ims = file_data.get("ims", [])

        if ims and not file_data.get("initial_comment") and file_data.get("comments_count") == 0:
            user_info = get_user_info(user_id)
            message_text = "[Shared a file]"
            if user_info:
                success = post_message_to_channel(user_id, message_text, user_info, [file_data])

                if not success:
                    try:
                        dm_response = client.conversations_open(users=user_id)
                        dm_channel = dm_response["channel"]["id"]
                        client.chat_postMessage(
                            channel=dm_channel,
                            type="ephemeral",
                            username="Fraud Department",
                            icon_emoji=":ban:",
                            text="*No luck for you, there was an issue processing your file*"
                        )

                    except SlackApiError as err:
                        print(f"Failed to send error msg: {err}")

        elif groups and not file_data.get("initial_comment") and file_data.get("comments_count") == 0:
            # Gosh that took a long time, grabbing the channel shares to get thread_ts, quite creative, eh?
            thread_ts = file_data.get("shares")["private"][CHANNEL][0]["thread_ts"]

            for user in thread_manager.active_cache:
                if thread_manager.active_cache[user]["thread_ts"] == thread_ts:
                    send_dm_to_user(user, "[Shared file]", [file_data])


    except SlackApiError as err:
        logger.error(f"Error handling file_shared event: {err}")




def format_file(files):
    """Format file for a nice view in message"""
    if not files:
        return ""

    file_info = []
    for file in files:
        file_type = file.get("mimetype", "unknown")
        file_name = file.get("name", "unknown file")
        file_size = file.get("size", 0)

        if file_size > 1024 * 1024:
            size_str = f"{file_size / (1024 * 1024):.1f}MB"
        elif file_size > 1024:
            size_str = f"{file_size / 1024:.1f}KB"
        else:
            size_str = f"{file_size}B"

        file_info.append(f"File *{file_name} ({file_type}, {size_str})")

    return "\n" + "\n".join(file_info)

def download_reupload_files(files, channel, thread_ts=None):
    """Download files, then reupload them to the target channel"""
    reuploaded = []
    for file in files:
        try:
            file_url = file.get("url_private_download") or file.get("url_private")
            if not file_url:
                print(f"Can't really download without any url for file {file.get('name', 'unknown')}")
                continue

            headers = {'Authorization': f"Bearer {os.getenv('SLACK_BOT_TOKEN')}"}
            response = requests.get(file_url, headers=headers)

            if response.status_code == 200:
                upload_params = {
                    "channel": channel,
                    "file": response.content,
                    "filename": file.get("name", "file"),
                    "title": file.get("title", file.get("name", "Some file without name?"))
                }

                if thread_ts:
                    upload_params["thread_ts"] = thread_ts

                upload_response = client.files_upload_v2(**upload_params)

                if upload_response.get("ok"):
                    reuploaded.append(upload_response["file"])
                else:
                    print(f"Failed to reupload file: {upload_response.get('error')}")

        except Exception as err:
            print(f"Error processing file: {file.get('name', 'unknown'): {err}}")

    return reuploaded


@app.event("message")
def handle_message_events(body, logger):
    """Handle message events including deletions"""
    event = body.get("event", {})
    
    if event.get("subtype") == "message_deleted":
        handle_message_deletion(event, logger)
    elif event.get("subtype") == "message_changed":
        handle_message_changed(event, logger)

def handle_message_deletion(event, logger):
    """Handle message deletion events"""
    try:
        deleted_ts = event.get("deleted_ts")
        channel = event.get("channel")
        
        if not deleted_ts or not channel:
            return
            
        if channel == CHANNEL:
            handle_fraud_dept_deletion(deleted_ts, logger)
        else:
            handle_user_dm_deletion(deleted_ts, channel, logger)
            
    except Exception as err:
        logger.error(f"Error handling message deletion: {err}")

def handle_fraud_dept_deletion(deleted_ts, logger):
    """Handle deletion of messages by fraud dept members"""
    try:
        mapping = thread_manager.get_message_mapping(deleted_ts)
        if not mapping:
            return
            
        user_id = mapping["user_id"]
        dm_ts = mapping["dm_ts"]
        
        try:
            dm_response = client.conversations_open(users=[user_id])
            dm_channel = dm_response["channel"]["id"]
            
            try:
                user_client.chat_delete(
                    channel=dm_channel,
                    ts=dm_ts,
                    as_user=True
                )
                print(f"Deleted DM message for user {user_id}")
            except SlackApiError:
                try:
                    client.chat_delete(
                        channel=dm_channel,
                        ts=dm_ts
                    )
                    print(f"Deleted DM message for user {user_id} (as bot)")
                except SlackApiError as delete_err:
                    print(f"Failed to delete DM message for user {user_id}: {delete_err}")
                    
            mapping = thread_manager.get_message_mapping(deleted_ts)
            thread_ts = mapping.get("thread_ts") if mapping else None
            thread_manager.remove_message_mapping(deleted_ts)
            if thread_ts:
                dispatch_event("message.deleted", {
                    "thread_ts": thread_ts,
                    "message_id": deleted_ts
                })
            
        except SlackApiError as err:
            print(f"Error accessing DM channel for user {user_id}: {err}")
                
    except Exception as err:
        logger.error(f"Error in fraud dept deletion handler: {err}")

def handle_user_dm_deletion(deleted_ts, dm_channel, logger):
    """Handle deletion of messages by users"""
    pass

def handle_message_changed(event, logger):
    try:
        message = event.get("message", {})
        edited = message.get("edited")
        if not message or not edited:
            return
        ts = message.get("ts")
        channel = event.get("channel")
        if channel != CHANNEL:
            return
        mapping = thread_manager.get_message_mapping(ts)
        if not mapping:
            return
        thread_ts = mapping.get("thread_ts")
        content = message.get("text", "")
        dispatch_event("message.updated", {
            "thread_ts": thread_ts,
            "message": {
                "id": ts,
                "content": content,
                "timestamp": datetime.utcnow().replace(tzinfo=timezone.utc).isoformat().replace("+00:00","Z"),
                "is_from_user": False,
                "author": {"name": "Unknown"}
            }
        })
    except Exception as err:
        logger.error(f"Error handling message_changed: {err}")


@app.error
def error_handler(error, body, logger):
    logger.exception(f"Error: {error}")
    logger.info(f"Request body: {body}")

if __name__ == "__main__":
    auto_close_thread = threading.Thread(target=check_inactive_threads, daemon=True)
    auto_close_thread.start()

    handler = SocketModeHandler(app, os.getenv("SLACK_APP_TOKEN"))
    print("Bot running!")
    print("Auto-close inactive threads system started")
    handler.start()
