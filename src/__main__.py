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

# Slack setup
app = App(token=os.getenv("SLACK_BOT_TOKEN"))
client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))
user_client = WebClient(token=os.getenv("SLACK_USER_TOKEN"))

CHANNEL = os.getenv("CHANNEL_ID")
AI_ALERTS_CHANNEL = "C09F4AVU6UA"  # Channel for AI resolution alerts

# Airtable setup
airtable_api = Api(os.getenv("AIRTABLE_API_KEY"))
airtable_base = airtable_api.base(os.getenv("AIRTABLE_BASE_ID"))

# Thread stuff
thread_manager = ThreadManager(airtable_base, client)

# Macros
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

def call_ai_api(text):
    """Call ai.hackclub.com API to formalize text"""
    try:
        prompt = f"""./no_think

Rewrite this message to be professional and appropriate for customer support. Be firm but also relatively casual and friendly. Do not use slang. Make it clear, direct, and business-appropriate without being overly formal. Do not add placeholders, such as [Your Name]. Do not use em-dashes. Do not call them "customers" or anything like that. Just improve the tone and clarity of the exact message provided:

{text}"""
        
        response = requests.post("https://ai.hackclub.com/chat/completions", 
            headers={"Content-Type": "application/json"},
            json={
                "model": "openai/gpt-oss-120b",
                "messages": [{"role": "user", "content": prompt}]
            })
        
        if response.status_code == 200:
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            if not content:
                print(f"AI API returned empty content for: '{text}'")
                print(f"Full response: {data}")
            return content
        else:
            print(f"AI API error: {response.status_code} - {response.text}")
            return None
    except Exception as err:
        print(f"Error calling AI API: {err}")
        return None

def analyze_thread_resolution(conversation_text):
    """Use AI to determine if a thread appears to be resolved"""
    try:
        prompt = f"""./no_think

Analyze this customer support conversation and determine if the issue appears to be resolved.

Look for:
- Clear resolution statements from support staff
- User acknowledgment or satisfaction
- Final answers or decisions being communicated
- Ban confirmations or account actions completed
- Appeals being closed with final decisions

Respond with ONLY "RESOLVED" or "UNRESOLVED" based on whether the conversation appears to have reached a clear conclusion.

Conversation:
{conversation_text}"""
        
        response = requests.post("https://ai.hackclub.com/chat/completions", 
            headers={"Content-Type": "application/json"},
            json={
                "model": "openai/gpt-oss-120b",
                "messages": [{"role": "user", "content": prompt}]
            })
        
        if response.status_code == 200:
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip().upper()
            return content == "RESOLVED"
        else:
            print(f"AI resolution analysis error: {response.status_code} - {response.text}")
            return False
    except Exception as err:
        print(f"Error analyzing thread resolution: {err}")
        return False

def post_ai_resolution_alert(user_id, thread_info):
    """Post AI-detected resolution to alerts channel with mark resolved button"""
    try:
        user_info = get_user_info(user_id)
        display_name = user_info["display_name"] if user_info else user_id
        
        thread_ts = thread_info["thread_ts"]
        thread_url = f"https://hackclub.slack.com/archives/{CHANNEL}/p{thread_ts.replace('.', '')}"
        
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🤖 *AI Detection: Thread May Be Resolved*\n\nUser: <@{user_id}> ({display_name})\nThread: <{thread_url}|View Thread>\n\nAI analysis suggests this thread conversation appears to be resolved."
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Mark as Resolved"
                        },
                        "style": "primary",
                        "action_id": "ai_mark_resolved",
                        "value": user_id
                    },
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Keep Open"
                        },
                        "action_id": "ai_keep_open",
                        "value": user_id
                    }
                ]
            }
        ]
        
        client.chat_postMessage(
            channel=AI_ALERTS_CHANNEL,
            text=f"AI detected potentially resolved thread for {display_name}",
            blocks=blocks,
            username="AI Resolution Detection",
            icon_emoji=":robot_face:"
        )
        
        print(f"Posted AI resolution alert for user {user_id}")
        return True
        
    except Exception as err:
        print(f"Error posting AI resolution alert: {err}")
        return False

def check_inactive_threads():
    """Check for inactive threads and send reminders"""
    while True:
        try:
            time.sleep(3600 * 6)  # Check every 6 hours
            inactive_threads = thread_manager.get_inactive_threads(48)
            
            if inactive_threads:
                reminder_text = f"🔔 *Thread Activity Reminder*\n\nThe following {len(inactive_threads)} thread(s) have been inactive for 2+ days:\n\n"
                
                for thread in inactive_threads:
                    user_id = thread["user_id"]
                    hours_inactive = int(thread["hours_inactive"])
                    thread_ts = thread["thread_info"]["thread_ts"]
                    reminder_text += f"• <@{user_id}> - {hours_inactive} hours inactive - https://hackclub.slack.com/archives/{CHANNEL}/p{thread_ts.replace('.', '')}\n"
                
                reminder_text += "\nPlease review and resolve these threads."
                
                try:
                    client.chat_postMessage(
                        channel=CHANNEL,
                        text=reminder_text
                    )
                    print(f"Sent reminder for {len(inactive_threads)} inactive threads")
                except SlackApiError as err:
                    print(f"Error sending reminder: {err}")
                    
        except Exception as err:
            print(f"Error in inactive thread checker: {err}")

def check_ai_thread_resolutions():
    """Background task to check for resolved threads using AI"""
    checked_threads = set()  # Track threads we've already analyzed
    
    while True:
        try:
            time.sleep(7200)  # Check every 2 hours
            print("Starting AI thread resolution analysis...")
            
            active_threads = list(thread_manager.active_cache.items())
            newly_analyzed = 0
            
            for user_id, thread_info in active_threads:
                # Skip if we've already analyzed this thread
                thread_key = f"{user_id}_{thread_info.get('thread_ts')}"
                if thread_key in checked_threads:
                    continue
                
                # Get the full conversation
                conversation = thread_manager.get_thread_conversation(user_id)
                if not conversation:
                    continue
                
                # Skip very short conversations (likely not resolved)
                if len(conversation) < 100:
                    continue
                
                print(f"Analyzing thread for user {user_id}...")
                
                # Analyze with AI
                if analyze_thread_resolution(conversation):
                    print(f"AI detected resolved thread for user {user_id}")
                    post_ai_resolution_alert(user_id, thread_info)
                    newly_analyzed += 1
                
                # Mark this thread as analyzed
                checked_threads.add(thread_key)
                
                # Small delay between analyses to avoid rate limits
                time.sleep(2)
            
            print(f"AI analysis complete. Found {newly_analyzed} potentially resolved threads.")
            
            # Clean up checked_threads set if it gets too large
            if len(checked_threads) > 1000:
                checked_threads.clear()
                print("Cleared checked threads cache")
                
        except Exception as err:
            print(f"Error in AI thread resolution checker: {err}")

def get_standard_channel_msg(user_id, message_text):
    """Get blocks for a standard message uploaded into channel with 2 buttons"""
    return [
        { # Quick notice to whom the message is directed to
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"<@{user_id}> (User ID: `{user_id}`)"
            },
        },
        { # Message
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": message_text
            }
        },
        { # A little guide, cause why not
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Reply in this thread to send a response to the user"
                }
            ]
        },
        { # Fancy buttons
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
                    "confirm": { # Confirmation screen of delete thread button
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
    # Try getting name, profile pic and display name of the user
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
    # Add file info into the message
    # if files:
    #    message_text += format_files_for_message(files)

    # Slack is kinda weird and must have message text even when only file is shared
    if not message_text or message_text.strip() == "":
        return None

    file_yes = False
    if message_text == "[Shared a file]":
        file_yes = True

    # Try uploading stuff into an old thread
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

            # Remember to upload files if they exist!
            # Temp v2
            if file_yes and files: #and message_text.strip() != "" and message_text == "[Shared file]":
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
    # Create a new thread
    else:
        return create_new_thread(user_id, message_text, user_info)

def create_new_thread(user_id, message_text, user_info, files=None):
    """Create new thread in the channel"""
    try:
        # Add file info into the message
        # if files:
        #    message_text += format_files_for_message(files)

        # Message
        response = client.chat_postMessage(
            channel=CHANNEL,
            text=f"*{user_id}*:\n{message_text}",
            username=user_info["display_name"],
            icon_url=user_info["avatar"],
            blocks=get_standard_channel_msg(user_id, message_text)
        )

        # Upload files if they exist!
        if files:
            download_reupload_files(files, CHANNEL, response["ts"])

        # Create an entry in db
        success = thread_manager.create_active_thread(
            user_id,
            CHANNEL,
            response["ts"],
            response["ts"]
        )
        if success:
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
        # Get DM channel of the user
        dm_response = client.conversations_open(users=[user_id])
        dm_channel = dm_response["channel"]["id"]
        
        if files or reply_text == "[Shared file]":
            return None
            
        # Temp v2
        # if not reply_text or reply_text.strip() == "":
        #    if files:
        #        reply_text = "[Shared file]"
        #    else:
        #        reply_text = "[Empty message]"

        # Message them
        response = client.chat_postMessage(
            channel=dm_channel,
            text=reply_text,
            username="Fraud Department",
            icon_emoji=":ban:"
        )

        # Upload files if they are there
        # Temp v2

        return response["ts"] if response.get("ok") else None

    except SlackApiError as err:
        print(f"Error sending reply to user {user_id}: {err}")
        print(f"Error response: {err.response}")
        return None

def extract_user_id(text):
    """Extracts user ID from a mention text <@U000000> or from a direct ID"""
    # 'Deep' mention
    mention_format = re.search(r"<@([A-Z0-9]+)>", text)
    if mention_format:
        return mention_format.group(1)

    # Direct UID
    id_match = re.search(r"\b(U[A-Z0-9]{8,})\b", text)
    if id_match:
        return id_match.group(1)

    return None


@app.command("/fdchat")
def handle_fdchat_cmd(ack, respond, command):
    """Handle conversations started by staff"""
    ack()

    # A little safeguard against unauthorized usage, much easier to do it in one channel than checking
    # Which person ran the command
    if command.get("channel_id") != CHANNEL:
        respond({
            "response_type": "ephemeral",
            "text": f"This command can only be used in one place. If you don't know it, don't even try"
        })
        return

    command_text = command.get("text", "").strip()

    # Validation goes brrr
    if not command_text:
        respond({
            "response_type": "ephemeral",
            "text": "Usage: /fdchat @user your message' or '/fdchat U000000 your message'"
        })
        return

    requester_id = command.get("user_id")

    # Getting the info about request
    parts = command_text.split(" ", 1)
    user_id = parts[0]
    staff_message = expand_macros(parts[1])

    # Enter the nickname pls
    target_user_id = extract_user_id(user_id)
    if not target_user_id:
        respond({
            "response_type": "ephemeral",
            "text": "Provide a valid user ID: U000000 or a mention: @name"
        })
        return

    # Get user info
    user_info = get_user_info(target_user_id)
    if not user_info:
        respond({
            "response_type": "ephemeral",
            "text": f"Couldn't find user info for {target_user_id}"
        })
        return

    # Check if user has an active thread, if so - use it
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

            # Only echo if macros were used
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
                # Store mapping + dispatch staff message event
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

            # Some nice logs for clarity
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
    # Try to create a new thread (Try, not trying. It was standing out a lot, I had to fix it a little)
    try:
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

        # Track the thread
        thread_manager.create_active_thread(
            target_user_id,
            CHANNEL,
            response["ts"],
            response["ts"]
        )
        thread_manager.store_message_mapping(response["ts"], target_user_id, dm_ts, original_sent_text, response["ts"])  # root msg mapping
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

        # Only echo if macros were used
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
    #if message_text and files:
    #    return

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

    # Skip bot stuff
    if message.get("bot_id"):
        return

    # DMs to the bot
    if channel_type == "im":
        handle_dms(user_id, message_text, files, say)
    # Replies in the support channel or !backup in main channel
    elif channel_id == CHANNEL:
        if message_text and message_text.strip() == "!backup":
            handle_backup_command(message, client)
        elif "thread_ts" in message:
            handle_channel_reply(message, client)

def handle_channel_reply(message, client):
    """Handle replies in channel to send them to users"""
    thread_ts = message["thread_ts"]
    reply_text = message["text"]
    files = message.get("files", [])
    fraud_dept_ts = message["ts"]

    # Check for $ai command
    if reply_text and reply_text.startswith("$ai "):
        handle_ai_command(message, client)
        return

    # Check for !backup command
    if reply_text and reply_text.strip() == "!backup":
        handle_backup_command(message, client)
        return

    # Check if it's a direct macro (starts with $)
    is_macro = reply_text and any(reply_text.startswith(macro) for macro in MACROS.keys())
    
    # Allow for notes (private messages between staff) if message isn't started with '!' or isn't a macro
    if not reply_text or (not is_macro and len(reply_text) > 0 and reply_text[0] != '!'):
        return

    # Remove ! prefix if present (for backwards compatibility)
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

def handle_backup_command(message, client):
    """Handle !backup command to start fraud case extraction"""
    try:
        thread_ts = message.get("thread_ts")
        user_id = message.get("user")

        initial_message = "🔄 **Backup Started**\n\nFraudpheus message extraction initiated...\nThis will extract all messages from fraud cases."

        if thread_ts:
            response = client.chat_postMessage(
                channel=CHANNEL,
                thread_ts=thread_ts,
                text=initial_message,
                username="Backup Bot"
            )
        else:
            response = client.chat_postMessage(
                channel=CHANNEL,
                text=initial_message,
                username="Backup Bot"
            )

        def run_backup():
            import subprocess
            import os
            try:
                script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                script_path = os.path.join(script_dir, "slack_to_mattermost_migration.py")

                result = subprocess.run([
                    "python", script_path
                ], capture_output=True, text=True, cwd=script_dir)

                if result.returncode == 0:
                    success_msg = f"✅ **Backup Complete!**\n\nMessage extraction finished successfully.\nCheck the channel for detailed results."
                else:
                    success_msg = f"❌ **Backup Failed**\n\nError: {result.stderr[:500]}"

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

            except Exception as e:
                error_msg = f"❌ **Backup Error**\n\nFailed to run extraction: {str(e)[:500]}"
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

def handle_ai_command(message, client):
    """Handle $ai command for message formalization"""
    try:
        original_text = message["text"][4:].strip()  # Remove "$ai "
        thread_ts = message["thread_ts"]

        if not original_text:
            print("AI command failed: Empty message after $ai")
            client.reactions_add(
                channel=CHANNEL,
                timestamp=message["ts"],
                name="x"
            )
            return

        # Call AI API
        formatted_text = call_ai_api(original_text)

        if not formatted_text:
            print(f"AI command failed: API returned no response for text: '{original_text}'")
            client.reactions_add(
                channel=CHANNEL,
                timestamp=message["ts"],
                name="x"
            )
            return

        # Just show the AI suggestion as a guide (no send buttons)
        client.chat_postMessage(
            channel=CHANNEL,
            thread_ts=thread_ts,
            text=f"🤖 *AI Writing Suggestion:*\n\n*Your text:*\n{original_text}\n\n*AI suggestion:*\n{formatted_text}",
            username="AI Writing Guide",
            icon_emoji=":robot_face:"
        )

        client.reactions_add(
            channel=CHANNEL,
            timestamp=message["ts"],
            name="robot_face"
        )

    except Exception as err:
        print(f"Error in AI command handler: {err}")
        try:
            client.reactions_add(
                channel=CHANNEL,
                timestamp=message["ts"],
                name="x"
            )
        except SlackApiError:
            pass


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


@app.action("ai_mark_resolved")
def handle_ai_mark_resolved(ack, body, client):
    """Handle marking thread as resolved from AI suggestion"""
    ack()
    
    user_id = body["actions"][0]["value"]
    
    try:
        success = thread_manager.complete_thread(user_id)
        if success:
            # Update the message to show it was resolved
            client.chat_update(
                channel=body["channel"]["id"],
                ts=body["message"]["ts"],
                text=f"✅ Thread for <@{user_id}> marked as resolved",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"✅ *Thread Resolved*\n\nUser: <@{user_id}>\nMarked as resolved based on AI analysis."
                        }
                    }
                ]
            )
            print(f"AI-suggested thread for user {user_id} marked as resolved")
        else:
            print(f"Failed to resolve AI-suggested thread for user {user_id}")
    except Exception as err:
        print(f"Error handling AI mark resolved: {err}")

@app.action("ai_keep_open")
def handle_ai_keep_open(ack, body, client):
    """Handle keeping thread open from AI suggestion"""
    ack()
    
    user_id = body["actions"][0]["value"]
    
    try:
        # Update the message to show it was kept open
        client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text=f"📝 Thread for <@{user_id}> kept open",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"📝 *Thread Kept Open*\n\nUser: <@{user_id}>\nAI suggestion overridden - thread remains active."
                    }
                }
            ]
        )
        print(f"AI-suggested thread for user {user_id} kept open")
    except Exception as err:
        print(f"Error handling AI keep open: {err}")

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
            # Going through some cursor stuff, cause of limits, grab 100 per iteration
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

                # Get these messages
                response = client.conversations_replies(**api_args)
                messages = response["messages"]

                # Go through every message, delete em. First as user (Admins can delete other people's messages)
                # If that fails then as a bot
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

                # If there are more messages, grab em
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
        # ID of stuff
        file_id = event["file_id"]
        user_id = event["user_id"]
        # Get that file info
        file_info = client.files_info(file=file_id)
        file_data = file_info["file"]

        # Check if this is a DM
        channels = file_data.get("channels", [])
        groups = file_data.get("groups", [])
        ims = file_data.get("ims", [])

        #
        #if groups and not file_data.get("initial_comment") and file_data.get("comments_count") == 0:
        #    success = send_dm_to_user(user_id, "", files=[file_data])

        # Warning, warning - this is a DM! Also don't process files with messages, they are handled elsewhere
        if ims and not file_data.get("initial_comment") and file_data.get("comments_count") == 0:
            user_info = get_user_info(user_id)
            message_text = "[Shared a file]"
            if user_info:
                success = post_message_to_channel(user_id, message_text, user_info, [file_data])

                if not success:
                    # Try to send an error message to the user, so he at least knows it failed...
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

        # Message to the channel
        elif groups and not file_data.get("initial_comment") and file_data.get("comments_count") == 0:
            # Gosh that took a long time, grabbing the channel shares to get thread_ts, quite creative, eh?
            thread_ts = file_data.get("shares")["private"][CHANNEL][0]["thread_ts"]

            # Find that user and finally message them
            for user in thread_manager.active_cache:
                if thread_manager.active_cache[user]["thread_ts"] == thread_ts:
                    send_dm_to_user(user, "[Shared file]", [file_data])


    except SlackApiError as err:
        logger.error(f"Error handling file_shared event: {err}")




def format_file(files):
    """Format file for a nice view in message"""
    # If there are no files, no need for formatting
    if not files:
        return ""

    # Collect info about files
    file_info = []
    for file in files:
        # Get Type, name, size
        file_type = file.get("mimetype", "unknown")
        file_name = file.get("name", "unknown file")
        file_size = file.get("size", 0)

        # Convert into a nice style
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
        # Try downloading the file
        try:
            # Get that URL to download it
            file_url = file.get("url_private_download") or file.get("url_private")
            if not file_url:
                print(f"Can't really download without any url for file {file.get('name', 'unknown')}")
                continue

            headers = {'Authorization': f"Bearer {os.getenv('SLACK_BOT_TOKEN')}"}
            response = requests.get(file_url, headers=headers)

            # Upload that file!
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

                # Awesome, file works - append it to the list
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
    """Handle deletion of messages by fraud dept members - delete corresponding DM"""
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
    """Handle deletion of messages by users - keep them in fraud dept channel"""
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
    # Start background thread for checking inactive threads
    reminder_thread = threading.Thread(target=check_inactive_threads, daemon=True)
    reminder_thread.start()
    
    # Start background thread for AI resolution analysis
    ai_resolution_thread = threading.Thread(target=check_ai_thread_resolutions, daemon=True)
    ai_resolution_thread.start()
    
    handler = SocketModeHandler(app, os.getenv("SLACK_APP_TOKEN"))
    print("Bot running!")
    print("Background reminder system started")
    print("AI resolution detection system started")
    handler.start()
