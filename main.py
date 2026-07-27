from openai import OpenAI

from brain.agent import DeveloperAgent
from brain.approval_controller import ConversationalController
from tools.project_scanner import construir_indice

client = OpenAI(
    base_url="http://localhost:1234/v1",
    api_key="lm-studio"
)

print("DeveloperAI v0.5")
print("Escribe 'salir' para cerrar\n")

construir_indice()
agent = DeveloperAgent(client=client, memory_file="memory/memory.json", prompt_dir="prompts", base_dir=".")
controller = ConversationalController(agent)

while True:
    mensaje = input("Tú: ")

    if mensaje.lower() == "salir":
        break

    respuesta = controller.process_message(mensaje)

    print("\nIA:")
    print(respuesta)
    print()
