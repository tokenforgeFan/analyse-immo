# Analyseur d'annonces immobilières — document de reprise

> À coller en début de conversation pour reprendre le projet où il en est.

---

## 1. Objectif

Reproduire pour le **marché français** le *Listing Tool* de Reventure
(`reventure.app/listingtool`) : on donne une annonce (ou juste un code postal),
et l'outil renvoie une **estimation de valeur fondée sur les ventes réelles**,
un verdict sur/sous-coté, la tendance du marché et une recommandation d'offre.

Destiné à devenir un **produit public**, pas seulement un usage perso.

**Statut : en ligne et fonctionnel** → `analyse-immo.onrender.com`

---

## 2. Le socle : pourquoi la DVF

Aux États-Unis, Reventure doit *estimer* les prix de vente (non publics dans
plusieurs États). En France, l'administration publie **toutes les ventes réelles
depuis 2014** : la **DVF** (Demandes de Valeurs Foncières, DGFiP, open data).

C'est la différence clé du projet : **l'estimation repose sur des prix de vente
réels, pas sur un modèle**. C'est aussi la partie 100 % légale et défendable,
contrairement au scraping d'annonces.

### Sources de données utilisées

| Donnée | Source | Clé | Remarque |
|---|---|---|---|
| Ventes réelles | DVF géolocalisée (Etalab) | code postal / lat-lon | `files.data.gouv.fr/geo-dvf/latest/csv/{année}/departements/{dept}.csv.gz` |
| Loyers indicatifs | « Carte des loyers » ANIL | **code INSEE** commune | Millésime 2025, annuel. Citation obligatoire. |
| Géocodage | Base Adresse Nationale (BAN) | — | `api-adresse.data.gouv.fr` — adresse→lat/lon, CP→code INSEE |

**Citation obligatoire pour les loyers** :
« Estimations ANIL, à partir des données du Groupe SeLoger et de leboncoin ».

---

## 3. Fichiers du projet

Dossier local : `C:\analyse_immo`

| Fichier | Rôle | Sur le serveur ? |
|---|---|---|
| `analyse_immo.py` | **Moteur** : comparables, estimation, options, tendance, score, graphe | oui |
| `app.py` | **Site web Flask** : formulaires, routes, rendu HTML | oui |
| `serveur.py` | Lanceur de **production** (Waitress) | oui |
| `loyers.py` | Chargement de la Carte des loyers ANIL | oui |
| `construire_base.py` | Ingestion DVF → base DuckDB | non (local) |
| `extracteur.py` | Navigateur piloté Playwright | **non** (trop lourd pour l'hébergement gratuit) |
| `dvf.duckdb` | Base de ventes nettoyée | oui |
| `loyers_data/` | CSV des loyers ANIL | oui |
| `dvf_data/` | CSV DVF bruts | non (local) |

---

## 4. Comment ça marche

### 4.1 Moteur d'estimation (`analyse_immo.py`)

1. **Sélection des comparables** — même type de bien, zone géographique
   (rayon 8 km si une adresse est fournie, sinon code postal), surface à ±35 %,
   ventes des 36 derniers mois.
2. **Nettoyage** (déterminant pour la fiabilité) :
   - exclusion des **ventes groupées** (appartement + cave + parking sur une même
     mutation : `valeur_fonciere` est le total → fausse le prix/m²) ;
   - exclusion des prix/m² aberrants (bornes 300 – 25 000 €/m²).
3. **Tendance locale** — prix/m² médian année par année, taux annualisé
   (borné à −15 % / +20 %), ignorée si moins de 10 ventes/an.
4. **Recalage** — chaque vente comparable est réévaluée au niveau de prix
   d'aujourd'hui selon ce taux. L'estimation est donc « au prix actuel ».
5. **Ajustements** — options du bien + terrasses (voir §4.2).
6. **Verdict** — écart au prix affiché : > +10 % surcoté, < −10 % sous-coté.

Fonctions principales :
- `analyser_bien(df, cp, type_local, surface, prix, options, centre, terr_nc, terr_c)`
- `analyse_marche(df, cp, type_local, centre)` — sans bien précis, par code postal
- `graphique_marche(pool, cp, type_local, taux, chemin)` — chandeliers, accepte un `BytesIO`
- `score_marche(taux, ventes_an, dispersion)` — note /100

### 4.2 Ajustements

**Options** (bloc `OPTIONS` en haut de `analyse_immo.py`, modifiable) :

| Code | Option | Ajustement |
|---|---|---|
| P | Piscine enterrée | +20 000 € |
| S | Panneaux solaires (propriétaire) | +10 000 € |
| G | Garage / box fermé | +15 000 € |
| D | Dépendances | +15 000 € |
| T | Grand terrain (>1500 m²) | +20 000 € |
| R | Rénové récemment | +8 % |
| V | Vue dégagée | +7 % |
| N | DPE A ou B | +5 % |
| F | DPE F ou G (passoire) | **−12 %** |

Ces montants sont **indicatifs** : la DVF ne contient aucune information sur les
options. Le seul ajustement solidement documenté est la décote des passoires
thermiques.

**Terrasses** (surface pondérée, conforme aux pratiques du métier — le barème
Légifrance retient 0,40 pour les terrasses accessibles, le marché 0,30–0,50) :
- non couverte : **3 m² = 1 m² habitable** (coef. 0,33)
- couverte : **2 m² = 1 m² habitable** (coef. 0,50)

Constantes `TERRASSE_NON_COUVERTE` / `TERRASSE_COUVERTE`.
⚠️ Rendements décroissants : ne pas saisir une très grande terrasse en entier.

### 4.3 Score de marché (/100)

Tendance 45 % + liquidité (ventes/an) 30 % + stabilité (dispersion) 25 %.
Note **le marché**, pas le bien. Libellés : porteur / correct / mou / difficile.

### 4.4 Volet locatif

Loyer ANIL (€/m²/mois, **charges comprises**) → loyer d'un bien type →
**rendement brut** = loyer × 12 / prix au m² × 100.

⚠️ Loyers **d'annonces modélisés**, pas signés. Rendement **brut** : le net réel
est typiquement 2 à 3 points plus bas (taxe foncière, charges, vacance, fiscalité).

---

## 5. Extraction des annonces — état réel

| Site | En local (Playwright) | Sur Render (gratuit) |
|---|---|---|
| **PAP** | fonctionne | partiellement (requête simple) |
| **Bien'ici** | fonctionne | non (application JavaScript) |
| **Leboncoin** | souvent (DataDome) | non |

**Pourquoi** : Bien'ici et Leboncoin construisent leur page en JavaScript → il faut
un vrai navigateur. Playwright a besoin de Chromium (> 512 Mo de RAM) → impossible
sur les hébergements gratuits.

**Contournement en place** : `infos_depuis_url()` lit ce que l'**URL** révèle
(type de bien, commune → code postal via la BAN), sans charger la page.
Ex. `bienici.com/annonce/vente/saint-remeze/maison/...` → Maison + 07700.
Surface et prix restent à saisir.

**En local**, `extracteur.py` lance un **vrai Chrome avec profil persistant**
(dossier `.navigateur_profil`) et fenêtre **visible** pour Leboncoin, afin de
passer manuellement une vérification DataDome une fois pour toutes.

**Cadre** : extraction **à la demande** (une URL saisie par l'utilisateur), jamais
de collecte massive. À réexaminer si le site prend de l'ampleur.

---

## 6. Base de données

`construire_base.py` télécharge les CSV DVF et construit `dvf.duckdb`.
Le **nettoyage est fait à l'ingestion** (ventes groupées, aberrations) : les
requêtes ensuite prennent ~30 ms par département.

```bash
python construire_base.py 07 26 30 84    # quelques départements
python construire_base.py                # France entière (plusieurs Go)
```

`app.py` utilise la base si `dvf.duckdb` existe, sinon retombe sur le
téléchargement de CSV à la demande.

⚠️ **Le département est déduit du code postal**, pas du vrai département : les
codes postaux débordent des frontières départementales. Cohérent partout dans le
code, mais comparables partiels en zone frontalière si le département voisin
n'est pas ingéré.

⚠️ **Rebâtir la base** à chaque nouveau millésime DVF (~2 fois par an).

---

## 7. Lancement

### Local (version complète, avec extraction automatique)
```bash
pip install flask waitress pandas numpy matplotlib duckdb requests beautifulsoup4 playwright
python -m playwright install chromium     # PAS "playwright install" (pas dans le PATH)
python app.py                             # http://localhost:5000
```

### Production
```bash
python serveur.py     # Waitress, port 8000 (variable PORT), DEBUG=0
```

---

## 8. Hébergement

**Actuel : Render, offre gratuite** — 512 Mo, déploiement automatique depuis
GitHub (`tokenforgeFan/analyse-immo`, branche `main`).
- Build : `pip install -r requirements.txt`
- Start : `python serveur.py`
- Région : Frankfurt

**Limites** : mise en veille après 15 min d'inactivité (~30 s de réveil),
pas de Playwright.

**Écartés** : Hugging Face Spaces (Docker devenu payant), Oracle Always Free
(carte bancaire, capacité ARM aléatoire, administration lourde).

**Pour lever les limites** : VPS ~5 €/mois (Hetzner, Scaleway) → Playwright,
pas de veille, plus de RAM. Un `Dockerfile` complet existe déjà.

---

## 9. Conformité

- `noindex` + `robots.txt` bloquant : la DGFiP interdit l'indexation des
  transactions DVF par les moteurs de recherche.
- Uniquement des **agrégats** (médianes, quartiles) — aucune transaction
  individuelle ni adresse n'est publiée.
- Pied de page : valeur indicative, sans portée contractuelle + citation ANIL.

---

## 10. Pièges déjà rencontrés (à ne pas refaire)

1. **Ventes groupées DVF** — un appartement vendu avec cave et parking a une
   `valeur_fonciere` totale. Sans filtrage, le prix/m² est faussé.
2. **Maisons : le terrain est inclus** dans le prix mais pas dans la surface
   bâtie → prix/m² très dispersé, écarts à interpréter avec prudence.
3. **Titre du graphe** — affiche la valeur **lissée** (moyenne mobile), pas la
   médiane du dernier trimestre (trop bruitée).
4. **Deux prix/m² différents** — le rapport filtre sur la taille du bien, le
   graphe montre tout le marché. Normal, ce ne sont pas les mêmes populations.
5. **Prix Leboncoin** — plusieurs `price` dans le JSON de page (options payantes,
   annonces similaires) : cibler l'annonce principale, ignorer les blocs
   `similar`/`related`, gérer les centimes.
6. **matplotlib n'est pas thread-safe** → verrou + génération en `BytesIO`
   (sinon deux visiteurs se volent leur graphique).
7. **Cache mémoire** borné (`MAX_DEPTS_CACHE`, 3 par défaut) sinon 512 Mo saturés.
8. **Windows** : `playwright install` échoue (PATH) → `python -m playwright install`.

---

## 11. Suite possible

- **Carte des ventes comparables** (lat/lon déjà présents dans la base)
- **Rapport PDF** exportable
- **DPE** en étape dédiée (facteur de prix majeur aujourd'hui)
- **Design** plus abouti
- **VPS payant** pour retrouver l'extraction automatique en ligne
- **France entière** en base (nécessite un stockage adapté, > limite Git)
- Historique de recherches / comptes utilisateurs → **Postgres** à côté de DuckDB
  (DuckDB est en lecture seule, pas fait pour ça)

---

## 12. Contexte utilisateur

- Travaille sous **Windows / PowerShell**, projet dans `C:\analyse_immo`
- Zone de test habituelle : **Ardèche (07700, 07000)**
- Préfère avancer **étape par étape**, avec des instructions concrètes
- Réponses en **français**
