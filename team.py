import os
import google.generativeai as genai
from dotenv import load_dotenv

# Load our API key just like before
load_dotenv()
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

# ---------------------------------------------------------
# DEFINE OUR TEAM OF AGENTS
# We create different agents by giving them a "system_instruction".
# This tells the AI what its specific job is.
# ---------------------------------------------------------

# Agent 1: The Researcher
researcher_agent = genai.GenerativeModel(
    model_name='gemini-2.5-flash',
    system_instruction="You are an expert researcher. The user will give you a topic. You must find 3 fascinating, little-known facts about this topic. Output ONLY the facts in a bulleted list. Do not add greetings."
)

# Agent 2: The Writer
writer_agent = genai.GenerativeModel(
    model_name='gemini-2.5-flash',
    system_instruction="You are a professional, creative copywriter. Your job is to take raw bullet points from a researcher and turn them into one single, highly engaging and fun paragraph. Make it sound exciting."
)

def start_workflow(topic: str):
    print(f"\n==============================================")
    print(f"STARTING TEAM WORKFLOW FOR: '{topic}'")
    print(f"==============================================\n")
    
    # Step 1: Pass the topic to the Researcher
    print("🤖 [Agent 1] Researcher is gathering facts...")
    research_result = researcher_agent.generate_content(topic)
    
    print("\n--- RESEARCHER'S ROUGH NOTES ---")
    print(research_result.text)
    print("--------------------------------\n")
    
    # Step 2: Pass the Researcher's notes to the Writer
    print("✍️ [Agent 2] Writer is turning the notes into a story...")
    prompt_for_writer = f"Here are the notes: \n\n{research_result.text}"
    final_article = writer_agent.generate_content(prompt_for_writer)
    
    print("\n🌟 --- FINAL OUTPUT --- 🌟")
    print(final_article.text)
    print("==============================================\n")

if __name__ == "__main__":
    print("Welcome to your custom Multi-Agent Team!")
    while True:
        user_topic = input("Enter a topic for the team to research (or type 'exit'): ")
        if user_topic.lower() == 'exit':
            break
        start_workflow(user_topic)
