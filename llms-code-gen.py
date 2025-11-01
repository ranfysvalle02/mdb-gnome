import os
import sys
from dotenv import load_dotenv
from langchain_openai import AzureChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.pydantic_v1 import BaseModel, Field
from typing import Literal

# --- Configuration ---

# Load environment variables from .env file
load_dotenv()

# Get Azure OpenAI credentials from environment
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_DEPLOYMENT_NAME = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME")

# --- Constants ---
IGNORED_DIRS = {
    "__pycache__", "temp_exports",".git", ".venv", "venv", "node_modules", "build", "dist", ".vscode",
}
IGNORED_FILES = {
    ".DS_Store", "package-lock.json", "yarn.lock", "llms-code.txt", ".env"
}
# Max characters to read from a file for analysis (to avoid context overload)
MAX_FILE_CHARS = 8000 

# --- Pydantic Model for Structured Output ---

class FileAnalysis(BaseModel):
    """Structured analysis of a single code file."""
    summary: str = Field(description="A concise, 1-2 sentence summary of the file's primary purpose and functionality.")
    category: Literal[
        "Configuration", 
        "Data Model", 
        "Application Logic", 
        "Utility", 
        "UI/View", 
        "Testing", 
        "Documentation", 
        "Infrastructure", 
        "Other"
    ] = Field(description="The single best category for this file.")

# --- Helper Functions ---

def should_ignore(path, is_dir):
    """Check if a directory or file should be ignored."""
    name = os.path.basename(path)
    if is_dir:
        return name in IGNORED_DIRS
    return name in IGNORED_FILES or name.startswith('.')

def generate_tree(start_path):
    """Generates a text-based directory tree structure."""
    tree_lines = []
    for root, dirs, files in os.walk(start_path, topdown=True):
        # Filter ignored directories
        dirs[:] = [d for d in dirs if not should_ignore(os.path.join(root, d), is_dir=True)]
        
        level = root.replace(start_path, '').count(os.sep)
        indent = ' ' * 4 * (level)
        
        # Don't add the root dir itself, just its contents
        if root != start_path:
            tree_lines.append(f"{indent}{os.path.basename(root)}/")
        
        sub_indent = ' ' * 4 * (level + 1)
        for f in files:
            if not should_ignore(os.path.join(root, f), is_dir=False):
                tree_lines.append(f"{sub_indent}{f}")
                
    return "\n".join(tree_lines)

# --- Main Application ---

def main():
    """
    Runs the full "analyze-then-synthesize" process.
    Pass 1 (Map): Analyzes each file individually.
    Pass 2 (Reduce): Synthesizes a final report.
    """
    
    # --- 1. Setup and Validation ---
    print("Initializing Code Summarizer...")
    if not all([AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_OPENAI_DEPLOYMENT_NAME]):
        print("Error: Missing Azure OpenAI environment variables.")
        print("Please set AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, and AZURE_OPENAI_DEPLOYMENT_NAME in a .env file.")
        return

    # Check for target directory
    start_path = sys.argv[1] if len(sys.argv) > 1 else "./"
    if not os.path.isdir(start_path):
        print(f"Error: Directory not found: {start_path}")
        print("Usage: python code_summarizer.py [optional_path_to_directory]")
        return
        
    start_path = os.path.abspath(start_path)
    project_name = os.path.basename(start_path)
    print(f"Target project: {project_name} (at {start_path})")

    # --- 2. Initialize LLM and Chains ---
    
    # Shared LLM instance
    try:
        llm = AzureChatOpenAI(
            azure_endpoint=AZURE_OPENAI_ENDPOINT,
            api_key=AZURE_OPENAI_API_KEY,
            api_version="2024-02-01", # Use a recent, stable API version
            azure_deployment=AZURE_OPENAI_DEPLOYMENT_NAME,
            temperature=0,
            max_retries=2,
        )
    except Exception as e:
        print(f"Error initializing AzureChatOpenAI: {e}")
        return

    # "Map" Chain: Analyzes a single file
    analysis_parser = JsonOutputParser(pydantic_object=FileAnalysis)
    analysis_prompt = ChatPromptTemplate.from_messages([
        ("system", 
         "You are an expert code analyst. Your goal is to analyze a single file and provide a concise summary and category. "
         "Respond *only* with the JSON object requested.\n{format_instructions}"),
        ("human", 
         "Analyze the following file: `path/to/{file_path}`\n\n"
         "File Content (truncated):\n```\n{file_content}\n```")
    ]).partial(format_instructions=analysis_parser.get_format_instructions())
    
    analysis_chain = analysis_prompt | llm | analysis_parser

    # "Reduce" Chain: Synthesizes the final report
    synthesis_prompt = ChatPromptTemplate.from_messages([
        ("system",
         "You are a principal engineer creating a high-level summary of a codebase, in the style of an `llms-code.txt` file. "
         "Your purpose is to give a new developer a quick overview of the entire project. "
         "You will be given the project's file tree and a list of summaries for individual files. "
         "Synthesize this information into a single, cohesive markdown document.\n\n"
         "Structure your response:\n"
         "1.  **Project Overview**: Start with a high-level paragraph describing the project's purpose.\n"
         "2.  **Key Components**: Create sections based on the file categories (e.g., 'Application Logic', 'Data Models', 'Configuration'). "
         "Under each section, list the relevant files and their summaries in a readable way (e.g., bullet points).\n"
         "3.  **How it Works**: If possible, add a brief section explaining how the components interact (e.g., '`main.py` loads `config.json` and uses `utils.py`...').\n"
         "4.  **Developer Notes**: Add a final section with any quick notes for a new developer (e.g., 'Start by looking at `main.py`...')."),
        ("human",
         "Please generate the `llms-code.txt` for the project: `{project_name}`\n\n"
         "--- FILE TREE ---\n{tree}\n\n"
         "--- FILE SUMMARIES ---\n{summaries}")
    ])
    
    synthesizer_chain = synthesis_prompt | llm

    # --- 3. "Map" Step: Analyze all files ---
    print("\n--- Pass 1: Analyzing Files (Map) ---")
    all_analyses = []
    
    # First, generate the file tree (as suggested by the user)
    print("Generating file tree...")
    tree_structure = generate_tree(start_path)
    
    print("Walking directory and analyzing files...")
    for root, dirs, files in os.walk(start_path, topdown=True):
        # Filter ignored directories
        dirs[:] = [d for d in dirs if not should_ignore(os.path.join(root, d), is_dir=True)]
        
        for file in files:
            file_path = os.path.join(root, file)
            if should_ignore(file_path, is_dir=False):
                continue

            # Get relative path for cleaner display
            rel_path = os.path.relpath(file_path, start_path)
            
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read(MAX_FILE_CHARS)
                
                if not content.strip():
                    print(f"  -> Skipping empty file: {rel_path}")
                    continue

                print(f"  Analyzing: {rel_path}...")
                
                # Run the "Map" chain
                analysis = analysis_chain.invoke({
                    "file_path": rel_path.replace(os.sep, '/'),
                    "file_content": content
                })
                
                all_analyses.append({
                    "path": rel_path,
                    "summary": analysis['summary'],
                    "category": analysis['category']
                })

            except UnicodeDecodeError:
                print(f"  -> Skipping non-UTF-8 file: {rel_path}")
            except Exception as e:
                print(f"  -> Error analyzing file {rel_path}: {e}")
    
    print("\n--- Pass 2: Synthesizing Report (Reduce) ---")

    # --- 4. "Reduce" Step: Synthesize ---

    # Format the summaries for the prompt
    summary_lines = []
    # Sort analyses to group by category, making it easier for the LLM
    all_analyses.sort(key=lambda x: x['category']) 
    for analysis in all_analyses:
        # Re-normalize path for consistency
        path = analysis['path'].replace(os.sep, '/')
        summary_lines.append(
            f"- [{analysis['category']}] {path}: {analysis['summary']}"
        )
    summaries_text = "\n".join(summary_lines)
    
    if not summaries_text:
        print("Error: No files were successfully analyzed. Cannot generate report.")
        return

    try:
        # Run the "Reduce" chain
        llms_code_content = synthesizer_chain.invoke({
            "project_name": project_name,
            "tree": f"{project_name}/\n{tree_structure}",
            "summaries": summaries_text
        })

        # --- 5. Save the output ---
        output_filename = "llms-code.txt"
        # Save the file in the directory that was scanned
        output_path = os.path.join(start_path, output_filename) 
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(llms_code_content.content) # Get string content from AIMessage

        print(f"\n✅ Success! Generated `{output_filename}` in {start_path}")

    except Exception as e:
        print(f"\nError during final synthesis: {e}")
        print("Failed to generate `llms-code.txt`.")
        return

if __name__ == "__main__":
    main()
