# Auto-Settle AI (Streamlit) - settlement & negotiation engine

**The AI describes; the code decides.** The vision model only says *which part is damaged, what kind of damage, how severe, and whether it is repairable*. It never sees a price.
`settlement_engine.py` (plain Python, no AI) turns that description into money, runs the negotiation and hands over to a human when needed.

## Files
| File | What it is |
|---|---|
| `settlement_engine.py` | **The brain.** Price lookup, depreciation, policy rules, claim history, profit guards, reasons, negotiation, human takeover, audit events. No AI, no Streamlit. |
| `settlement_config.json` | **Every dial** (percentages, thresholds, depreciation slabs, labour rates). Edit and save; next claim uses it. |
| `parts_catalog.csv` | **Price dataset**: vehicle type x make x segment x part -> part cost, labour hours, material, repairable?, structural? |
| `claim_history.csv` | Sample claim history by registration number (vehicle + owner). |
| `core.py` | AI side: model access (Claude / OpenAI-compatible / Ollama), vision prompt (describe only), safe JSON parsing, number-checked re-wording and Hindi translation. |
| `app.py` | Streamlit UI: claim flow, reasons, chat, adjuster queue with human approve / override / reject, audit trail, rules & prices editor. |
| `cli_demo.py` | Test the engine in the terminal, no API key, no Streamlit. |
| `test_settlement_engine.py` | 67 automated tests (incl. 3,000 fuzzed negotiations). |
| `make_catalog.py` | Optional: regenerates the placeholder price CSV. |

## How a number is produced
1. AI returns items: `part, type, severity, repairable`.
2. **Price lookup** in `parts_catalog.csv` by (vehicle type, make, segment, part); unknown make -> generic row -> human review.
3. **Repair or replace**: repairable parts are priced as labour + paint/material, unless that nears a new part's price; lights, mirrors, glass are always replaced.
4. **Depreciation** on replaced parts by vehicle age and material (zero-dep add-on removes it). **Exclusions** (e.g. tyres, or parts the policy does not cover) pay nothing and say so. **Excess** is deducted.
   => *Amount payable* = what the insurer would pay a garage.
5. **Cash band**: opening offer ~75%, ceiling ~90% of the amount payable, moved up for newer vehicles / clean history, down for old vehicles / recent claims.
6. **Profit guards**: insurer must save at least `min_saving_pct` vs the garage route on every deal; optional per-policy loss-ratio cap (needs the annual premium); a final firewall blocks any amount above the engine's authority.
7. **Negotiation**: shrinking concessions, final offer on round `max_rounds`, every message carries the reasons.
8. **Human takeover** (any of): intake flag (low confidence, severe/structural damage, mismatch, fraud flag, repeat claim for the same part, over cap, lapsed policy ...), a **hyper-inflated demand**, failed negotiation, rule-changing or angry messages, a deal above `approval_above_inr`, or a random audit sample. Human overrides above the engine limit need a written justification. Everything is in the audit trail.

## Run on your laptop
    pip install -r requirements.txt
    streamlit run app.py
Choose ONE model backend with environment variables (PowerShell: `$env:NAME="value"`), or use Streamlit secrets (see `secrets_toml.example`):

| Backend | Variables |
|---|---|
| Claude (default) | `ANTHROPIC_API_KEY`, optional `MODEL` |
| Any OpenAI-compatible API (OpenAI, Gemini, Groq, OpenRouter) | `PROVIDER=openai`, `OPENAI_API_KEY`, `OPENAI_MODEL` (must support images), optional `OPENAI_BASE_URL` |
| Small local model (Ollama, under 20B parameters) | `PROVIDER=ollama`, optional `OLLAMA_MODEL` (default qwen2.5vl:7b) |

**No AI key? You can still test everything**: use "Enter findings manually" in the app, or `cli_demo.py`.

## Test the negotiation (no key needed)
    python -m pytest -q                      # all automated tests
    python cli_demo.py --scenarios           # 8 scripted customers incl. hyper-inflated, injection, angry
    python cli_demo.py --audit               # chat as the customer in the terminal
    python cli_demo.py --type bike --make "Royal Enfield" --age-years 6 --parts "fuel_tank:dent:moderate:y" --reg RJ14CD5678
    python cli_demo.py --set settlement.ceiling_pct=0.92 --set human.approval_above_inr=5000

## Change the data or the rules
* Prices: replace `parts_catalog.csv` with real rates (same columns; `*` = wildcard make/segment). It reloads automatically.
* Rules: edit `settlement_config.json` (or the "Settlement rules & prices" tab in admin mode). Bad combinations (e.g. floor above ceiling) are rejected with a clear message.
* History: add rows to `claim_history.csv`, or type history into the admin sidebar.

## Host it publicly (free options)
1. Put all files above (plus `parts_catalog.csv`, `settlement_config.json`, `claim_history.csv`) in a GitHub repository.
2. Streamlit Community Cloud (share.streamlit.io): New app, pick the repo and `app.py`.
3. In the app's Settings, Secrets, add your variables (see `secrets_toml.example`).
Note: on free hosting `claims_db.json` and any config edits made in the app are wiped on restart. Commit config changes to GitHub to keep them.

Never paste an API key into the code or a public repository.

## Honest limits
All prices, percentages, depreciation slabs and thresholds are **illustrative placeholders**, not any insurer's tariff. AI damage-detection accuracy is not measured yet. Customer chat is understood by rules (amounts, accept / why / anger / injection words), not by an AI.
