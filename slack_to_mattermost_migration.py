#!/usr/bin/env python3

import os
import json
import time
import requests
from datetime import datetime, timezone
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from dotenv import load_dotenv
from pyairtable import Api

load_dotenv()

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
if not SLACK_BOT_TOKEN:
    print("ERROR: SLACK_BOT_TOKEN not found in environment variables")
    exit(1)

client = WebClient(token=SLACK_BOT_TOKEN)
CHANNEL = os.getenv("CHANNEL_ID", "C096P2QHTM5")

airtable_api = Api(os.getenv("AIRTABLE_API_KEY"))
airtable_base = airtable_api.base(os.getenv("AIRTABLE_BASE_ID"))

class FraudpheusExtractor:
    def __init__(self):
        self.client = client
        self.channel = CHANNEL
        self.active_threads_table = airtable_base.table("Active Threads")
        self.completed_threads_table = airtable_base.table("Completed Threads")
        self.processed_cases = 0
        self.start_time = time.time()
        self.status_message_ts = None
        self.cases_data = {
            "export_timestamp": datetime.now(timezone.utc).isoformat(),
            "channel_id": self.channel,
            "fraud_cases": [],
            "users": {},
            "statistics": {}
        }

    def get_user_info(self, user_id):
        if user_id in self.cases_data["users"]:
            return self.cases_data["users"][user_id]

        try:
            response = self.client.users_info(user=user_id)
            user = response["user"]
            user_info = {
                "id": user_id,
                "name": user.get("name", ""),
                "real_name": user.get("real_name", ""),
                "display_name": user.get("profile", {}).get("display_name", ""),
                "email": user.get("profile", {}).get("email", ""),
                "is_bot": user.get("is_bot", False),
                "avatar": user.get("profile", {}).get("image_72", "")
            }
            self.cases_data["users"][user_id] = user_info
            return user_info
        except SlackApiError as e:
            print(f"Error getting user info for {user_id}: {e}")
            fallback_info = {
                "id": user_id,
                "name": "unknown",
                "real_name": "Unknown User",
                "display_name": "Unknown",
                "email": "",
                "is_bot": False,
                "avatar": ""
            }
            self.cases_data["users"][user_id] = fallback_info
            return fallback_info

    def post_status_update(self):
        elapsed = time.time() - self.start_time
        hours = int(elapsed // 3600)
        minutes = int((elapsed % 3600) // 60)

        status_text = f"""🔄 **Fraudpheus Case Export Status**

**Progress:** {self.processed_cases} cases processed
**Runtime:** {hours}h {minutes}m
**Started:** {datetime.fromtimestamp(self.start_time).strftime('%Y-%m-%d %H:%M:%S')}

*Extracting all fraud case data...*"""

        try:
            if self.status_message_ts:
                self.client.chat_update(
                    channel=self.channel,
                    ts=self.status_message_ts,
                    text=status_text
                )
            else:
                response = self.client.chat_postMessage(
                    channel=self.channel,
                    text=status_text,
                    username="Fraudpheus Extractor",
                    icon_emoji=":mag:"
                )
                self.status_message_ts = response["ts"]
        except SlackApiError as e:
            print(f"Error posting status update: {e}")

    def get_all_case_threads(self):
        active_threads = []
        completed_threads = []

        try:
            active_records = self.active_threads_table.all()
            for record in active_records:
                fields = record["fields"]
                if fields.get("thread_ts") and fields.get("user_id"):
                    active_threads.append({
                        "thread_ts": fields.get("thread_ts"),
                        "user_id": fields.get("user_id"),
                        "channel": fields.get("channel"),
                        "status": "active"
                    })

            completed_records = self.completed_threads_table.all()
            for record in completed_records:
                fields = record["fields"]
                if fields.get("thread_ts") and fields.get("user_id"):
                    completed_threads.append({
                        "thread_ts": fields.get("thread_ts"),
                        "user_id": fields.get("user_id"),
                        "channel": fields.get("channel"),
                        "status": "completed"
                    })

        except Exception as e:
            print(f"Error loading from Airtable: {e}")

        all_threads = active_threads + completed_threads
        print(f"Found {len(active_threads)} active and {len(completed_threads)} completed fraud cases")
        return all_threads

    def extract_case_data(self, case_thread):
        try:
            thread_ts = case_thread["thread_ts"]
            user_id = case_thread["user_id"]

            response = self.client.conversations_replies(
                channel=self.channel,
                ts=thread_ts,
                limit=1000
            )

            messages = response.get("messages", [])
            if not messages:
                return None

            self.get_user_info(user_id)

            case_data = {
                "case_id": thread_ts,
                "reported_user_id": user_id,
                "status": case_thread["status"],
                "thread_ts": thread_ts,
                "messages": [],
                "staff_actions": [],
                "files_shared": [],
                "created_at": None,
                "last_activity": None,
                "total_messages": len(messages)
            }

            for i, message in enumerate(messages):
                msg_user_id = message.get("user")

                if msg_user_id:
                    self.get_user_info(msg_user_id)

                message_data = {
                    "ts": message.get("ts"),
                    "user": msg_user_id,
                    "text": message.get("text", ""),
                    "timestamp": datetime.fromtimestamp(float(message.get("ts", 0))).isoformat() if message.get("ts") else None,
                    "is_bot": message.get("bot_id") is not None,
                    "bot_id": message.get("bot_id"),
                    "username": message.get("username"),
                    "files": [],
                    "reactions": message.get("reactions", []),
                    "edited": message.get("edited"),
                    "is_from_reported_user": msg_user_id == user_id and not message.get("bot_id")
                }

                for file_info in message.get("files", []):
                    file_data = {
                        "id": file_info.get("id"),
                        "name": file_info.get("name"),
                        "title": file_info.get("title"),
                        "mimetype": file_info.get("mimetype"),
                        "size": file_info.get("size"),
                        "url_private": file_info.get("url_private"),
                        "url_private_download": file_info.get("url_private_download"),
                        "permalink": file_info.get("permalink"),
                        "filetype": file_info.get("filetype"),
                        "is_image": file_info.get("mimetype", "").startswith("image/") if file_info.get("mimetype") else False
                    }
                    message_data["files"].append(file_data)
                    case_data["files_shared"].append(file_data)

                if message.get("text", "").startswith(("$", "!")):
                    case_data["staff_actions"].append(message_data)

                case_data["messages"].append(message_data)

                if i == 0:
                    case_data["created_at"] = message_data["timestamp"]

                case_data["last_activity"] = message_data["timestamp"]

            return case_data

        except SlackApiError as e:
            print(f"Error extracting case {thread_ts}: {e}")
            return None

    def run_extraction(self):
        print("Starting Fraudpheus case extraction...")

        self.post_status_update()

        all_cases = self.get_all_case_threads()

        if not all_cases:
            print("No fraud cases found")
            return

        for case_thread in all_cases:
            print(f"Processing case {self.processed_cases + 1}/{len(all_cases)}: {case_thread['thread_ts']}")

            case_data = self.extract_case_data(case_thread)
            if case_data:
                self.cases_data["fraud_cases"].append(case_data)

            self.processed_cases += 1

            if self.processed_cases % 5 == 0 or time.time() - getattr(self, '_last_status_update', 0) >= 60:
                self.post_status_update()
                setattr(self, '_last_status_update', time.time())

            time.sleep(1)

        self.cases_data["statistics"] = {
            "total_cases": len(self.cases_data["fraud_cases"]),
            "active_cases": len([c for c in self.cases_data["fraud_cases"] if c["status"] == "active"]),
            "completed_cases": len([c for c in self.cases_data["fraud_cases"] if c["status"] == "completed"]),
            "total_users": len(self.cases_data["users"]),
            "total_files": sum(len(c["files_shared"]) for c in self.cases_data["fraud_cases"]),
            "processing_time_seconds": time.time() - self.start_time
        }

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"fraudpheus_cases_{timestamp}.json"

        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(self.cases_data, f, indent=2, ensure_ascii=False)

        completion_text = f"""✅ **Fraudpheus Case Export Complete!**

**Final Statistics:**
• {self.cases_data['statistics']['total_cases']} fraud cases exported
• {self.cases_data['statistics']['active_cases']} active cases
• {self.cases_data['statistics']['completed_cases']} completed cases
• {self.cases_data['statistics']['total_users']} users involved
• {self.cases_data['statistics']['total_files']} files/images shared
• Processing time: {int(self.cases_data['statistics']['processing_time_seconds'] // 60)} minutes

**Export saved to:** `{filename}`

All fraud case data extracted! 🔍"""

        try:
            self.client.chat_postMessage(
                channel=self.channel,
                text=completion_text,
                username="Fraudpheus Extractor",
                icon_emoji=":white_check_mark:"
            )
        except SlackApiError as e:
            print(f"Error posting completion message: {e}")

        print(f"\n{'='*60}")
        print("FRAUDPHEUS EXTRACTION COMPLETE!")
        print(f"Exported {self.cases_data['statistics']['total_cases']} fraud cases")
        print(f"Active: {self.cases_data['statistics']['active_cases']}, Completed: {self.cases_data['statistics']['completed_cases']}")
        print(f"Files shared: {self.cases_data['statistics']['total_files']}")
        print(f"Data saved to: {filename}")
        print(f"{'='*60}")

def main():
    try:
        extractor = FraudpheusExtractor()
        extractor.run_extraction()
    except KeyboardInterrupt:
        print("\nExtraction interrupted by user")
    except Exception as e:
        print(f"Extraction failed with error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()