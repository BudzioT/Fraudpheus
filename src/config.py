import os
from dotenv import load_dotenv
from slack_bolt import App
from slack_sdk import WebClient
from pyairtable import Api

load_dotenv()

app = App(token=os.getenv("SLACK_BOT_TOKEN"))
client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))
user_client = WebClient(token=os.getenv("SLACK_USER_TOKEN"))

CHANNEL = os.getenv("CHANNEL_ID")

airtable_api = Api(os.getenv("AIRTABLE_API_KEY"))
airtable_base = airtable_api.base(os.getenv("AIRTABLE_BASE_ID"))
