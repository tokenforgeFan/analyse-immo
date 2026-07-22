"""
loyers.py  -  Loyers indicatifs par commune ("Carte des loyers", ANIL).

Source : Estimations ANIL, a partir des donnees du Groupe SeLoger et de leboncoin
         (data.gouv.fr, millesime 2025, loyers charges comprises, biens loues vides).

ATTENTION : ce sont des loyers d'ANNONCES modelises, pas des loyers signes.
Instantane annuel -> ordre de grandeur, pas une mesure exacte.

Le fichier est indexe par code INSEE de commune (pas par code postal).
"""

import os
import re
import urllib.request

import pandas as pd

DOSSIER = "loyers_data"

# Ressources data.gouv (millesime 2025). Le nom reel du fichier permet de
# distinguer maisons (mai) / appartements (app).
RESSOURCES = [
    "https://www.data.gouv.fr/api/1/datasets/r/55b34088-0964-415f-9df7-d87dd98a09be",
    "https://www.data.gouv.fr/api/1/datasets/r/14a1fe11-b2d1-49b3-9f6b-83d12df9482c",
    "https://www.data.gouv.fr/api/1/datasets/r/5e3b28a4-cf56-43a3-ae79-43cceeb27f8c",
    "https://www.data.gouv.fr/api/1/datasets/r/129f764d-b613-44e4-952c-5ff50a8c9b73",
]

_CACHE = None


def _type_depuis_nom(nom):
    """Deduit le type de bien du nom de fichier ANIL (pred-mai-..., pred-app-...)."""
    n = nom.lower()
    if "mai" in n:
        return "Maison"
    if re.search(r"app12|app-?1|app-?2", n):
        return None            # T1-T2 : on ignore (on veut toutes typologies)
    if re.search(r"app3", n):
        return None            # T3+   : on ignore
    if "app" in n:
        return "Appartement"
    return None


def telecharger_loyers():
    """Telecharge les fichiers de loyers. Renvoie la liste des chemins."""
    os.makedirs(DOSSIER, exist_ok=True)
    chemins = []
    for url in RESSOURCES:
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                dispo = r.headers.get("Content-Disposition", "")
                m = re.search(r'filename="?([^";]+)', dispo)
                nom = m.group(1) if m else os.path.basename(r.url) or "loyers.csv"
                contenu = r.read()
            dest = os.path.join(DOSSIER, nom)
            with open(dest, "wb") as f:
                f.write(contenu)
            chemins.append(dest)
            print(f"  {nom} OK")
        except Exception as e:
            print(f"  echec {url[-12:]} : {e}")
    return chemins


def _lire_csv(chemin):
    """Lit un CSV ANIL (separateur et decimale variables)."""
    for sep, dec in ((";", ","), (",", "."), (";", "."), (",", ",")):
        try:
            df = pd.read_csv(chemin, sep=sep, decimal=dec, dtype=str,
                             encoding="utf-8", engine="python")
            if df.shape[1] >= 3:
                return df
        except Exception:
            continue
    return None


def charger_loyers(dossier=DOSSIER):
    """Renvoie {'Maison': df, 'Appartement': df} indexes par code INSEE."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    out = {}
    if not os.path.isdir(dossier):
        _CACHE = out
        return out

    for nom in sorted(os.listdir(dossier)):
        if not nom.lower().endswith(".csv"):
            continue
        type_local = _type_depuis_nom(nom)
        if type_local is None or type_local in out:
            continue
        df = _lire_csv(os.path.join(dossier, nom))
        if df is None:
            continue
        # Reperage souple des colonnes (INSEE + loyer predit au m2)
        col_insee = next((c for c in df.columns if "insee" in c.lower()), None)
        col_loyer = next((c for c in df.columns if "loypred" in c.lower()), None)
        if not col_insee or not col_loyer:
            continue
        petit = df[[col_insee, col_loyer]].copy()
        petit.columns = ["code_insee", "loyer_m2"]
        petit["code_insee"] = petit["code_insee"].astype(str).str.strip().str.zfill(5)
        petit["loyer_m2"] = pd.to_numeric(
            petit["loyer_m2"].astype(str).str.replace(",", ".", regex=False),
            errors="coerce")
        out[type_local] = petit.dropna().set_index("code_insee")
    _CACHE = out
    return out


def loyer_m2(code_insee, type_local, dossier=DOSSIER):
    """Loyer indicatif au m2 (charges comprises) pour une commune. None si inconnu."""
    tables = charger_loyers(dossier)
    t = tables.get(type_local)
    if t is None:
        return None
    code = str(code_insee).strip().zfill(5)
    try:
        return float(t.loc[code, "loyer_m2"])
    except Exception:
        return None


if __name__ == "__main__":
    print("Telechargement de la Carte des loyers (ANIL)...")
    telecharger_loyers()
    tables = charger_loyers()
    for t, df in tables.items():
        print(f"  {t} : {len(df)} communes")
