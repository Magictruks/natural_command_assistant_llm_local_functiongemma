# assistant.py
import json
import re
from typing import Dict, Any

from transformers import AutoProcessor, AutoModelForCausalLM
import torch

from actions import run_tests, deploy_app, generate_test_report

MODEL_ID = "google/functiongemma-270m-it"

# NOTE: tools definition must follow the doc shape: type:"function", function:{...}
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Runs a test suite",
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["unit", "integration", "e2e"]
                    },
                    "environment": {
                        "type": "string",
                        "enum": ["dev", "staging", "prod"]
                    }
                },
                "required": ["type", "environment"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "deploy_app",
            "description": "Deploys a version of the application",
            "parameters": {
                "type": "object",
                "properties": {
                    "version": {"type": "string"},
                    "environment": {"type": "string", "enum": ["staging", "preprod"]}
                },
                "required": ["version", "environment"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "generate_test_report",
            "description": "Generates a test report",
            "parameters": {
                "type": "object",
                "properties": {
                    "format": {"type": "string", "enum": ["html", "pdf"]}
                },
                "required": ["format"]
            }
        }
    }
]


def load_model_and_processor():
    processor = AutoProcessor.from_pretrained(MODEL_ID, device_map="auto")
    model = AutoModelForCausalLM.from_pretrained(MODEL_ID, dtype="auto", device_map="auto")
    return processor, model


def build_message(user_input: str):
    """
    Build the chat message list as in the docs:
    - a developer (system) role line is REQUIRED to activate function-calling logic
    - a user role with the query
    We will pass this to processor.apply_chat_template(...)
    """
    return [
        {
            "role": "developer",
            "content": "You are a model that can do function calling with the following functions"
        },
        {
            "role": "user",
            "content": user_input
        }
    ]


def parse_function_call(output_text: str) -> Dict[str, Any]:
    """
    Parse the model output from FunctionGemma style:
    <start_function_call>call:function_name{key:<escape>Value<escape>,...}<end_function_call>

    Returns dict: { "name": str, "arguments": dict }
    Raises ValueError if parsing fails.
    """
    # Find function call block
    start_tag = "<start_function_call>"
    end_tag = "<end_function_call>"

    s = output_text
    si = s.find(start_tag)
    ei = s.find(end_tag, si + 1)

    if si == -1 or ei == -1:
        raise ValueError("No function call tags found in model output")

    block = s[si + len(start_tag):ei].strip()  # e.g. call:get_current_temperature{location:<escape>London<escape>}
    # Expect format: call:func_name{...}
    m = re.match(r"call:([^{]+)\{(.*)\}$", block, flags=re.DOTALL)
    if not m:
        raise ValueError(f"Unexpected function_call format: {block!r}")

    func_name = m.group(1).strip()
    args_body = m.group(2).strip()  # e.g. location:<escape>London<escape>

    # Replace the custom escape token with quotes (doc uses <escape> to denote quotes)
    args_body = args_body.replace("<escape>", "\"")

    # Convert simple key:value pairs into JSON:
    # - add quotes around keys
    # - ensure commas separate pairs (they usually are)
    # We assume values are quoted (after replacing <escape>), or numbers/booleans.
    # Add quotes around unquoted keys: e.g. location:"London" -> "location":"London"
    args_body_json_like = re.sub(r'(\b[a-zA-Z_][a-zA-Z0-9_]*\b)\s*:', r'"\1":', args_body)

    # Wrap with braces
    json_text = "{" + args_body_json_like + "}"

    # Load JSON
    try:
        args = json.loads(json_text)
    except Exception as e:
        raise ValueError(f"Failed to decode args JSON: {json_text!r}") from e

    return {"name": func_name, "arguments": args}


def call_function(call: dict):
    name = call["name"]
    args = call["arguments"]

    if name == "run_tests":
        run_tests(**args)
    elif name == "deploy_app":
        deploy_app(**args)
    elif name == "generate_test_report":
        generate_test_report(**args)
    else:
        print("❌ Unknown function", name)


def main():
    processor, model = load_model_and_processor()
    model_device = next(model.parameters()).device

    print("💬 Natural Language Command Assistant (Ctrl+C to exit)\n")

    while True:
        user_input = input("🧑‍💻 > ").strip()
        if not user_input:
            continue

        message = build_message(user_input)

        # apply_chat_template builds the inputs expected by FunctionGemma
        inputs = processor.apply_chat_template(
            message,
            tools=TOOLS,
            add_generation_prompt=True,
            return_dict=True,
            return_tensors="pt"
        )

        # move tensors to model device
        inputs = inputs.to(model_device)

        # generate
        out = model.generate(**inputs, pad_token_id=processor.eos_token_id, max_new_tokens=128)
        # decode only the generated portion (like in the doc)
        # out[0] may have shape [seq_len], inputs["input_ids"][0] length is the prompt length
        generated = processor.decode(out[0][len(inputs["input_ids"][0]):], skip_special_tokens=True)

        try:
            function_call = parse_function_call(generated)
            print("🔧 Parsed function call:", function_call)
            call_function(function_call)
        except Exception as e:
            print("❌ Could not interpret the command:", e)
            print("Raw model output:\n", generated)


if __name__ == "__main__":
    main()
