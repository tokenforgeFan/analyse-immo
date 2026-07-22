"""
app.py  -  Interface web de l'analyseur immobilier (facon Reventure).
Tu colles l'URL d'une annonce Leboncoin / PAP / Bien'ici -> analyse via la DVF.

Lancer :   python app.py     puis ouvrir  http://localhost:5000

Necessite :  pip install flask requests beautifulsoup4
Extraction auto fiable (sites JS) :  pip install playwright ; playwright install chromium
"""

import os
import io
import re
import json
import base64
import threading

import pandas as pd
from flask import Flask, request, render_template_string
import analyse_immo as A

try:
    import requests
    from bs4 import BeautifulSoup
except Exception:
    requests = None

try:
    from extracteur import fetch_playwright, DISPO as PLAYWRIGHT_DISPO
except Exception:
    fetch_playwright, PLAYWRIGHT_DISPO = None, False

app = Flask(__name__)
_CACHE_DF = {}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "fr-FR,fr;q=0.9",
}


def detecter_site(url):
    if "leboncoin" in url:
        return "leboncoin"
    if "bienici" in url:
        return "bienici"
    if "pap.fr" in url or "pap.immo" in url:
        return "pap"
    return "inconnu"


_IGNORER = {"similar", "related", "recommendations", "suggestions",
            "similarads", "similar_ads", "nearby", "autres"}


def _tous_les_prix(obj, out):
    """Collecte les nombres ressemblant a un prix, en ignorant les blocs
    d'annonces similaires/suggerees."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in _IGNORER:
                continue
            if k.lower() in ("price", "price_cents") and isinstance(v, (int, float)):
                out.append(v)
            elif k.lower() == "price" and isinstance(v, list):
                out.extend(x for x in v if isinstance(x, (int, float)))
            else:
                _tous_les_prix(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _tous_les_prix(v, out)


def _prix_leboncoin(data):
    """Cible le prix de l'annonce principale (chemin Leboncoin) en priorite."""
    try:
        ad = data["props"]["pageProps"]["ad"]
        p = ad.get("price")
        if isinstance(p, list) and p:
            p = p[0]
        if isinstance(p, (int, float)) and 10000 <= p <= 5_000_000:
            return int(p)
        pc = ad.get("price_cents")
        if pc and 10000 <= pc / 100 <= 5_000_000:
            return int(pc / 100)
    except Exception:
        pass
    return None


def _prix_realiste(data):
    """Repli : parmi les candidats (hors annonces similaires), le plus grand prix
    dans une fourchette immo plausible. Gere les centimes."""
    bruts = []
    _tous_les_prix(data, bruts)
    cands = []
    for v in bruts:
        for c in (v, v / 100):
            if 10000 <= c <= 5_000_000:
                cands.append(int(c))
    return max(cands) if cands else None


def _chercher(dico, cles):
    if isinstance(dico, dict):
        for k, v in dico.items():
            if k.lower() in cles and isinstance(v, (int, float, str)):
                return v
            t = _chercher(v, cles)
            if t is not None:
                return t
    elif isinstance(dico, list):
        for v in dico:
            t = _chercher(v, cles)
            if t is not None:
                return t
    return None


# ---- Recuperation de la page (2 methodes) ---------------------------------
def fetch_simple(url):
    if requests is None:
        return None
    try:
        return requests.get(url, headers=HEADERS, timeout=15).text
    except Exception:
        return None


def recuperer_html(url):
    """Essaie le navigateur Playwright (fiable sur sites JS), sinon requete simple."""
    html = fetch_playwright(url) if fetch_playwright else None
    if html:
        return html, "navigateur"
    return fetch_simple(url), "requete simple"


# ---- Lecture des infos dans la page ---------------------------------------
def parser_html(url, html, source=""):
    infos = {"site": detecter_site(url), "url": url, "source": source}
    if not html:
        infos["note"] = "Page non recuperee (site protege ?). Remplis le formulaire."
        return infos
    soup = BeautifulSoup(html, "html.parser")

    # 1) JSON-LD (schema.org) : present sur la plupart des annonces rendues
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "{}")
        except Exception:
            continue
        if not infos.get("prix"):
            p = _chercher(data, {"price"})
            if p:
                pv = re.sub(r"[^\d]", "", str(p))
                if pv and 10000 <= int(pv) <= 5_000_000:
                    infos["prix"] = pv
        if not infos.get("code_postal"):
            c = _chercher(data, {"postalcode"})
            if c:
                infos["code_postal"] = re.sub(r"[^\d]", "", str(c))[:5]

    # 2) Etat JS embarque (Leboncoin __NEXT_DATA__ / Bien'ici __INITIAL_STATE__)
    if infos["site"] in ("leboncoin", "bienici"):
        raw = None
        blob = soup.find("script", id="__NEXT_DATA__")
        if blob and blob.string:
            raw = blob.string
        else:
            mm = re.search(r'__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>', html, re.S)
            raw = mm.group(1) if mm else None
        if raw:
            try:
                data = json.loads(raw)
                prix_real = _prix_leboncoin(data) or _prix_realiste(data)
                if prix_real:
                    infos["prix"] = str(prix_real)
                cp = _chercher(data, {"zipcode", "code_postal", "postalcode"})
                if cp:
                    infos["code_postal"] = re.sub(r"[^\d]", "", str(cp))[:5]
                surf = _chercher(data, {"square", "surface", "surfacearea"})
                if surf:
                    infos["surface"] = re.sub(r"[^\d]", "", str(surf))
            except Exception:
                pass

    # 3) Repli sur le texte rendu
    texte = soup.get_text(" ", strip=True)
    if not infos.get("surface"):
        ms = re.search(r"(\d{2,3})\s?m", texte)
        if ms:
            infos["surface"] = ms.group(1)
    if not infos.get("code_postal"):
        mc = re.search(r"\b(\d{5})\b", texte)
        if mc:
            infos["code_postal"] = mc.group(1)
    if not infos.get("type_local"):
        infos["type_local"] = "Maison" if "maison" in texte.lower()[:2000] else "Appartement"
    if not infos.get("prix"):
        montants = re.findall(r"(\d[\d\s\u00a0.]{3,})\s?(?:€|EUR|euros)", texte)
        cands = [int(re.sub(r"[^\d]", "", m)) for m in montants if re.sub(r"[^\d]", "", m)]
        cands = [c for c in cands if 10000 <= c <= 5_000_000]
        if cands:
            infos["prix"] = str(cands[0])
    return infos


def extraire(url):
    html, source = recuperer_html(url)
    return parser_html(url, html, source)


# ---- Donnees + graphique ---------------------------------------------------
BASE_DUCKDB = "dvf.duckdb"

try:
    import duckdb
except Exception:
    duckdb = None


def get_df(cp):
    """Charge les ventes du departement : base DuckDB si presente (production),
    sinon telechargement CSV a la volee (mode local)."""
    dept = A.departement_depuis_cp(cp)
    if dept in _CACHE_DF:
        return _CACHE_DF[dept]

    # 1) Base de production
    if duckdb is not None and os.path.exists(BASE_DUCKDB):
        try:
            con = duckdb.connect(BASE_DUCKDB, read_only=True)
            df = con.execute(
                "SELECT * FROM ventes WHERE departement = ?", [dept]
            ).fetch_df()
            con.close()
            if len(df):
                df["date_mutation"] = pd.to_datetime(df["date_mutation"])
                if len(_CACHE_DF) >= MAX_DEPTS_CACHE:
                    _CACHE_DF.pop(next(iter(_CACHE_DF)))   # evite de saturer la RAM
                _CACHE_DF[dept] = df
                return df
        except Exception:
            pass

    # 2) Repli : CSV telecharges a la demande
    fichiers = A.telecharger_si_besoin(dept)
    _CACHE_DF[dept] = A.charger(fichiers) if fichiers else None
    return _CACHE_DF[dept]


_LOCK_GRAPHE = threading.Lock()
MAX_DEPTS_CACHE = int(os.environ.get("MAX_DEPTS_CACHE", 3))   # 3 = petit hebergeur (512 Mo)


def chart_base64(res):
    """Genere le graphe en memoire (pas de fichier partage entre utilisateurs).
    matplotlib n'est pas thread-safe -> un seul graphe a la fois."""
    buf = io.BytesIO()
    with _LOCK_GRAPHE:
        ok = A.graphique_marche(res["pool"], res["cp"], res["type_local"],
                                res["taux"], chemin=buf)
    if not ok:
        return None
    return base64.b64encode(buf.getvalue()).decode()


PAGE = """
<!doctype html><html lang=fr><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=robots content="noindex, nofollow">
<title>Analyse immo - DVF</title>
<style>
 body{background:#0d1117;color:#e6edf3;font-family:system-ui,Arial;margin:0;padding:24px}
 .box{max-width:820px;margin:0 auto}
 h1{font-size:22px;margin:0 0 4px} .sub{color:#8b949e;margin:0 0 22px;font-size:14px}
 input,select{background:#161b22;border:1px solid #30363d;color:#e6edf3;padding:11px;
   border-radius:8px;font-size:15px;width:100%;box-sizing:border-box}
 label{display:block;margin:12px 0 5px;color:#8b949e;font-size:13px}
 .row{display:flex;gap:12px} .row>div{flex:1}
 button{background:#238636;color:#fff;border:0;padding:12px 20px;border-radius:8px;
   font-size:15px;font-weight:600;cursor:pointer;margin-top:18px;width:100%}
 .card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:20px;margin-top:18px}
 .verdict{font-size:20px;font-weight:700;padding:10px 14px;border-radius:8px;display:inline-block}
 .SURCOTE{background:#3d1418;color:#ef5350} .AUPRIXDUMARCHE{background:#1c2b1e;color:#3fb950}
 .SOUSCOTE{background:#132a26;color:#26a69a}
 table{width:100%;border-collapse:collapse;margin-top:10px} td{padding:6px 0;font-size:15px}
 td.k{color:#8b949e} td.v{text-align:right;font-weight:600}
 img{width:100%;border-radius:10px;margin-top:14px}
 .opt{color:#8b949e;font-size:13px;margin-top:6px}
 .warn{color:#d29922;font-size:13px;margin-top:8px}
 .ok{color:#3fb950;font-size:13px}
 .opts{display:grid;grid-template-columns:1fr 1fr;gap:9px 16px;margin-top:6px}
 .chk{display:flex;align-items:center;gap:8px;color:#e6edf3;font-size:14px;margin:0}
 .chk input{width:auto;margin:0}
 .amt{color:#8b949e;font-size:12px;margin-left:auto}
 details.aide{margin-top:14px;background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:10px 12px}
 details.aide summary{cursor:pointer;color:#58a6ff;font-size:14px}
 .aidetxt{color:#c9d1d9;font-size:13px;line-height:1.55;margin-top:8px}
 .aidetxt b{color:#e6edf3}
 .score{margin-top:18px;padding-top:14px;border-top:1px solid #30363d}
 .scoretop{display:flex;justify-content:space-between;align-items:baseline;font-size:15px}
 .scoreval{font-weight:700;font-size:18px}
 .bar{height:8px;background:#21262d;border-radius:5px;margin-top:8px;overflow:hidden}
 .fill{height:100%;background:linear-gradient(90deg,#ef5350,#d29922,#3fb950);border-radius:5px}
 .scoredet{color:#8b949e;font-size:12px;margin-top:7px}
 .proj{margin-top:12px;font-size:15px}
 .footer{color:#586069;font-size:11px;line-height:1.5;margin-top:26px;
   border-top:1px solid #21262d;padding-top:12px}
</style></head><body><div class=box>
 <h1>Analyseur d'annonce immobiliere</h1>
 <p class=sub>Colle l'URL d'une annonce Leboncoin, PAP ou Bien'ici, ou saisis directement les infos du bien.
   Donnees : DVF (ventes reelles).</p>

 <form method=post action="/">
   <label>URL de l'annonce</label>
   <input name=url placeholder="https://www.bienici.com/annonce/..." value="{{ url|default('') }}">
   <button type=submit>Analyser</button>
 </form>

 {% if form %}
 <div class=card>
   <p class=sub>Verifie et complete les infos
     {% if form.note %}<span class=warn>({{ form.note }})</span>
     {% elif form.source %}<span class=ok>(lu via {{ form.source }})</span>{% endif %}</p>
   <form method=post action="/resultat">
     <input type=hidden name=url value="{{ form.url }}">
     <div class=row>
       <div><label>Code postal</label><input name=code_postal value="{{ form.code_postal|default('') }}"></div>
       <div><label>Type</label><select name=type_local>
         <option {% if form.type_local=='Maison' %}selected{% endif %}>Maison</option>
         <option {% if form.type_local!='Maison' %}selected{% endif %}>Appartement</option>
       </select></div>
     </div>
     <div class=row>
       <div><label>Surface (m2)</label><input name=surface value="{{ form.surface|default('') }}"></div>
       <div><label>Prix affiche (EUR)</label><input name=prix value="{{ form.prix|default('') }}"></div>
     </div>
     <div class=row>
       <div><label>Terrasse non couverte (m2)</label><input name=terr_nc value="{{ form.terr_nc|default('') }}"></div>
       <div><label>Terrasse couverte (m2)</label><input name=terr_c value="{{ form.terr_c|default('') }}"></div>
     </div>
     <p class=opt>Ponderation : 3 m2 non couverte = 1 m2 habitable ; 2 m2 couverte = 1 m2.</p>
     <label>Adresse (facultatif, ameliore la precision)</label>
     <input name=adresse value="{{ form.adresse|default('') }}">
     <label>Options du bien</label>
     <div class=opts>
       {% for k, lab, montant in options_list %}
       <label class=chk><input type=checkbox name=options value="{{ k }}"
         {% if form and form.options and k in form.options %}checked{% endif %}> {{ lab }}
         <span class=amt>{{ montant }}</span></label>
       {% endfor %}
     </div>
     <details class=aide>
       <summary>Aide - laquelle cocher ?</summary>
       <div class=aidetxt>
         L'outil part de la valeur marche standard et <b>ajoute</b> la plus-value de chaque option cochee.<br><br>
         <b>Piscine (+20 000)</b> : seulement enterree et en bon etat.<br>
         <b>Solaire (+10 000)</b> : uniquement si les panneaux appartiennent au proprietaire (pas loues).<br>
         <b>Garage (+15 000)</b> : garage ferme, pas une place exterieure. Vaut plus en ville.<br>
         <b>Dependances (+15 000)</b> : grange, atelier, studio... surface annexe reellement exploitable.<br>
         <b>Grand terrain (+20 000)</b> : seulement si nettement au-dessus de la normale du secteur (~1500 m2+), sinon deja compte dans le prix/m2.<br>
         <b>Renove (+8 %)</b> : refait a neuf (cuisine, sdb, elec, isolation), pas un coup de peinture.<br>
         <b>Vue (+7 %)</b> : vue degagee / exceptionnelle, pas juste "pas de vis-a-vis".<br>
         <b>DPE A/B (+5 %)</b> : tres performant, faibles charges.<br>
         <b>DPE F/G (-12 %)</b> : passoire thermique, seul malus et le plus fiable - decote reelle du marche.<br><br>
         Deux regles : dans le doute, ne coche pas ; reste sobre (cumuler gonfle vite l'estimation).
         Montants modifiables en haut de analyse_immo.py (bloc OPTIONS).
       </div>
     </details>
     <button type=submit>Voir l'analyse</button>
   </form>
 </div>
 {% endif %}

 {% if res %}
 <div class=card>
   {% if not res.ok %}
     <p style="color:#ef5350">{{ res.raison }}</p>
   {% else %}
   <span class="verdict {{ res.verdict|replace(' ','')|replace('-','') }}">{{ res.verdict }}
     ({{ '%+.1f'|format(res.ecart) }} %)</span>
   <table>
     <tr><td class=k>{{ res.type_local }} de {{ res.surface|int }} m2 - {{ res.cp }}</td>
         <td class=v>{{ res.n }} ventes ({{ res.mode }})</td></tr>
     <tr><td class=k>Prix/m2 median (recale)</td><td class=v>{{ e(res.pm2) }}/m2</td></tr>
     <tr><td class=k>Valeur marche standard</td><td class=v>{{ e(res.estim_marche) }}</td></tr>
     {% for label, plus in res.options %}
     <tr><td class=k>+ {{ label }}</td><td class=v>{{ e(plus) }}</td></tr>
     {% endfor %}
     <tr><td class=k><b>Valeur estimee ajustee</b></td><td class=v>{{ e(res.estim_ajustee) }}</td></tr>
     <tr><td class=k>Prix affiche</td><td class=v>{{ e(res.prix) }}</td></tr>
     <tr><td class=k>Offre a viser</td><td class=v>{{ e(res.offre) }}</td></tr>
   </table>

   <div class=score>
     <div class=scoretop>
       <span>Marche local : <b>{{ res.score_libelle }}</b></span>
       <span class=scoreval>{{ res.score }}/100</span>
     </div>
     <div class=bar><div class=fill style="width:{{ res.score }}%"></div></div>
     <div class=scoredet>
       {% if res.taux is not none %}Tendance {{ '%+.1f'|format(res.taux*100) }} %/an -
       {% endif %}~{{ res.ventes_an|int }} ventes/an - dispersion {{ '%.0f'|format(res.dispersion*100) }} %
     </div>
     {% if res.projection %}
     <div class=proj>Projection a 12 mois : <b>{{ e(res.projection) }}</b>
       <span class=amt>(si la tendance locale se poursuit)</span></div>
     {% endif %}
   </div>
   {% if chart %}<img src="data:image/png;base64,{{ chart }}">{% endif %}
   {% endif %}
 </div>
 {% endif %}
 <p class=footer>Estimations statistiques issues des donnees DVF (DGFiP, open data), agregees
   et anonymisees - aucune transaction individuelle n'est publiee. Valeur indicative,
   sans portee contractuelle : ne remplace pas l'avis d'un professionnel.</p>
</div></body></html>
"""


def _options_list():
    out = []
    for k, (label, typ, val) in A.OPTIONS.items():
        montant = (f"+{val:,} EUR".replace(",", " ") if typ == "eur" else f"{val:+.0%}")
        out.append((k, label, montant))
    return out


@app.route("/robots.txt")
def robots():
    # Conformite DVF : les transactions ne doivent pas etre indexees
    return "User-agent: *\nDisallow: /\n", 200, {"Content-Type": "text/plain"}


@app.route("/", methods=["GET", "POST"])
def accueil():
    ctx = {"e": A.euros, "playwright": PLAYWRIGHT_DISPO, "options_list": _options_list()}
    if request.method == "POST":
        url = request.form.get("url", "").strip()
        ctx["url"] = url
        infos = extraire(url) if url else {"url": url}
        if url and not (infos.get("prix") or infos.get("surface") or infos.get("code_postal")):
            infos.setdefault("note", "l'annonce n'a pas pu etre lue automatiquement - "
                                     "complete les champs ci-dessous")
        ctx["form"] = infos
    return render_template_string(PAGE, **ctx)


@app.route("/resultat", methods=["POST"])
def resultat():
    f = request.form
    ctx = {"e": A.euros, "url": f.get("url", ""), "playwright": PLAYWRIGHT_DISPO,
           "options_list": _options_list()}
    try:
        cp = f["code_postal"].strip()
        type_local = f.get("type_local", "Appartement")
        surface = float(f["surface"].replace(",", "."))
        prix = float(re.sub(r"[^\d]", "", f["prix"]))
        options = "".join(f.getlist("options"))
        adresse = f.get("adresse", "").strip()
        terr_nc = float(f.get("terr_nc", "") or 0)
        terr_c = float(f.get("terr_c", "") or 0)
    except Exception:
        ctx["form"] = {
            "url": f.get("url", ""), "code_postal": f.get("code_postal", ""),
            "type_local": f.get("type_local", ""), "surface": f.get("surface", ""),
            "prix": f.get("prix", ""), "adresse": f.get("adresse", ""),
            "options": "".join(f.getlist("options")),
            "terr_nc": f.get("terr_nc", ""), "terr_c": f.get("terr_c", ""),
            "note": "Surface et prix sont obligatoires - complete puis relance.",
        }
        return render_template_string(PAGE, **ctx)

    df = get_df(cp)
    if df is None:
        ctx["res"] = {"ok": False, "raison": "Donnees DVF indisponibles pour ce departement."}
        return render_template_string(PAGE, **ctx)

    centre = A.geocoder(adresse, cp) if adresse else None
    res = A.analyser_bien(df, cp, type_local, surface, prix, options, centre, terr_nc, terr_c)
    ctx["res"] = res
    if res.get("ok"):
        ctx["chart"] = chart_base64(res)
    return render_template_string(PAGE, **ctx)


if __name__ == "__main__":
    etat = "avec navigateur Playwright" if PLAYWRIGHT_DISPO else "sans Playwright (extraction limitee)"
    base = "base DuckDB" if (duckdb and os.path.exists(BASE_DUCKDB)) else "CSV a la demande"
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "1") == "1"    # DEBUG=0 en production
    print(f"\n  Analyseur immo demarre ({etat}, {base}).")
    print(f"  Ouvre ton navigateur sur  http://localhost:{port}\n")
    app.run(debug=debug, port=port, host="0.0.0.0" if not debug else "127.0.0.1")
