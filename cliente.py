import requests
import subprocess
import json
import re
import os

API_URL = "http://127.0.0.1:8080/v1/chat/completions"


# ---------- MODEL ----------

def ask_model(messages):
    r = requests.post(API_URL, json={
        "messages": messages,
        "temperature": 0.2
    })
    return r.json()["choices"][0]["message"]["content"]


# ---------- PARSER ----------

def extract_json(text):
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except:
            pass

    print("❌ JSON inválido:")
    print(text)
    return None


# ---------- TOOLS ----------

def create_file(path, content):
    with open(path, "w") as f:
        f.write(content)
    return f"Archivo creado: {path}"


def read_file(path):
    if not os.path.exists(path):
        return "Archivo no existe"

    with open(path, "r") as f:
        return f.read()


def edit_file(path, content):
    if not os.path.exists(path):
        return "Archivo no existe"

    with open(path, "w") as f:
        f.write(content)

    return f"Archivo editado: {path}"


def run_command(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.stdout + result.stderr


# ---------- EXECUTOR ----------

def execute_action(action):
    tool = action.get("tool")
    data = action.get("input", {})

    if tool == "create_file":
        path = data.get("path") or data.get("file_name")
        content = data.get("content")

        if not path or not content:
            return "❌ create_file inválido"

        with open(path, "w") as f:
            f.write(content)

        return f"📄 Archivo creado: {path}"

    elif tool == "run_command":
        cmd = data.get("command")

        if not cmd:
            return "❌ comando vacío"

        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)

        return f"💻 {cmd}\n{result.stdout}\n{result.stderr}"



    elif tool == "read_file":


        if not path:
            return "❌ read_file inválido"

        if not os.path.exists(path):
            return "❌ archivo no existe"

        with open(path, "r") as f:
            return f.read()


    return f"⚠ tool desconocido: {tool}"

# ---------- CONFIRM ----------

def confirm(actions):
    print("\n⚠️ Acciones propuestas:\n")

    for i, a in enumerate(actions):
        print(f"{i+1}. {a}")

    choice = input("\n¿Ejecutar estas acciones? (y/n): ").lower()
    return choice == "y"


# ---------- AGENT LOOP ----------

messages = [
    {
        "role": "system",
        "content": """
Eres un agente que controla un sistema Linux.

Responde SOLO en JSON con este formato obligatorio:


{
  "actions": [
    {
      "tool": "create_file | read_file | edit_file | run_command | finish",
      "input": { ... }
    }
  ]
}

Reglas:
- NO expliques nada
- SOLO JSON
- Puedes usar múltiples acciones
- Usa "finish" cuando termines

Responde SOLO con JSON válido.

PROHIBIDO:
- texto antes o después
- explicaciones
- markdown


"""
    }
]

user_input = input("💬 > ")
messages.append({"role": "user", "content": user_input})


while True:
    response = ask_model(messages)
    print("\n🧠 Modelo:", response)

    data = extract_json(response)
    if not data:
        break

    actions = data.get("actions", [])

    # terminar


    # confirmación
    if not confirm(actions):
        print("❌ Cancelado")
        break

    if any(a.get("tool") == "finish" for a in actions):
        print("✅ Tarea finalizada")
        break

    results = []
    finished = False

    for action in actions:
        if action.get("tool") == "finish":
            finished = True
            continue

        result = execute_action(action)
        print("⚙️", result)
        results.append(result)

    if finished:
        print("✅ Tarea finalizada")


    # feedback al modelo
    messages.append({"role": "assistant", "content": response})
    messages.append({
        "role": "user",
        "content": "Resultados:\n" + "\n".join(results)
    })
