from __future__ import annotations

import json
import math
import re
import secrets
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from fingerprint import analyze_global_outputs, generate_challenges, parse_numbers
from bank_builder import build_bank, read_rows
from challenge_suite import fingerprint_suite


PROJECT = Path(__file__).resolve().parent
DATA_FILE = PROJECT / "data" / "gpt_reference.jsonl"
BANK_FILE = PROJECT / "data" / "gpt_bank.json"

def bank_summary(bank: dict) -> dict:
    return {
        "model_count": len(bank["models"]),
        "response_count": sum(model["response_count"] for model in bank["models"]),
        "number_count": sum(model["valid_number_count"] for model in bank["models"]),
        "models": [
            {
                "id": model["id"],
                "display_name": model["display_name"],
                "responses": model["response_count"],
                "valid_numbers": model["valid_number_count"],
            }
            for model in bank["models"]
        ],
    }


def make_row(
    model_label: str,
    text: str,
    condition: str,
    challenge_id: str,
    expected_count: int = 0,
    temperature: str | float = "unknown",
    bank_id: str = "reference-bank",
    wrapper_transport: str | None = None,
    provider: str = "api",
    prompt: str | None = None,
    base_prompt: str | None = None,
    system_prompt: str | None = None,
    user_prefix: str | None = None,
) -> dict:
    numbers = parse_numbers(text)
    threshold = max(80, math.ceil(expected_count * 0.55)) if expected_count else 80
    row_id = f"{condition}-{secrets.token_hex(8)}"
    return {
        "row_id": row_id,
        "parent_row_id": row_id,
        "bank_id": bank_id,
        "source": model_label,
        "model_id": model_label,
        "exact_version": model_label,
        "condition_id": condition,
        "nuisance_condition_id": condition,
        "wrapper_id": condition,
        "wrapper_transport": wrapper_transport or ("manual_import" if condition == "manual" else "clean"),
        "provider": "manual" if condition == "manual" else provider,
        "challenge_id": challenge_id,
        "task_index": 0,
        "requested_count": expected_count,
        "parsed_count": len(numbers),
        "strict_threshold": threshold,
        "strict_valid": len(numbers) >= threshold,
        "temperature": temperature,
        "config_id": "enrollment",
        "prompt": prompt,
        "base_prompt": base_prompt,
        "system_prompt": system_prompt,
        "user_prefix": user_prefix,
        "text": text,
        "error": None,
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }


def append_rows(rows: list[dict], data_file: Path = DATA_FILE) -> None:
    with data_file.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def rebuild_bank(data_file: Path = DATA_FILE, bank_file: Path = BANK_FILE) -> dict:
    previous = json.loads(bank_file.read_text(encoding="utf-8")) if bank_file.exists() else None
    bank = build_bank(read_rows(data_file), previous["calibration"] if previous else None)
    bank_file.write_text(json.dumps(bank, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return bank


def enroll_manual(
    model_label: str,
    pasted: str,
    data_file: Path = DATA_FILE,
    bank_file: Path = BANK_FILE,
    bank_id: str = "reference-bank",
) -> dict:
    blocks = [
        block.strip()
        for block in re.split(r"(?m)^\s*===OUTPUT===\s*$", pasted)
        if block.strip()
    ]
    rows = [
        make_row(
            model_label=model_label,
            text=block,
            condition="manual",
            challenge_id=f"manual-{secrets.token_hex(6)}",
            bank_id=bank_id,
        )
        for block in blocks
    ]
    accepted = [row for row in rows if row["strict_valid"]]
    append_rows(accepted, data_file)
    bank = rebuild_bank(data_file, bank_file)
    return {
        "submitted": len(rows),
        "accepted": len(accepted),
        "rejected": len(rows) - len(accepted),
        "parsed_numbers": [row["parsed_count"] for row in rows],
        "bank": bank_summary(bank),
    }


def request_completion(
    base_url: str,
    api_key: str,
    api_model: str,
    prompt: str,
    temperature: float | None,
    api_format: str = "responses",
    system_prompt: str = "",
) -> str:
    from responses_client import request_response

    if api_format != "responses":
        raise ValueError("只支持 Responses 接口")
    return request_response(base_url, api_key, api_model, prompt, temperature, system_prompt)["text"]


def test_automatic(
    base_url: str,
    api_key: str,
    api_model: str,
    temperature: float | None,
    bank: dict,
    api_format: str = "responses",
) -> dict:
    target_count = 3
    max_attempts = 6
    challenges = generate_challenges(max_attempts)
    outputs = []
    errors = []
    for challenge in challenges:
        try:
            text = request_completion(
                base_url,
                api_key,
                api_model,
                challenge["prompt"],
                temperature,
                api_format,
            )
            minimum = max(80, math.ceil(challenge["expected_count"] * 0.55))
            parsed_count = len(parse_numbers(text))
            if parsed_count >= minimum:
                outputs.append(
                    {
                        "text": text,
                        "expected_count": challenge["expected_count"],
                    }
                )
            else:
                errors.append(f"有效数字不足：{parsed_count}/{minimum}")
        except Exception as error:
            errors.append(str(error))
        if len(outputs) == target_count:
            break
    result = analyze_global_outputs(outputs, bank)
    result["api_test"] = {
        "requested": target_count,
        "attempted": len(outputs) + len(errors),
        "max_attempts": max_attempts,
        "received": len(outputs),
        "errors": errors,
    }
    return result


def enroll_automatic(
    base_url: str,
    api_key: str,
    api_model: str,
    model_label: str,
    sample_count: int,
    temperature: float | None,
    api_format: str = "responses",
    data_file: Path = DATA_FILE,
    bank_file: Path = BANK_FILE,
    bank_id: str = "reference-bank",
    provider: str = "api",
) -> dict:
    suite = fingerprint_suite()
    if sample_count < 3 or sample_count > len(suite):
        raise ValueError(f"采集回答数必须在 3 到 {len(suite)} 之间")
    selected = [suite[int(index * len(suite) / sample_count)] for index in range(sample_count)]
    rows = []
    errors = []

    def collect(task: dict) -> tuple[dict | None, list[str]]:
        task_errors = []
        base_prompt = task["prompt"]
        prompt = base_prompt
        if task["user_prefix"]:
            prompt = task["user_prefix"] + "\n\nFinal task:\n" + prompt
        system_prompt = task["system"]
        for _ in range(2):
            try:
                text = request_completion(
                    base_url,
                    api_key,
                    api_model,
                    prompt,
                    temperature,
                    api_format,
                    system_prompt,
                )
                row = make_row(
                    model_label=model_label,
                    text=text,
                    condition=task["condition"],
                    challenge_id=task["challenge_id"],
                    expected_count=task["expected_count"],
                    temperature=temperature if temperature is not None else "provider_default",
                    bank_id=bank_id,
                    wrapper_transport=task["transport"],
                    provider=provider,
                    prompt=prompt,
                    base_prompt=base_prompt,
                    system_prompt=system_prompt,
                    user_prefix=task["user_prefix"],
                )
                if row["strict_valid"]:
                    return row, task_errors
                task_errors.append(
                    f"{task['challenge_id']} 有效数字不足：{row['parsed_count']}/{row['strict_threshold']}"
                )
            except Exception as error:
                task_errors.append(f"{task['challenge_id']}: {error}")
        return None, task_errors

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(collect, task) for task in selected]
        for future in as_completed(futures):
            row, task_errors = future.result()
            errors.extend(task_errors)
            if row is not None:
                rows.append(row)
    accepted = [row for row in rows if row["strict_valid"]]
    if not accepted:
        raise ValueError("没有获得可用回答，指纹库未修改")
    append_rows(accepted, data_file)
    bank = rebuild_bank(data_file, bank_file)
    return {
        "requested": sample_count,
        "received": len(accepted),
        "accepted": len(accepted),
        "rejected": sample_count - len(accepted),
        "errors": errors,
        "bank": bank_summary(bank),
    }
