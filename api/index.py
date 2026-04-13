from flask import Flask, request, jsonify, render_template
from vercel import Vercel
import pdfplumber
import os
import uuid
import requests
import re
import json
import hashlib
from datetime import datetime, timedelta

app = Flask(__name__)
app.wsgi_app = Vercel(app)

UPLOAD_FOLDER = "/tmp/uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

DATA_DIR = "/tmp/data"
os.makedirs(DATA_DIR, exist_ok=True)
USERS_FILE = os.path.join(DATA_DIR, "users.json")
CODES_FILE = os.path.join(DATA_DIR, "codes.json")

def load_json(filepath, default):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r') as f:
                return json.load(f)
        except: pass
    return default

def save_json(filepath, data):
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2)

users_db = load_json(USERS_FILE, {})
codes_db = load_json(CODES_FILE, {"codes": [], "used": []})

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat")
default_model = os.environ.get("OLLAMA_MODEL", "llama3.2:latest")
uncensored_model = os.environ.get("UNCENSORED_MODEL", "dolphin-llama3")
code_model = os.environ.get("CODE_MODEL", "codellama:7b")

PLANS = {
    "normal": {"name": "Normal", "price": 0, "pdf_limit": 10, "features": ["pdf_chat"]},
    "freemium": {"name": "Freemium", "price": 20, "pdf_limit": float('inf'), "days_active": 10, "features": ["pdf_chat", "pdf_unlimited"]},
    "premium": {"name": "Premium", "price": 150, "features": ["pdf_chat", "pdf_unlimited", "free_chat", "uncensored_model"]},
    "fultra": {"name": "Fultra", "price": 250, "features": ["pdf_chat", "pdf_unlimited", "free_chat", "uncensored_model", "code_ide"]}
}

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def generate_codes(count=1):
    import random
    new_codes = []
    for _ in range(count):
        numbers = ''.join([str(random.randint(0, 9)) for _ in range(5)])
        letters = ''.join([random.choice('ABCDEFGHJKLMNPQRSTUVWXYZ') for _ in range(3)])
        code = numbers
        for letter in letters:
            pos = random.randint(0, len(code))
            code = code[:pos] + letter + code[pos:]
        new_codes.append(code)
    return new_codes

def verify_code(code):
    global codes_db
    code = code.upper().strip()
    if code in codes_db["used"]:
        return None, "Codigo ya usado"
    if code in codes_db["codes"]:
        codes_db["used"].append(code)
        codes_db["codes"].remove(code)
        save_json(CODES_FILE, codes_db)
        return True, "Valido"
    return None, "Codigo invalido"

pdf_store = {}
chat_histories = {}

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/login-page")
def login_page():
    return render_template("login.html")

@app.route("/redeem")
def redeem_page():
    return render_template("redeem.html")

# Auth
@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.json
    username = data.get("username", "").strip().lower()
    password = data.get("password", "")
    
    if not username or not password:
        return jsonify({"error": "Completa todos los campos"}), 400
    
    if username in users_db:
        if users_db[username]["password"] == hash_password(password):
            if users_db[username].get("banned"):
                return jsonify({"error": "Usuario baneado"}), 403
            return jsonify({"success": True, "username": username, "plan": users_db[username].get("plan", "normal"), "features": users_db[username].get("features", PLANS["normal"]["features"])})
    return jsonify({"error": "Credenciales invalidas"}), 401

@app.route("/api/register", methods=["POST"])
def api_register():
    data = request.json
    username = data.get("username", "").strip().lower()
    password = data.get("password", "")
    
    if not username or not password:
        return jsonify({"error": "Completa todos los campos"}), 400
    if len(username) < 3 or len(password) < 4:
        return jsonify({"error": "Minimo 3 caracteres usuario, 4 contrasena"}), 400
    if username in users_db:
        return jsonify({"error": "Usuario ya existe"}), 400
    
    users_db[username] = {"password": hash_password(password), "plan": "normal", "features": PLANS["normal"]["features"], "created": datetime.now().isoformat(), "pdf_count": 0, "banned": False}
    save_json(USERS_FILE, users_db)
    return jsonify({"success": True, "username": username, "plan": "normal", "features": PLANS["normal"]["features"]})

@app.route("/api/redeem", methods=["POST"])
def api_redeem():
    data = request.json
    code = data.get("code", "").strip()
    username = data.get("username", "")
    
    if not username or username not in users_db:
        return jsonify({"error": "No has iniciado sesion"}), 401
    
    valid, msg = verify_code(code)
    if not valid:
        return jsonify({"error": msg}), 400
    
    users_db[username]["plan"] = "fultra"
    users_db[username]["features"] = PLANS["fultra"]["features"]
    save_json(USERS_FILE, users_db)
    return jsonify({"success": True, "message": "Fultra activado!", "plan": "fultra", "features": PLANS["fultra"]["features"]})

@app.route("/api/user")
def api_user():
    username = request.args.get("username", "")
    if username and username in users_db:
        return jsonify({"username": username, "plan": users_db[username].get("plan", "normal"), "features": users_db[username].get("features", []), "banned": users_db[username].get("banned", False)})
    return jsonify({"username": None})

@app.route("/api/plans")
def api_plans():
    return jsonify(PLANS)

# Upload
@app.route("/api/upload", methods=["POST"])
def upload_pdf():
    username = request.form.get("username", "")
    if not username or username not in users_db:
        return jsonify({"error": "No has iniciado sesion"}), 401
    user = users_db[username]
    if user.get("banned"):
        return jsonify({"error": "Usuario baneado"}), 403
    if "pdf_unlimited" not in user.get("features", []) and user.get("pdf_count", 0) >= 10:
        return jsonify({"error": "Limite de 10 PDFs alcanzado"}), 400
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    file = request.files["file"]
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Solo PDF"}), 400
    
    file_id = str(uuid.uuid4())
    file_path = os.path.join(UPLOAD_FOLDER, f"{file_id}.pdf")
    file.save(file_path)
    
    try:
        pages = []
        with pdfplumber.open(file_path) as pdf:
            for i, page in enumerate(pdf.pages, 1):
                page_text = page.extract_text()
                if page_text:
                    pages.append({"page": i, "text": page_text})
        full_text = "\n".join([p["text"] for p in pages])
        
        pdf_store[file_id] = {"filename": file.filename, "text": full_text, "pages": pages, "username": username}
        users_db[username]["pdf_count"] = users_db[username].get("pdf_count", 0) + 1
        save_json(USERS_FILE, users_db)
        return jsonify({"success": True, "fileId": file_id, "filename": file.filename, "pageCount": len(pages)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/clear-session-pdfs", methods=["POST"])
def clear_session_pdfs():
    username = request.json.get("username", "")
    for fid in [f for f, d in pdf_store.items() if d.get("username") == username]:
        del pdf_store[fid]
    if username in users_db:
        users_db[username]["pdf_count"] = 0
        save_json(USERS_FILE, users_db)
    return jsonify({"deleted": True})

@app.route("/api/pdfs")
def list_pdfs():
    username = request.args.get("username", "")
    if not username or username not in users_db:
        return jsonify([])
    return jsonify([{"fileId": f, "filename": pdf_store[f]["filename"], "pageCount": len(pdf_store[f]["pages"])} for f in pdf_store if pdf_store[f].get("username") == username])

@app.route("/api/pdf/<file_id>/content")
def pdf_content(file_id):
    if file_id not in pdf_store:
        return jsonify({"error": "Not found"}), 404
    pages = pdf_store[file_id]["pages"]
    return jsonify({"pages": [{"page": p["page"], "text": p["text"]} for p in pages], "filename": pdf_store[file_id]["filename"]})

# Chat
@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json
    message = data.get("message", "").strip()
    username = data.get("username", "")
    
    if not username or username not in users_db:
        return jsonify({"error": "No has iniciado sesion"}), 401
    user = users_db[username]
    if user.get("banned"):
        return jsonify({"error": "Usuario baneado"}), 403
    
    session_id = data.get("sessionId", "main")
    if session_id not in chat_histories:
        chat_histories[session_id] = []
    chat_histories[session_id].append({"role": "user", "content": message})
    
    try:
        model_to_use = uncensored_model if "uncensored_model" in user.get("features", []) else default_model
        
        payload = {"model": model_to_use, "messages": [{"role": "system", "content": "Eres un asistente util."}, *chat_histories[session_id][-10:]], "stream": False}
        response = requests.post(OLLAMA_URL, json=payload, timeout=120)
        result = response.json()
        
        ai_response = result.get("message", {}).get("content", "Error de conexion con IA")
        chat_histories[session_id].append({"role": "assistant", "content": ai_response})
        return jsonify({"response": ai_response})
    except Exception as e:
        return jsonify({"response": "Error: " + str(e)}), 500

@app.route("/api/clear-history", methods=["POST"])
def clear_history():
    session_id = request.json.get("sessionId", "main")
    if session_id in chat_histories:
        chat_histories[session_id] = []
    return jsonify({"success": True})

@app.route("/api/models")
def list_models():
    return jsonify({"installed": [default_model, uncensored_model, code_model]})

# Code IDE
@app.route("/api/code-chat", methods=["POST"])
def code_chat():
    data = request.json
    username = data.get("username", "")
    
    if not username or username not in users_db:
        return jsonify({"error": "No has iniciado sesion"}), 401
    if "code_ide" not in users_db[username].get("features", []):
        return jsonify({"error": "Code IDE solo en Fultra"}), 403
    
    try:
        model_to_use = uncensored_model if "uncensored_model" in users_db[username].get("features", []) else code_model
        payload = {"model": model_to_use, "messages": [{"role": "system", "content": "Genera codigo completo."}, {"role": "user", "content": data.get("message", "")}], "stream": False}
        response = requests.post(OLLAMA_URL, json=payload, timeout=180)
        result = response.json()
        ai_response = result.get("message", {}).get("content", "")
        
        code = None
        if "```" in ai_response:
            match = re.search(r'```[\w]*\n?([\s\S]*?)```', ai_response)
            if match:
                code = match.group(1).strip()
        
        return jsonify({"response": ai_response, "code": code, "filename": "generated.py" if code else None})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True, port=5000)
