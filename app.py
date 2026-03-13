#!/usr/bin/env python3
"""
NEA Soccer Bot — Backend Flask
  GET  /api/leagues          → lista de ligas disponibles
  GET  /api/matches?league=X → partidos de Polymarket para esa liga
  POST /api/analyze          → análisis NEA completo con Gemini
"""

import os, re, json, math, requests, sys
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, request
from flask_cors import CORS
from concurrent.futures import ThreadPoolExecutor

def _cargar_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path): return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, _, v = line.partition("=")
            k = k.strip(); v = v.strip().strip('"').strip("'")
            if k and k not in os.environ: os.environ[k] = v
_cargar_env()

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

@app.route('/')
def index():
    return app.send_static_file('index.html')

@app.route('/favicon.ico')
def favicon():
    import base64
    from flask import Response
    # Minimal 1x1 transparent ICO — silences browser 404
    ico = base64.b64decode(
        "AAABAAEAAQEAAAEAGAAwAAAAFgAAACgAAAABAAAAAgAAAAEAGAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="
    )
    return Response(ico, mimetype='image/x-icon')

@app.route('/.well-known/appspecific/com.chrome.devtools.json')
def chrome_devtools():
    return jsonify({}), 200

GAMMA_API = "https://gamma-api.polymarket.com/events"
HEADERS   = {"User-Agent": "Mozilla/5.0 (NEA-Soccer-Bot/2.0)"}

# ── Ligas ──────────────────────────────────────────────────────────────────────
LIGAS = [
    {"id": "all",          "name": "🌍 Todas las ligas",            "slugs": []},
    {"id": "ucl",          "name": "🏆 Champions League",           "slugs": ["ucl","champions-league"]},
    {"id": "uel",          "name": "🟠 Europa League",              "slugs": ["uel","europa-league"]},
    {"id": "uecl",         "name": "🔵 Conference League",          "slugs": ["uecl","conference-league","europa-conference-league"]},
    {"id": "epl",          "name": "🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier League",             "slugs": ["epl","premier-league"]},
    {"id": "laliga",       "name": "🇪🇸 La Liga",                   "slugs": ["la-liga"]},
    {"id": "seriea",       "name": "🇮🇹 Serie A",                   "slugs": ["serie-a"]},
    {"id": "bundesliga",   "name": "🇩🇪 Bundesliga",                "slugs": ["bundesliga"]},
    {"id": "ligue1",       "name": "🇫🇷 Ligue 1",                   "slugs": ["ligue-1"]},
    {"id": "eredivisie",   "name": "🇳🇱 Eredivisie",                "slugs": ["eredivisie"]},
    {"id": "superlig",     "name": "🇹🇷 Süper Lig",                 "slugs": ["super-lig"]},
    {"id": "saudi",        "name": "🇸🇦 Saudi Pro League",          "slugs": ["saudi-professional-league"]},
    {"id": "mls",          "name": "🇺🇸 MLS",                       "slugs": ["mls"]},
    {"id": "ligamx",       "name": "🇲🇽 Liga MX",                   "slugs": ["liga-mx"]},
    {"id": "brasileirao",  "name": "🇧🇷 Brasileirao",               "slugs": ["brazil-serie-a"]},
    {"id": "argentina",    "name": "🇦🇷 Argentina Primera",         "slugs": ["argentina-primera-division"]},
    {"id": "libertadores", "name": "🏆 Copa Libertadores",          "slugs": ["copa-libertadores"]},
    {"id": "efl",          "name": "🏴󠁧󠁢󠁳󠁣󠁴󠁿 EFL Championship",            "slugs": ["efl-championship"]},
    {"id": "portugal",     "name": "🇵🇹 Primeira Liga",             "slugs": ["portuguese-primeira-liga"]},
    {"id": "aleague",      "name": "🇦🇺 A-League",                  "slugs": ["a-league-soccer"]},
    {"id": "j1",           "name": "🇯🇵 J-League",                  "slugs": ["j1-league","j2-league"]},
    {"id": "colombia",     "name": "🇨🇴 Colombia Primera A",        "slugs": ["colombia-primera-a"]},
    {"id": "egypt",        "name": "🇪🇬 Egypt Premier League",      "slugs": ["egypt-premier-league"]},
]

VARIANT_HINTS = [
    "more-markets","halftime","correct-score","both-teams",
    "asian","over-under","anytime","first-scorer","red-card","corners",
]
SOCCER_KW = [
    "soccer"," fc "," cf "," sc "," ac ","football club","champions league",
    "premier league","la liga","serie a","bundesliga","ligue","eredivisie",
    "primera division","win on 202","end in a draw","moneyline",
]
AMERICAN = ["nfl","super bowl","quarterback","touchdown"]

def is_soccer(text):
    t = text.lower()
    if any(h in t for h in AMERICAN): return False
    return any(k in t for k in SOCCER_KW)

def is_variant(slug):
    return any(v in slug.lower() for v in VARIANT_HINTS)

def slug_base(slug):
    m = re.search(r'\d{4}-\d{2}-\d{2}', slug)
    return slug[:m.end()] if m else slug.rsplit("-",1)[0]

def get_end_dt(ev):
    raw = ev.get("endDate") or ""
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z","+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except: return None

def hours_left(ev):
    dt  = get_end_dt(ev)
    now = datetime.now(timezone.utc)
    if not dt: return None
    h = (dt - now).total_seconds() / 3600
    return h if h >= 0 else None

def fmt_hours(h):
    if h is None: return "?"
    return f"{int(h)}h {int((h - int(h))*60):02d}m"

def get_vol(ev):
    for k in ("volumeNum","volume","volume24hr"):
        try:
            v = float(ev.get(k) or 0)
            if v > 0: return v
        except: pass
    return 0.0

def get_liq(ev):
    for k in ("liquidityNum","liquidity"):
        try:
            v = float(ev.get(k) or 0)
            if v > 0: return v
        except: pass
    return 0.0

def parse_moneyline(ev):
    for m in (ev.get("markets") or []):
        q = (m.get("question") or "").lower()
        if "win on 202" not in q and "end in a draw" not in q: continue
        outcomes   = m.get("outcomes") or []
        prices     = m.get("outcomePrices") or []
        if "draw" in q:
            continue
        try:
            pairs = {}
            for o, p in zip(outcomes, prices):
                pairs[o] = float(p)
            return pairs
        except: pass
    return {}

def fetch_events_for_slugs(tag_slugs, days=2):
    seen, events = set(), []
    now = datetime.now(timezone.utc)
    end_max = now + timedelta(days=days + 0.5)

    def fetch_tag(tag):
        results = []
        offset, limit = 0, 100
        for _ in range(10):
            try:
                r = requests.get(GAMMA_API,
                    params={"active":"true","closed":"false",
                            "tag_slug":tag,"limit":limit,"offset":offset},
                    headers=HEADERS, timeout=12)
                r.raise_for_status()
                page = r.json()
                if not isinstance(page, list) or not page: break
                results.extend(page)
                if len(page) < limit: break
                offset += limit
            except: break
        return results

    if not tag_slugs:
        offset, limit = 0, 100
        for _ in range(40):
            try:
                r = requests.get(GAMMA_API,
                    params={"active":"true","closed":"false","limit":limit,"offset":offset},
                    headers=HEADERS, timeout=12)
                r.raise_for_status()
                page = r.json()
                if not isinstance(page, list) or not page: break
                for ev in page:
                    eid = ev.get("id") or ev.get("slug")
                    if eid and eid not in seen:
                        seen.add(eid); events.append(ev)
                if len(page) < limit: break
                offset += limit
            except: break
    else:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for results in pool.map(fetch_tag, tag_slugs):
                for ev in results:
                    eid = ev.get("id") or ev.get("slug")
                    if eid and eid not in seen:
                        seen.add(eid); events.append(ev)

    return events

def normalize_events(raw_events, days=2):
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days + 0.5)

    filtered = []
    for ev in raw_events:
        dt = get_end_dt(ev)
        if not dt: continue
        if dt < now or dt > cutoff: continue
        title = ev.get("title","") or ""
        tags_text = " ".join(t.get("label","") for t in (ev.get("tags") or []))
        if not is_soccer(title + " " + tags_text): continue
        filtered.append(ev)

    groups = {}
    for ev in filtered:
        slug = ev.get("slug","") or ""
        base = slug_base(slug)
        groups.setdefault(base, []).append(ev)

    games = []
    for base, group in groups.items():
        principals = [e for e in group if not is_variant(e.get("slug",""))]
        variants   = [e for e in group if is_variant(e.get("slug",""))]
        ev = max(principals, key=get_vol) if principals else max(variants, key=get_vol)

        dt   = get_end_dt(ev)
        hl   = hours_left(ev)
        tags = [t.get("label","") for t in (ev.get("tags") or [])
                if t.get("label") not in ("Sports","Games","")]

        title = ev.get("title","") or ""
        home, away = title, ""
        for sep in [" vs. ", " vs "]:
            if sep in title:
                home, away = [x.strip() for x in title.split(sep, 1)]
                break

        poly_home = poly_draw = poly_away = None

        def _token_overlap(a, b):
            """True if any word token of 'a' appears in 'b' (case-insensitive, min 3 chars)."""
            for tok in re.split(r'\W+', a.lower()):
                if len(tok) >= 3 and tok in b.lower():
                    return True
            return False

        # Rank markets: prefer moneyline (3-outcome) over binary (Yes/No)
        def _market_priority(m):
            outs = m.get("outcomes") or []
            q = (m.get("question") or "").lower()
            if len(outs) == 3: return 0
            if "draw" in q or "end in a draw" in q: return 1
            if "win" in q or "moneyline" in q: return 2
            return 3

        all_markets = ev.get("markets") or []
        sorted_markets = sorted(all_markets, key=_market_priority)

        # ── DEBUG: dump raw market structure once per game ──────────────────────
        print(f"\n[PRICES] Game: {title} | home='{home}' away='{away}'")
        print(f"[PRICES] Total markets: {len(all_markets)}")
        for i, m in enumerate(all_markets[:8]):  # show first 8 max
            print(f"  [{i}] q={m.get('question','')!r}  slug={m.get('marketSlug','')!r}")
            print(f"       outcomes={m.get('outcomes')}  prices={m.get('outcomePrices')}")
        # ────────────────────────────────────────────────────────────────────────

        def _parse_field(val):
            """Polymarket devuelve outcomes/prices como string JSON o como lista real."""
            if isinstance(val, str):
                try:
                    val = json.loads(val)
                except Exception:
                    return []
            return val if isinstance(val, list) else []

        for m in sorted_markets:
            q = (m.get("question") or "").lower()
            outcomes = _parse_field(m.get("outcomes"))
            prices   = _parse_field(m.get("outcomePrices"))
            if not outcomes or not prices: continue
            # Skip prop/variant markets
            if is_variant(m.get("marketSlug","") or ""):
                continue
            try:
                p_list = [float(pr) for pr in prices]
                p = dict(zip(outcomes, p_list))

                # ── Case A: 3-outcome moneyline [Home, Draw, Away] ──────────────
                if len(outcomes) == 3:
                    h_found = d_found = a_found = None
                    for o_name, pr in p.items():
                        o_lower = o_name.lower()
                        if "draw" in o_lower or "empate" in o_lower or o_lower == "x":
                            d_found = pr
                        elif home and _token_overlap(home, o_name):
                            h_found = pr
                        elif away and _token_overlap(away, o_name):
                            a_found = pr
                    print(f"[PRICES] 3-way market: h={h_found} d={d_found} a={a_found} | outcomes={list(p.keys())}")
                    if h_found is not None and d_found is not None and a_found is not None:
                        poly_home = h_found
                        poly_draw = d_found
                        poly_away = a_found
                        print(f"[PRICES] ✅ 3-way match SUCCESS")
                        break
                    else:
                        print(f"[PRICES] ⚠ 3-way partial miss — home='{home}' away='{away}'")

                # ── Case B: Binary Yes/No market ───────────────────────────────
                else:
                    yes_val = p.get("Yes") or p.get("YES") or p.get("yes")
                    if yes_val is None:
                        for o_name, pr in p.items():
                            o_lower = o_name.lower()
                            if "draw" in o_lower or "empate" in o_lower:
                                if poly_draw is None: poly_draw = pr
                            elif home and _token_overlap(home, o_name):
                                if poly_home is None: poly_home = pr
                            elif away and _token_overlap(away, o_name):
                                if poly_away is None: poly_away = pr
                        continue

                    if "draw" in q or "end in a draw" in q:
                        if poly_draw is None:
                            poly_draw = yes_val
                            print(f"[PRICES] Binary draw={yes_val!r} from q={q!r}")
                    elif home and _token_overlap(home, q):
                        if poly_home is None:
                            poly_home = yes_val
                            print(f"[PRICES] Binary home={yes_val!r} from q={q!r}")
                    elif away and _token_overlap(away, q):
                        if poly_away is None:
                            poly_away = yes_val
                            print(f"[PRICES] Binary away={yes_val!r} from q={q!r}")
                    else:
                        print(f"[PRICES] Binary NO MATCH: q={q!r} outcomes={list(p.keys())}")

            except Exception as pe:
                print(f"[PRICES] Exception: {pe}")

        print(f"[PRICES] FINAL → home={poly_home} draw={poly_draw} away={poly_away}")

        games.append({
            "id":         base,
            "slug":       ev.get("slug",""),
            "title":      title,
            "home":       home,
            "away":       away,
            "end_date":   dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "?",
            "hours_left": round(hl, 2) if hl else None,
            "hours_str":  fmt_hours(hl),
            "volume":     round(get_vol(ev)),
            "liquidity":  round(get_liq(ev)),
            "tags":       tags,
            "poly_home":  poly_home,
            "poly_draw":  poly_draw,
            "poly_away":  poly_away,
            "url":        f"https://polymarket.com/event/{ev.get('slug','')}",
        })

    games.sort(key=lambda x: x["hours_left"] if x["hours_left"] else 9999)
    return games


# ── NEA Formula ────────────────────────────────────────────────────────────────

def poisson_pmf(k, lam):
    if lam <= 0: return 1.0 if k == 0 else 0.0
    log_p = -lam + k * math.log(lam) - sum(math.log(i) for i in range(1, k+1))
    return math.exp(log_p)

def dixon_coles_tau(h, a, lH, lA, rho=-0.10):
    """Corrección de Dixon-Coles para marcadores bajos.
    Poisson independiente subestima empates 0-0 y 1-1 ~2-4%.
    rho≈-0.10 es el valor empírico estándar para fútbol europeo."""
    if   h == 0 and a == 0: return 1.0 - lH * lA * rho
    elif h == 1 and a == 0: return 1.0 + lA * rho
    elif h == 0 and a == 1: return 1.0 + lH * rho
    elif h == 1 and a == 1: return 1.0 - rho
    else:                   return 1.0

def poisson_matrix(lH, lA, max_goals=8):
    win = draw = loss = 0.0
    for h in range(max_goals + 1):
        for a in range(max_goals + 1):
            p = poisson_pmf(h, lH) * poisson_pmf(a, lA) * dixon_coles_tau(h, a, lH, lA)
            if h > a: win += p
            elif h == a: draw += p
            else: loss += p
    return win, draw, loss

def v_real(p_xg, p_form):
    """Probabilidad real estimada — SIN incluir el mercado.
    El edge se calcula externamente como (p_mercado - v_real).
    Incluir p_market aquí arrastraría el vig de la casa hacia la predicción,
    garantizando EV≈0 por construcción matemática."""
    return 0.65 * p_xg + 0.35 * p_form


# ── Gemini ─────────────────────────────────────────────────────────────────────

NUMERIC_FIELDS = [
    "xG_home_attack", "xGA_home_defense", "xG_away_attack", "xGA_away_defense",
    "home_advantage", "form_home", "form_away", "form_draw",
    "injury_index_home", "injury_index_away",
]

def _single_gemini_call(api_key, home, away, league, run_index):
    from google import genai
    from google.genai import types

    print(f"\n[Gemini Run #{run_index}] Iniciando llamada: {home} vs {away}")
    client = genai.Client(api_key=api_key)

    prompt = f"""You are a top soccer analytics expert. Analyze this match: {home} vs {away} ({league or "Soccer"}).

Using your knowledge of current season statistics, return ONLY a valid JSON object (no markdown fences, no explanation):
{{
  "xG_home_attack": <avg xG/game for {home} at home. CRITICAL: Heavily penalize (reduce by 0.5-1.0) if key attackers/playmakers are absent, float 0.1-3.0>,
  "xGA_home_defense": <avg xGA/game conceded by {home} at home. Increase if key defenders missing, float 0.1-3.0>,
  "xG_away_attack": <avg xG/game for {away} away. CRITICAL: Heavily penalize if key attackers absent, float 0.1-3.0>,
  "xGA_away_defense": <avg xGA/game conceded by {away} away. Increase if key defenders missing, float 0.1-3.0>,
  "home_advantage": <home advantage multiplier, typically 1.05-1.20>,
  "form_home": <probability home wins based on last 5 games, float 0.0-1.0>,
  "form_away": <probability away wins based on last 5 games, float 0.0-1.0>,
  "form_draw": <draw probability from recent form context, float 0.0-1.0>,
  "injury_index_home": <impact of missing players home: positive=none missing, negative=SEVERE key absences (can drop to -0.30 for superstar absences), float -0.30 to 0.05>,
  "injury_index_away": <impact of missing players away (can drop to -0.30 for superstar absences), float -0.30 to 0.05>,
  "analysis": "<3 sentences: current form, EXPLICIT mention of key injuries and how they alter the xG/xGA math, main tactical factor>"
}}"""

    text = ""
    try:
        print(f"[Run #{run_index}] Intentando con gemini-2.0-flash + Google Search grounding...")
        for chunk in client.models.generate_content_stream(
            model="gemini-2.5-flash-lite",
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        ):
            if chunk.text:
                text += chunk.text
        print(f"[Run #{run_index}] Grounding OK, respuesta: {len(text)} chars")
    except Exception as grounding_err:
        print(f"[Run #{run_index}] Grounding falló ({grounding_err}), usando fallback sin tools...")
        text = ""
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=prompt,
            )
            text = response.text or ""
            print(f"[Run #{run_index}] Fallback OK, respuesta: {len(text)} chars")
        except Exception as fallback_err:
            print(f"[Run #{run_index}] Fallback también falló: {fallback_err}")
            raise fallback_err

    print(f"[Run #{run_index}] Texto crudo (primeros 300 chars): {text[:300]!r}")
    cleaned = re.sub(r"```json|```", "", text).strip()
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        print(f"[Run #{run_index}] ERROR: No se encontró JSON en la respuesta")
        raise ValueError(f"[Run {run_index}] Gemini no devolvió JSON. Respuesta: {text[:300]}")
    try:
        result = json.loads(m.group())
        print(f"[Run #{run_index}] JSON parseado OK: xG_home={result.get('xG_home_attack')}, xG_away={result.get('xG_away_attack')}")
    except json.JSONDecodeError as je:
        print(f"[Run #{run_index}] ERROR parseando JSON: {je}")
        raise ValueError(f"[Run {run_index}] JSON inválido: {je}")
    result["_run"] = run_index
    return result


def gemini_analyze(api_key, home, away, league, pin_home=None, pin_draw=None, pin_away=None, n_runs=10):
    results = []
    errors  = []

    print(f"\n{'='*60}")
    print(f"[NEA] Analizando: {home} vs {away} | Liga: {league} | {n_runs} runs")
    print(f"{'='*60}")

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {
            pool.submit(_single_gemini_call, api_key, home, away, league, i+1): i
            for i in range(n_runs)
        }
        for future in futures:
            try:
                results.append(future.result(timeout=90))
            except Exception as e:
                errors.append(str(e))
                print(f"[Run FAILED] {e}")

    print(f"\n[NEA] Runs exitosos: {len(results)}/{n_runs}")
    if errors:
        print(f"[NEA] Errores: {errors}")

    if not results:
        raise ValueError(f"Todas las llamadas a Gemini fallaron: {'; '.join(errors)}")

    averaged = {}
    for field in NUMERIC_FIELDS:
        vals = [r[field] for r in results if field in r and isinstance(r[field], (int, float))]
        averaged[field] = round(sum(vals) / len(vals), 4) if vals else 0.0

    def distance(r):
        return sum((r.get(f, 0) - averaged[f])**2 for f in NUMERIC_FIELDS)
    best_run = min(results, key=distance)
    averaged["analysis"] = best_run.get("analysis", "")

    # ── Corrección #5: Control de varianza entre runs ────────────────────────────
    # Alta varianza = Gemini está alucinando o los datos son inciertos.
    # Se alerta al usuario si la desviación estándar de xG supera 0.30.
    variance_warnings = []
    for field in ["xG_home_attack", "xG_away_attack"]:
        vals = [r[field] for r in results if field in r and isinstance(r[field], (int, float))]
        if len(vals) >= 2:
            mean = sum(vals) / len(vals)
            std  = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
            averaged[f"_std_{field}"] = round(std, 4)
            if std > 0.30:
                variance_warnings.append(f"{field}: std={std:.2f} (alta incertidumbre)")
    averaged["_variance_warnings"] = variance_warnings
    if variance_warnings:
        print(f"[NEA] ⚠ Alta varianza entre runs: {variance_warnings}")

    runs_data = []
    for r in sorted(results, key=lambda x: x.get("_run", 0)):
        runs_data.append({
            "run":        r.get("_run"),
            "xG_home":    round(r.get("xG_home_attack", 0), 3),
            "xG_away":    round(r.get("xG_away_attack", 0), 3),
            "form_home":  round(r.get("form_home", 0), 3),
            "form_away":  round(r.get("form_away", 0), 3),
            "form_draw":  round(r.get("form_draw", 0), 3),
            "home_adv":   round(r.get("home_advantage", 0), 3),
        })

    averaged["_runs_data"]  = runs_data
    averaged["_runs_ok"]    = len(results)
    averaged["_runs_total"] = n_runs
    return averaged


# ── Flask routes ───────────────────────────────────────────────────────────────

@app.route("/api/leagues")
def api_leagues():
    return jsonify(LIGAS)


@app.route("/api/matches")
def api_matches():
    league_id = request.args.get("league", "all")
    days      = float(request.args.get("days", 2))

    liga = next((l for l in LIGAS if l["id"] == league_id), LIGAS[0])
    tag_slugs = liga["slugs"]

    raw    = fetch_events_for_slugs(tag_slugs, days=days)
    games  = normalize_events(raw, days=days)

    return jsonify({
        "league":  liga["name"],
        "count":   len(games),
        "matches": games,
    })


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    data    = request.json or {}
    api_key = data.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY","")
    if not api_key:
        return jsonify({"error": "Gemini API Key requerida"}), 400

    home       = data.get("home","")
    away       = data.get("away","")
    league     = data.get("league","")
    poly_home  = data.get("poly_home")
    poly_draw  = data.get("poly_draw")
    poly_away  = data.get("poly_away")
    pin_home   = data.get("pin_home")
    pin_draw   = data.get("pin_draw")
    pin_away   = data.get("pin_away")

    if not home or not away:
        return jsonify({"error": "Faltan home/away"}), 400

    print(f"\n[API] /analyze request: {home} vs {away} | league={league}")
    print(f"[API] poly_home={poly_home}, poly_draw={poly_draw}, poly_away={poly_away}")
    try:
        ai = gemini_analyze(api_key, home, away, league, pin_home, pin_draw, pin_away)
        print(f"[API] gemini_analyze OK: {list(ai.keys())}")
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[API] GEMINI ERROR:\n{tb}")
        return jsonify({"error": f"Gemini error: {e}", "traceback": tb}), 500

    try:
        # ── Corrección #1: λ normalizado (modelo Maher/Dixon-Coles) ──────────────
        # λ = (ataque/μ) × (defensa_rival/μ) × μ × ventaja
        # Evita el error dimensional de multiplicar goles×goles.
        LEAGUE_AVG = 1.35  # promedio goles/equipo/partido (EPL, LaLiga, Serie A ≈ 1.2-1.4)
        lH = (ai["xG_home_attack"] / LEAGUE_AVG) * (ai["xGA_away_defense"] / LEAGUE_AVG) * LEAGUE_AVG * ai["home_advantage"]
        lA = (ai["xG_away_attack"]  / LEAGUE_AVG) * (ai["xGA_home_defense"]  / LEAGUE_AVG) * LEAGUE_AVG
        win, draw, loss = poisson_matrix(lH, lA)
        tot = win + draw + loss
        xH, xD, xA = win/tot, draw/tot, loss/tot

        if pin_home and pin_draw and pin_away:
            rH, rD, rA = 1/pin_home, 1/pin_draw, 1/pin_away
            ov = rH + rD + rA
            mH, mD, mA = rH/ov, rD/ov, rA/ov
        else:
            mH, mD, mA = xH, xD, xA

        # ── Corrección #3: injury_index multiplicativo ───────────────────────────
        # Multiplicar es más correcto: una lesión grave impacta más a un equipo
        # mediocre (0.50 × 0.70 = 0.35) que a uno dominante (0.90 × 0.70 = 0.63).
        # injury_index range: -0.30 a +0.05 → factor: 0.70 a 1.05
        fH = min(1.0, max(0.0, ai["form_home"] * (1.0 + ai["injury_index_home"])))
        fD = min(1.0, max(0.0, ai["form_draw"]))
        fA = min(1.0, max(0.0, ai["form_away"] * (1.0 + ai["injury_index_away"])))

        # ── Corrección #2 aplicada: v_real solo recibe p_xg y p_form ────────────
        vrH = v_real(xH, fH)
        vrD = v_real(xD, fD)
        vrA = v_real(xA, fA)

        def nea_signal(nea):
            if nea <= -10: return "BARGAIN"
            if nea >=  10: return "OVERPRICED"
            return "NEUTRAL"

        def outcome_block(label, team, p_poly, vr, p_market, p_xg, p_form):
            if p_poly is None:
                return {"label": label, "team": team, "no_data": True}
            nea = (p_poly - vr) * 100
            return {
                "label":    label,
                "team":     team,
                "p_poly":   round(p_poly * 100, 1),
                "v_real":   round(vr * 100, 1),
                "p_market": round(p_market * 100, 1),
                "p_xg":     round(p_xg * 100, 1),
                "p_form":   round(p_form * 100, 1),
                "nea":      round(nea, 1),
                "signal":   nea_signal(nea),
            }

        payload = {
            "home":    home,
            "away":    away,
            "league":  league,
            "poisson": {
                "lambda_home": round(lH, 3),
                "lambda_away": round(lA, 3),
                "win_pct":  round(xH * 100, 1),
                "draw_pct": round(xD * 100, 1),
                "loss_pct": round(xA * 100, 1),
            },
            "outcomes": [
                outcome_block("LOCAL",     home,   poly_home, vrH, mH, xH, fH),
                outcome_block("EMPATE",    "Draw", poly_draw, vrD, mD, xD, fD),
                outcome_block("VISITANTE", away,   poly_away, vrA, mA, xA, fA),
            ],
            "ai_vars": {
                "xG_home_attack":    round(ai["xG_home_attack"], 2),
                "xGA_home_defense":  round(ai["xGA_home_defense"], 2),
                "xG_away_attack":    round(ai["xG_away_attack"], 2),
                "xGA_away_defense":  round(ai["xGA_away_defense"], 2),
                "home_advantage":    round(ai["home_advantage"], 2),
                "injury_index_home": round(ai["injury_index_home"], 3),
                "injury_index_away": round(ai["injury_index_away"], 3),
            },
            "analysis":   ai.get("analysis",""),
            "runs_data":  ai.get("_runs_data", []),
            "runs_ok":    ai.get("_runs_ok", 1),
            "runs_total": ai.get("_runs_total", 5),
            "variance_warnings": ai.get("_variance_warnings", []),
        }
        print(f"[API] Respuesta final: poisson={payload.get('poisson')}")
        print(f"[API] Analysis preview: {str(payload.get('analysis',''))[:100]}")
        print(f"[API] Enviando 200 OK al frontend")
        return jsonify(payload)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print("ERROR en cálculo NEA:\n", tb)
        return jsonify({"error": f"Error en cálculo: {e}", "traceback": tb}), 500


@app.route("/api/debug-markets")
def api_debug_markets():
    """
    Diagnóstico: muestra los mercados crudos de Polymarket para un partido.
    Uso: GET /api/debug-markets?q=lille
    """
    q = (request.args.get("q") or "").lower()
    league_id = request.args.get("league", "all")
    days = float(request.args.get("days", 3))

    liga = next((l for l in LIGAS if l["id"] == league_id), LIGAS[0])
    raw = fetch_events_for_slugs(liga["slugs"], days=days)

    results = []
    for ev in raw:
        title = (ev.get("title") or "").lower()
        if q and q not in title:
            continue
        markets_info = []
        for m in (ev.get("markets") or []):
            markets_info.append({
                "question":      m.get("question"),
                "marketSlug":    m.get("marketSlug"),
                "outcomes":      m.get("outcomes"),
                "outcomePrices": m.get("outcomePrices"),
            })
        results.append({
            "title":   ev.get("title"),
            "slug":    ev.get("slug"),
            "markets": markets_info,
        })

    return jsonify(results)


if __name__ == "__main__":
    print("\n NEA Soccer Bot - Backend")
    port = int(os.environ.get("PORT", 5000))
    print(f"   http://localhost:{port}\n")
    app.run(debug=False, host="0.0.0.0", port=port)