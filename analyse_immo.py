"""
analyse_immo.py  (v3)  -  Analyseur d'annonce immobiliere (marche francais, DVF)
================================================================================
Nouveautes v3 :
  - Tendance des prix calculee depuis la DVF locale (annee par annee).
  - Recalage des ventes anciennes au niveau de prix d'AUJOURD'HUI (indexation).
  - Direction du marche (hausse / stable / baisse) + taux annuel.
(v2 : rayon geographique, terrain, reajustement selon options)

Pour le lancer :   python analyse_immo.py
"""

import os
import sys
import json
import math
import subprocess
import urllib.request
import urllib.parse
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

BASE = "https://files.data.gouv.fr/geo-dvf/latest/csv"
BAN = "https://api-adresse.data.gouv.fr/search/"
DOSSIER = "dvf_data"
ANNEES = ["2021", "2022", "2023", "2024", "2025"]   # 5 ans pour une vraie tendance
PRIX_M2_MIN, PRIX_M2_MAX = 300, 25000
RAYON_KM = 8
MOIS_COMPARABLES = 36        # fenetre de recence pour les comparables
TAUX_MIN, TAUX_MAX = -0.15, 0.20   # bornes de securite sur le taux annuel

OPTIONS = {
    "P": ("Piscine enterree",            "eur", 20000),
    "S": ("Panneaux solaires (proprio)", "eur", 10000),
    "G": ("Garage / box ferme",          "eur", 15000),
    "D": ("Dependances (grange, atelier)", "eur", 15000),
    "T": ("Grand terrain (> 1500 m2)",   "eur", 20000),
    "R": ("Renove recemment",            "pct",  0.08),
    "V": ("Vue degagee / exceptionnelle","pct",  0.07),
    "N": ("DPE A ou B (tres bon)",       "pct",  0.05),
    "F": ("DPE F ou G (passoire)",       "pct", -0.12),
}

# Ponderation des terrasses : X m2 de terrasse = 1 m2 habitable equivalent
TERRASSE_NON_COUVERTE = 3.0   # 3 m2 non couverte -> 1 m2 habitable (coeff 0.33)
TERRASSE_COUVERTE = 2.0       # 2 m2 couverte     -> 1 m2 habitable (coeff 0.50)


def departement_depuis_cp(cp):
    cp = cp.strip().zfill(5)
    if cp.startswith(("97", "98")):
        return cp[:3]
    if cp.startswith("20"):
        return "2A"
    return cp[:2]


def telecharger_si_besoin(dept):
    os.makedirs(DOSSIER, exist_ok=True)
    fichiers = []
    for annee in ANNEES:
        dest = os.path.join(DOSSIER, f"{dept}_{annee}.csv.gz")
        if not os.path.exists(dest):
            url = f"{BASE}/{annee}/departements/{dept}.csv.gz"
            try:
                print(f"  Telechargement des ventes {annee}...", flush=True)
                urllib.request.urlretrieve(url, dest)
            except Exception as e:
                print(f"  (impossible de recuperer {annee} : {e})")
                continue
        fichiers.append(dest)
    return fichiers


def charger(fichiers):
    cols = ["id_mutation", "date_mutation", "valeur_fonciere", "code_postal",
            "nom_commune", "type_local", "surface_reelle_bati",
            "surface_terrain", "longitude", "latitude"]
    df = pd.concat(
        [pd.read_csv(f, usecols=lambda c: c in cols,
                     dtype={"code_postal": "string", "id_mutation": "string"},
                     low_memory=False) for f in fichiers],
        ignore_index=True,
    )
    df["date_mutation"] = pd.to_datetime(df["date_mutation"], errors="coerce")
    for c in ["valeur_fonciere", "surface_reelle_bati", "surface_terrain",
              "longitude", "latitude"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["code_postal"] = df["code_postal"].str.zfill(5)
    return df


def geocoder(adresse, cp):
    try:
        q = urllib.parse.urlencode({"q": adresse, "postcode": cp, "limit": 1})
        with urllib.request.urlopen(BAN + "?" + q, timeout=10) as r:
            data = json.load(r)
        lon, lat = data["features"][0]["geometry"]["coordinates"]
        return lat, lon
    except Exception:
        return None


def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dlmb/2)**2
    return 2 * R * math.asin(math.sqrt(a))


def pool_zone(df, cp, type_local, centre):
    """Toutes les ventes du type dans la zone (rayon ou code postal), nettoyees.
    Sert de base a la fois aux comparables ET a la tendance."""
    base = df[(df["type_local"] == type_local) & (df["valeur_fonciere"] > 0)
              & (df["surface_reelle_bati"] > 0)].copy()
    batis = df[df["type_local"].isin({"Appartement", "Maison"})]
    simples = batis.groupby("id_mutation").size()
    base = base[base["id_mutation"].isin(simples[simples == 1].index)]
    base["prix_m2"] = base["valeur_fonciere"] / base["surface_reelle_bati"]
    base = base[(base["prix_m2"] >= PRIX_M2_MIN) & (base["prix_m2"] <= PRIX_M2_MAX)]

    if centre is not None:
        geo = base.dropna(subset=["latitude", "longitude"]).copy()
        geo["dist_km"] = geo.apply(
            lambda r: haversine(centre[0], centre[1], r["latitude"], r["longitude"]), axis=1)
        rayon = geo[geo["dist_km"] <= RAYON_KM]
        if len(rayon) >= 8:
            return rayon, f"rayon {RAYON_KM} km"
    return base[base["code_postal"] == str(cp).zfill(5)], "code postal"


def tendance(pool):
    """Serie annuelle du prix/m2 median + taux annuel moyen (borne)."""
    p = pool.copy()
    p["annee"] = p["date_mutation"].dt.year
    serie = p.groupby("annee").agg(med=("prix_m2", "median"),
                                   nb=("prix_m2", "size"))
    serie = serie[serie["nb"] >= 10]          # on ignore les annees trop maigres
    if len(serie) < 2:
        return serie, None
    annees = serie.index.tolist()
    p0, p1 = serie["med"].iloc[0], serie["med"].iloc[-1]
    n = annees[-1] - annees[0]
    taux = (p1 / p0) ** (1 / n) - 1
    taux = max(TAUX_MIN, min(TAUX_MAX, taux))
    return serie, taux


def graphique_marche(pool, cp, type_local, taux=None, chemin="marche.png"):
    """Trace le marche local facon cours de bourse (bougies trimestrielles)."""
    p = pool.dropna(subset=["date_mutation", "prix_m2"]).copy()
    p["periode"] = p["date_mutation"].dt.to_period("Q")
    grp = p.groupby("periode")["prix_m2"]
    valides = grp.size()[grp.size() >= 5].index
    if len(valides) < 4:                      # repli annuel si trop maigre
        p["periode"] = p["date_mutation"].dt.to_period("Y")
        grp = p.groupby("periode")["prix_m2"]
        valides = grp.size()[grp.size() >= 5].index
    stats = grp.agg(
        p10=lambda x: x.quantile(.10), q1=lambda x: x.quantile(.25),
        med="median", q3=lambda x: x.quantile(.75),
        p90=lambda x: x.quantile(.90), nb="size").loc[sorted(valides)]
    if len(stats) < 2:
        return None

    x = np.arange(len(stats)); med = stats["med"].values
    ma = pd.Series(med).rolling(3, min_periods=1).mean()   # niveau lisse
    plt.style.use("dark_background")
    fig, (ax, axv) = plt.subplots(2, 1, figsize=(11, 6.5), sharex=True,
        gridspec_kw={"height_ratios": [4, 1], "hspace": 0.05})
    fig.patch.set_facecolor("#0d1117")
    for a in (ax, axv):
        a.set_facecolor("#0d1117"); a.grid(color="#233043", linewidth=0.6, alpha=0.7)

    VERT, ROUGE, L = "#26a69a", "#ef5350", 0.6
    for i in range(len(stats)):
        c = VERT if (med[i] >= med[i-1] if i > 0 else True) else ROUGE
        ax.plot([x[i], x[i]], [stats["p10"].iloc[i], stats["p90"].iloc[i]], color=c, lw=1.2, zorder=2)
        bas, haut = stats["q1"].iloc[i], stats["q3"].iloc[i]
        ax.add_patch(Rectangle((x[i]-L/2, bas), L, haut-bas, facecolor=c, edgecolor=c, alpha=0.9, zorder=3))
        ax.plot([x[i]-L/2, x[i]+L/2], [med[i], med[i]], color="#e6edf3", lw=1.4, zorder=4)
    if len(med) >= 3:
        ax.plot(x, ma, color="#f0b429", lw=1.8, alpha=0.9, label="Tendance (moy. mobile)")
        ax.legend(loc="upper left", facecolor="#161b22", edgecolor="#233043",
                  labelcolor="#e6edf3", fontsize=9)
    vol_c = [VERT if (med[i] >= med[i-1] if i > 0 else True) else ROUGE for i in range(len(stats))]
    axv.bar(x, stats["nb"].values, width=L, color=vol_c, alpha=0.55)
    axv.set_ylabel("Ventes", color="#8b949e", fontsize=9)

    perf = f"{taux:+.1%}/an" if taux is not None else ""
    ax.set_title(f"{type_local}s  -  {cp}    {int(round(ma.iloc[-1])):,} EUR/m2 (lisse)   {perf}".replace(",", " "),
                 color="#e6edf3", fontsize=14, fontweight="bold", loc="left", pad=12)
    ax.set_ylabel("Prix / m2 (EUR)", color="#8b949e")
    ax.tick_params(colors="#8b949e"); axv.tick_params(colors="#8b949e")
    axv.set_xticks(x); axv.set_xticklabels([str(pp) for pp in stats.index],
                                           rotation=45, ha="right", fontsize=8, color="#8b949e")
    for s in list(ax.spines.values()) + list(axv.spines.values()):
        s.set_color("#233043")
    fig.text(0.99, 0.01, "Source : DVF (ventes reelles) - facon cours de bourse",
             ha="right", color="#586069", fontsize=7)
    fig.savefig(chemin, dpi=130, bbox_inches="tight", facecolor="#0d1117", format="png")
    plt.close(fig)
    return chemin


def ouvrir_fichier(chemin):
    try:
        if os.name == "nt":
            os.startfile(chemin)
        elif sys.platform == "darwin":
            subprocess.run(["open", chemin])
        else:
            subprocess.run(["xdg-open", chemin])
    except Exception:
        pass


def score_marche(taux, n_ventes_an, dispersion):
    """Note 0-100 du marche local : tendance + liquidite + stabilite.
    Indicateur de contexte, pas une note du bien lui-meme."""
    # Tendance : -5%/an -> 0, +5%/an -> 100
    s_tend = max(0, min(100, (taux + 0.05) / 0.10 * 100)) if taux is not None else 50
    # Liquidite : 0 vente/an -> 0, 60+ ventes/an -> 100
    s_liq = max(0, min(100, n_ventes_an / 60 * 100))
    # Stabilite : dispersion faible = marche lisible
    s_stab = max(0, min(100, (0.60 - dispersion) / 0.40 * 100))
    note = 0.45 * s_tend + 0.30 * s_liq + 0.25 * s_stab
    if note >= 70:
        libelle = "Marche porteur"
    elif note >= 50:
        libelle = "Marche correct"
    elif note >= 30:
        libelle = "Marche mou"
    else:
        libelle = "Marche difficile"
    return round(note), libelle


def analyse_marche(df, cp, type_local, centre=None):
    """Analyse du MARCHE d'une zone, sans bien precis : prix/m2, tendance,
    score, volume. Sert de base a l'analyse par code postal."""
    pool, mode = pool_zone(df, cp, type_local, centre)
    if len(pool) < 5:
        return {"ok": False, "raison": f"Trop peu de ventes ({len(pool)}) sur cette zone."}

    serie, taux = tendance(pool)
    limite = pd.Timestamp.today() - pd.DateOffset(months=MOIS_COMPARABLES)
    recents = pool[pool["date_mutation"] >= limite]
    base = recents if len(recents) >= 5 else pool

    pm2 = float(base["prix_m2"].median())
    q1, q3 = float(base["prix_m2"].quantile(.25)), float(base["prix_m2"].quantile(.75))
    dispersion = (q3 - q1) / pm2 if pm2 else 0
    serie_list = ([] if serie is None else
                  [(int(a), float(r["med"]), int(r["nb"])) for a, r in serie.iterrows()])
    ventes_an = (sum(nb for _, _, nb in serie_list) / len(serie_list)) if serie_list else 0
    note, libelle = score_marche(taux, ventes_an, dispersion)

    surf_med = float(base["surface_reelle_bati"].median())
    return {"ok": True, "mode": mode, "cp": cp, "type_local": type_local,
            "n": len(base), "pm2": pm2, "q1": q1, "q3": q3,
            "surface_mediane": surf_med,
            "prix_median": pm2 * surf_med,
            "serie": serie_list, "taux": taux,
            "score": note, "score_libelle": libelle,
            "ventes_an": ventes_an, "dispersion": dispersion,
            "projection_pm2": pm2 * (1 + taux) if taux is not None else None,
            "pool": pool}


def analyser_bien(df, cp, type_local, surface, prix, options="", centre=None,
                  terr_nc=0.0, terr_c=0.0):
    """Version reutilisable (web/API) : renvoie un dict complet. Meme logique
    que le rapport console."""
    pool, mode = pool_zone(df, cp, type_local, centre)
    if len(pool) < 3:
        return {"ok": False, "raison": f"Trop peu de ventes ({len(pool)}) sur cette zone."}
    serie, taux = tendance(pool)

    limite = pd.Timestamp.today() - pd.DateOffset(months=MOIS_COMPARABLES)
    comps = pool[(pool["date_mutation"] >= limite)
                 & (pool["surface_reelle_bati"].between(surface*0.65, surface*1.35))].copy()
    if len(comps) < 3:
        return {"ok": False, "raison": f"Trop peu de comparables recents ({len(comps)})."}

    if taux is not None:
        ae = (pd.Timestamp.today() - comps["date_mutation"]).dt.days / 365.25
        comps["pm2r"] = comps["prix_m2"] * (1 + taux) ** ae
    else:
        comps["pm2r"] = comps["prix_m2"]

    pm2 = comps["pm2r"].median()
    estim = pm2 * surface
    basse = comps["pm2r"].quantile(.25) * surface
    haute = comps["pm2r"].quantile(.75) * surface

    lignes, ajust = [], 0.0
    # Terrasses : converties en m2 habitables equivalents (surface ponderee)
    equiv = (terr_nc or 0) / TERRASSE_NON_COUVERTE + (terr_c or 0) / TERRASSE_COUVERTE
    if equiv > 0:
        terr_val = pm2 * equiv
        ajust += terr_val
        lignes.append((f"Terrasse (equiv. {equiv:.0f} m2 habitable)", terr_val))
    for k in (options or "").upper():
        if k in OPTIONS:
            label, typ, val = OPTIONS[k]
            plus = val if typ == "eur" else estim * val
            ajust += plus
            lignes.append((label, plus))
    estim_aj = estim + ajust

    ecart = (prix - estim_aj) / estim_aj if estim_aj else 0
    verdict = ("SURCOTE" if ecart > 0.10 else
               "SOUS-COTE" if ecart < -0.10 else "AU PRIX DU MARCHE")
    serie_list = ([] if serie is None else
                  [(int(a), float(r["med"]), int(r["nb"])) for a, r in serie.iterrows()])

    # Projection a 12 mois : extrapolation de la tendance locale (pas une prediction)
    projection = estim_aj * (1 + taux) if taux is not None else None

    # Score du marche local
    dispersion = float((comps["pm2r"].quantile(.75) - comps["pm2r"].quantile(.25)) / pm2)
    ventes_an = (sum(nb for _, _, nb in serie_list) / len(serie_list)) if serie_list else 0
    note, libelle = score_marche(taux, ventes_an, dispersion)

    return {"ok": True, "mode": mode, "n": len(comps),
            "terrain_median": (float(comps["surface_terrain"].median())
                               if comps["surface_terrain"].notna().any() else None),
            "serie": serie_list, "taux": taux, "pm2": pm2,
            "estim_marche": estim, "basse": basse, "haute": haute,
            "options": lignes, "estim_ajustee": estim_aj, "prix": prix,
            "ecart": ecart*100, "verdict": verdict, "offre": min(prix, estim_aj),
            "projection": projection, "score": note, "score_libelle": libelle,
            "dispersion": dispersion, "ventes_an": ventes_an,
            "pool": pool, "type_local": type_local, "cp": cp, "surface": surface}


def euros(x):
    return f"{x:,.0f}".replace(",", " ") + " EUR"


def main():
    print("\n=== ANALYSEUR D'ANNONCE IMMOBILIERE v3 (donnees DVF) ===\n")
    cp = input("Code postal du bien (ex: 07000)              : ").strip()
    adresse = input("Adresse pour la precision (facultatif, Entree pour passer) : ").strip()
    t = input("Type - A pour appartement, M pour maison     : ").strip().upper()
    type_local = "Maison" if t == "M" else "Appartement"
    surface = float(input("Surface habitable en m2 (ex: 150)            : ").replace(",", "."))
    prix = float(input("Prix affiche dans l'annonce (ex: 450000)     : ").replace(" ", ""))

    print("\n  Options du bien - tape les lettres qui s'appliquent (ex: PSG), ou Entree :")
    for k, (label, typ, val) in OPTIONS.items():
        montant = euros(val) if typ == "eur" else f"{val:+.0%}"
        print(f"      {k} = {label}  ({montant})")
    choix = input("  > ").strip().upper()

    dept = departement_depuis_cp(cp)
    print(f"\nDepartement {dept} - preparation des donnees...")
    fichiers = telecharger_si_besoin(dept)
    if not fichiers:
        print("Aucune donnee disponible. Verifie ta connexion ou le code postal.")
        return
    df = charger(fichiers)

    centre = geocoder(adresse, cp) if adresse else None
    if adresse and centre is None:
        print("  (adresse non localisee -> on reste au niveau code postal)")

    pool, mode = pool_zone(df, cp, type_local, centre)
    if len(pool) < 3:
        print(f"\nTrop peu de ventes ({len(pool)}) sur cette zone. Essaie un CP voisin.\n")
        return

    # Tendance des prix (sur toute la periode dispo)
    serie, taux = tendance(pool)

    # Comparables : recents + bande de surface
    limite = pd.Timestamp.today() - pd.DateOffset(months=MOIS_COMPARABLES)
    comps = pool[(pool["date_mutation"] >= limite)
                 & (pool["surface_reelle_bati"].between(surface*0.65, surface*1.35))].copy()
    if len(comps) < 3:
        print(f"\nTrop peu de comparables recents ({len(comps)}). Essaie un CP voisin.\n")
        return

    # Recalage a aujourd'hui : chaque vente est reevaluee au prix actuel
    aujourd_hui = pd.Timestamp.today()
    if taux is not None:
        annees_ecoulees = (aujourd_hui - comps["date_mutation"]).dt.days / 365.25
        comps["prix_m2_recale"] = comps["prix_m2"] * (1 + taux) ** annees_ecoulees
    else:
        comps["prix_m2_recale"] = comps["prix_m2"]

    pm2 = comps["prix_m2_recale"].median()
    estim_marche = pm2 * surface
    basse = comps["prix_m2_recale"].quantile(.25) * surface
    haute = comps["prix_m2_recale"].quantile(.75) * surface

    # Options
    lignes_opt, ajust = [], 0.0
    for k in choix:
        if k in OPTIONS:
            label, typ, val = OPTIONS[k]
            plus = val if typ == "eur" else estim_marche * val
            ajust += plus
            lignes_opt.append((label, plus))
    estim_ajustee = estim_marche + ajust

    ecart = (prix - estim_ajustee) / estim_ajustee
    verdict = ("SURCOTE" if ecart > 0.10 else
               "SOUS-COTE (opportunite)" if ecart < -0.10 else "AU PRIX DU MARCHE")

    # --- Rapport ---
    print("\n" + "=" * 54)
    print(f"  {type_local} de {surface:.0f} m2 - {cp}")
    print(f"  {len(comps)} ventes reelles comparables ({mode})")
    if type_local == "Maison" and comps["surface_terrain"].notna().any():
        print(f"  Terrain median des comparables : {comps['surface_terrain'].median():.0f} m2")

    # Tendance
    if taux is not None:
        direction = ("EN HAUSSE" if taux > 0.01 else
                     "EN BAISSE" if taux < -0.01 else "STABLE")
        print("\n  Tendance des prix (prix/m2 median par annee) :")
        for annee, row in serie.iterrows():
            print(f"      {annee} : {euros(row['med'])}/m2   ({int(row['nb'])} ventes)")
        print(f"  Marche {direction}  ({taux:+.1%}/an)  ->  estimation recalee a aujourd'hui")

    print()
    print(f"  Prix/m2 median (recale) : {euros(pm2)}/m2")
    print(f"  Valeur marche 'standard': {euros(estim_marche)}")
    print(f"    fourchette           : {euros(basse)} - {euros(haute)}")
    if lignes_opt:
        print("  " + "-" * 50)
        print("  Reajustement options (indicatif) :")
        for label, plus in lignes_opt:
            signe = "+" if plus >= 0 else "-"
            print(f"      {signe} {label:<32} {euros(abs(plus))}")
        print(f"  VALEUR ESTIMEE AJUSTEE  : {euros(estim_ajustee)}")
    else:
        print(f"  VALEUR ESTIMEE          : {euros(estim_ajustee)}")
    print("  " + "-" * 50)
    print(f"  Prix affiche            : {euros(prix)}")
    print(f"  Ecart / valeur ajustee  : {ecart*100:+.1f} %")
    print(f"  >>> {verdict}")
    print(f"  Offre a viser           : {euros(min(prix, estim_ajustee))}")
    print("=" * 54 + "\n")

    # Graphique du marche local, facon cours de bourse
    chemin = graphique_marche(pool, cp, type_local, taux)
    if chemin:
        print(f"  Graphique du marche enregistre : {os.path.abspath(chemin)}")
        ouvrir_fichier(chemin)
    else:
        print("  (pas assez de ventes pour tracer le graphique)")


if __name__ == "__main__":
    main()
