from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from flask_cors import CORS
import pdfplumber
import os
import uuid
import requests
import re
import json
import hashlib
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = 'crugus_secret_key_2024'
CORS(app)

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
USERS_FILE = os.path.join(DATA_DIR, "users.json")
CODES_FILE = os.path.join(DATA_DIR, "codes.json")

def load_json(filepath, default):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r') as f:
                return json.load(f)
        except:
            pass
    return default

def save_json(filepath, data):
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2)

users_db = load_json(USERS_FILE, {})
codes_db = load_json(CODES_FILE, {"codes": [], "used": []})

ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat")
default_model = os.environ.get("OLLAMA_MODEL", "llama3.2:latest")
code_model = os.environ.get("CODE_MODEL", "codellama:7b")
uncensored_model = os.environ.get("UNCENSORED_MODEL", "dolphin-llama3")

PLANS = {
    "normal": {
        "name": "Normal", "price": 0, "pdf_limit": 10,
        "features": ["pdf_chat", "character_detection"]
    },
    "freemium": {
        "name": "Freemium", "price": 20, "pdf_limit": float('inf'), "days_active": 10,
        "features": ["pdf_chat", "character_detection", "pdf_unlimited"]
    },
    "premium": {
        "name": "Premium", "price": 150,
        "features": ["pdf_chat", "character_detection", "pdf_unlimited", "free_chat", "uncensored_model"]
    },
    "fultra": {
        "name": "Fultra", "price": 250,
        "features": ["pdf_chat", "character_detection", "pdf_unlimited", "free_chat", "uncensored_model", "code_ide"]
    }
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
        for i, letter in enumerate(letters):
            pos = random.randint(0, len(code))
            code = code[:pos] + letter + code[pos:]
        new_codes.append(code)
    return new_codes

def save_codes(codes_list):
    global codes_db
    codes_db["codes"].extend(codes_list)
    save_json(CODES_FILE, codes_db)

def verify_code(code):
    global codes_db
    code = code.upper().strip()
    if code in codes_db["used"]:
        return None, "Codigo ya utilizado"
    if code in codes_db["codes"]:
        codes_db["used"].append(code)
        codes_db["codes"].remove(code)
        save_json(CODES_FILE, codes_db)
        return True, "Codigo valido!"
    return None, "Codigo invalido"

# ==================== AUTH ROUTES ====================

@app.route("/login-page")
def login_page():
    return render_template("login.html")

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
            return jsonify({
                "success": True, "username": username,
                "plan": users_db[username].get("plan", "normal"),
                "features": users_db[username].get("features", PLANS["normal"]["features"])
            })
        else:
            return jsonify({"error": "Contrasena incorrecta"}), 401
    return jsonify({"error": "Usuario no existe"}), 404

@app.route("/api/register", methods=["POST"])
def api_register():
    data = request.json
    username = data.get("username", "").strip().lower()
    password = data.get("password", "")
    
    if not username or not password:
        return jsonify({"error": "Completa todos los campos"}), 400
    if len(username) < 3:
        return jsonify({"error": "Usuario minimo 3 caracteres"}), 400
    if len(password) < 4:
        return jsonify({"error": "Contrasena minimo 4 caracteres"}), 400
    if username in users_db:
        return jsonify({"error": "Usuario ya existe"}), 400
    
    users_db[username] = {
        "password": hash_password(password),
        "plan": "normal",
        "features": PLANS["normal"]["features"],
        "created": datetime.now().isoformat(),
        "pdf_count": 0,
        "banned": False,
        "freemium_expires": None
    }
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
    
    return jsonify({"success": True, "message": "Plan Fultra activado!", "plan": "fultra", "features": PLANS["fultra"]["features"]})

@app.route("/api/user")
def api_user():
    username = request.args.get("username", "")
    if username and username in users_db:
        user = users_db[username]
        return jsonify({"username": username, "plan": user.get("plan", "normal"), "features": user.get("features", []), "banned": user.get("banned", False)})
    return jsonify({"username": None})

@app.route("/api/plans")
def api_plans():
    return jsonify(PLANS)

@app.route("/redeem")
def redeem_page():
    return render_template("redeem.html")

# ==================== MAIN APP ====================

pdf_store = {}
chat_histories = {}

@app.route("/")
def index():
    return render_template("index.html")

def extract_pdf_with_pages(pdf_path):
    pages_text = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            page_text = page.extract_text()
            if page_text:
                pages_text.append({"page": i, "text": page_text})
    return pages_text

def find_relevant_context(text, query, flexible=False):
    query_words = [w.lower() for w in query.split() if len(w) > 2]
    if not query_words:
        return []
    passages = []
    lines = text.split('\n')
    best_match = None
    best_score = 0
    related = []
    for line in lines:
        line_lower = line.lower().strip()
        if len(line_lower) < 10:
            continue
        matches = sum(1 for w in query_words if w in line_lower)
        if matches >= 2:
            score = matches * 5
            if score > best_score:
                best_score = score
                best_match = line.strip()
        elif matches == 1 and flexible:
            related.append(line.strip())
    if best_match:
        passages.append(best_match)
        if flexible:
            passages.extend(related[:3])
    return passages[:5]

def detect_characters(text):
    exclude_first = {'Asuntos', 'Ministerio', 'Gobierno', 'Estado', 'Jefe', 'Embajada', 'Consulado', 'Delegacion', 'Comite', 'Secretario', 'Director', 'Presidente', 'Primer', 'Segundo', 'Ultimo', 'Nuevo', 'Imperio', 'Reino', 'Republica', 'Monarquia', 'Ejercito', 'Armada', 'Guerra', 'Paz', 'Alianza', 'Coalicion', 'Tratado', 'Documento', 'Archivo', 'Politica', 'Economia', 'Defensa', 'Relaciones', 'Nacional', 'Local'}
    exclude_last = {'Guerra', 'Mundial', 'Nacional', 'Imperial', 'Revolucion', 'Moderna', 'Francesa', 'Inglesa', 'Aleman', 'Espanola', 'Civil', 'General', 'Mayor', 'Menor', 'Exteriores', 'Defensa', 'Marina', 'Estado', 'Gobierno', 'Reino', 'Republica', 'Monarquia', 'Puerto', 'Isla', 'Ciudad', 'Pais', 'Region', 'Provincia', 'Monte', 'Rio', 'Mar'}
    name_counts = {}
    seen = set()
    pattern = r'\b([A-Z][a-z]{2,})\s+([A-Z][a-z]{2,})\b'
    for line in text.split('\n')[:2000]:
        line = line.strip()
        if len(line) < 10 or len(line) > 300:
            continue
        if any(p in line.upper() for p in ['CAPITULO', 'CHAPTER', 'PAGINA', 'PAGE']):
            continue
        for first, last in re.findall(pattern, line):
            if len(first) < 3 or len(last) < 3:
                continue
            if first in exclude_first or last in exclude_last:
                continue
            name = f"{first} {last}"
            if name not in seen:
                seen.add(name)
                name_counts[name] = name_counts.get(name, 0) + 1
    return [{"name": n, "mentions": c, "type": "person"} for n, c in sorted(name_counts.items(), key=lambda x: x[1], reverse=True) if c >= 2][:12]

# ==================== PDF ROUTES ====================

@app.route("/api/upload", methods=["POST"])
def upload_pdf():
    username = request.form.get("username", "")
    if not username or username not in users_db:
        return jsonify({"error": "No has iniciado sesion"}), 401
    user = users_db[username]
    if user.get("banned"):
        return jsonify({"error": "Usuario baneado"}), 403
    if "pdf_unlimited" not in user.get("features", []) and user.get("pdf_count", 0) >= 30:
        return jsonify({"error": "Limite de PDFs alcanzado (30)"}), 400
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    file = request.files["file"]
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Solo PDF"}), 400
    
    file_id = str(uuid.uuid4())
    file_path = os.path.join(UPLOAD_FOLDER, f"{file_id}.pdf")
    file.save(file_path)
    
    try:
        pages = extract_pdf_with_pages(file_path)
        full_text = "\n".join([p["text"] for p in pages])
        characters = detect_characters(full_text)
        pdf_store[file_id] = {"filename": file.filename, "text": full_text, "pages": pages, "path": file_path, "characters": characters, "username": username}
        users_db[username]["pdf_count"] = users_db[username].get("pdf_count", 0) + 1
        save_json(USERS_FILE, users_db)
        return jsonify({"success": True, "fileId": file_id, "filename": file.filename, "textLength": len(full_text), "pageCount": len(pages), "characters": characters})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/clear-session-pdfs", methods=["POST"])
def clear_session_pdfs():
    username = request.json.get("username", "")
    deleted = 0
    for fid in [f for f, d in pdf_store.items() if d.get("username") == username]:
        try:
            if os.path.exists(pdf_store[fid]["path"]):
                os.remove(pdf_store[fid]["path"])
        except: pass
        del pdf_store[fid]
        deleted += 1
    if username in users_db:
        users_db[username]["pdf_count"] = 0
        save_json(USERS_FILE, users_db)
    return jsonify({"deleted": deleted})

@app.route("/api/pdfs")
def list_pdfs():
    username = request.args.get("username", "")
    if not username or username not in users_db:
        return jsonify([])
    return jsonify([{"fileId": f, "filename": pdf_store[f]["filename"], "textLength": len(pdf_store[f]["text"]), "pageCount": len(pdf_store[f]["pages"]), "characters": pdf_store[f].get("characters", [])} for f in pdf_store if pdf_store[f].get("username") == username])

@app.route("/api/pdf/<file_id>/content")
def pdf_content(file_id):
    if file_id not in pdf_store:
        return jsonify({"error": "Not found"}), 404
    pages = pdf_store[file_id]["pages"]
    return jsonify({"pages": [{"page": p["page"], "text": p["text"]} for p in pages], "filename": pdf_store[file_id]["filename"]})

@app.route("/api/pdf/<file_id>/search", methods=["POST"])
def search_pdf(file_id):
    if file_id not in pdf_store:
        return jsonify({"error": "Not found"}), 404
    query = request.json.get("query", "")
    if not query:
        return jsonify({"results": [], "context": ""})
    results = []
    for page in pdf_store[file_id]["pages"]:
        if query.lower() in page["text"].lower():
            for line in page["text"].split('\n'):
                if query.lower() in line.lower():
                    results.append({"page": page["page"], "text": line.strip()[:500], "highlight": query})
                    break
    return jsonify({"query": query, "results": results[:8], "totalPages": len(pdf_store[file_id]["pages"])})

# ==================== CHAT ROUTES ====================

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
    if data.get("mode") == "ai" and "free_chat" not in user.get("features", []):
        return jsonify({"error": "Free chat solo en Premium/Fultra"}), 403
    
    session_id = data.get("sessionId", "main")
    if session_id not in chat_histories:
        chat_histories[session_id] = []
    chat_histories[session_id].append({"role": "user", "content": message})
    
    try:
        selected_pdfs = data.get("selectedPdfs", [])
        model_to_use = uncensored_model if "uncensored_model" in user.get("features", []) else default_model
        context = ""
        characters = []
        
        for pdf_id in selected_pdfs:
            if pdf_id in pdf_store and pdf_store[pdf_id].get("username") == username:
                pdf_data = pdf_store[pdf_id]
                passages = find_relevant_context(pdf_data["text"], message, flexible=True)
                if passages:
                    context += f"\n\n--- {pdf_data['filename']} ---\n" + "\n".join(passages)
                characters.extend(pdf_data.get("characters", [])[:5])
        
        if context:
            system_prompt = f"Eres un asistente util. Responde basandote en:\n{context}"
        else:
            system_prompt = "Eres un asistente util."
        
        payload = {"model": model_to_use, "messages": [{"role": "system", "content": system_prompt}, *chat_histories[session_id][-10:]], "stream": False}
        response = requests.post(ollama_url, json=payload, timeout=120)
        result = response.json()
        
        if "error" in result:
            return jsonify({"error": result["error"]}), 500
        
        ai_response = result["message"]["content"]
        chat_histories[session_id].append({"role": "assistant", "content": ai_response})
        
        return jsonify({"response": ai_response, "characters": characters})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/clear-history", methods=["POST"])
def clear_history():
    session_id = request.json.get("sessionId", "main")
    if session_id in chat_histories:
        chat_histories[session_id] = []
    return jsonify({"success": True})

@app.route("/api/models")
def list_models():
    try:
        response = requests.get("http://localhost:11434/api/tags", timeout=5)
        if response.status_code == 200:
            return jsonify({"installed": [m["name"] for m in response.json().get("models", [])]})
    except: pass
    return jsonify({"installed": [default_model]})

# ==================== CODE IDE ====================

CODE_PROMPTS = {
    "generate": "Genera codigo COMPLETO y FUNCIONAL. Solo bloque markdown.",
    "fix": "Corrige el codigo. Solo devuelve codigo corregido.",
    "review": "Revisa el codigo y reporta errores.",
    "explain": "Explica el codigo paso a paso."
}

code_chat_history = {}

@app.route("/api/code-chat", methods=["POST"])
def code_chat():
    data = request.json
    username = data.get("username", "")
    
    if not username or username not in users_db:
        return jsonify({"error": "No has iniciado sesion"}), 401
    if "code_ide" not in users_db[username].get("features", []):
        return jsonify({"error": "Code IDE solo en plan Fultra"}), 403
    
    session_id = "code_" + username
    if session_id not in code_chat_history:
        code_chat_history[session_id] = []
    code_chat_history[session_id].append({"role": "user", "content": data.get("message", "")})
    
    try:
        model_to_use = code_model if "uncensored_model" not in users_db[username].get("features", []) else uncensored_model
        payload = {"model": model_to_use, "messages": [{"role": "system", "content": CODE_PROMPTS.get(data.get("mode"), CODE_PROMPTS["generate"])}, *code_chat_history[session_id][-10:]], "stream": False}
        response = requests.post(ollama_url, json=payload, timeout=180)
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

@app.route("/api/code-download", methods=["POST"])
def code_download():
    from flask import make_response
    import zipfile
    import io
    files = request.json.get("files", {})
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for filename, content in files.items():
            zf.writestr(filename, content.get("content", ""))
    zip_buffer.seek(0)
    response = make_response(zip_buffer.getvalue())
    response.headers['Content-Type'] = 'application/zip'
    response.headers['Content-Disposition'] = 'attachment; filename=project.zip'
    return response

if __name__ == "__main__":
    print("=" * 50)
    print("  CRUGUS AI STUDIO v2.0")
    print("  Sistema de Cuentas y Planes")
    print("=" * 50)
    print("  Admin: DerosOwner / frijol-quemado")
    print("  Admin Panel: http://localhost:5000/admin")
    print("=" * 50)
    app.run(debug=True, port=5000)
