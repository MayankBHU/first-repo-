"""
Auto-Settle SETTLEMENT ENGINE  (pure Python, no AI, no Streamlit - fully testable)

Division of labour
  * The vision AI only DESCRIBES damage: part, damage type, severity, repairable yes/no.
  * THIS file turns that description into money: price lookup, depreciation, policy rules,
    claim history, company-profit guards, the negotiation, and the hand-over to a human.
  * The chat model (if used) may only re-word messages. It never sees or sets a number.

Every tunable number lives in DEFAULT_CONFIG (overridden by settlement_config.json).
Every price lives in parts_catalog.csv. Edit those two files - never this code - to change behaviour.

ALL RUPEE VALUES AND PERCENTAGES SHIPPED WITH THE PROJECT ARE ILLUSTRATIVE ASSUMPTIONS.
Replace them with the insurer's real tariff before anything is relied on.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG_PATH = os.path.join(HERE, "parts_catalog.csv")
CONFIG_PATH = os.path.join(HERE, "settlement_config.json")
HISTORY_PATH = os.path.join(HERE, "claim_history.csv")

SEVERITIES = ("minor", "moderate", "severe")
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}
DAMAGE_TYPES = ("scratch", "dent", "crack", "broken")

# --------------------------------------------------------------------------------------
# 1. CONFIG  (every "dial" in one place)
# --------------------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "labour_rate_per_hour": {"car": 600, "suv": 650, "bike": 350, "scooter": 350, "default": 500},
    # repair_material_fraction = paint/filler/consumables as a share of the new-part price
    "severity": {
        "minor": {"repair_material_fraction": 0.12, "labour_mult": 0.50},
        "moderate": {"repair_material_fraction": 0.30, "labour_mult": 0.85},
        "severe": {"repair_material_fraction": 0.55, "labour_mult": 1.20},
    },
    "damage_type_mult": {"scratch": 0.60, "dent": 1.00, "crack": 1.20, "broken": 1.40},
    "repair_cap_vs_replace": 0.85,      # if repairing costs more than 85% of replacing, replace instead
    "replace_labour_mult": 0.80,        # fitting a new part takes less labour than full repair work
    # Depreciation applies to REPLACED PARTS only (not labour, not paint/repair material).
    # Metal parts: age-slab table [[max_age_months, depreciation], ...]; others flat. Illustrative, IRDAI-style.
    "depreciation": {
        "metal": [[6, 0.0], [12, 0.05], [24, 0.15], [36, 0.25], [48, 0.35], [60, 0.45], [100000, 0.50]],
        "plastic": 0.50, "rubber": 0.50, "fibre": 0.30, "glass": 0.0,
    },
    "zero_dep_addon_removes_depreciation": True,
    "idv_age_schedule": [[6, 0.05], [12, 0.15], [24, 0.20], [36, 0.30], [48, 0.40], [60, 0.50], [100000, 0.60]],
    "default_idv_new": {
        "car": {"economy": 500000, "mid": 900000, "premium": 2000000},
        "suv": {"economy": 800000, "mid": 1400000, "premium": 3000000},
        "bike": {"economy": 90000, "mid": 180000, "premium": 400000},
        "scooter": {"economy": 80000, "mid": 120000, "premium": 200000},
    },
    "default_excess": {"car": 1000, "suv": 1000, "bike": 100, "scooter": 100, "default": 1000},
    "default_excluded_parts": ["tyre"],            # excluded unless the policy says otherwise
    "make_aliases": {"maruti": "maruti suzuki", "suzuki": "suzuki", "mercedes": "mercedes-benz", "benz": "mercedes-benz",
                     "vw": "volkswagen", "re": "royal enfield", "enfield": "royal enfield", "mg motor": "mg"},

    "settlement": {
        "floor_pct": 0.75,             # opening offer = this share of the amount payable
        "ceiling_pct": 0.90,           # the most the engine may ever agree on its own
        "ceiling_hard_max": 0.95,      # no adjustment may push the ceiling above this
        "floor_hard_min": 0.50,        # ... or the floor below this
        "min_saving_pct": 0.07,        # PROFIT GUARD 1: insurer must save at least 7% vs the garage route
        "step_pct": 0.04,              # first concession = 4% of amount payable
        "step_decay": 0.75,            # each later concession is 75% of the previous one
        "min_step_inr": 100,
        "max_rounds": 4,               # on this round the engine makes its FINAL offer (= its ceiling)
        "accept_gap_pct": 0.04,        # a counter this close above our offer (and <= ceiling) is accepted
        "settle_at_customer_figure_if_lower": False,   # False = if customer asks for less than our offer we still pay our offer (fair)
        "round_down_to": 10,
        "max_messages": 16,            # runaway-chat protection
        "age_bands": [
            {"max_age_months": 24, "floor_delta": 0.03, "ceiling_delta": 0.03, "label": "newer vehicle (up to 2 years)"},
            {"max_age_months": 60, "floor_delta": 0.00, "ceiling_delta": 0.00, "label": "mid-age vehicle (2-5 years)"},
            {"max_age_months": 120, "floor_delta": -0.03, "ceiling_delta": -0.05, "label": "older vehicle (5-10 years)"},
            {"max_age_months": 100000, "floor_delta": -0.06, "ceiling_delta": -0.10, "label": "old vehicle (over 10 years)"},
        ],
        "history": {"per_claim_12m_penalty": 0.02, "max_penalty": 0.08, "clean_36m_bonus": 0.02},
    },
    "profit": {
        "max_loss_ratio": 0.85,        # PROFIT GUARD 2: paid claims / premiums collected on this policy
        "premium_years": 3,
    },
    "human": {
        "auto_settle_cap_inr": 25000,  # amount payable above this -> human decides from the start
        "approval_above_inr": 15000,   # a deal above this is only final after a human approves it
        "min_ai_confidence": 70,
        "severe_to_human": True,
        "structural_to_human": True,
        "max_claims_12m_auto": 3,
        "repeat_part_days": 180,       # same part claimed again within this many days -> human
        "total_loss_ratio_of_idv": 0.75,
        "hyper_inflation_factor": 1.25,     # customer asks > 125% of the amount payable ...
        "hyper_min_abs_gap_inr": 1500,      # ... and at least this much more -> human takes over
        "audit_sample_rate": 0.10,     # share of AUTO deals randomly sent to a human for after-the-fact audit
        "unstable_extra_parts": 2,     # AI's 2nd look found this many more parts -> human
        "fallback_pricing_to_human": True,  # no price data for this make -> human
        "override_requires_note": True,
    },
}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def validate_config(cfg: dict) -> list:
    """Return a list of human-readable problems (empty list = config is safe to use)."""
    errs = []
    s, h, p = cfg["settlement"], cfg["human"], cfg["profit"]
    if not (0 < s["floor_hard_min"] <= s["floor_pct"] < s["ceiling_pct"] <= s["ceiling_hard_max"] <= 1):
        errs.append("Need 0 < floor_hard_min <= floor_pct < ceiling_pct <= ceiling_hard_max <= 1.")
    if not (0 <= s["min_saving_pct"] < 1):
        errs.append("min_saving_pct must be between 0 and 1.")
    if 1 - s["min_saving_pct"] < s["floor_pct"]:
        errs.append("min_saving_pct is so large that even the opening offer would break the profit guard.")
    if s["step_pct"] <= 0 or not (0 < s["step_decay"] <= 1) or s["max_rounds"] < 1:
        errs.append("step_pct must be > 0, step_decay in (0,1], max_rounds >= 1.")
    if h["approval_above_inr"] > h["auto_settle_cap_inr"]:
        errs.append("approval_above_inr should not be higher than auto_settle_cap_inr.")
    if h["hyper_inflation_factor"] <= 1:
        errs.append("hyper_inflation_factor must be > 1.")
    if not (0 <= h["audit_sample_rate"] <= 1):
        errs.append("audit_sample_rate must be between 0 and 1.")
    if not (0 < p["max_loss_ratio"] <= 1.5):
        errs.append("max_loss_ratio looks wrong (expected 0-1.5).")
    return errs


def load_config(path: str | None = CONFIG_PATH, overrides: dict | None = None) -> dict:
    """DEFAULT_CONFIG  <-  settlement_config.json  <-  overrides (e.g. the admin sidebar)."""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg = deep_merge(cfg, json.load(f))
    if overrides:
        cfg = deep_merge(cfg, overrides)
    errs = validate_config(cfg)
    if errs:
        raise ValueError("Invalid settlement config: " + " | ".join(errs))
    return cfg


# --------------------------------------------------------------------------------------
# 2. PRICE DATASET  (vehicle type x make x segment x part)
# --------------------------------------------------------------------------------------
def _n(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _part(s) -> str:
    return re.sub(r"[\s\-]+", "_", str(s or "").strip().lower())


class Catalog:
    """Loads parts_catalog.csv. Columns:
    vehicle_type, make, segment, part, part_cost_inr, labour_hours, material, repairable, structural
    make/segment may be '*' (wildcard = generic price used when the exact make is unknown).
    The file is re-read automatically when it changes on disk."""

    def __init__(self, path: str = CATALOG_PATH, aliases: dict | None = None):
        self.path, self.aliases, self._mtime, self.rows = path, aliases or {}, None, {}
        self.load()

    def load(self):
        rows = {}
        with open(self.path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                key = (_n(r["vehicle_type"]), _n(r["make"]), _n(r["segment"]), _part(r["part"]))
                rows[key] = {
                    "part_cost": float(r["part_cost_inr"]), "labour_hours": float(r["labour_hours"]),
                    "material": _n(r.get("material") or "metal"),
                    "repairable": _n(r.get("repairable") or "yes") in ("yes", "y", "true", "1"),
                    "structural": _n(r.get("structural") or "no") in ("yes", "y", "true", "1"),
                }
        self.rows, self._mtime = rows, os.path.getmtime(self.path)

    def reload_if_changed(self):
        try:
            if os.path.getmtime(self.path) != self._mtime:
                self.load()
        except OSError:
            pass

    def norm_make(self, make) -> str:
        m = _n(make)
        return self.aliases.get(m, m)

    def vehicle_types(self):
        return sorted({k[0] for k in self.rows})

    def makes(self, vtype):
        return sorted({k[1] for k in self.rows if k[0] == _n(vtype) and k[1] != "*"})

    def segments(self, vtype):
        return sorted({k[2] for k in self.rows if k[0] == _n(vtype) and k[2] != "*"})

    def parts(self, vtype):
        return sorted({k[3] for k in self.rows if k[0] == _n(vtype)})

    def lookup(self, vtype, make, segment, part):
        """Most specific match wins. Returns (row, level) or (None, 'missing').
        level: exact | make_generic | segment_generic | generic.  Anything but 'exact'/'make_generic'
        means the make was not in the dataset (generic price used)."""
        t, m, s, p = _n(vtype), self.norm_make(make), _n(segment), _part(part)
        for key, level in (((t, m, s, p), "exact"), ((t, m, "*", p), "make_generic"),
                           ((t, "*", s, p), "segment_generic"), ((t, "*", "*", p), "generic")):
            if key in self.rows:
                return self.rows[key], level
        return None, "missing"


_CATALOGS = {}


def get_catalog(cfg: dict | None = None, path: str = CATALOG_PATH) -> Catalog:
    """Shared, auto-reloading catalog (the CSV is only re-read when the file changes)."""
    cat = _CATALOGS.get(path)
    if cat is None:
        cat = _CATALOGS[path] = Catalog(path, (cfg or DEFAULT_CONFIG)["make_aliases"])
    cat.reload_if_changed()
    return cat


# --------------------------------------------------------------------------------------
# 3. INPUT OBJECTS
# --------------------------------------------------------------------------------------
@dataclass
class Vehicle:
    vtype: str = "car"            # car | suv | bike | scooter
    make: str = ""                # maruti suzuki, honda, mg ...
    segment: str = "economy"      # economy | mid | premium
    model: str = ""
    age_months: int = 36
    idv: float | None = None      # insured declared value; estimated from config if None
    reg_no: str = ""


@dataclass
class Policy:
    active: bool = True
    cover: str = "comprehensive"  # comprehensive | third_party
    zero_dep: bool = False        # zero-depreciation add-on
    excluded_parts: list = field(default_factory=list)
    voluntary_excess: float = 0.0
    excess_override: float | None = None
    claim_limit: float = 50000.0
    premium_annual: float | None = None   # optional: enables the loss-ratio profit guard


@dataclass
class History:
    vehicle_claims_12m: int = 0
    vehicle_claims_36m: int = 0
    owner_claims_36m: int = 0
    total_paid_36m: float = 0.0
    fraud_flag: bool = False
    prior_parts: list = field(default_factory=list)   # [{"part": "bumper", "days_ago": 90}]


@dataclass
class Findings:
    items: list = field(default_factory=list)   # [{"part","type","severity","repairable"}]
    confidence: float = 100.0
    quality_ok: bool = True
    notes: str = ""
    added: int = 0                    # parts found only on the AI's second look
    claimed_part: str | None = None   # what the customer says is damaged
    extra_flags: list = field(default_factory=list)   # e.g. "Duplicate photo"

    @staticmethod
    def from_vision(f: dict, claimed_part=None, extra_flags=()):
        raw = f.get("items") or ([f] if f.get("part") else [])
        return Findings(items=raw, confidence=float(f.get("confidence", 0) or 0), quality_ok=bool(f.get("quality_ok", True)),
                        notes=str(f.get("notes", "")), added=int(f.get("added", 0) or 0),
                        claimed_part=claimed_part, extra_flags=list(extra_flags))


def load_history(reg_no: str, path: str = HISTORY_PATH) -> History | None:
    """Look a registration number up in claim_history.csv (None if unknown)."""
    if not reg_no or not os.path.exists(path):
        return None
    key = re.sub(r"[\s\-]", "", reg_no).upper()
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if re.sub(r"[\s\-]", "", r["reg_no"]).upper() == key:
                parts = []
                for tok in filter(None, (r.get("prior_parts") or "").split(";")):
                    p, _, d = tok.partition(":")
                    parts.append({"part": _part(p), "days_ago": int(d or 0)})
                return History(int(r["vehicle_claims_12m"]), int(r["vehicle_claims_36m"]), int(r["owner_claims_36m"]),
                               float(r["total_paid_36m"]), _n(r["fraud_flag"]) in ("yes", "true", "1"), parts)
    return None


# --------------------------------------------------------------------------------------
# 4. HELPERS
# --------------------------------------------------------------------------------------
def inr(n) -> str:
    """Rupees with Indian digit grouping: 1,23,456."""
    v = int(round(n or 0)); digits = str(abs(v))
    if len(digits) > 3:
        digits = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", digits[:-3]) + "," + digits[-3:]
    return ("-" if v < 0 else "") + "\u20b9" + digits


def pretty(part: str) -> str:
    return str(part).replace("_", " ")


def age_text(months: int) -> str:
    y, m = divmod(max(0, int(months)), 12)
    return " ".join(x for x in (f"{y} year{'s' if y != 1 else ''}" if y else "", f"{m} month{'s' if m != 1 else ''}" if m else "") if x) or "under a month"


def floor_to(x: float, step: int) -> float:
    return float(math.floor(max(0.0, x) / step) * step) if step > 1 else round(max(0.0, x), 2)


def dep_pct(material: str, age_months: int, zero_dep: bool, cfg: dict) -> float:
    if zero_dep and cfg["zero_dep_addon_removes_depreciation"]:
        return 0.0
    spec = cfg["depreciation"].get(material, cfg["depreciation"]["metal"])
    if isinstance(spec, (int, float)):
        return float(spec)
    for max_m, pct in spec:
        if age_months <= max_m:
            return float(pct)
    return float(spec[-1][1])


def estimate_idv(v: Vehicle, cfg: dict) -> float:
    if v.idv:
        return float(v.idv)
    base = cfg["default_idv_new"].get(_n(v.vtype), cfg["default_idv_new"]["car"]).get(_n(v.segment), 500000)
    dep = next((p for m, p in cfg["idv_age_schedule"] if v.age_months <= m), cfg["idv_age_schedule"][-1][1])
    return base * (1 - dep)


# --------------------------------------------------------------------------------------
# 5. THE QUOTE: description of damage  ->  money
# --------------------------------------------------------------------------------------
@dataclass
class Quote:
    vehicle: dict
    policy: dict
    items: list            # one dict per damaged part, with every rupee shown
    gross: float           # garage cost before depreciation (covered parts only)
    dep_total: float
    subtotal: float        # after depreciation
    excess: float
    net: float             # AMOUNT PAYABLE under the policy = what the insurer would pay a garage
    floor_pct: float
    ceiling_pct: float
    opening: float         # opening cash offer
    max_amount: float      # the ABSOLUTE most the engine may agree to alone (every guard applied)
    hyper_amount: float    # a demand above this is "hyper-inflated" -> human
    idv: float
    flags: list            # [{"code","text","public"}]  any flag -> human adjuster
    reasons: list          # [{"code","text","amount"}]  customer-facing explanation
    risk: int
    confidence: float
    decision: str          # "negotiate" | "human"
    notes: str = ""
    band: list = field(default_factory=lambda: [0.0, 0.0])
    adjustments: list = field(default_factory=list)   # how the floor/ceiling were moved, and why
    insurer_saving_at_max: float = 0.0

    def to_dict(self):
        return asdict(self)


def build_quote(vehicle: Vehicle, policy: Policy, history: History, findings: Findings,
                cfg: dict | None = None, catalog: Catalog | None = None) -> Quote:
    cfg = cfg or load_config()
    catalog = catalog or get_catalog(cfg)
    catalog.reload_if_changed()
    S, H, P = cfg["settlement"], cfg["human"], cfg["profit"]
    vt = _n(vehicle.vtype)
    rate = cfg["labour_rate_per_hour"].get(vt, cfg["labour_rate_per_hour"]["default"])
    flags, reasons, adjustments = [], [], []

    def flag(code, text, public=None):
        flags.append({"code": code, "text": text, "public": public or "This claim needs a human adjuster to review it."})

    def why(code, text, amount=0.0):
        reasons.append({"code": code, "text": text, "amount": round(float(amount), 2)})

    # ---- clean up what the AI described ------------------------------------------------
    merged = {}
    for it in findings.items:
        part = _part(it.get("part"))
        sev = _n(it.get("severity"))
        typ = _n(it.get("type"))
        if sev not in SEV_RANK:
            sev = "moderate"; flag("UNCLEAR_SEVERITY", f"AI gave an unreadable severity for {pretty(part)}; treated as moderate.")
        if typ not in cfg["damage_type_mult"]:
            typ = "dent"
        rep = it.get("repairable")
        rep = None if rep is None else (rep if isinstance(rep, bool) else _n(rep) in ("yes", "true", "1", "y"))
        cur = merged.get(part)
        if cur is None or SEV_RANK[sev] > SEV_RANK[cur["severity"]]:
            merged[part] = {"part": part, "type": typ, "severity": sev, "repairable": rep if cur is None or rep is False else cur["repairable"]}
        elif rep is False:
            cur["repairable"] = False
    parts_seen = list(merged)

    # ---- policy-level cover ------------------------------------------------------------
    whole_claim_excluded = False
    if not policy.active:
        flag("POLICY_LAPSED", "Policy is not active; a human must confirm cover.", "Your policy status needs to be confirmed by a human adjuster.")
    if _n(policy.cover) != "comprehensive":
        whole_claim_excluded = True
        flag("NOT_COVERED_POLICY", "Policy is third-party only: own-vehicle damage is not covered.",
             "Your policy type does not cover damage to your own vehicle. A human adjuster will confirm this and explain your options.")
        why("NOT_COVERED", "Your policy is third-party only, which does not cover damage to your own vehicle.")
    excluded = {_part(p) for p in cfg["default_excluded_parts"]} | {_part(p) for p in policy.excluded_parts}

    # ---- price every part ---------------------------------------------------------------
    lines, fallback_used, structural = [], False, False
    for part, it in merged.items():
        row, level = catalog.lookup(vt, vehicle.make, vehicle.segment, part)
        line = {"part": part, "type": it["type"], "severity": it["severity"], "ai_repairable": it["repairable"],
                "action": "none", "covered": True, "part_value": 0.0, "labour": 0.0, "dep_pct": 0.0, "dep_amount": 0.0,
                "line_total": 0.0, "price_match": level, "note": ""}
        if row is None:
            line.update(covered=False, action="unpriced", note="not in price list")
            flag("UNKNOWN_PART", f"'{pretty(part)}' is not in the price list for a {vt}.", "One of the damaged parts needs to be priced by a human adjuster.")
            lines.append(line); continue
        if level not in ("exact", "make_generic"):
            fallback_used = True
        if row["structural"]:
            structural = True
        if part in excluded or whole_claim_excluded:
            line.update(covered=False, action="excluded", note="not covered")
            if part in excluded:
                why("NOT_COVERED", f"{pretty(part).capitalize()} is not covered under your policy, so nothing is paid for it.")
            lines.append(line); continue
        tmult = cfg["damage_type_mult"][it["type"]]
        sev = cfg["severity"][it["severity"]]
        replace_cost = row["part_cost"] + row["labour_hours"] * rate * cfg["replace_labour_mult"]
        can_repair = row["repairable"] and it["repairable"] is not False
        action = "replace"
        if can_repair:
            mat = row["part_cost"] * sev["repair_material_fraction"] * tmult
            lab = row["labour_hours"] * rate * sev["labour_mult"] * tmult
            if mat + lab <= cfg["repair_cap_vs_replace"] * replace_cost:
                action = "repair"; line.update(part_value=mat, labour=lab, note="repairable: priced as repair (labour + paint/material)")
            else:
                line["note"] = "repair would cost almost as much as a new part: priced as replacement"
        else:
            line["note"] = ("the AI judged it not repairable" if it["repairable"] is False else "this part type is normally replaced, not repaired")
        if action == "replace":
            pct = dep_pct(row["material"], vehicle.age_months, policy.zero_dep, cfg)
            line.update(part_value=row["part_cost"], labour=row["labour_hours"] * rate * cfg["replace_labour_mult"],
                        dep_pct=pct, dep_amount=row["part_cost"] * pct)
        line["action"] = action
        line["line_total"] = line["part_value"] + line["labour"] - line["dep_amount"]
        for k in ("part_value", "labour", "dep_amount", "line_total"):
            line[k] = round(line[k], 2)
        lines.append(line)

    covered = [l for l in lines if l["covered"] and l["action"] in ("repair", "replace")]
    gross = sum(l["part_value"] + l["labour"] for l in covered)
    dep_total = sum(l["dep_amount"] for l in covered)
    subtotal = gross - dep_total

    # ---- reasons (customer-facing) --------------------------------------------------------
    for l in covered:
        nm = pretty(l["part"]).capitalize()
        if l["action"] == "repair":
            why("REPAIR", f"{nm} ({l['severity']} {l['type']}) can be repaired, so the cost is a repair (labour {inr(l['labour'])} + material {inr(l['part_value'])}) rather than a new part.", l["line_total"])
        else:
            why("REPLACE", f"{nm} ({l['severity']} {l['type']}) needs a new part ({l['note']}): part {inr(l['part_value'])} + fitting {inr(l['labour'])}.", l["line_total"])
    if dep_total > 0:
        why("AGE_DEPRECIATION", f"Your vehicle is {age_text(vehicle.age_months)} old, so replaced parts are depreciated by {inr(dep_total)} in total "
            f"(metal parts follow an age schedule; plastic and rubber parts depreciate more).", -dep_total)
    elif any(l["action"] == "replace" for l in covered):
        why("NO_DEPRECIATION", "No depreciation was deducted (your zero-depreciation cover, or glass parts, or a very new vehicle).")

    default_x = cfg["default_excess"].get(vt, cfg["default_excess"]["default"])
    excess = (policy.excess_override if policy.excess_override is not None else default_x) + policy.voluntary_excess
    net = max(0.0, subtotal - excess) if subtotal > 0 else 0.0
    if subtotal > 0:
        why("EXCESS", f"Your policy excess of {inr(min(excess, subtotal))} is deducted, as it is on every claim.", -min(excess, subtotal))

    # ---- settlement band: age + history, then profit guards -------------------------------------
    floor_pct, ceil_pct = S["floor_pct"], S["ceiling_pct"]
    band = next((b for b in S["age_bands"] if vehicle.age_months <= b["max_age_months"]), S["age_bands"][-1])
    floor_pct += band["floor_delta"]; ceil_pct += band["ceiling_delta"]
    if band["ceiling_delta"]:
        adjustments.append({"code": "AGE_BAND", "text": f"{band['label']}: ceiling {band['ceiling_delta']*100:+.0f}%, opening {band['floor_delta']*100:+.0f}%"})
        why("AGE_SETTLEMENT", f"Because this is an {band['label']}, the cash settlement is " + ("slightly higher" if band["ceiling_delta"] > 0 else "lower") + " than for an average-age vehicle.")
    hs = S["history"]
    if history.vehicle_claims_12m > 0:
        pen = min(hs["max_penalty"], hs["per_claim_12m_penalty"] * history.vehicle_claims_12m)
        floor_pct -= pen; ceil_pct -= pen
        adjustments.append({"code": "CLAIM_HISTORY", "text": f"{history.vehicle_claims_12m} claim(s) in 12 months: both limits -{pen*100:.0f}%"})
        why("CLAIM_HISTORY", f"There {'was' if history.vehicle_claims_12m == 1 else 'were'} {history.vehicle_claims_12m} earlier claim(s) on this vehicle in the last 12 months, which lowers the cash offer.")
    elif history.vehicle_claims_36m == 0 and history.owner_claims_36m == 0:
        floor_pct += hs["clean_36m_bonus"]; ceil_pct += hs["clean_36m_bonus"]
        adjustments.append({"code": "CLEAN_HISTORY", "text": f"No claims in 36 months: both limits +{hs['clean_36m_bonus']*100:.0f}%"})
        why("CLEAN_HISTORY", "You have no claims in the last 3 years, which improves your cash offer.")
    ceil_pct = min(ceil_pct, S["ceiling_hard_max"])
    floor_pct = max(floor_pct, S["floor_hard_min"])
    # PROFIT GUARD 1: the insurer must keep at least min_saving_pct of the garage-route cost
    guard1 = 1 - S["min_saving_pct"]
    if ceil_pct > guard1:
        adjustments.append({"code": "PROFIT_MIN_SAVING", "text": f"ceiling cut from {ceil_pct*100:.0f}% to {guard1*100:.0f}% so the insurer saves at least {S['min_saving_pct']*100:.0f}% vs the garage route"})
        ceil_pct = guard1
    floor_pct = min(floor_pct, ceil_pct - 0.02)
    max_amount = floor_to(net * ceil_pct, S["round_down_to"])
    opening = floor_to(net * floor_pct, S["round_down_to"])
    # PROFIT GUARD 2: loss ratio on this policy
    if policy.premium_annual:
        budget = policy.premium_annual * P["premium_years"] * P["max_loss_ratio"]
        remaining = budget - history.total_paid_36m
        if remaining < net * S["floor_hard_min"]:
            flag("PROFIT_LOSS_RATIO", f"Paying even {S['floor_hard_min']*100:.0f}% would push this policy's loss ratio above {P['max_loss_ratio']*100:.0f}% "
                 f"(budget {inr(budget)}, already paid {inr(history.total_paid_36m)}).", "This claim needs a human adjuster to review it.")
        elif max_amount > remaining:
            adjustments.append({"code": "PROFIT_LOSS_RATIO", "text": f"maximum cut from {inr(max_amount)} to {inr(remaining)} to keep this policy's loss ratio under {P['max_loss_ratio']*100:.0f}%"})
            max_amount = floor_to(remaining, S["round_down_to"])
        opening = min(opening, floor_to(max_amount * 0.95, S["round_down_to"]))
    opening = min(opening, max_amount)
    idv = estimate_idv(vehicle, cfg)

    # ---- human-review flags ----------------------------------------------------------------------
    conf = findings.confidence
    if not findings.quality_ok:
        flag("PHOTO_QUALITY", "Photo is blurry, dark, cropped or not a vehicle.", "The photo is not clear enough. Please upload a clearer photo, or a human adjuster will help.")
    if conf < H["min_ai_confidence"]:
        flag("LOW_CONFIDENCE", f"AI confidence {conf:.0f}% is below the {H['min_ai_confidence']}% threshold.")
    if H["severe_to_human"] and any(l["severity"] == "severe" for l in lines):
        flag("SEVERE_DAMAGE", "Severe damage needs a human inspection.", "Severe damage needs an in-person inspection by a human adjuster.")
    if H["structural_to_human"] and structural:
        flag("STRUCTURAL", "A structural part (frame/roof/fork) is damaged; safety-critical.", "Structural damage needs an in-person inspection by a human adjuster.")
    if fallback_used and H["fallback_pricing_to_human"]:
        flag("FALLBACK_PRICING", f"No price data for make '{vehicle.make}'; generic prices were used.", "Your vehicle's parts prices need to be confirmed by a human adjuster.")
    if findings.claimed_part and _part(findings.claimed_part) not in parts_seen:
        flag("PART_MISMATCH", f"Customer claimed '{findings.claimed_part}' but the photo shows {', '.join(pretty(p) for p in parts_seen) or 'no damaged part'}.",
             "The damaged part you named does not match the photo, so a human adjuster will check.")
    if findings.added >= H["unstable_extra_parts"]:
        flag("UNSTABLE_AI", f"AI found {findings.added} more damaged parts on a second look: first answer was unstable.")
    for e in findings.extra_flags:
        flag("EXTRA", str(e), "This claim needs a human adjuster to review it.")
    if not parts_seen:
        flag("NO_DAMAGE_FOUND", "The AI found no damaged part.", "No damage could be identified, so a human adjuster will review it.")
    if covered and net <= 0:
        flag("BELOW_EXCESS", f"Repair value {inr(subtotal)} does not exceed the excess {inr(excess)}: nothing payable.",
             f"The assessed repair value is not more than your policy excess ({inr(excess)}), so no payment is due. A human adjuster will confirm this.")
        why("BELOW_EXCESS", "After depreciation, the repair value is not more than your policy excess, so no cash payment is due.")
    if parts_seen and not covered and not whole_claim_excluded and all(l["action"] == "excluded" for l in lines):
        flag("ALL_EXCLUDED", "Every damaged part is excluded from the policy.", "The damaged part(s) are not covered by your policy. A human adjuster will confirm this.")
    if net > H["auto_settle_cap_inr"]:
        flag("OVER_CAP", f"Amount payable {inr(net)} exceeds the auto-settle cap {inr(H['auto_settle_cap_inr'])}.", "This claim value needs a human adjuster's approval.")
    if subtotal > policy.claim_limit:
        flag("OVER_CLAIM_LIMIT", f"Repair estimate {inr(subtotal)} exceeds the policy claim limit {inr(policy.claim_limit)}.", "This claim needs a human adjuster to review it.")
    if gross > idv * H["total_loss_ratio_of_idv"]:
        flag("POSSIBLE_TOTAL_LOSS", f"Garage cost {inr(gross)} is over {H['total_loss_ratio_of_idv']*100:.0f}% of the vehicle's declared value {inr(idv)}.", "Possible total loss: a human adjuster will inspect the vehicle.")
    if history.vehicle_claims_12m >= H["max_claims_12m_auto"]:
        flag("CLAIM_FREQUENCY", f"{history.vehicle_claims_12m} claims on this vehicle in the last 12 months.")
    if history.fraud_flag:
        flag("FRAUD_FLAG", "Vehicle/owner carries a fraud flag from a previous claim.", "This claim needs a human adjuster to review it.")
    for p in history.prior_parts:
        if _part(p.get("part")) in parts_seen and int(p.get("days_ago", 9999)) <= H["repeat_part_days"]:
            flag("REPEAT_PART", f"'{pretty(p['part'])}' was already claimed {p['days_ago']} days ago (limit {H['repeat_part_days']}): possibly unrepaired earlier damage.",
                 "A similar claim was recently made, so a human adjuster will review it.")
            break

    risk = min(100, round((100 - conf) * 0.5 + len(flags) * 20))
    band_est = (100 - conf) / 200
    why("CASH_BASIS", "A cash settlement is paid instantly with no garage wait or surveyor visit, so it is offered as a share of the garage-route amount "
        f"(your amount payable is {inr(net)}).")
    return Quote(vehicle=asdict(vehicle), policy=asdict(policy), items=lines, gross=round(gross, 2), dep_total=round(dep_total, 2),
                 subtotal=round(subtotal, 2), excess=round(excess, 2), net=round(net, 2), floor_pct=round(floor_pct, 4), ceiling_pct=round(ceil_pct, 4),
                 opening=opening, max_amount=max_amount,
                 hyper_amount=round(max(net * H["hyper_inflation_factor"], net + H["hyper_min_abs_gap_inr"]), 2),
                 idv=round(idv, 2), flags=flags, reasons=reasons, risk=risk, confidence=conf,
                 decision="human" if flags else "negotiate", notes=findings.notes,
                 band=[round(net * (1 - band_est), 2), round(net * (1 + band_est), 2)], adjustments=adjustments,
                 insurer_saving_at_max=round(net - max_amount, 2))


# --------------------------------------------------------------------------------------
# 6. READING THE CUSTOMER'S MESSAGE
# --------------------------------------------------------------------------------------
_DEVANAGARI = str.maketrans("\u0966\u0967\u0968\u0969\u096a\u096b\u096c\u096d\u096e\u096f", "0123456789")
_UNIT_AFTER = r"(?:%|(?:days?|months?|years?|yrs?|km|kms|percent|per\s?cent|hours?|hrs?|times|rounds?|am|pm|st|nd|rd|th|kg)\b)"
_AMT_RE = re.compile(r"(?P<cur>\u20b9|rs\.?|inr)?\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<mul>k\b|thousand\b|lakhs?\b|lacs?\b|crores?\b|cr\b)?", re.I)
_MULT = {"k": 1e3, "thousand": 1e3, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "crore": 1e7, "crores": 1e7, "cr": 1e7}


def parse_amounts(text: str) -> list:
    """Rupee amounts in a message: '25,000', 'Rs 25k', '1.5 lakh', '\u20b92000'. Ignores '3 days', '10%', '2nd'."""
    t = text.translate(_DEVANAGARI)
    out = []
    for m in _AMT_RE.finditer(t):
        tail = t[m.end():m.end() + 14]
        if not m.group("cur") and not m.group("mul") and re.match(r"\s*" + _UNIT_AFTER, tail, re.I):
            continue
        try:
            v = float(m.group("num").replace(",", ""))
        except ValueError:
            continue
        if m.group("mul"):
            v *= _MULT[m.group("mul").lower()]
        if v > 0 and not any(abs(v - o) < 1 for o in out):
            out.append(v)
    return out


INJECT = r"ignore (all|previous|your|the)|system prompt|you are now|override|pretend|jailbreak|developer mode|reveal (your|the) (rules|limit|ceiling|maximum)|what is your (limit|ceiling|maximum)|act as"
ANGRY = r"lawyer|legal action|sue\b|court|ombudsman|irdai|consumer forum|consumer court|cheat|fraud|scam|useless|worst|angry|complaint|police|media"
ACCEPT = r"\b(accept|accepted|agree|agreed|deal|ok|okay|fine|yes|yep|yeah|sure|done|confirm|confirmed|theek hai|thik hai|haan|ha|manzoor)\b"
REJECT = r"\b(no|not|nahi|nahin|never|reject|refuse|too low|low|insufficient|unfair|less)\b"
WHY = r"\b(why|reason|reasons|explain|breakdown|break down|how come|how did|justify|basis|kyun|kyu|kaise|details?)\b"


# --------------------------------------------------------------------------------------
# 7. THE NEGOTIATION  (code decides every number)
# --------------------------------------------------------------------------------------
class GuardrailViolation(Exception):
    """Raised only if a bug tries to commit an amount above the engine's authority."""


class Negotiator:
    STATUSES = ("negotiating", "settled", "pending_approval", "escalated", "approved_by_human", "rejected_by_human")

    def __init__(self, quote: Quote, cfg: dict | None = None, claim_id: str = ""):
        self.cfg = cfg or load_config()
        self.q, self.claim_id = quote, claim_id
        self.status = "negotiating" if quote.decision == "negotiate" else "escalated"
        self.offer, self.round, self.messages = quote.opening, 0, 0
        self.final_offer_made, self.amount, self.transcript, self.audit, self.flushed = False, None, [], [], 0
        self.human = None
        self.escalation_reasons = [f["text"] for f in quote.flags] if self.status == "escalated" else []
        self._log("QUOTE", {"net": quote.net, "opening": quote.opening, "max": quote.max_amount, "risk": quote.risk},
                  "NEGOTIATE" if self.status == "negotiating" else "ESCALATE_TO_HUMAN",
                  "no flags" if not quote.flags else "; ".join(f"[{f['code']}] {f['text']}" for f in quote.flags))

    # ---- audit ---------------------------------------------------------------------------------
    def _log(self, step, inp, decision, reason, actor="engine"):
        self.audit.append({"ts": datetime.now().isoformat(timespec="seconds"), "claim": self.claim_id, "actor": actor,
                           "step": step, "input": inp, "decision": decision, "reason": reason})

    def new_audit_events(self):
        ev = self.audit[self.flushed:]
        self.flushed = len(self.audit)
        return ev

    # ---- messages ---------------------------------------------------------------------------------
    def _reasons_text(self, limit=None):
        rs = [r["text"] for r in self.q.reasons]
        return "\n".join("- " + t for t in (rs[:limit] if limit else rs))

    def explain(self) -> str:
        q = self.q
        return (f"Here is how I got there. Garage repair {inr(q.gross)}, minus depreciation {inr(q.dep_total)}, minus policy excess {inr(min(q.excess, q.subtotal))} "
                f"= amount payable {inr(q.net)}.\n{self._reasons_text()}")

    def opening_message(self) -> str:
        q = self.q
        if self.status == "escalated":
            return self.handover_message()
        return (f"Hello! I\u2019ve reviewed your photos. Assessed garage repair is {inr(q.gross)}; after depreciation and your policy excess, the amount payable is {inr(q.net)}.\n"
                f"If you take cash instead of a garage repair, I can offer {inr(self.offer)} right now, paid instantly.\n\nHow this was worked out:\n{self._reasons_text()}\n\n"
                "Reply with the amount you have in mind, or ask me to explain any line.")

    def handover_message(self) -> str:
        pubs = []
        for f in self.q.flags:
            if f["public"] not in pubs:
                pubs.append(f["public"])
        extra = [t for t in self.escalation_reasons if t not in [f["text"] for f in self.q.flags]]
        body = " ".join(pubs) if pubs else "A human adjuster will review your claim."
        if extra and not pubs:
            body = "A human adjuster will review your claim."
        return body + " I\u2019m passing your claim to a human adjuster, who will contact you. Your claim reference is kept in the audit trail."

    # ---- guardrail firewall -------------------------------------------------------------------------
    def _safe(self, amount: float) -> float:
        if amount > self.q.max_amount + 0.005:
            self._log("GUARDRAIL", {"attempted": amount, "max": self.q.max_amount}, "BLOCKED", "amount above engine authority")
            raise GuardrailViolation(f"{amount} > {self.q.max_amount}")
        return amount

    def _escalate(self, code: str, internal: str, customer: str) -> str:
        self.status = "escalated"
        self.escalation_reasons.append(f"[{code}] {internal}")
        self._log("ESCALATE", {"round": self.round, "offer": self.offer}, "HUMAN_TAKEOVER", f"[{code}] {internal}")
        return f"{customer} I\u2019m passing your claim to a human adjuster, who will contact you."

    def _close(self, amount: float, how: str) -> str:
        amount = self._safe(amount)
        H = self.cfg["human"]
        sampled = int(hashlib.sha256(f"{self.claim_id}|{amount}".encode()).hexdigest(), 16) % 10000 < H["audit_sample_rate"] * 10000
        self.amount = amount
        if amount > H["approval_above_inr"] or sampled:
            self.status = "pending_approval"
            why = (f"deal {inr(amount)} is above the human-approval threshold {inr(H['approval_above_inr'])}" if amount > H["approval_above_inr"]
                   else "randomly selected for human audit")
            self._log("DEAL_AGREED", {"amount": amount, "how": how}, "PENDING_HUMAN_APPROVAL", why)
            return (f"We have agreed on {inr(amount)} ({how}). Because of the amount, a human adjuster must approve it before payment. "
                    "You\u2019ll be contacted shortly.") if amount > H["approval_above_inr"] else (
                    f"We have agreed on {inr(amount)} ({how}). A human adjuster will do a quick routine check before payment is released.")
        self.status = "settled"
        self._log("DEAL_AGREED", {"amount": amount, "how": how}, "SETTLED", "within engine authority, no review required")
        return f"Deal confirmed: {inr(amount)} cash settlement ({how}). Payment can be released now."

    # ---- the conversation ---------------------------------------------------------------------------
    def accept_current_offer(self) -> str:
        if self.status != "negotiating":
            return "This claim is no longer being negotiated."
        self._log("CUSTOMER", {"text": "[clicked Accept current offer]"}, "ACCEPT", f"at {inr(self.offer)}")
        return self._close(self.offer, "your acceptance of my offer")

    def reply(self, text: str) -> str:
        if self.status != "negotiating":
            return "This claim is no longer in automatic negotiation."
        text = (text or "").strip()[:500]
        self.messages += 1
        self.transcript.append(("customer", text))
        self._log("CUSTOMER", {"text": text}, "RECEIVED", f"round {self.round}, offer {inr(self.offer)}")
        out = self._decide(text)
        self.transcript.append(("engine", out))
        return out

    def _decide(self, text: str) -> str:
        q, S, H = self.q, self.cfg["settlement"], self.cfg["human"]
        low = text.lower()
        if re.search(INJECT, low):
            return self._escalate("INJECTION", "Message tried to change or extract the bot's rules.", "Your message looks like an attempt to change or probe my rules, so it has been flagged.")
        if re.search(ANGRY, low):
            return self._escalate("DISTRESS_OR_LEGAL", "Angry or legal-threat language.", "I can hear this is frustrating, and a person should handle it.")
        if self.messages > S["max_messages"]:
            return self._escalate("TOO_MANY_MESSAGES", "Chat exceeded the maximum number of messages.", "We have been going back and forth for a while.")
        amounts = parse_amounts(text)
        note = ""
        known = {i["part"] for i in q.items}
        for p in self._catalog_parts():
            if re.search(r"\b" + re.escape(pretty(p)) + r"s?\b", low) and p not in known:
                note = (f" I can only price damage that was assessed from your photos ({', '.join(pretty(k) for k in sorted(known))}). "
                        "If your {0} is also damaged, please upload another photo so it can be assessed; I can\u2019t add parts through chat.".format(pretty(p)))
                self._log("CHAT_NEW_PART", {"part": p}, "IGNORED", "amounts only change through assessed evidence")
                break
        if re.search(WHY, low) and not amounts:
            self._log("EXPLAIN", {}, "REASONS_SHOWN", "customer asked why")
            return self.explain() + f"\n\nMy current offer is {inr(self.offer)}." + note
        if len(amounts) > 1:
            return f"I see more than one amount in your message ({', '.join(inr(a) for a in amounts)}). Which single figure do you want to settle at?" + note
        if not amounts:
            if re.search(ACCEPT, low) and not re.search(REJECT, low):
                return self._close(self.offer, "your acceptance of my offer")
            return (f"Could you tell me the amount you have in mind, in rupees? My current offer is {inr(self.offer)}." + note)
        return self._counter(amounts[0]) + note

    def _catalog_parts(self):
        try:
            return get_catalog(self.cfg).parts(self.q.vehicle["vtype"])
        except Exception:
            return []

    def _counter(self, ask: float) -> str:
        q, S = self.q, self.cfg["settlement"]
        self.round += 1
        gap = S["accept_gap_pct"] * q.net
        self._log("COUNTER", {"ask": ask, "round": self.round}, "EVALUATING", f"offer={inr(self.offer)} max={inr(q.max_amount)} hyper_above={inr(q.hyper_amount)}")
        if ask > q.hyper_amount:
            return self._escalate("HYPER_INFLATED", f"Customer demanded {inr(ask)}, {ask / max(q.net, 1):.1f}x the amount payable {inr(q.net)} (limit for auto-handling {inr(q.hyper_amount)}).",
                                  f"{inr(ask)} is far above the {inr(q.net)} that the assessment supports, and I\u2019m not able to negotiate an amount that far outside the claim value.")
        if ask <= self.offer:
            return self._close(ask if S["settle_at_customer_figure_if_lower"] else self.offer, "your figure is at or below my current offer")
        if ask <= q.max_amount and ask - self.offer <= gap:
            return self._close(ask, "your figure is within my approved range")
        if self.final_offer_made:
            return self._escalate("NO_AGREEMENT", f"Customer wants {inr(ask)} after the final offer of {inr(self.offer)}.",
                                  f"I\u2019m sorry we couldn\u2019t agree: my final offer of {inr(self.offer)} is the most I\u2019m allowed to approve.")
        if self.round >= S["max_rounds"] or self.offer >= q.max_amount:
            self.offer = self._safe(max(self.offer, q.max_amount)); self.final_offer_made = True
            self._log("OFFER", {"offer": self.offer, "round": self.round}, "FINAL_OFFER", "round limit reached or ceiling reached")
            return (f"My final offer is {inr(self.offer)}, the most I\u2019m allowed to approve for this claim. "
                    f"I can\u2019t go to {inr(ask)}: your amount payable after depreciation, excess and policy terms is {inr(q.net)}, and a cash settlement is always paid below that full amount. "
                    "Please accept it, or I can pass this to a human adjuster.")
        step = max(S["min_step_inr"], S["step_pct"] * q.net * (S["step_decay"] ** (self.round - 1)))
        new = floor_to(min(self.offer + step, q.max_amount, ask), S["round_down_to"])
        new = self._safe(max(self.offer, new))
        self._log("OFFER", {"offer": new, "round": self.round, "step": round(step, 2)}, "COUNTER_OFFER", f"conceded {inr(new - self.offer)}")
        self.offer = new
        if ask > q.max_amount:
            return (f"I can\u2019t go as high as {inr(ask)}. Your amount payable after depreciation and excess is {inr(q.net)}, and a cash settlement is always paid below the full "
                    f"garage-route amount because it skips the garage, surveyor and waiting costs. I can move to {inr(new)}.")
        return f"I can move to {inr(new)}. Would that work?"

    # ---- the human takes over --------------------------------------------------------------------------
    def human_decide(self, action: str, reviewer: str, note: str = "", amount: float | None = None) -> str:
        """action: 'approve' (accept the agreed/engine amount) | 'set_amount' (human's own figure) | 'reject'."""
        if self.status not in ("escalated", "pending_approval"):
            raise ValueError(f"Claim is '{self.status}', nothing for a human to decide.")
        if not reviewer.strip():
            raise ValueError("Reviewer name is required for the audit trail.")
        H = self.cfg["human"]
        if action == "approve":
            amt = self.amount if self.amount is not None else (amount if amount is not None else self.q.max_amount)
            if self.status == "escalated" and amount is None and self.amount is None:
                amt = self.q.opening if self.q.net > 0 else 0.0
            self.status, self.amount = "approved_by_human", float(amt)
        elif action == "set_amount":
            if amount is None or amount < 0:
                raise ValueError("A non-negative amount is required.")
            if amount > self.q.max_amount and H["override_requires_note"] and len(note.strip()) < 10:
                raise ValueError(f"{inr(amount)} is above the engine's limit {inr(self.q.max_amount)}: a written justification (10+ characters) is mandatory.")
            self.status, self.amount = "approved_by_human", float(amount)
        elif action == "reject":
            if len(note.strip()) < 5:
                raise ValueError("A rejection reason is required.")
            self.status, self.amount = "rejected_by_human", 0.0
        else:
            raise ValueError("Unknown action")
        over = self.amount > self.q.max_amount + 0.005
        self.human = {"reviewer": reviewer.strip(), "action": action, "note": note.strip(), "amount": self.amount, "override_above_engine_limit": over}
        self._log("HUMAN_DECISION", {"action": action, "amount": self.amount, "note": note.strip()},
                  self.status.upper(), f"reviewer {reviewer.strip()}" + (" - OVERRIDE ABOVE ENGINE LIMIT" if over else ""), actor="human:" + reviewer.strip())
        return self.status

    # ---- save / load ---------------------------------------------------------------------------------------
    def to_dict(self):
        return {"claim_id": self.claim_id, "quote": self.q.to_dict(), "status": self.status, "offer": self.offer, "round": self.round,
                "messages": self.messages, "final_offer_made": self.final_offer_made, "amount": self.amount, "transcript": self.transcript,
                "audit": self.audit, "flushed": self.flushed, "human": self.human, "escalation_reasons": self.escalation_reasons}

    @staticmethod
    def from_dict(d: dict, cfg: dict | None = None) -> "Negotiator":
        n = Negotiator.__new__(Negotiator)
        n.cfg, n.claim_id, n.q = cfg or load_config(), d["claim_id"], Quote(**d["quote"])
        n.status, n.offer, n.round, n.messages = d["status"], d["offer"], d["round"], d["messages"]
        n.final_offer_made, n.amount, n.transcript = d["final_offer_made"], d["amount"], [tuple(x) for x in d["transcript"]]
        n.audit, n.flushed, n.human, n.escalation_reasons = d["audit"], d["flushed"], d["human"], d["escalation_reasons"]
        return n
