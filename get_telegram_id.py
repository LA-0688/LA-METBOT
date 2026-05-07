import os
import requests
from dotenv import load_dotenv
import time

load_dotenv()
token = os.environ.get("TELEGRAM_BOT_TOKEN")

if not token or token == "your_telegram_bot_token_here":
    print("Error: Please set TELEGRAM_BOT_TOKEN in your .env file first.")
    exit()

print("Checking for messages sent to your bot...")
print("If you haven't yet, open Telegram, find your bot, and send it a message saying 'Hello'!")

while True:
    try:
        response = requests.get(f"https://api.telegram.org/bot{token}/getUpdates").json()
        if response.get("ok") and len(response["result"]) > 0:
            chat_id = response["result"][0]["message"]["chat"]["id"]
            print(f"\n✅ SUCCESS! We found your Chat ID: {chat_id}")
            print(f"👉 Please add this to your .env file as: TELEGRAM_CHAT_ID={chat_id}")
            break
        else:
            print("Waiting for you to send a message to the bot... (checking again in 5 seconds)")
            time.sleep(5)
    except Exception as e:
        print(f"Error checking API: {e}")
        break
