#!/usr/bin/env python3
"""IZhang Aviation Infographic Maker — backend.

Serves the static frontend and proxies aircraft-data synthesis requests
to the DeepSeek API so the key never reaches the browser.
"""
import json
import os
import re
import subprocess
import sys
import time

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"), static_url_path="/static")

# V5.0: mission-control dashboard, /articles tracker, /publish queue
from v5 import v5bp  # noqa: E402
app.register_blueprint(v5bp)

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"


def _api_key():
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key.strip()
    key_file = os.path.join(BASE_DIR, "deepseek.key")
    if os.path.exists(key_file):
        with open(key_file) as f:
            return f.read().strip()
    return None


SYSTEM_PROMPT = """You are an aviation data editor for a bilingual (English + Simplified Chinese) aircraft infographic.
The user pastes raw text copied from planespotters.net (and possibly news snippets) about ONE airframe, plus its registration.
Synthesize it into STRICT JSON, nothing else. Schema:

{
  "reg": "AP-BOI",                          // the registration the user asked about
  "airline_en": "K2 Airways",               // operator matching that registration
  "airline_cn": "K2航空",                    // official Chinese name if it exists (中国南方航空, 卡塔尔航空...), otherwise a natural transliteration
  "manufacturer": "Boeing",
  "type_en": "Boeing 737-4M0(BDSF)",        // exact current variant
  "type_cn": "波音737-4M0(BDSF)",
  "headline_en": "Boeing 737-400 Freighter",// short friendly type name
  "headline_cn": "波音737-400货机",
  "status_en": "Written Off",               // Active / Stored / Written Off / Scrapped ...
  "status_cn": "已注销（失事）",
  "status_tone": "lost",                    // one of: "ok" (active), "stored", "lost" (written off / scrapped / crashed)
  "facts": [                                 // 6-10 key facts, in display order
    {"label_en": "Manufacturer Serial Number", "label_cn": "制造序列号", "value_en": "29210", "value_cn": "29210"},
    {"label_en": "First Flight", "label_cn": "首飞日期", "value_en": "8 Jan 1999", "value_cn": "1999年1月8日"}
  ],
  "history": [                               // condensed operator history, chronological, COMPLETE
    {"reg": "VP-BAR", "operator_en": "Aeroflot", "operator_cn": "俄罗斯航空", "period": "Apr 1999 – 2004", "note_en": "leased from GECAS", "note_cn": "自GECAS租赁"}
  ],
  "notes_en": "",                            // 2-4 sentence summary of incident / special story if present, else ""
  "notes_cn": "",
  "boarding_pass": {                          // ONLY used by the Boarding-Pass design; ignored everywhere else
    "airline_iata": "CZ",                     // operator 2-letter IATA code
    "airline_numeric": "784",                 // operator 3-digit ticketing/airline code
    "flight_no": "CZ 327",                    // IATA code + a plausible flight number
    "from_iata": "CAN", "from_city": "Guangzhou",
    "to_iata": "LAX",  "to_city": "Los Angeles",  // a REAL route this operator flies with this type
    "dep_time": "12:40", "boarding_time": "12:00",// 24h clock; boarding 30-45 min before departure
    "gate": "A12", "seat": "12A", "zone": "3",
    "fare_class": "Y", "fare_basis": "YLXR3M", "tier": "Gold",
    "price": "USD 1180.00"                     // realistic economy one-way estimate for that route
  }
}

Rules:
- THE SUBJECT: the registration the user provides selects WHICH operator stint is the subject of the
  infographic. "reg", "airline_en"/"airline_cn", "status" context, plus the Config, Engines and Hex Code
  facts must correspond to THAT registration's row in the operator history — even if the airframe was
  later re-registered to someone else. Airframe-level facts (MSN, Line Number, First Flight, Age,
  Production Site, Test Registrations, airframe status) stay airframe-level. Operators before AND after
  the subject stint still appear in history, chronological.
- facts: include when available — MSN (制造序列号), Line Number (生产线编号), First Flight (首飞日期), Age (机龄), Production Site (生产地点), Engines (发动机), Config/Cabin (构型), Hex Code — latest (应答机编码), Test Registration(s) (测试注册号), Conversion date (改装日期), Aircraft Name (机名), Fleet Number (机队编号). Fields VARY by manufacturer — Airbus pages have no Line Number, some airframes have no First Flight, Saab MSNs look like "340B-242", regional types may carry fleet numbers. Include whatever genuinely exists, omit what doesn't. Keep values short. value_cn: translate dates to 1999年1月8日 style; keep codes/numbers as-is.
- history: show the WHOLE career — never silently drop a stint. VERY long careers: aircraft rarely
  change hands dozens of times except through leasing — if one owner repeatedly leases the airframe
  out to MANY different short-term operators (e.g. Air Urga → UNHAS / Kam Air / Royal Air, returned
  and re-leased over a decade), collapse that whole era into ONE row: operator = the owner, period
  spanning the era, note_en = "leased out to UNHAS, Kam Air, Royal Air and others" (note_cn likewise).
  Never output dozens of lease/return ping-pong rows. Also MERGE aggressively: "opby"/"opf" chains are one row; repeated lease/return ping-pong between the same owner and the same operator (e.g. Air Urga ↔ UNHAS, ret, re-leased, ret ...) collapses into ONE row per operator with a period like "2015 – 2023 (multiple leases)" and a note "repeatedly leased from Air Urga". Pure lessor rows (GECAS, AerCap, Saab Aircraft Leasing) become rows only when the airframe notably sat with them; otherwise fold into the note. Derive "period" from delivered dates (start – next row's start; last row open-ended like "Oct 2024 –" or ending on the incident date). note_en max ~70 chars, only the most meaningful remark (lease source, converted to freighter, wfu/std, crashed...). Chronological order. Aim for ≤ 14 condensed rows while still covering every operator.
- notes: if the text contains an incident / crash / news story / notable trivia, summarize in 2-4 sentences (EN) with a faithful Chinese translation. Otherwise both "".
- Chinese must be natural Simplified Chinese, not machine-literal.
- Airline names in history: short common names (Aeroflot not "Aeroflot - Russian Airlines").
- If a field is genuinely absent, omit that fact row rather than inventing data. Never invent.
- boarding_pass: fabricate a plausible but realistic mock ticket for a novelty boarding-pass graphic —
  pick a REAL route this operator actually flies with this aircraft type (correct IATA codes + city
  names), a realistic economy one-way fare in USD for that route, a flight number using the operator's
  IATA code, 24h boarding/departure times (boarding 30-45 min before departure), a gate/seat/zone, a
  fare class letter, a suitable fare-basis code, and an elite tier. Include airline_iata (2-letter) and
  airline_numeric (3-digit). This block is decorative and ONLY feeds the boarding-pass design.
- Output ONLY the JSON object. No markdown fences, no commentary."""


FICTIONAL_SUFFIX = """

FICTIONAL / VIRTUAL-LIVERY MODE (OVERRIDE): The user is designing a VIRTUAL, fictional livery and WANTS
invented data. Fabricate a plausible, internally-consistent career for this airframe under the given
fictional airline and aircraft type. Invent realistic-looking airframe facts (MSN, Line Number, First
Flight, Age, Production Site, Engines, Cabin Configuration, Hex Code) appropriate to the type, and a short
believable operator history (3-6 rows) that STARTS with or prominently features the fictional airline as
the current operator. Keep everything believable and consistent with the real aircraft type; you MAY invent
freely because this is explicitly a fictional showcase. Set status to Active / 运营中 (status_tone "ok")
unless the user text implies otherwise. Write a 1-3 sentence fictional REMARKS story (notes_en / notes_cn)
about the livery. Use the schema unchanged. Output ONLY the JSON object."""


ROCKET_PROMPT = """You are an aerospace data editor for a bilingual (English + Simplified Chinese) launch-vehicle infographic.
The user pastes raw text (typically a Wikipedia infobox) about ONE rocket, plus its name.
Synthesize STRICT JSON, nothing else. Schema:

{
  "name_en": "Long March 10B",
  "name_cn": "长征十号乙",                     // official Chinese name if it exists, otherwise a natural translation
  "headline_en": "Medium-lift launch vehicle",
  "headline_cn": "中型运载火箭",
  "manufacturer_en": "CALT", "manufacturer_cn": "中国运载火箭技术研究院",
  "country_en": "China", "country_cn": "中国",
  "status_en": "Active", "status_cn": "现役",
  "status_tone": "ok",                        // ok=active/in development, stored=retired, lost=cancelled
  "first_flight": "10 Jul 2026", "first_flight_cn": "2026年7月10日",
  "key_facts": [                              // EXACTLY 4-5 MOST important facts for the minimal poster:
    {"label_en": "Height", "label_cn": "全长", "value_en": "70.2 m", "value_cn": "70.2米"}
  ],                                          // prefer: Function, Height, Payload to LEO, First flight, Total launches
  "sections": [                               // for the tall infographic — cover EVERYTHING in the source, grouped logically
    {"title_en": "OVERVIEW",         "title_cn": "概览",     "facts": [{"label_en":"Function","label_cn":"用途","value_en":"...","value_cn":"...","priority":1}]},
    {"title_en": "DIMENSIONS",       "title_cn": "尺寸",     "facts": []},
    {"title_en": "PAYLOAD CAPACITY", "title_cn": "运载能力", "facts": []},
    {"title_en": "FIRST STAGE",      "title_cn": "第一级",   "facts": []},
    {"title_en": "SECOND STAGE",     "title_cn": "第二级",   "facts": []},
    {"title_en": "LAUNCH HISTORY",   "title_cn": "发射记录", "facts": []}
  ],
  "notes_en": "", "notes_cn": ""              // 1-3 sentence story if the source has one, else ""
}

Rules:
- Values short and metric only ("70.2 m", "16,000 kg → 200 km LEO", "SL 8,750 kN"); drop imperial duplicates.
- Payload rows: one fact per orbit/altitude ("Payload to LEO (200 km)" → "16,000 kg").
- Stage rows: engines ("7 × YF-100K"), thrust (SL/vac), specific impulse, burn time, propellant.
- Family / based on / comparable rockets go in OVERVIEW.
- Section list is flexible: include only sections that have facts; add sections if the source needs them.
- Dates: "10 Jul 2026" / "2026年7月10日". Chinese natural (长征、朱雀、猎鹰 official names).
- STRIP Wikipedia citation markers like [1], [2], [edit on Wikidata] from every value.
- EVERY fact gets "priority": 1 = essential (function, height, payload to LEO, status, first flight,
  engines and propellant per stage, total launches), 2 = standard (diameter, stages, manufacturer,
  launch sites, thrust, family), 3 = nice-to-have (comparable rockets, based on, specific impulse,
  burn time, successes). The infographic drops priority-3 facts first when space is short.
- Omit anything not in the source; never invent. Output ONLY the JSON object."""


def _call_deepseek(system, user):
    key = _api_key()
    if not key:
        return None, ("DeepSeek API key not configured on server", 500)
    payload = {
        "model": "deepseek-chat",
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    try:
        r = requests.post(
            DEEPSEEK_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=180,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
    except requests.RequestException as e:
        return None, (f"DeepSeek request failed: {e}", 502)
    content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(content), None
    except json.JSONDecodeError:
        return None, ("Model returned invalid JSON", 502)


@app.route("/api/synthesize_rocket", methods=["POST"])
def synthesize_rocket():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    raw = (body.get("raw") or "").strip()
    if not name or not raw:
        return jsonify({"error": "rocket name and raw text are both required"}), 400
    data, err = _call_deepseek(ROCKET_PROMPT, f"Rocket name: {name}\n\nRaw pasted text:\n{raw}")
    if err:
        return jsonify({"error": err[0]}), err[1]
    return jsonify(data)


REMOVEBG_SERVER = "https://image-upscaling.net"
REMOVEBG_CLIENT_ID = ""


AF_FETCH = os.path.join(BASE_DIR, "af_fetch.py")


@app.route("/api/af_fetch", methods=["POST"])
def af_fetch():
    """Auto-fetch airframe data from airfleets.net for a registration (manual
    generator). Runs the same headful-chromium-under-xvfb fetcher the bot uses;
    ~1 min. Fails loudly so the UI can fall back to the Planespotters button."""
    b = request.get_json(force=True, silent=True) or {}
    reg = (b.get("reg") or "").strip()
    if not reg or len(reg) > 20:
        return jsonify({"ok": False, "error": "need a registration"}), 400
    try:
        # separate profile dir from the bot's so the two never lock-contend
        env = {**os.environ, "AF_PROFILE": "/root/infographic-maker/.af_profile_site"}
        p = subprocess.run(["xvfb-run", "-a", sys.executable, AF_FETCH, reg],
                           capture_output=True, text=True, timeout=150, env=env)
        lines = [x for x in (p.stdout or "").strip().splitlines() if x.strip()]
        j = json.loads(lines[-1]) if lines else {"ok": False, "error": "no output"}
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "timed out (airfleets challenge)"}), 504
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:150]}), 502
    return jsonify(j)


@app.route("/api/deepseek_balance")
def deepseek_balance():
    """DeepSeek account balance (money). Key from deepseek.key."""
    try:
        key = open(os.path.join(BASE_DIR, "deepseek.key")).read().strip()
        r = requests.get("https://api.deepseek.com/user/balance",
                         headers={"Authorization": f"Bearer {key}",
                                  "Accept": "application/json"}, timeout=15).json()
        infos = r.get("balance_infos") or []
        # the account carries per-currency balances (USD + CNY); pick the funded
        # one so we don't show $0.00 when there's e.g. ¥28 sitting in CNY
        def _f(x):
            try:
                return float(x.get("total_balance") or 0)
            except (TypeError, ValueError):
                return 0.0
        b = max(infos, key=_f) if infos else {}
        return jsonify({"is_available": r.get("is_available"),
                        "total_balance": b.get("total_balance"),
                        "currency": b.get("currency"),
                        "topped_up_balance": b.get("topped_up_balance"),
                        "granted_balance": b.get("granted_balance"),
                        "all": infos})
    except Exception as e:
        return jsonify({"error": str(e)[:120]}), 502


@app.route("/api/removebg_quota")
def removebg_quota():
    """Live used/free quota + balance from image-upscaling.net for this client_id.
    (get_account_info is the documented account endpoint; quota is per day.)"""
    try:
        r = requests.get(f"{REMOVEBG_SERVER}/get_account_info",
                         cookies={"client_id": REMOVEBG_CLIENT_ID}, timeout=15)
        j = r.json()
        return jsonify({"quota_used": j.get("quota_used"), "quota_free": j.get("quota_free"),
                        "balance": j.get("balance"),
                        "has_free_quota_left": j.get("has_free_quota_left")})
    except (requests.RequestException, ValueError) as e:
        return jsonify({"error": str(e)[:120]}), 502


@app.route("/api/removebg", methods=["POST"])
def removebg():
    """Proxy to image-upscaling.net AI background removal (rocket images)."""
    f = request.files.get("image")
    if not f:
        return jsonify({"error": "no image"}), 400
    cookies = {"client_id": REMOVEBG_CLIENT_ID}
    try:
        up = requests.post(
            f"{REMOVEBG_SERVER}/removebg_upload",
            files={"image": (f.filename or "rocket.png", f.stream, f.mimetype or "image/png")},
            cookies=cookies,
            timeout=120,
        )
    except requests.RequestException as e:
        return jsonify({"error": f"upload failed: {e}"}), 502
    txt = (up.text or "").strip()
    low = txt.lower()
    if up.status_code in (402, 403, 429) or any(
        k in low for k in ("quota", "credit", "limit exceeded", "insufficient", "balance too low")
    ):
        return jsonify({"error": "credits"}), 402
    if up.status_code != 200 or not txt or len(txt) > 300:
        return jsonify({"error": f"upload failed: HTTP {up.status_code} {txt[:120]}"}), 502
    original = txt

    deadline = time.time() + 300
    while time.time() < deadline:
        time.sleep(3)
        try:
            st = requests.get(f"{REMOVEBG_SERVER}/removebg_get_status_v2", cookies=cookies, timeout=60)
            entries = st.json()
        except (requests.RequestException, ValueError):
            continue
        if isinstance(entries, dict):  # be permissive about the wrapper shape
            for key in ("files", "images", "results", "data"):
                if isinstance(entries.get(key), list):
                    entries = entries[key]
                    break
            else:
                entries = [entries]
        if not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict) or e.get("original_filename") != original:
                continue
            if not e.get("completed"):
                break
            iu = e.get("image_url") or e.get("url") or ""
            if iu and not iu.startswith("http"):
                iu = f"{REMOVEBG_SERVER}/{iu.lstrip('/')}"
            if not iu:
                return jsonify({"error": "result had no image url"}), 502
            try:
                img = requests.get(iu, cookies=cookies, params={"delete_after_download": ""}, timeout=120)
                img.raise_for_status()
            except requests.RequestException as e2:
                return jsonify({"error": f"download failed: {e2}"}), 502
            return Response(img.content, mimetype="image/png")
    return jsonify({"error": "timed out waiting for the result"}), 504


# V5.0: "/" is now the dashboard (v5 blueprint); the generator lives at
# /aircraft, and /rocket deep-links straight into rocket mode.
@app.route("/aircraft")
@app.route("/rocket")
def generator():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/health")
def health():
    return jsonify({"ok": True})


KEYWORDS_PROMPT = (
    "You produce Bing IMAGE-search keywords for an aviation article, so the "
    "author can find real reference photos related to what the article actually "
    "talks about. Read the article and return JSON: "
    '{"keywords": ["k1","k2","k3","k4"]} — EXACTLY 4 distinct search phrases.\n'
    "Rules:\n"
    "1. keyword #1 — and ONLY #1 — is about the aircraft itself: "
    "'<airline English name> <aircraft type/variant> <registration>' "
    "(e.g. 'Qanot Sharq A321neo XLR UK32111'). Include the registration if known.\n"
    "2. keywords #2, #3, #4 must each be a DIFFERENT important element that "
    "actually appears in the article — NOT the aircraft again. Prefer, in order: "
    "(a) a named person (executive, founder, pilot, designer); (b) another "
    "institution/company/airport/authority mentioned; (c) another aircraft TYPE "
    "the same airline operates or used to operate. If the article lacks one of "
    "these, substitute the next most photo-yielding proper noun from the text.\n"
    "3. Use proper nouns; keep each phrase 2-5 words; no generic words, no "
    "punctuation-only tokens, no duplicates. Keep names in their original script "
    "if that yields better image results (e.g. a person's Latin-script name).\n"
    "Output JSON only."
)


@app.route("/api/keywords", methods=["POST"])
def keywords():
    """Derive 4 content-based image-search keywords from a written article."""
    body = request.get_json(force=True, silent=True) or {}
    airline = (body.get("airline") or "").strip()
    reg = (body.get("registration") or "").strip()
    type_en = (body.get("type_en") or "").strip()
    title = (body.get("title") or "").strip()
    text = (body.get("body") or "").strip()[:3500]
    user = (f"Airline (OCR): {airline}\nRegistration: {reg}\n"
            f"Aircraft type (if known): {type_en}\n"
            f"Article title: {title}\n\nArticle body:\n{text}")
    data, err = _call_deepseek(KEYWORDS_PROMPT, user)
    if err:
        return jsonify({"error": err[0]}), err[1]
    kws = data.get("keywords") if isinstance(data, dict) else None
    if not isinstance(kws, list):
        return jsonify({"error": "no keywords"}), 502
    clean = []
    for k in kws:
        k = str(k or "").strip()
        if k and k.lower() not in [x.lower() for x in clean]:
            clean.append(k)
    return jsonify({"keywords": clean[:4]})


@app.route("/api/synthesize", methods=["POST"])
def synthesize():
    body = request.get_json(force=True, silent=True) or {}
    reg = (body.get("registration") or "").strip()
    raw = (body.get("raw") or "").strip()
    fictional = bool(body.get("fictional"))
    if not reg:
        return jsonify({"error": "registration is required"}), 400
    if not fictional and not raw:
        return jsonify({"error": "registration and raw text are both required"}), 400

    key = _api_key()
    if not key:
        return jsonify({"error": "DeepSeek API key not configured on server"}), 500

    if fictional:
        system = SYSTEM_PROMPT + FICTIONAL_SUFFIX
        airline = (body.get("airline") or "").strip()
        type_en = (body.get("type_en") or "").strip()
        type_cn = (body.get("type_cn") or "").strip()
        user = (
            f"FICTIONAL virtual livery.\nRegistration: {reg}\n"
            f"Fictional airline: {airline or '(invent one)'}\n"
            f"Aircraft type: {type_en or '(choose a plausible type)'}"
            + (f"\n机型（中文）: {type_cn}" if type_cn else "")
            + (f"\n\nOptional inspiration text (may be blank):\n{raw}" if raw else "")
        )
        temperature = 0.9
    else:
        system = SYSTEM_PROMPT
        user = f"Registration: {reg}\n\nRaw pasted text:\n{raw}"
        temperature = 0.2

    payload = {
        "model": "deepseek-chat",
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    try:
        r = requests.post(
            DEEPSEEK_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=180,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
    except requests.RequestException as e:
        return jsonify({"error": f"DeepSeek request failed: {e}"}), 502

    # Be forgiving about fenced output despite json_object mode
    content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return jsonify({"error": "Model returned invalid JSON", "raw": content}), 502
    return jsonify(data)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8640"))
    app.run(host="0.0.0.0", port=port)
