import json, os, re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QA_FILE  = f"{BASE_DIR}/qa_pairs.json"

def load_qa():
    """App format: [{"id":1,"q":"...","a":"..."}]"""
    if os.path.exists(QA_FILE):
        try:
            with open(QA_FILE) as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception as e:
            print(f"[QA] load error: {e}")
    return []

def save_qa(qa_list):
    with open(QA_FILE, "w") as f:
        json.dump(qa_list, f, indent=4)

def _norm(s: str) -> str:
    """lowercase, strip punctuation and a leading wake word"""
    s = (s or "").lower().strip()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"^(hey |hi |ok |okay )?ben\s+", "", s)
    return re.sub(r"\s+", " ", s).strip()

def find_answer(spoken_text: str):
    spoken = _norm(spoken_text)
    if not spoken:
        return None
    qa = load_qa()
    # exact match first
    for item in qa:
        q = _norm(item.get("q", ""))
        if q and q == spoken:
            print(f"[QA] Exact match: '{item.get('q')}'")
            return item.get("a")
    # then partial
    for item in qa:
        q = _norm(item.get("q", ""))
        if q and (q in spoken or spoken in q):
            print(f"[QA] Partial match: '{item.get('q')}'")
            return item.get("a")
    return None
