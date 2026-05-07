import os
import wikipedia
import google.generativeai as genai
from dotenv import load_dotenv
from fpdf import FPDF

load_dotenv()
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

# ---------------------------------------------------------
# PDF GENERATOR
# ---------------------------------------------------------
def save_article_to_pdf(topic: str, article_text: str):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    
    # Add a bold title
    pdf.set_font("Helvetica", style="B", size=16)
    pdf.cell(0, 10, txt=f"Research Article: {topic.title()}", ln=True, align="C")
    pdf.ln(10) # Add some space
    
    # Add the article text
    pdf.set_font("Helvetica", size=12)
    # We replace special smart quotes with normal ones so the PDF library doesn't crash
    clean_text = article_text.replace('“', '"').replace('”', '"').replace('’', "'").replace('—', '-')
    
    # Write the text to the PDF
    pdf.multi_cell(0, 7, txt=clean_text)
    
    # Save the file
    filename = f"{topic.replace(' ', '_').lower()}.pdf"
    pdf.output(filename)
    return filename

# ---------------------------------------------------------
# 1. DEFINE OUR TOOLS
# ---------------------------------------------------------
def search_wikipedia(query: str) -> str:
    """Searches Wikipedia for the given query and returns a summary. Use this to find real-world factual information."""
    print(f"  🔍 [Tool Execution] Searching Wikipedia for: '{query}'...")
    try:
        return wikipedia.summary(query, sentences=3)
    except wikipedia.exceptions.DisambiguationError as e:
        return f"Query is too broad. Possible options: {e.options[:5]}"
    except wikipedia.exceptions.PageError:
        return "No Wikipedia page found for that query."
    except Exception as e:
        return f"An error occurred: {e}"


# ---------------------------------------------------------
# 2. CREATE THE AGENTS
# ---------------------------------------------------------

researcher_agent = genai.GenerativeModel(
    model_name='gemini-2.5-flash',
    tools=[search_wikipedia],
    system_instruction="You are an expert researcher. Use your Wikipedia search tool to find 3 accurate, real-world facts about the user's topic. ONLY output the facts in a bulleted list based on what the tool returns."
)

writer_agent = genai.GenerativeModel(
    model_name='gemini-2.5-flash',
    system_instruction="You are a brilliant copywriter. Take the researcher's bullet points and write a fun, engaging 2-paragraph article about it. Make it sound like an exciting magazine article. Do not use emojis."
)

def run_smart_team(topic):
    print(f"\n==============================================")
    print(f"STARTING SMART TEAM WORKFLOW FOR: '{topic}'")
    print(f"==============================================\n")
    
    print("🤖 [Agent 1] Researcher is firing up its search tools...")
    research_chat = researcher_agent.start_chat(enable_automatic_function_calling=True)
    research_result = research_chat.send_message(f"Please research: {topic}")
    
    print("\n--- RESEARCHER'S VERIFIED FINDINGS ---")
    print(research_result.text)
    print("--------------------------------------\n")
    
    print("✍️ [Agent 2] Writer is crafting the final article...")
    final_article = writer_agent.generate_content(f"Here is the verified research: {research_result.text}")
    
    print("\n🌟 --- FINAL MAGAZINE ARTICLE --- 🌟")
    print(final_article.text)
    
    # Save the output to a PDF
    print("\n📄 Saving final article to PDF...")
    pdf_filename = save_article_to_pdf(topic, final_article.text)
    print(f"✅ Successfully saved to: {pdf_filename}")
    print("==============================================\n")

if __name__ == "__main__":
    print("Welcome to your Smart Multi-Agent Team (with PDF Export)!")
    while True:
        topic = input("\nEnter a topic to research (e.g., 'Capybara', 'Quantum Computing') or type 'exit': ")
        if topic.lower() in ['exit', 'quit']:
            break
        run_smart_team(topic)
