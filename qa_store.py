import json, os

QA_FILE = "qa_store.json"

def load_qa():
    if os.path.exists(QA_FILE):
        with open(QA_FILE, "r") as f:
            return json.load(f)
    return {}

def save_qa(qa_dict):
    with open(QA_FILE, "w") as f:
        json.dump(qa_dict, f, indent=2)

def add_qa(question: str, answer: str):
    qa = load_qa()
    qa[question.lower().strip()] = answer
    save_qa(qa)
    print(f"[QA] Stored: Q='{question}' A='{answer}'")

def find_answer(spoken_text: str) -> str | None:
    qa = load_qa()
    spoken = spoken_text.lower().strip()
    if spoken in qa:
        return qa[spoken]
    for q, a in qa.items():
        if q in spoken or spoken in q:
            return a
    return None
