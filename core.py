"""Auto-Settle AI - the AI side.  The AI only DESCRIBES damage (part, type, severity, repairable) and may
RE-WORD messages. It never sees prices and never sets an amount: all money logic is in settlement_engine.py."""
import base64, json, os, re
import requests
from settlement_engine import inr, get_catalog, SEVERITIES, DAMAGE_TYPES  # noqa: F401  (re-exported for app.py)

SEV = SEVERITIES
TYP = DAMAGE_TYPES


def parts_for(vtype):
    """Damageable parts for a vehicle type, read from the price dataset (so the AI and the prices always agree)."""
    return get_catalog().parts(vtype)


# ---------- LLM access: Claude, any OpenAI-compatible API (OpenAI, Gemini, Groq, OpenRouter), or a local Ollama model ----------
def env_cfg():
    p = os.getenv("PROVIDER", "anthropic")
    if p == "openai": return {"type": "openai", "model": os.getenv("OPENAI_MODEL", ""), "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"), "key": os.getenv("OPENAI_API_KEY", "")}
    if p == "ollama": return {"type": "ollama", "model": os.getenv("OLLAMA_MODEL", "qwen2.5vl:7b"), "base_url": os.getenv("OLLAMA_URL", "http://localhost:11434"), "key": ""}
    return {"type": "anthropic", "model": os.getenv("MODEL", "claude-sonnet-5-5"), "base_url": "", "key": os.getenv("ANTHROPIC_API_KEY", "")}


def ask_llm(prompt, img_jpeg=None, cfg=None):
    cfg = cfg or env_cfg(); t = "openai" if cfg["type"] == "openai-compatible" else cfg["type"]; b64 = base64.b64encode(img_jpeg).decode() if img_jpeg else None
    if t == "ollama":
        m = {"role": "user", "content": prompt}
        if b64: m["images"] = [b64]
        base = (cfg.get("base_url") or "http://localhost:11434").rstrip("/")
        if base.endswith("/api/chat"): base = base[:-9]
        r = requests.post(base + "/api/chat", timeout=300, json={"model": cfg["model"], "messages": [m], "stream": False})
        r.raise_for_status(); return r.json()["message"]["content"]
    if t == "openai":
        from openai import OpenAI
        content = [{"type": "text", "text": prompt}]
        if b64: content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}})
        client = OpenAI(api_key=cfg["key"], base_url=(cfg.get("base_url") or "https://api.openai.com/v1"))
        r = client.chat.completions.create(model=cfg["model"], messages=[{"role": "user", "content": content}], max_tokens=900, timeout=120)
        return r.choices[0].message.content
    import anthropic
    content = []
    if b64: content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
    content.append({"type": "text", "text": prompt})
    r = anthropic.Anthropic(api_key=cfg.get("key") or None).messages.create(model=cfg["model"], max_tokens=900, messages=[{"role": "user", "content": content}])
    return r.content[0].text


def score(found, truth):
    """Precision and recall of the damaged parts a model found versus the true parts."""
    f, t = set(found), set(truth); tp = len(f & t)
    return (round(tp / len(f), 2) if f else 0.0, round(tp / len(t), 2) if t else 0.0)


# ---------- vision: DESCRIBE ONLY ----------
def vision_prompt(vtype):
    parts = "|".join(parts_for(vtype))
    return (f"You are a careful motor-insurance damage inspector. The vehicle is a {vtype}. Examine the WHOLE image systematically, area by area. "
            "List EVERY visibly damaged, torn or missing part, including partly visible ones; do not stop at the most obvious damage. "
            "Describe damage ONLY. Do NOT mention prices, costs or repair estimates. Return ONLY JSON: "
            '{"items": [{"part": one of %s, "type": one of dent|scratch|crack|broken, "severity": one of minor|moderate|severe, '
            '"repairable": true if body-work could restore the part to good condition, false if it must be replaced}], '
            '"confidence": 0-100 integer for how sure you are you found ALL damage, "quality_ok": false if blurry, dark, cropped or not a %s, '
            '"box": [x,y,width,height] fractions 0-1 around the worst damage, "notes": one short sentence}.') % (parts, vtype)


_SYN = {"front_bumper": "bumper", "rear_bumper": "bumper", "headlamp": "headlight", "head_light": "headlight", "head_lamp": "headlight",
        "side_mirror": "mirror", "wing_mirror": "mirror", "rear_view_mirror": "mirror", "hood": "bonnet", "trunk": "boot", "tail_light": "taillight",
        "tail_lamp": "taillight", "taillamp": "taillight", "windscreen": "windshield", "tire": "tyre", "petrol_tank": "fuel_tank", "gas_tank": "fuel_tank",
        "wing": "fender", "mudguard": "fender", "front_fender": "fender", "rear_fender": "fender", "alloy_wheel": "wheel", "rim": "wheel"}


def _json_from(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m: raise ValueError("No JSON object found in model response")
    raw = m.group(0)
    try: return json.loads(raw)                       # well-formed? use as is (never corrupt apostrophes in notes)
    except Exception: pass
    raw = re.sub(r",\s*([}\]])", r"\1", raw)          # trailing commas
    raw = re.sub(r"\bTrue\b", "true", re.sub(r"\bFalse\b", "false", raw))
    raw = re.sub(r"(?<=[{,\[:\s])'([^'\n]*)'(?=\s*[:,}\]])", r'"\1"', raw)   # single-quoted tokens only
    raw = re.sub(r'(?<=\{|,)\s*(\w+)\s*:', r' "\1":', raw)                    # unquoted keys
    return json.loads(raw)


def clean_item(i):
    part = re.sub(r"[\s\-]+", "_", str(i.get("part", "")).strip().lower()); part = _SYN.get(part, part)
    rep = i.get("repairable")
    if isinstance(rep, str): rep = rep.strip().lower() in ("true", "yes", "y", "1")
    return {"part": part, "type": str(i.get("type", "dent")).strip().lower(), "severity": str(i.get("severity", "moderate")).strip().lower(),
            "repairable": rep if isinstance(rep, bool) else None}


def analyse_image(jpeg, cfg=None, vtype="car"):
    r = _json_from(ask_llm(vision_prompt(vtype), jpeg, cfg))
    r["items"] = [clean_item(i) for i in (r.get("items") or ([r] if r.get("part") else []))]
    try:  # second pass: look again for missed parts
        r2 = _json_from(ask_llm("Earlier you listed these damaged parts: " + json.dumps(r["items"]) + ". Look at the image again, area by area. "
                                'Return ONLY JSON {"items":[every damaged part including any you missed, each {"part","type","severity","repairable"}]}. '
                                "Describe damage only, no prices. Allowed parts: " + "|".join(parts_for(vtype)), jpeg, cfg))
        items2 = [clean_item(i) for i in r2.get("items", [])]
        old = {i["part"] for i in r["items"]}
        if len(items2) >= len(old): r["added"] = len({i["part"] for i in items2} - old); r["items"] = items2
    except Exception: pass
    return r


# ---------- wording helpers: the chat model may only re-phrase; numbers are verified ----------
def _nums(t): return sorted(re.findall(r"\d[\d,]*", t.translate(str.maketrans("\u0966\u0967\u0968\u0969\u096a\u096b\u096c\u096d\u096e\u096f", "0123456789"))))


def safe_rewrite(text, instruction, cfg=None):
    """Ask the model to re-word `text`, but keep the ORIGINAL if any number changed, appeared or vanished."""
    try:
        out = ask_llm(instruction + " Keep every number, rupee amount and fact exactly as given. Output only the result: " + text, cfg=cfg).strip()
        return out if out and _nums(out) == _nums(text) else text
    except Exception:
        return text


def translate_hi(text, cfg=None):
    return safe_rewrite(text, "Translate to Hindi (Devanagari).", cfg)


def friendly(text, cfg=None):
    return safe_rewrite(text, "Re-word this insurance message to sound warmer and more natural, same length or shorter, no new promises.", cfg)
