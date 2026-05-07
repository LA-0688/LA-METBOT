import os
import google.generativeai as genai
from dotenv import load_dotenv

# 1. Load environment variables
load_dotenv()

# Configure the API key from the environment
api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    print("Error: GEMINI_API_KEY not found. Please set it in your .env file.")
    exit(1)

genai.configure(api_key=api_key)

# 2. Define our Tools (Functions)
# These are normal Python functions, but we must write clear docstrings
# because Gemini reads the docstrings to know WHEN and HOW to use the tool.

def add_numbers(a: float, b: float) -> float:
    """Adds two numbers together."""
    print(f"  [Tool Execution] Agent is adding {a} and {b}...")
    return a + b

def multiply_numbers(a: float, b: float) -> float:
    """Multiplies two numbers together."""
    print(f"  [Tool Execution] Agent is multiplying {a} and {b}...")
    return a * b

def get_current_weather(location: str) -> str:
    """Gets the current weather for a given location."""
    print(f"  [Tool Execution] Agent is fetching weather for {location}...")
    # In a real app, this would call a real weather API like OpenWeatherMap.
    # We are mocking (faking) it for this tutorial.
    location_lower = location.lower()
    if "tokyo" in location_lower:
        return "Sunny and 22°C"
    elif "london" in location_lower:
        return "Rainy and 12°C"
    else:
        return "Clear skies and 20°C"

# 3. Initialize the Model and give it our tools
# We pass the functions directly in a list.
model = genai.GenerativeModel(
    model_name='gemini-2.5-flash',
    tools=[add_numbers, multiply_numbers, get_current_weather]
)

def start_agent_session():
    print("Welcome to your first Gemini Agent!")
    print("This agent has tools to add/multiply numbers and check the weather.")
    print("Type 'exit' to quit.\n")
    
    # 4. Start the chat session
    # enable_automatic_function_calling=True is the magic here.
    # It tells the Gemini SDK to automatically handle the loop:
    # Model asks for tool -> SDK runs python function -> SDK sends result back to model -> Model gives final answer.
    chat = model.start_chat(enable_automatic_function_calling=True)
    
    while True:
        user_input = input("\nYou: ")
        if user_input.lower() in ['exit', 'quit']:
            break
            
        print("-" * 40)
        try:
            # Send the message to the agent
            response = chat.send_message(user_input)
            print("-" * 40)
            print(f"Agent: {response.text}")
        except Exception as e:
            print(f"An error occurred: {e}")

if __name__ == "__main__":
    start_agent_session()
