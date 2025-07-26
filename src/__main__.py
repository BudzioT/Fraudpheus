import os
import re
import time

import requests
from dotenv import load_dotenv

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from pyairtable import Api

from src.thread_manager import ThreadManager

load_dotenv()

# Slack setup
app = App(token=os.getenv("SLACK_BOT_TOKEN"))
client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))
user_client = WebClient(token=os.getenv("SLACK_USER_TOKEN"))

CHANNEL = os.getenv("CHANNEL_ID")

# Airtable setup
airtable_api = Api(os.getenv("AIRTABLE_API_KEY"))
airtable_base = airtable_api.base(os.getenv("AIRTABLE_BASE_ID"))

# Thread stuff
thread_manager = ThreadManager(airtable_base)


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
                    "value": user_id
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

        # Temp v2
        # if not reply_text or reply_text.strip() == "":
        #    if files:
        #        reply_text = "[Shared file]"
        #    else:
        #        reply_text = "[Empty message]"

        # Message them
        client.chat_postMessage(
            channel=dm_channel,
            text=reply_text,
            username="Fraudpheus",
            icon_emoji=":orph:"
        )

        # Upload files if they are there
        # Temp v2
        if files and reply_text == "[Shared file]":
            download_reupload_files(files, dm_channel)

        return True

    except SlackApiError as err:
        print(f"Error sending reply to user {user_id}: {err}")
        print(f"Error response: {err.response}")
        return False

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
    staff_message = parts[1]

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
            client.chat_postMessage(
                channel=CHANNEL,
                thread_ts=thread_info["thread_ts"],
                text=f"*<@{requester_id}> continued:*\n{staff_message}"
            )
            success = send_dm_to_user(target_user_id, staff_message)
            thread_manager.update_thread_activity(target_user_id)

            # Some nice logs for clarity
            if success:
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
        success = send_dm_to_user(target_user_id, staff_message)
        if not success:
            respond({
                "response_type": "ephemeral",
                "text": f"Failed to send DM to {target_user_id}"
            })
            return

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

    #print(f"Message received - Channel: {channel_id}, Type: {channel_type}")

    # Skip bot stuff
    if message.get("bot_id"):
        return

    # DMs to the bot
    if channel_type == "im":
        handle_dms(user_id, message_text, files, say)
    # Replies in the support channel
    elif channel_id == CHANNEL and "thread_ts" in message:
        handle_channel_reply(message, client)

def handle_channel_reply(message, client):
    """Handle replies in channel to send them to users"""
    thread_ts = message["thread_ts"]
    reply_text = message["text"]
    files = message.get("files", [])

    # Allow for notes (private messages between staff) by starting message with '!'
    if not reply_text or (len(reply_text) > 0 and reply_text[0] == '!'):
        return

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
        success = send_dm_to_user(target_user_id, reply_text, files)

        # Some logging
        if success:
            thread_manager.update_thread_activity(target_user_id)
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
                            username="Fraudpheus",
                            icon_emoji=":orph:",
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
    """Please just don't spam errors that I have unhandled request"""
    pass

@app.error
def error_handler(error, body, logger):
    logger.exception(f"Error: {error}")
    logger.info(f"Request body: {body}")

if __name__ == "__main__":
    handler = SocketModeHandler(app, os.getenv("SLACK_APP_TOKEN"))
    print("Bot running!")
    handler.start()
