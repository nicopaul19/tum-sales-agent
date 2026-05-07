#!/usr/bin/env python3
"""
Requirements Analyzer (AI GTM Expert)

Evaluates Tally applications in the Notion Requirements DB and uses OpenAI 
to propose AI/ML solutions, identify potential blockers, and suggest follow-up questions.
"""

import os
import sys
import json
import requests
from dotenv import load_dotenv
from pathlib import Path
from openai import OpenAI
from typing import Optional, Dict

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(ROOT_DIR / ".env")
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
REQUIREMENTS_DB_ID = os.getenv("NOTION_DB_REQUIREMENTS_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}
API = "https://api.notion.com/v1"

client = OpenAI(api_key=OPENAI_API_KEY)


def get_text(page: dict, prop: str) -> str:
    p = page["properties"].get(prop, {})
    if p.get("type") == "rich_text":
        return "".join(rt["plain_text"] for rt in p.get("rich_text", []))
    if p.get("type") == "title":
        return "".join(rt["plain_text"] for rt in p.get("title", []))
    return ""


def get_select(page: dict, prop: str) -> str:
    p = page["properties"].get(prop, {})
    sel = p.get("select")
    return sel["name"] if sel else ""


def query_db_all(db_id: str) -> list:
    results = []
    payload = {"page_size": 100}
    has_more = True
    while has_more:
        resp = requests.post(f"{API}/databases/{db_id}/query", headers=HEADERS, json=payload)
        if resp.status_code != 200:
            print(f"  ERROR querying DB {db_id}: {resp.status_code}")
            break
        data = resp.json()
        results.extend(data.get("results", []))
        has_more = data.get("has_more", False)
        if has_more:
            payload["start_cursor"] = data["next_cursor"]
    return results


def update_page(page_id: str, new_solution_text: str, new_remarks_text: str):
    properties = {}
    if new_solution_text:
        properties["Sugg. AI Solution"] = {"rich_text": [{"text": {"content": new_solution_text[:2000]}}]}
    if new_remarks_text:
        properties["Remarks & Questions"] = {"rich_text": [{"text": {"content": new_remarks_text[:2000]}}]}

    resp = requests.patch(
        f"{API}/pages/{page_id}",
        headers=HEADERS,
        json={"properties": properties},
    )
    if resp.status_code != 200:
        print(f"  ERROR updating page {page_id}: {resp.status_code}")
        return False
    return True


def analyze_application(app_data: Dict) -> Dict[str, str]:
    prompt = f"""
You are an expert AI Go-to-Market (GTM) and Forward Deployed Engineer evaluating an NGO's project application.
Your goal is to analyze their pain points, suggest a highly precise AI/ML solution to their problem, highlight technical blockers, and formulate practical questions to ask them.

IMPORTANT CONTEXT:
1. Be extremely concise and avoid fluff. Get straight to the point.
2. Be super precise in what we could build for them technically.
3. For potential problems/blockers, strictly focus on the technical problems/blockers for our engineering teams (e.g., data quality, scalability, API limits, integration complexity, privacy). NEVER say "Potential blockers include limited technical expertise within the student club" or similar. Assume the engineering team is highly capable but constrained by normal student timelines.

Candidate Application Data:
Organization Name: {app_data.get('org_name')}
Problem to Solve: {app_data.get('problem')}
Current Effort: {app_data.get('effort')}
Usage Frequency: {app_data.get('frequency')}
Additional Benefits: {app_data.get('benefits')}
Data Readiness: {app_data.get('data_nature')} | {app_data.get('data_avail')} | {app_data.get('data_lang')}
Tech Stack: {app_data.get('tech_stack')}

Your task is to provide two independent blocks of text (JSON format):
1. "solution": The precise, technical AI/ML solution that we could build to solve their problem, including what the potential technical blockers for our engineering team could be. Prefix this string with "[AI GTM Expert]: "
2. "remarks": Questions our team should ask them to clarify the technical requirements, constraints, or data availability. Prefix this string with "[AI GTM Expert Questions]: "

Return ONLY valid unstructured JSON with keys "solution" and "remarks". Do not wrap it in markdown. Do not include markdown json ticks. Just valid raw JSON.
"""
    try:
        response = client.chat.completions.create(
            model="o3-mini",
            response_format={ "type": "json_object" },
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        content = response.choices[0].message.content
        if content.startswith("```json"):
            content = content[7:-3]
        elif content.startswith("```"):
            content = content[3:-3]
        return json.loads(content)
    except Exception as e:
        print(f"Error calling OpenAI API: {e}")
        return {}


def process():
    print("=" * 60)
    print("AI GTM Expert - Requirements Analyzer")
    print("=" * 60)
    
    if not OPENAI_API_KEY:
        print("OPENAI_API_KEY missing.")
        sys.exit(1)
        
    entries = query_db_all(REQUIREMENTS_DB_ID)
    print(f"Found {len(entries)} total applications.")
    
    analyzed_count = 0
    
    for entry in entries:
        org_name = get_text(entry, "1.1 Organization Name")
        existing_solution = get_text(entry, "Sugg. AI Solution")
        existing_remarks = get_text(entry, "Remarks & Questions")
        
        # Skip if already analyzed by the agent
        if "[AI GTM Expert]" in existing_solution or "[AI Expert]" in existing_solution:
            print(f"Skipping '{org_name}' - already analyzed.")
            continue
            
        print(f"\nAnalyzing '{org_name}'...")
        
        app_data = {
            "org_name": org_name,
            "problem": get_text(entry, "1.2 The Problem you wish to be solved"),
            "effort": get_text(entry, "1.3 Current effort"),
            "frequency": get_text(entry, "1.4 Usage frequency"),
            "benefits": get_text(entry, "1.5 Additional benefits"),
            "data_nature": get_text(entry, "2.1 Nature of Data"),
            "data_avail": get_select(entry, "2.2 Data Availability"),
            "data_lang": get_text(entry, "2.4 Data Language"),
            "tech_stack": get_text(entry, "3.3 Current technical ecosystem"),
        }
        
        analysis = analyze_application(app_data)
        
        if analysis:
            new_sol = analysis.get("solution", "")
            new_rem = analysis.get("remarks", "")
            
            # Append if existing content is present
            if existing_solution.strip():
                final_sol = existing_solution.strip() + "\n\n" + new_sol
            else:
                final_sol = new_sol
                
            if existing_remarks.strip():
                final_rem = existing_remarks.strip() + "\n\n" + new_rem
            else:
                final_rem = new_rem
                
            success = update_page(entry["id"], final_sol, final_rem)
            if success:
                print(f"  Successfully updated analysis for {org_name}")
                analyzed_count += 1
            else:
                print(f"  Failed to update Notion for {org_name}")
    
    print("\n" + "=" * 60)
    print(f"Finished. Analzyed {analyzed_count} new applications.")
    print("=" * 60)

if __name__ == "__main__":
    process()
