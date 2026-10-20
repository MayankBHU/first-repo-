import hashlib, io, json, os, re, tempfile, time
import streamlit as st
from PIL import Image, ImageDraw
import core
import settlement_engine as E

st.set_page_config(page_title="Auto-Settle AI", layout="wide")
DB = "claims_db.json"  # persistent store: claim queue (with full engine snapshot), audit log, photo fingerprints
STATE = {"negotiating": "Open", "escalated": "Needs review", "pending_approval": "Needs approval", "settled": "Settled - awaiting payout",
         "approved_by_human": "Approved by adjuster", "rejected_by_human": "Rejected by adjuster"}

def db_load():
    try: return json.load(open(DB))
    except Exception: return {"queue": [], "log": [], "hashes": []}
def db_save(d): json.dump(d, open(DB, "w"), indent=1)
def log(ev, detail, claim=""):
    d = db_load(); d["log"].append({"t": time.strftime("%H:%M:%S"), "claim": claim, "ev": ev, "detail": detail}); db_save(d)

def flush(neg):
    """Copy the engine's new audit events (time, actor, step, input, decision, reason) into the persistent audit trail."""
    ev = neg.new_audit_events()
    if ev:
        d = db_load()
        for e in ev:
            d["log"].append({"t": e["ts"][11:], "claim": neg.claim_id, "ev": f"{e['step']} -> {e['decision']}", "detail": f"[{e['actor']}] {e['reason']} | input: {json.dumps(e['input'], default=str)}"})
        db_save(d)

def persist(neg, **extra):
    flush(neg); d = db_load(); q = next((x for x in d["queue"] if x["id"] == neg.claim_id), None)
    if q is None:
        q = {"id": neg.claim_id, "paid": 0}; d["queue"].append(q)
    q.update(state=STATE[neg.status], net=neg.q.net, snapshot=neg.to_dict())
    q["detail"] = ("ESCALATED: " + "; ".join(neg.escalation_reasons or [f["text"] for f in neg.q.flags])) if neg.status == "escalated" else (
        f"Agreed {E.inr(neg.amount)}, awaiting human approval" if neg.status == "pending_approval" else
        "Negotiating" if neg.status == "negotiating" else f"{STATE[neg.status]}: {E.inr(neg.amount)}")
    q.update(extra)
    if extra.get("state") in ("Auto-settled", "Paid after approval"): q["detail"] = f"{extra['state']}: {E.inr(neg.amount)}"
    db_save(d)

def to_jpeg(raw, is_video):
    if is_video:
        import cv2
        p = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4"); p.write(raw); p.close()
        cap = cv2.VideoCapture(p.name); cap.set(cv2.CAP_PROP_POS_FRAMES, cap.get(cv2.CAP_PROP_FRAME_COUNT) // 2)
        ok, fr = cap.read(); cap.release()
        if not ok: raise ValueError("Could not read the video")
        img = Image.fromarray(fr[:, :, ::-1])
    else: img = Image.open(io.BytesIO(raw)).convert("RGB")
    img.thumbnail((1280, 1280)); b = io.BytesIO(); img.save(b, "JPEG", quality=85); return img, b.getvalue()

base_cfg = E.load_config()
catalog = E.get_catalog(base_cfg)
B = base_cfg

# ---------- sidebar ----------
with st.sidebar:
    admin = st.checkbox("\U0001f527 Insurer / Admin mode", value=False)
    over, policy, manual_hist = {}, E.Policy(), None
    if admin:
        with st.expander("Settlement dials", expanded=True):
            S = B["settlement"]
            fl = st.number_input("Opening offer (% of amount payable)", 1, 100, int(round(S["floor_pct"] * 100)))
            ce = st.number_input("Ceiling: most the engine may agree (%)", 1, 100, int(round(S["ceiling_pct"] * 100)))
            ms = st.number_input("PROFIT GUARD: insurer must save at least (%)", 0, 50, int(round(S["min_saving_pct"] * 100)))
            stp = st.number_input("First concession (% of amount payable)", 1, 50, int(round(S["step_pct"] * 100)))
            mr = st.number_input("Final offer on round", 1, 10, int(S["max_rounds"]))
            over["settlement"] = {"floor_pct": fl / 100, "ceiling_pct": ce / 100, "min_saving_pct": ms / 100, "step_pct": stp / 100, "max_rounds": int(mr)}
        with st.expander("Human takeover thresholds", expanded=True):
            H = B["human"]
            cap = st.number_input("Amount payable above this -> human decides (\u20b9)", 0, 5000000, int(H["auto_settle_cap_inr"]))
            apv = st.number_input("Deal above this needs human approval (\u20b9)", 0, 5000000, int(H["approval_above_inr"]))
            hyp = st.number_input("Hyper-inflation: demand above x times payable", 1.05, 10.0, float(H["hyper_inflation_factor"]), 0.05)
            mc = st.number_input("Min AI confidence (%)", 0, 100, int(H["min_ai_confidence"]))
            smp = st.number_input("Random human audit of auto deals (%)", 0, 100, int(round(H["audit_sample_rate"] * 100)))
            over["human"] = {"auto_settle_cap_inr": cap, "approval_above_inr": apv, "hyper_inflation_factor": hyp, "min_ai_confidence": mc, "audit_sample_rate": smp / 100}
        with st.expander("Policy record"):
            allp = sorted({p for t in catalog.vehicle_types() for p in catalog.parts(t)})
            policy = E.Policy(active=st.selectbox("Status", ["Active", "Lapsed"]) == "Active", cover=st.selectbox("Cover", ["comprehensive", "third_party"]),
                              zero_dep=st.checkbox("Zero-depreciation add-on", False), excluded_parts=st.multiselect("Parts NOT covered by this policy", allp),
                              voluntary_excess=st.number_input("Voluntary excess (\u20b9)", 0, 100000, 0), claim_limit=st.number_input("Max claim limit (\u20b9)", 0, 5000000, 50000),
                              premium_annual=(st.number_input("Annual premium (\u20b9, 0 = unknown)", 0, 5000000, 0) or None))
        with st.expander("Claim history (override)"):
            if st.checkbox("Enter history manually instead of looking up the registration number"):
                manual_hist = E.History(st.number_input("Claims on vehicle, last 12 months", 0, 20, 0), st.number_input("Claims on vehicle, last 36 months", 0, 30, 0),
                                        st.number_input("Claims by owner, last 36 months", 0, 30, 0), st.number_input("Total paid, last 36 months (\u20b9)", 0, 5000000, 0),
                                        st.checkbox("Fraud flag on file", False), [])
    try: cfg = E.load_config(overrides=over)
    except ValueError as e: st.error(str(e)); cfg = base_cfg

    st.header("AI models")
    if "profiles" not in st.session_state:
        default_cfg = None
        try:
            s = st.secrets["ai"]
            default_cfg = {"type": s.get("type", "openai"), "model": s["model_name"], "base_url": s.get("base_url", ""), "key": s.get("api_key", "")}
        except Exception: pass
        st.session_state.profiles = {"Default": default_cfg} if default_cfg else {}
    if admin:
        with st.expander("Add a model to try", expanded=False):
            pn = st.text_input("Name (your label)", key="pn"); pt = st.selectbox("Type", ["openai-compatible", "anthropic", "ollama"], key="pt")
            pm = st.text_input("Model name (must support images)", key="pm"); pb = st.text_input("Base URL (not needed for anthropic)", key="pb")
            pk = st.text_input("API key (kept only in this session)", type="password", key="pk")
            if st.button("Add model") and pn and pm:
                st.session_state.profiles[pn] = {"type": "openai" if pt == "openai-compatible" else pt, "model": pm, "base_url": pb, "key": pk}
    if st.session_state.profiles:
        active = st.selectbox("Model used for claims", list(st.session_state.profiles)) if admin else list(st.session_state.profiles)[0]
        ai_cfg = st.session_state.profiles[active]
    else:
        if admin: st.warning("No AI model configured. Add one above, or set secrets in Streamlit Cloud. (You can still test everything with manual findings.)")
        ai_cfg = None

t1, t2, t3, t4, t5, t6 = st.tabs(["New claim", "Adjuster dashboard", "Audit trail", "Settlement rules & prices", "Assumptions", "Compare models"])

# ======================================== NEW CLAIM ========================================
with t1:
    st.title("Auto-Settle AI")
    st.caption("Upload a photo of your vehicle damage. We\u2019ll assess the damage, work out what the policy pays, and offer you an instant cash settlement with the reasons for every figure.")
    c1, c2, c3, c4 = st.columns(4)
    vtype = c1.selectbox("Vehicle type", catalog.vehicle_types(), index=catalog.vehicle_types().index("car") if "car" in catalog.vehicle_types() else 0)
    mk = c2.selectbox("Make", catalog.makes(vtype) + ["other (not listed)"])
    make = st.text_input("Type your make", key="mk2") if mk.startswith("other") else mk
    seg = c3.selectbox("Segment", catalog.segments(vtype), index=0)
    age_y = c4.number_input("Vehicle age (years)", 0.0, 40.0, 3.0, 0.5)
    d1, d2, d3 = st.columns(3)
    reg = d1.text_input("Registration number (optional)", placeholder="e.g. RJ14AB1234")
    model_name = d2.text_input("Model / variant (optional)")
    idv = d3.number_input("Insured declared value \u20b9 (0 = estimate)", 0, 50000000, 0)
    vehicle = E.Vehicle(vtype, make, seg, model_name, int(round(age_y * 12)), float(idv) or None, reg)
    looked = E.load_history(reg)
    history = manual_hist or looked or E.History()
    if admin and reg: st.caption(("History found for this registration: " + json.dumps({k: v for k, v in history.__dict__.items() if v})) if looked else "No stored history for this registration (treated as clean).")

    up = st.file_uploader("Damage photo or video", type=["jpg", "jpeg", "png", "webp", "mp4", "mov"])
    parts_here = core.parts_for(vtype)
    claimed = st.selectbox("Customer says the damaged part is", parts_here)
    with st.expander("AI unavailable? Enter findings manually (what the vision AI would return)"):
        mp = st.multiselect("Damaged parts", parts_here, parts_here[:1]); mt = st.selectbox("Damage type", list(core.TYP))
        msv = st.selectbox("Severity", list(core.SEV), 1); mrep = st.checkbox("Repairable (body-work can fix it)", True)
        mc2 = st.slider("Confidence %", 0, 100, 85); mq = st.checkbox("Photo is clear", True)
        manual = st.button("Use manual findings")
    go = st.button("Analyse with AI", type="primary", disabled=(up is None or ai_cfg is None))
    if ai_cfg is None and up is not None: st.info("No AI model configured. Enable Insurer/Admin mode in the sidebar to add one.")
    finding, extra = None, []
    if go and up:
        with st.spinner("Analysing your photo \u2014 this takes about 10-15 seconds..."):
            try:
                raw = up.getvalue(); img, jpeg = to_jpeg(raw, up.type.startswith("video"))
                h = hashlib.sha256(jpeg).hexdigest(); d = db_load()
                if h in d["hashes"]: extra.append("Duplicate photo: this image was already submitted")
                else: d["hashes"].append(h); db_save(d)
                finding = core.analyse_image(jpeg, ai_cfg, vtype); log("AI_VISION", json.dumps(finding))
                st.session_state.img = (img, finding.get("box"))
            except Exception as e: st.error(f"AI step failed: {e}. Use the manual option."); log("AI_ERROR", str(e))
    if manual and mp:
        finding = {"items": [{"part": p, "type": mt, "severity": msv, "repairable": mrep} for p in mp], "confidence": mc2, "quality_ok": mq, "notes": "Manually entered findings"}
        log("MANUAL_INPUT", json.dumps(finding)); st.session_state.pop("img", None)
    if finding:
        d = db_load(); cid = f"C{len(d['queue']) + 1}"
        try:
            quote = E.build_quote(vehicle, policy, history, E.Findings.from_vision(finding, claimed, extra), cfg, catalog)
            neg = E.Negotiator(quote, cfg, cid); opening = neg.opening_message(); neg.transcript.append(("engine", opening))
            st.session_state.neg, st.session_state.chat = neg, [("assistant", opening)]
            persist(neg)
        except Exception as e: st.error(f"Could not build the estimate: {e}")

    neg = st.session_state.get("neg")
    if neg:
        c = neg.q
        if "img" in st.session_state:
            im, box = st.session_state.img; im = im.copy()
            if isinstance(box, list) and len(box) == 4:
                try:
                    w, h = im.size; x, y, bw, bh = box; ImageDraw.Draw(im).rectangle([x*w, y*h, (x+bw)*w, (y+bh)*h], outline="red", width=4)
                except Exception: pass
            st.image(im, width=380)
        a, b = st.columns([3, 2])
        with a:
            st.subheader(f"Assessment \u2014 claim {neg.claim_id}")
            st.dataframe([{"Part": E.pretty(i["part"]), "Damage": f"{i['severity']} {i['type']}", "AI says repairable": {True: "yes", False: "no", None: "-"}[i["ai_repairable"]],
                           "Priced as": i["action"] if i["covered"] else ("NOT COVERED" if i["action"] == "excluded" else i["action"]), "Parts/material": i["part_value"], "Labour": i["labour"],
                           "Age dep. %": round(i["dep_pct"] * 100), "Line total (\u20b9)": i["line_total"]} for i in c.items], width="stretch", hide_index=True)
            st.write(f"Garage repair: **{E.inr(c.gross)}** | Depreciation: **-{E.inr(c.dep_total)}** | Excess: **-{E.inr(min(c.excess, c.subtotal))}** | **Amount payable: {E.inr(c.net)}**")
            st.markdown("**Why this amount**"); [st.write("- " + r["text"]) for r in c.reasons]
            if admin:
                with st.expander("Insurer view: limits, profit protection, flags", expanded=True):
                    st.write(f"Opening offer **{E.inr(c.opening)}** ({c.floor_pct*100:.0f}%) | Engine maximum **{E.inr(c.max_amount)}** ({c.ceiling_pct*100:.0f}%) | Insurer keeps at least **{E.inr(c.insurer_saving_at_max)}** vs the garage route")
                    st.write(f"Hyper-inflation trigger: any demand above **{E.inr(c.hyper_amount)}** | Vehicle declared value (IDV): {E.inr(c.idv)} | AI confidence {c.confidence:.0f}%")
                    [st.write("- " + a_["text"]) for a_ in c.adjustments]; st.metric("Fraud and risk score", f"{c.risk}/100"); st.caption(c.notes)
                    [st.warning(f"[{f['code']}] {f['text']}") for f in c.flags]
        with b:
            if neg.status == "escalated":
                st.error("Passed to a human adjuster")
                if admin: [st.write("- " + r) for r in (neg.escalation_reasons or [f["text"] for f in c.flags])]
            else:
                st.subheader("Settlement conversation")
            lang = st.selectbox("Chat language", ["English", "Hindi"]) if neg.status == "negotiating" else "English"
            for r, m in st.session_state.chat: st.chat_message(r).write(m)
            if True:
                if neg.status == "negotiating":
                    if st.button("Accept current offer"):
                        rep = neg.accept_current_offer(); st.session_state.chat.append(("assistant", rep)); neg.transcript.append(("engine", rep)); persist(neg); st.rerun()
                    msg = st.chat_input("Your reply or counteroffer in rupees")
                    if msg:
                        st.session_state.chat.append(("user", msg)); rep = neg.reply(msg)
                        st.session_state.chat.append(("assistant", core.translate_hi(rep, ai_cfg) if lang == "Hindi" and ai_cfg else rep)); persist(neg); st.rerun()
                elif neg.status == "pending_approval":
                    st.info(f"Agreed amount {E.inr(neg.amount)} is waiting for a human adjuster's approval (see the Adjuster dashboard).")
                elif neg.status == "settled":
                    st.success(f"Final settlement {E.inr(neg.amount)}")
                    upi = st.text_input("UPI ID for instant payout (simulated)")
                    if st.button("Pay now"):
                        if re.fullmatch(r"[A-Za-z0-9._-]{2,}@[A-Za-z]{2,}", upi):
                            neg._log("PAYOUT", {"amount": neg.amount}, "PAID (simulated)", f"UPI transfer to **{upi[2:]}", actor="system")
                            persist(neg, state="Auto-settled", paid=neg.amount, saved=c.net - neg.amount); st.success("Payment initiated (simulated). Claim closed.")
                        else: st.error("That UPI ID does not look valid.")
                elif neg.status in ("approved_by_human", "rejected_by_human"): st.success(f"{STATE[neg.status]}: {E.inr(neg.amount)}")

# ======================================== ADJUSTER DASHBOARD ========================================
with t2:
    d = db_load(); q = d["queue"]; n = len(q); stp = sum(1 for x in q if x["state"] == "Auto-settled")
    k = st.columns(5); k[0].metric("Cases", n); k[1].metric("Straight-through rate", f"{stp/n*100:.0f}%" if n else "-")
    k[2].metric("Waiting for a human", sum(1 for x in q if x["state"] in ("Needs review", "Needs approval")))
    k[3].metric("Total paid out", E.inr(sum(x.get("paid", 0) for x in q))); k[4].metric("Saved vs garage-route amount", E.inr(sum(x.get("saved", 0) for x in q)))
    st.caption("A human takes over when: a flag is raised at intake, a customer demands a hyper-inflated amount, negotiation fails, or a deal is above the approval threshold (plus a random audit sample).")
    for x in reversed(q):
        waiting = x["state"] in ("Needs review", "Needs approval")
        with st.expander(f"{x['id']} \u2014 {x['state']} \u2014 {x['detail'][:110]}", expanded=waiting):
            snap = x.get("snapshot")
            if not snap: st.write("(older record without full details)"); continue
            ng = E.Negotiator.from_dict(snap, cfg); qq = ng.q
            st.write(f"Vehicle: {qq.vehicle['vtype']} / {qq.vehicle['make']} / {qq.vehicle['segment']} / {E.age_text(qq.vehicle['age_months'])} | Amount payable **{E.inr(qq.net)}** | opening {E.inr(qq.opening)} | engine max **{E.inr(qq.max_amount)}** | risk {qq.risk}/100")
            if ng.escalation_reasons or qq.flags: st.warning("\n".join("- " + r for r in (ng.escalation_reasons or [f["text"] for f in qq.flags])))
            if ng.amount is not None: st.write(f"Amount on the table: **{E.inr(ng.amount)}**")
            with st.expander("Reasons given to the customer"): [st.write("- " + r["text"]) for r in qq.reasons]
            with st.expander("Chat transcript"): [st.write(f"**{w}:** {t}") for w, t in ng.transcript]
            if waiting:
                rv, nt = st.text_input("Reviewer name (required)", key=f"rv{x['id']}"), st.text_area("Decision note (mandatory to override the limit or to reject)", key=f"nt{x['id']}")
                amt = st.number_input("Amount (\u20b9)", 0, 5000000, int(ng.amount if ng.amount is not None else qq.max_amount), key=f"am{x['id']}")
                b1, b2, b3 = st.columns(3)
                def act(action, amount=None):
                    try:
                        ng.human_decide(action, rv, nt, amount); persist(ng, **({"approved": ng.amount} if ng.amount is not None else {})); st.rerun()
                    except ValueError as e: st.error(str(e))
                if ng.amount is not None and b1.button("Approve agreed amount", key=f"ap{x['id']}"): act("approve")
                if b2.button("Set my own amount", key=f"so{x['id']}"): act("set_amount", float(amt))
                if b3.button("Reject claim", key=f"rj{x['id']}"): act("reject")
                if ng.amount is not None and amt > qq.max_amount: st.caption(f"\u26a0 {E.inr(amt)} is above the engine's limit {E.inr(qq.max_amount)}: a written justification is mandatory and the override is audited.")
            elif x["state"] == "Approved by adjuster" and not x.get("paid") and ng.amount:
                if st.button("Release payout (simulated)", key=f"pay{x['id']}"):
                    ng._log("PAYOUT", {"amount": ng.amount}, "PAID (simulated)", "released after human approval", actor="system")
                    persist(ng, state="Paid after approval", paid=ng.amount, saved=qq.net - ng.amount); st.rerun()
    if st.button("Clear all stored data (demo reset)"): db_save({"queue": [], "log": [], "hashes": []}); st.session_state.pop("neg", None); st.rerun()

# ======================================== AUDIT ========================================
with t3:
    d = db_load(); ids = ["All"] + sorted({e.get("claim", "") for e in d["log"] if e.get("claim")})
    pick = st.selectbox("Claim", ids); rows = [e for e in d["log"] if pick == "All" or e.get("claim") == pick]
    st.dataframe(rows, width="stretch")
    st.download_button("Download audit trail (JSON)", json.dumps(rows, indent=1), "audit_trail.json")

# ======================================== RULES & PRICES ========================================
with t4:
    st.subheader("Settlement rules (every number the engine uses)")
    st.caption("Source of truth: settlement_config.json (defaults live in settlement_engine.py). Change a value, validate, save: the next claim uses it. The sidebar dials are temporary overrides on top of this.")
    path = E.CONFIG_PATH
    cur = json.dumps(E.load_config(), indent=2)
    if admin:
        txt = st.text_area("settlement_config.json", cur, height=380)
        e1, e2 = st.columns(2)
        if e1.button("Validate"):
            try: E.load_config(path=None, overrides=json.loads(txt)); st.success("Valid.")
            except Exception as ex: st.error(str(ex))
        if e2.button("Save to settlement_config.json"):
            try: E.load_config(path=None, overrides=json.loads(txt)); open(path, "w", encoding="utf-8").write(txt); st.success("Saved. (On free hosting the file resets when the app restarts: copy it back into GitHub to keep it.)")
            except Exception as ex: st.error(f"Not saved: {ex}")
        st.download_button("Download current config", cur, "settlement_config.json")
    else:
        st.info("Enable Insurer / Admin mode in the sidebar to edit the rules."); st.json(E.load_config(), expanded=False)
    st.divider(); st.subheader("Parts price dataset (vehicle type x make x segment x part)")
    st.caption(f"{len(catalog.rows)} rows loaded from parts_catalog.csv. All prices shipped are illustrative placeholders: replace the file with real rates. Unknown makes fall back to generic rows and are escalated to a human.")
    l1, l2, l3, l4 = st.columns(4)
    lt = l1.selectbox("Type", catalog.vehicle_types(), key="lt"); lm = l2.selectbox("Make", catalog.makes(lt) + ["(unlisted make)"], key="lm")
    ls = l3.selectbox("Segment", catalog.segments(lt), key="ls"); lp = l4.selectbox("Part", catalog.parts(lt), key="lp")
    row, level = catalog.lookup(lt, "zzz" if lm.startswith("(") else lm, ls, lp)
    st.write({"match level": level, **(row or {})})
    with open(E.CATALOG_PATH, "rb") as fh: st.download_button("Download parts_catalog.csv", fh.read(), "parts_catalog.csv")
    if admin:
        newcsv = st.file_uploader("Replace the price dataset (same columns)", type=["csv"], key="newcsv")
        if newcsv is not None and st.button("Use this price file"):
            import csv as _csv
            need = {"vehicle_type", "make", "segment", "part", "part_cost_inr", "labour_hours", "material", "repairable", "structural"}
            txt = newcsv.getvalue().decode("utf-8-sig")
            if not need <= set((_csv.DictReader(io.StringIO(txt)).fieldnames or [])): st.error("Missing columns. Needed: " + ", ".join(sorted(need)))
            else:
                open(E.CATALOG_PATH, "w", encoding="utf-8", newline="").write(txt); catalog.load(); st.success(f"Loaded {len(catalog.rows)} rows."); st.rerun()
    st.divider(); st.subheader("Claim history file (claim_history.csv)")
    st.caption("Looked up by registration number. Try RJ14AB1234 (clean), RJ14CD5678 (2 claims), DL01XY9999 (fraud flag), MH12ZZ0001 (bumper claimed 60 days ago), RJ20EF4321 (3 claims in 12 months).")
    try:
        import pandas as pd; st.dataframe(pd.read_csv(E.HISTORY_PATH), width="stretch", hide_index=True)
    except Exception: st.write("(no history file)")

# ======================================== ASSUMPTIONS ========================================
with t5:
    st.subheader("Architecture principle")
    st.info("\U0001f9e0 **The AI describes; the code decides.** The vision model says only which part is damaged, what kind, how severe and whether it is repairable. It never sees prices. "
            "settlement_engine.py looks the part up in a price dataset, applies age depreciation, policy rules and claim history, protects the insurer\u2019s margin, runs the negotiation and decides when a human must take over.")
    st.markdown("""
| Layer | Technology |
|-------|-----------|
| **Frontend** | Streamlit (Python) |
| **Vision AI** | Any OpenAI-compatible API (Gemini, GPT-4o, Groq), Anthropic Claude, or local Ollama model (<20B params); describes damage only |
| **Settlement engine** | Pure Python (`settlement_engine.py`): price lookup, depreciation, history, profit guards, reasons, negotiation, human takeover, audit |
| **Data** | `parts_catalog.csv` (type x make x segment x part), `claim_history.csv`, `settlement_config.json` |
""")
    st.subheader("Assumptions")
    st.markdown("""
- **All prices, percentages and thresholds are illustrative placeholders**, editable in the files above. They are not real insurer tariffs.
- **Depreciation** applies to replaced parts only (metal by age slab, plastic and rubber 50%, fibre 30%, glass 0%), in the style of Indian motor-insurance practice; verify against the insurer\u2019s own schedule. Zero-dep cover removes it.
- **Repair vs replace:** a part the AI calls repairable is priced as repair (labour + paint/material) unless that nears the cost of a new part; some parts (lights, mirrors, glass) are always replaced.
- **Cash offer** = a share of the amount the insurer would pay a garage (after depreciation and excess). Newer vehicles and clean histories get a higher share; older vehicles and recent claims get a lower one.
- **Profit protection:** (1) the insurer must save at least a set % versus the garage route on every deal; (2) optional loss-ratio cap per policy. Every amount passes a final firewall that blocks anything above the engine\u2019s authority.
- **Human takeover:** any intake flag, a demand above the hyper-inflation trigger, a failed negotiation, a deal above the approval threshold, and a random audit sample. Human overrides above the limit need a written justification and are audited.
- **Not covered:** real payment integration, real tariff data, vehicle market valuation beyond a simple IDV estimate.
""")
    st.subheader("Known limitations")
    st.markdown("""
- AI damage-detection accuracy has **not been measured** on a labelled dataset yet; its self-reported confidence is not reliable on its own.
- Free Streamlit hosting **wipes saved claims and config edits on restart** and sleeps when idle.
- Customer messages are understood with rules (amounts, accept/why/anger/injection words), not by an AI; unusual phrasing falls back to asking for a rupee figure.
- Hindi chat is translated by the model with a number-check; unverified in real use.
""")

# ======================================== COMPARE MODELS ========================================
with t6:
    import pandas as pd
    st.subheader("Compare AI models on the same photo")
    st.caption("Add models in the sidebar (enable Admin mode), upload a test photo, tick the parts that are truly damaged, and run. Repeat with several photos, then read the averages.")
    cu = st.file_uploader("Test photo", type=["jpg", "jpeg", "png", "webp"], key="cmpu")
    cvt = st.selectbox("Vehicle type in photo", catalog.vehicle_types(), key="cvt")
    truth = st.multiselect("Parts that are truly damaged (enables accuracy scores)", core.parts_for(cvt))
    chosen = st.multiselect("Models to compare", list(st.session_state.profiles), default=list(st.session_state.profiles)[:1])
    if "cmp" not in st.session_state: st.session_state.cmp = []
    if st.button("Run comparison", disabled=not (cu and chosen)):
        _, jpeg = to_jpeg(cu.getvalue(), False)
        for m in chosen:
            t0 = time.time()
            try:
                r = core.analyse_image(jpeg, st.session_state.profiles[m], cvt); dt = round(time.time() - t0, 1)
                found = sorted({i.get("part") for i in r["items"]})
                row = {"Photo": cu.name, "Model": m, "Time (s)": dt, "Parts found": ", ".join(map(str, found)), "Self-reported confidence": r.get("confidence")}
                if truth: row["Precision"], row["Recall"] = core.score(found, truth)
            except Exception as e: row = {"Photo": cu.name, "Model": m, "Time (s)": round(time.time() - t0, 1), "Parts found": "ERROR: " + str(e)[:150]}
            st.session_state.cmp.append(row)
    if st.session_state.cmp:
        df = pd.DataFrame(st.session_state.cmp); st.dataframe(df, width="stretch")
        if "Recall" in df: st.write("**Average per model** (higher recall = misses fewer damaged parts; higher precision = invents fewer):"); st.dataframe(df.dropna(subset=["Recall"]).groupby("Model")[["Precision", "Recall", "Time (s)"]].mean().round(2), width="stretch")
        if st.button("Clear comparison results"): st.session_state.cmp = []; st.rerun()
