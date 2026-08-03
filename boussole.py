#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Boussole santé — Prototype phase 1
Tableau de bord des urgences du Québec à partir des données ouvertes du MSSS.

Usage :
    python3 boussole.py            # données réelles du MSSS (mises en cache 10 min)
    python3 boussole.py --demo     # données de démonstration intégrées

Aucune dépendance externe : Python 3.8+ seulement.
"""

import csv
import functools
import http.server
import io
import json
import re
import sqlite3
import ssl
import struct
import sys
import time
import unicodedata
import urllib.request
import os
import webbrowser
import zlib

# ---------------------------------------------------------------------------
# Sources officielles (Données Québec / MSSS, mises à jour chaque heure)
# ---------------------------------------------------------------------------
URL_CIVIERES = ("https://www.msss.gouv.qc.ca/professionnels/statistiques/"
                "documents/urgences/Releve_horaire_urgences_7jours.csv")
URL_PERSONNES = ("https://www.msss.gouv.qc.ca/professionnels/statistiques/"
                 "documents/urgences/Releve_horaire_urgences_7jours_nbpers.csv")

RES_CIVIERES = "a9272cc9-8234-40d1-9806-9f6b4c75c20d"
RES_PERSONNES = "b256f87f-40ec-4c79-bdba-a23e9c50e741"
CKAN_API = ("https://www.donneesquebec.ca/recherche/api/3/action/"
            "datastore_search?limit=1000&resource_id=")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Lien de soutien (Buy Me a Coffee, Ko-fi, GitHub Sponsors…). Laisser vide
# pour masquer le bouton « Soutenir la Boussole » du pied de page.
SOUTIEN_URL = ""

CACHE_TTL = 600  # secondes (10 min) — les données MSSS changent chaque heure
_cache = {"t": 0, "data": None, "source": None, "error": None,
          "releve": None, "hist": None}

# Base SQLite où chaque relevé horaire est conservé (étape 2 de la feuille de
# route : constituer l'historique qui servira aux tendances et à la prédiction).
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "boussole_historique.db")

# Coordonnées des installations : répertoire cartographique M02 du MSSS,
# diffusé sur Données Québec (latitude/longitude de chaque installation).
# Mis en cache localement 30 jours — ces coordonnées ne bougent presque jamais.
RES_M02 = "2aa06e66-c1d0-4e2f-bf3c-c2e413c3f84d"
COORDS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "boussole_coords.json")
COORDS_TTL = 30 * 24 * 3600  # 30 jours

DEMO_MODE = "--demo" in sys.argv
# Mode collecte : utilisé par la tâche automatique horaire (launchd).
# Télécharge et historise, mais n'ouvre pas le navigateur.
COLLECT_MODE = "--collecte" in sys.argv
# Mode site : génère le dossier site/ (PWA complète : index.html + données
# JSON + manifeste + service worker + icônes) pour l'hébergement.
SITE_MODE = "--site" in sys.argv
# Mode servir : génère le site puis le sert sur http://localhost:8765 pour
# tester la PWA en local (les service workers exigent HTTPS ou localhost).
SERVE_MODE = "--servir" in sys.argv
# Avec --reseau en plus : le serveur écoute aussi sur le réseau local, pour
# tester depuis un téléphone connecté au même Wi-Fi.
LAN_MODE = "--reseau" in sys.argv

# ---------------------------------------------------------------------------
# Correspondance établissement -> région (approximative, pour le regroupement)
# ---------------------------------------------------------------------------
REGIONS = [
    ("chu de quebec", "Capitale-Nationale"), ("universite laval", "Capitale-Nationale"),
    ("iucpq", "Capitale-Nationale"), ("mcgill", "Montréal"), ("chum", "Montréal"),
    ("chu sainte-justine", "Montréal"), ("institut de cardiologie", "Montréal"),
    ("mtl", "Montréal"),
    ("monteregie", "Montérégie"), ("montreal", "Montréal"),
    ("capitale-nationale", "Capitale-Nationale"), ("laval", "Laval"),
    ("lanaudiere", "Lanaudière"), ("laurentides", "Laurentides"),
    ("outaouais", "Outaouais"), ("estrie", "Estrie"),
    ("mauricie", "Mauricie et Centre-du-Québec"),
    ("centre-du-quebec", "Mauricie et Centre-du-Québec"),
    ("saguenay", "Saguenay–Lac-Saint-Jean"), ("lac-saint-jean", "Saguenay–Lac-Saint-Jean"),
    ("bas-saint-laurent", "Bas-Saint-Laurent"), ("gaspesie", "Gaspésie–Îles-de-la-Madeleine"),
    ("abitibi", "Abitibi-Témiscamingue"), ("cote-nord", "Côte-Nord"),
    ("chaudiere-appalaches", "Chaudière-Appalaches"), ("nord-du-quebec", "Nord-du-Québec"),
    ("james", "Nord-du-Québec"), ("nunavik", "Nunavik"),
    ("ungava", "Nunavik"), ("inuulitsivik", "Nunavik"),
]


# Le fichier « personnes » se termine par des lignes de totaux (16 régions +
# le Québec) qui ne sont pas des urgences : on les écarte du tableau.
AGREGATS = {"total_regional", "ensemble_du_quebec", "total"}


def _norm(text):
    """minuscule + sans accents, pour comparer des en-têtes/noms variables."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def region_for(etablissement):
    n = _norm(etablissement).replace("_", "-")
    for key, label in REGIONS:
        if key.replace(" ", "-") in n:
            return label
    return "Autres régions"


# ---------------------------------------------------------------------------
# Certificats HTTPS
# ---------------------------------------------------------------------------
# Sur macOS, le Python installé depuis python.org n'utilise PAS le trousseau du
# système : il attend son propre fichier de certificats, installé par le script
# « Install Certificates.command ». Si ce script n'a jamais été lancé, Python
# n'a AUCUNE autorité de certification et *toute* connexion HTTPS échoue avec
# « CERTIFICATE_VERIFY_FAILED ». On récupère donc un jeu de certificats valide
# où qu'il se trouve, plutôt que de désactiver la vérification (jamais).

# Emplacements habituels d'un fichier de certificats, du plus standard au moins.
CA_BUNDLES = (
    "/etc/ssl/cert.pem",                       # macOS (trousseau système exporté)
    "/private/etc/ssl/cert.pem",               # idem, chemin réel
    "/etc/ssl/certs/ca-certificates.crt",      # Debian / Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",        # Fedora / RHEL
    "/opt/homebrew/etc/ca-certificates/cert.pem",  # Homebrew (Apple Silicon)
    "/usr/local/etc/ca-certificates/cert.pem",     # Homebrew (Intel)
)

_ssl_ctx = None
_ssl_origin = None


def ssl_context():
    """Contexte HTTPS avec de vrais certificats, quelle que soit l'installation."""
    global _ssl_ctx, _ssl_origin
    if _ssl_ctx is not None:
        return _ssl_ctx

    # 1) Configuration normale de Python : on la garde si elle a des certificats.
    ctx = ssl.create_default_context()
    if ctx.get_ca_certs():
        _ssl_ctx, _ssl_origin = ctx, "configuration Python par défaut"
        return _ssl_ctx

    # 2) Le paquet certifi, s'il est présent (installé avec pip ou Python lui-même).
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        if ctx.get_ca_certs():
            _ssl_ctx, _ssl_origin = ctx, "certifi (" + certifi.where() + ")"
            return _ssl_ctx
    except Exception:
        pass

    # 3) Le fichier de certificats du système.
    for path in CA_BUNDLES:
        if not os.path.exists(path):
            continue
        try:
            ctx = ssl.create_default_context(cafile=path)
        except Exception:
            continue
        if ctx.get_ca_certs():
            _ssl_ctx, _ssl_origin = ctx, path
            return _ssl_ctx

    raise RuntimeError(
        "aucun certificat HTTPS trouvé sur cet ordinateur. "
        "Ouvrez le dossier Applications > Python 3.x et double-cliquez sur "
        "« Install Certificates.command », puis relancez boussole.py")


# ---------------------------------------------------------------------------
# Téléchargement et parsing tolérant du CSV MSSS
# ---------------------------------------------------------------------------
def fetch_csv(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/csv,*/*",
        "Accept-Language": "fr-CA,fr;q=0.9",
        "Referer": "https://www.donneesquebec.ca/",
    })
    raw = urllib.request.urlopen(req, timeout=20, context=ssl_context()).read()
    text = None
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:  # filet de sécurité : on ne bloque jamais sur l'encodage
        text = raw.decode("latin-1", "replace")
    sample = text[:2048]
    delim = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    return rows


def fetch_ckan(resource_id):
    """Source de rechange : API datastore de Données Québec (JSON)."""
    req = urllib.request.Request(CKAN_API + resource_id, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json"})
    j = json.load(urllib.request.urlopen(req, timeout=25, context=ssl_context()))
    if not j.get("success"):
        raise RuntimeError("réponse API sans succès")
    result = j["result"]
    fields = [f["id"] for f in result["fields"] if f["id"] != "_id"]
    rows = [fields]
    for rec in result["records"]:
        rows.append(["" if rec.get(f) is None else str(rec.get(f)) for f in fields])
    if len(rows) < 2:
        raise RuntimeError("API sans enregistrements")
    return rows


def fetch_coords():
    """Télécharge le répertoire M02 et en tire :
    - deux index de coordonnées (par permis et par nom normalisé) ;
    - la liste des points de service CLSC (nom, adresse, lat, lon), que le
      guide « Où aller ? » utilise pour montrer les CLSC les plus proches."""
    par_permis, par_nom, clsc = {}, {}, []
    offset, total = 0, 1
    while offset < total:
        url = ("https://www.donneesquebec.ca/recherche/api/3/action/"
               "datastore_search?resource_id={}&limit=1000&offset={}"
               .format(RES_M02, offset))
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT, "Accept": "application/json"})
        j = json.load(urllib.request.urlopen(req, timeout=30,
                                             context=ssl_context()))
        result = j["result"]
        total = result.get("total", 0)
        records = result["records"]
        if not records:
            break
        for r in records:
            try:
                lat = float(r["LATITUDE"])
                lon = float(r["LONGITUDE"])
            except (KeyError, TypeError, ValueError):
                continue
            adresse = ", ".join(str(x).strip() for x in
                                (r.get("ADRESSE"), r.get("MUN_NOM")) if x)
            entree = [lat, lon, adresse]
            permis = str(r.get("INSTAL_COD") or "").strip()
            if permis:
                par_permis[permis] = entree
            nom = _norm(r.get("INSTAL_NOM") or "")
            if nom:
                par_nom[nom] = entree
            if str(r.get("CLSC") or "").strip().lower() == "oui":
                clsc.append({"n": str(r.get("INSTAL_NOM") or "").strip(),
                             "a": adresse, "lat": lat, "lon": lon})
        offset += 1000
    if not par_permis:
        raise RuntimeError("répertoire M02 vide")
    return par_permis, par_nom, clsc


def load_coords():
    """Coordonnées des installations, avec cache local de 30 jours.
    En cas d'échec réseau : cache périmé s'il existe, sinon dictionnaires
    vides — la page fonctionne alors simplement sans distances."""
    cache = None
    try:
        with open(COORDS_PATH, encoding="utf-8") as f:
            cache = json.load(f)
        # v3 = entrées [lat, lon, adresse] + liste des CLSC ; sinon on refait.
        if cache.get("v") == 3 and time.time() - cache.get("t", 0) < COORDS_TTL:
            return cache["par_permis"], cache["par_nom"], cache["clsc"]
    except Exception:
        cache = None
    try:
        par_permis, par_nom, clsc = fetch_coords()
        with open(COORDS_PATH, "w", encoding="utf-8") as f:
            json.dump({"v": 3, "t": time.time(), "par_permis": par_permis,
                       "par_nom": par_nom, "clsc": clsc}, f, ensure_ascii=False)
        return par_permis, par_nom, clsc
    except Exception:
        if cache:  # périmé, mais mieux que rien
            return (cache["par_permis"], cache["par_nom"],
                    cache.get("clsc", []))
        return {}, {}, []


def load_rows(url_csv, resource_id):
    """Essaie le CSV du MSSS, puis l'API de Données Québec."""
    errors = []
    try:
        return fetch_csv(url_csv)
    except Exception as e:
        errors.append("CSV MSSS : " + (str(e) or repr(e)))
    try:
        return fetch_ckan(resource_id)
    except Exception as e:
        errors.append("API Données Québec : " + (str(e) or repr(e)))
    raise RuntimeError(" | ".join(errors))


def find_col(headers, *keywords, exclude=()):
    """Trouve l'index de la 1re colonne dont l'en-tête normalisé contient
    tous les mots-clés donnés (et aucun mot exclu)."""
    normed = [_norm(h) for h in headers]
    for i, h in enumerate(normed):
        if all(k in h for k in keywords) and not any(x in h for x in exclude):
            return i
    return None


def to_num(value):
    if value is None:
        return None
    v = str(value).strip().replace(",", ".")
    if v in ("", "-", "n/d", "nd", "na", "s/o"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def parse_msss():
    """Fusionne les deux fichiers MSSS.

    Retourne (liste d'installations, horodatage du relevé) — l'horodatage
    vient de la colonne « Mise_a_jour » du fichier (ex. 2026-08-02T13:46)
    et sert de clé pour ne pas enregistrer deux fois le même relevé.
    """
    rows_civ = load_rows(URL_CIVIERES, RES_CIVIERES)
    rows_per = load_rows(URL_PERSONNES, RES_PERSONNES)

    maj_i = find_col(rows_civ[0], "mise", "jour")
    releve = ""
    if maj_i is not None:
        for r in rows_civ[1:]:
            if maj_i < len(r) and r[maj_i].strip():
                releve = r[maj_i].strip()
                break
    if not releve:  # filet de sécurité : l'heure locale, arrondie à l'heure
        releve = time.strftime("%Y-%m-%dT%H:00")

    def index_file(rows, spec):
        headers = rows[0]
        cols = {name: find_col(headers, *kw, exclude=ex) for name, (kw, ex) in spec.items()}
        # Clé de fusion : le nom normalisé de l'installation. On n'utilise PAS le
        # numéro de permis : le MSSS n'inscrit pas le même numéro dans les deux
        # fichiers pour une trentaine d'installations (ex. Centre hospitalier de
        # St. Mary), alors que les noms, eux, concordent à 100 %.
        name_i = find_col(headers, "nom", "installation")
        etab_i = find_col(headers, "nom", "etablissement")
        region_i = find_col(headers, "region")
        permis_i = find_col(headers, "no", "permis")
        out = {}
        for r in rows[1:]:
            if name_i is None or name_i >= len(r):
                continue
            key = _norm(r[name_i])
            if not key or key in AGREGATS:  # lignes de totaux, pas des urgences
                continue
            rec = {"installation": r[name_i].strip(),
                   "etablissement": r[etab_i].strip() if etab_i is not None and etab_i < len(r) else "",
                   "region_off": r[region_i].strip() if region_i is not None and region_i < len(r) else "",
                   "permis": r[permis_i].strip() if permis_i is not None and permis_i < len(r) else ""}
            for name, i in cols.items():
                rec[name] = to_num(r[i]) if i is not None and i < len(r) else None
            out[key] = rec
        return out

    civ = index_file(rows_civ, {
        "civ_fonct": (("civieres", "fonctionnelles"), ()),
        "civ_occ": (("civieres", "occupees"), ()),
        "civ_24h": (("24",), ("48",)),
        "civ_48h": (("48",), ()),
    })
    per = index_file(rows_per, {
        "presents": (("presents",), ()),
        "attente_pec": (("attente",), ()),
        "dms_civiere": (("dms", "civiere"), ()),
        "dms_ambulatoire": (("dms", "ambulatoire"), ()),
    })

    par_permis, par_nom, _ = load_coords()

    merged = []
    for key, c in civ.items():
        p = per.get(key, {})
        fonct, occ = c.get("civ_fonct"), c.get("civ_occ")
        taux = round(100 * occ / fonct) if fonct and occ is not None else None
        # Le fichier « personnes » fournit la région officielle du MSSS ;
        # on ne retombe sur notre déduction par mots-clés que si elle manque.
        region = p.get("region_off") or region_for(
            c["etablissement"] + " " + c["installation"])
        # Coordonnées : par permis d'abord, par nom normalisé en repli.
        coords = (par_permis.get(c.get("permis") or "")
                  or par_nom.get(key))
        merged.append({
            "installation": c["installation"],
            "etablissement": c["etablissement"],
            "region": region,
            "lat": coords[0] if coords else None,
            "lon": coords[1] if coords else None,
            "adresse": coords[2] if coords and len(coords) > 2 else "",
            "taux": taux,
            "civ_occ": occ, "civ_fonct": fonct,
            "civ_24h": c.get("civ_24h"), "civ_48h": c.get("civ_48h"),
            "presents": p.get("presents"),
            "attente_pec": p.get("attente_pec"),
            "dms_civiere": p.get("dms_civiere"),
            "dms_ambulatoire": p.get("dms_ambulatoire"),
        })
    merged = [m for m in merged if m["taux"] is not None or m["presents"] is not None]
    merged.sort(key=lambda m: (m["taux"] is None, -(m["taux"] or 0)))
    return merged, releve


DEMO = [
    {"installation": "Hôpital Honoré-Mercier", "etablissement": "CISSS de la Montérégie-Est",
     "region": "Montérégie", "taux": 108, "civ_occ": 27, "civ_fonct": 25, "civ_24h": 6,
     "civ_48h": 1, "presents": 54, "attente_pec": 18, "dms_civiere": 21.4, "dms_ambulatoire": 5.2},
    {"installation": "Hôpital Pierre-Boucher", "etablissement": "CISSS de la Montérégie-Est",
     "region": "Montérégie", "taux": 132, "civ_occ": 45, "civ_fonct": 34, "civ_24h": 12,
     "civ_48h": 4, "presents": 88, "attente_pec": 31, "dms_civiere": 27.9, "dms_ambulatoire": 6.8},
    {"installation": "Hôpital du Haut-Richelieu", "etablissement": "CISSS de la Montérégie-Centre",
     "region": "Montérégie", "taux": 96, "civ_occ": 26, "civ_fonct": 27, "civ_24h": 3,
     "civ_48h": 0, "presents": 47, "attente_pec": 12, "dms_civiere": 17.1, "dms_ambulatoire": 4.6},
    {"installation": "Hôpital Charles-Le Moyne", "etablissement": "CISSS de la Montérégie-Centre",
     "region": "Montérégie", "taux": 121, "civ_occ": 52, "civ_fonct": 43, "civ_24h": 9,
     "civ_48h": 2, "presents": 96, "attente_pec": 26, "dms_civiere": 24.3, "dms_ambulatoire": 6.1},
    {"installation": "Hôpital Maisonneuve-Rosemont", "etablissement": "CIUSSS de l'Est-de-l'Île-de-Montréal",
     "region": "Montréal", "taux": 141, "civ_occ": 62, "civ_fonct": 44, "civ_24h": 15,
     "civ_48h": 6, "presents": 112, "attente_pec": 38, "dms_civiere": 30.2, "dms_ambulatoire": 7.4},
    {"installation": "Hôpital général juif", "etablissement": "CIUSSS du Centre-Ouest-de-l'Île-de-Montréal",
     "region": "Montréal", "taux": 89, "civ_occ": 48, "civ_fonct": 54, "civ_24h": 4,
     "civ_48h": 0, "presents": 79, "attente_pec": 15, "dms_civiere": 14.8, "dms_ambulatoire": 4.1},
    {"installation": "CHUL (CHU de Québec)", "etablissement": "CHU de Québec – Université Laval",
     "region": "Capitale-Nationale", "taux": 104, "civ_occ": 29, "civ_fonct": 28, "civ_24h": 5,
     "civ_48h": 1, "presents": 61, "attente_pec": 19, "dms_civiere": 19.6, "dms_ambulatoire": 5.0},
    {"installation": "Hôpital de la Cité-de-la-Santé", "etablissement": "CISSS de Laval",
     "region": "Laval", "taux": 118, "civ_occ": 59, "civ_fonct": 50, "civ_24h": 11,
     "civ_48h": 3, "presents": 102, "attente_pec": 29, "dms_civiere": 25.7, "dms_ambulatoire": 6.5},
]


# ---------------------------------------------------------------------------
# Historique SQLite (un enregistrement par installation et par relevé horaire)
# ---------------------------------------------------------------------------
def save_history(data, releve):
    """Ajoute le relevé courant à la base ; ignore ce qui y est déjà.

    La clé primaire (releve, installation) garantit qu'on peut relancer le
    script autant de fois qu'on veut dans la même heure sans créer de
    doublons. Retourne un petit résumé en français pour l'affichage.
    """
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS releves (
                releve          TEXT NOT NULL,  -- horodatage MSSS (2026-08-02T13:46)
                installation    TEXT NOT NULL,
                etablissement   TEXT,
                region          TEXT,
                taux            REAL,           -- occupation des civières, en %
                civ_occ         REAL,
                civ_fonct       REAL,
                civ_24h         REAL,
                civ_48h         REAL,
                presents        REAL,
                attente_pec     REAL,
                dms_civiere     REAL,
                dms_ambulatoire REAL,
                enregistre_le   TEXT NOT NULL,  -- date/heure de la sauvegarde
                PRIMARY KEY (releve, installation)
            )""")
        # Pour les futures requêtes de tendances (« cet hôpital, heure par heure »)
        con.execute("CREATE INDEX IF NOT EXISTS idx_installation "
                    "ON releves (installation, releve)")
        avant = con.total_changes
        maintenant = time.strftime("%Y-%m-%d %H:%M:%S")
        con.executemany(
            "INSERT OR IGNORE INTO releves VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(releve, d["installation"], d["etablissement"], d["region"],
              d["taux"], d["civ_occ"], d["civ_fonct"], d["civ_24h"],
              d["civ_48h"], d["presents"], d["attente_pec"],
              d["dms_civiere"], d["dms_ambulatoire"], maintenant)
             for d in data])
        con.commit()
        ajoutees = con.total_changes - avant
        n_releves, n_lignes = con.execute(
            "SELECT COUNT(DISTINCT releve), COUNT(*) FROM releves").fetchone()
        if ajoutees:
            return ("Historique : relevé {} enregistré ({} lignes) — la base "
                    "contient {} relevé(s), {} lignes au total.".format(
                        releve, ajoutees, n_releves, n_lignes))
        return ("Historique : relevé {} déjà enregistré — la base contient "
                "{} relevé(s), {} lignes au total.".format(
                    releve, n_releves, n_lignes))
    finally:
        con.close()


def load_trends():
    """Profil horaire par hôpital, tiré de l'historique SQLite.

    Pour chaque installation : la moyenne des personnes en attente à chaque
    heure de la journée (24 cases, None si jamais observée). Retourne aussi
    le nombre de jours distincts couverts — l'interface n'affiche les
    tendances qu'à partir de 3 jours, pour ne pas faire dire n'importe quoi
    à deux mesures.
    """
    try:
        con = sqlite3.connect(DB_PATH)
        try:
            jours = con.execute(
                "SELECT COUNT(DISTINCT substr(releve, 1, 10)) FROM releves"
            ).fetchone()[0]
            profils = {}
            # L'heure est extraite de l'horodatage MSSS : « 2026-08-02T13:46 »
            # → positions 12-13 = « 13 ».
            for inst, heure, moy in con.execute(
                    "SELECT installation, "
                    "       CAST(substr(releve, 12, 2) AS INTEGER), "
                    "       AVG(attente_pec) "
                    "FROM releves WHERE attente_pec IS NOT NULL "
                    "GROUP BY installation, "
                    "         CAST(substr(releve, 12, 2) AS INTEGER)"):
                if 0 <= heure <= 23:
                    profils.setdefault(inst, [None] * 24)[heure] = round(moy, 1)
            return jours, profils
        finally:
            con.close()
    except Exception:  # pas de base, base corrompue… : simplement pas de tendances
        return 0, {}


def load_recent(limite=48):
    """Les derniers relevés réels (48 h max) pour la courbe « dernières
    heures » de la fiche. Retourne (liste d'horodatages, {installation:
    [valeurs d'attente alignées sur la liste, None si absente]})."""
    try:
        con = sqlite3.connect(DB_PATH)
        try:
            rels = [r[0] for r in con.execute(
                "SELECT DISTINCT releve FROM releves "
                "ORDER BY releve DESC LIMIT ?", (limite,))]
            rels.reverse()
            if not rels:
                return [], {}
            idx = {r: i for i, r in enumerate(rels)}
            qmarks = ",".join("?" * len(rels))
            histo = {}
            for inst, rel, att in con.execute(
                    "SELECT installation, releve, attente_pec FROM releves "
                    "WHERE releve IN (" + qmarks + ")", rels):
                histo.setdefault(inst, [None] * len(rels))[idx[rel]] = att
            return rels, histo
        finally:
            con.close()
    except Exception:
        return [], {}


def get_data():
    now = time.time()
    if _cache["data"] and now - _cache["t"] < CACHE_TTL:
        return _cache
    if DEMO_MODE:
        _cache.update(t=now, data=DEMO, source="demo", error=None,
                      releve=None, hist=None)
        return _cache
    try:
        data, releve = parse_msss()
        _cache.update(t=now, data=data, source="msss", error=None,
                      releve=releve)
        # L'historique ne doit jamais empêcher l'affichage de la page :
        # en cas de pépin avec la base, on le signale et on continue.
        try:
            _cache["hist"] = save_history(data, releve)
        except Exception as exc:
            _cache["hist"] = ("Historique non enregistré ({}) — la page "
                              "fonctionne quand même.".format(str(exc) or repr(exc)))
    except Exception as exc:  # réseau bloqué, format modifié, etc.
        _cache.update(t=now, data=DEMO, source="demo", releve=None, hist=None,
                      error="Données réelles inaccessibles — affichage de la démo. Détail : {}".format(str(exc) or repr(exc)))
    return _cache


# ---------------------------------------------------------------------------
# Interface web (une page, aucun framework)
# ---------------------------------------------------------------------------
PAGE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Boussole santé — État des urgences du Québec</title>
__PWA__
<style>
:root{--bg:#f6f4ee;--card:#fdfcf9;--ink:#23252b;--mut:#6a6d76;--line:#e2e0d8;
--brand:#16468C;--brand-dark:#0f3369;--brand-tint:#e9eef7;--brand-on:#F5F9FF;
--fluide:#0d7f5f;--charge:#a86f0e;--deborde:#c03d2e;
--ok:#0d7f5f;--okbg:#e6f3ee;--warn:#a86f0e;--warnbg:#f8efdd;
--bad:#c03d2e;--badbg:#fdeeea;--info:#16468C;--infobg:#e9eef7;
--voile:rgba(30,32,40,.45);
--mono:ui-monospace,'SF Mono',SFMono-Regular,Menlo,Consolas,monospace;}
@media (prefers-color-scheme:dark){
:root{--bg:#15171c;--card:#1e2128;--ink:#e7e8ec;--mut:#9aa0ab;--line:#33373f;
--brand:#82aae8;--brand-dark:#a8c4f0;--brand-tint:#1d2b45;--brand-on:#0c1a30;
--fluide:#3bb890;--charge:#d9a34e;--deborde:#e0705f;
--ok:#3bb890;--okbg:#15332a;--warn:#d9a34e;--warnbg:#3a2e13;
--bad:#e0705f;--badbg:#3d1e19;--info:#82aae8;--infobg:#1d2b45;
--voile:rgba(0,0,0,.62);}
}
*{box-sizing:border-box;margin:0;}
body{font-family:-apple-system,'Segoe UI',Roboto,'Helvetica Neue',sans-serif;
background:var(--bg);color:var(--ink);line-height:1.5;padding:28px 20px 64px;
-webkit-font-smoothing:antialiased;}
main{max-width:1080px;margin:0 auto;}
a{color:var(--brand);text-decoration:none;}
.mono{font-family:var(--mono);}
.entete{display:flex;align-items:center;justify-content:space-between;
gap:14px;flex-wrap:wrap;}
.marque{display:flex;align-items:center;gap:11px;cursor:pointer;}
.marque img{width:40px;height:40px;}
.marque b{font-size:20px;font-weight:700;letter-spacing:-0.02em;color:var(--brand);}
.tag{font-family:var(--mono);font-size:12px;color:var(--mut);}
.onglets{display:flex;gap:5px;padding:4px;background:var(--card);
border:1px solid var(--line);border-radius:999px;}
.onglet{font:inherit;font-size:13.5px;font-weight:600;padding:8px 15px;
border-radius:999px;border:none;background:transparent;color:var(--mut);
cursor:pointer;letter-spacing:-0.01em;}
.onglet.on{background:var(--ink);color:var(--bg);}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:14px 0 20px;}
.chip{font-size:12px;font-family:var(--mono);padding:4px 11px;
border-radius:999px;background:var(--card);border:1px solid var(--line);
color:var(--mut);}
.chip.ok{background:var(--okbg);color:var(--ok);border-color:transparent;}
.chip.warn{background:var(--warnbg);color:var(--warn);border-color:transparent;}
#chip-pos{cursor:pointer;}
.c20{background:var(--card);border:1px solid var(--line);border-radius:20px;
padding:28px;}
.kick{font-family:var(--mono);font-size:11px;letter-spacing:0.08em;
text-transform:uppercase;color:var(--mut);}
.h1g{margin:12px 0 8px;font-size:38px;line-height:1.06;letter-spacing:-0.03em;
font-weight:600;}
.sous{font-size:15.5px;color:var(--mut);max-width:54ch;}
.triage{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));
gap:12px;margin-top:26px;}
.tcard{border-radius:16px;padding:19px;display:flex;flex-direction:column;
gap:7px;font:inherit;text-align:left;color:var(--ink);}
.tcard .tt{font-size:13px;font-weight:600;}
.tcard .td{font-size:16.5px;font-weight:600;letter-spacing:-0.01em;line-height:1.3;}
.tcard .tn{margin-top:8px;font-family:var(--mono);font-size:21px;font-weight:500;}
.t911{border:1px solid var(--bad);background:var(--badbg);}
.t911 .tt,.t911 .tn{color:var(--bad);}
.t811{border:1px solid var(--line);background:var(--bg);cursor:pointer;}
.t811 .tt{color:var(--mut);}
.tbrand{border:1.5px solid var(--brand);background:var(--brand);
color:var(--brand-on);cursor:pointer;}
.tbrand .tt{opacity:0.78;}
.tbrand:hover{background:var(--brand-dark);border-color:var(--brand-dark);}
.duo{display:grid;grid-template-columns:1.5fr 1fr;gap:16px;margin-top:16px;}
@media(max-width:840px){.duo{grid-template-columns:1fr;}
.h1g{font-size:30px;}.c20{padding:22px;}}
.bigmono{font-family:var(--mono);font-weight:500;line-height:1;color:var(--brand);}
.pill{font:inherit;font-size:14px;font-weight:600;padding:11px 18px;
border-radius:999px;cursor:pointer;}
.pill-n{border:1.5px solid var(--ink);background:var(--ink);color:var(--bg);}
.pill-n:hover{opacity:.85;}
.pill-b{border:none;background:var(--brand);color:var(--brand-on);}
.pill-b:hover{background:var(--brand-dark);}
.pill-o{border:1.5px solid var(--line);background:transparent;color:var(--ink);}
.pill-o:hover{border-color:var(--mut);}
.ligneres{display:flex;justify-content:space-between;gap:10px;font-size:14px;
margin:10px 0 8px;color:var(--mut);flex-wrap:wrap;}
.ligneres b{color:var(--ink);font-weight:600;}
.legdots{display:flex;gap:14px;font-size:13px;color:var(--mut);align-items:center;}
.dot{width:8px;height:8px;border-radius:99px;display:inline-block;
margin-right:5px;vertical-align:baseline;}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px;}
input,select,button{font:inherit;}
input[type=search],select{padding:11px 16px;border:1px solid var(--line);
border-radius:999px;background:var(--card);color:var(--ink);font-size:14px;}
input[type=search]{flex:1;min-width:210px;}
.bar button{padding:11px 16px;border:1px solid var(--line);border-radius:999px;
background:var(--card);color:var(--ink);font-size:14px;font-weight:500;
cursor:pointer;}
.bar button:hover{border-color:var(--mut);}
.notice{background:var(--warnbg);color:var(--warn);border-radius:12px;
padding:10px 14px;font-size:13px;margin-bottom:16px;}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;
padding:20px 22px;margin-bottom:10px;cursor:pointer;}
.card:hover{border-color:var(--mut);}
.row{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;}
.ktype{font-size:12px;font-family:var(--mono);color:var(--mut);}
.name{font-weight:600;font-size:18px;letter-spacing:-0.015em;line-height:1.25;
margin-top:3px;}
.attq{font-family:var(--mono);font-size:19px;font-weight:500;line-height:1.05;
white-space:nowrap;text-align:right;}
.attqs{font-size:11px;color:var(--mut);margin-top:4px;text-align:right;}
.etab{color:var(--mut);font-size:12px;margin-top:2px;}
.badge{font-size:13px;font-weight:600;padding:3px 10px;border-radius:999px;
white-space:nowrap;font-family:var(--mono);}
.b-ok{background:var(--okbg);color:var(--ok);}
.b-warn{background:var(--warnbg);color:var(--warn);}
.b-bad{background:var(--badbg);color:var(--bad);}
.jauge{height:6px;background:var(--line);border-radius:999px;margin-top:14px;
overflow:hidden;}
.jauge i{display:block;height:6px;border-radius:999px;}
.basrow{margin-top:10px;display:flex;justify-content:space-between;gap:10px;
font-size:12.5px;color:var(--mut);flex-wrap:wrap;}
.det{color:var(--brand);font-weight:600;}
.cmp{color:var(--mut);font-weight:600;cursor:pointer;}
.cmp.on{color:var(--ok);}
.rg{font-size:12px;font-family:var(--mono);font-weight:500;color:var(--mut);
text-transform:uppercase;letter-spacing:.06em;margin:22px 0 8px;}
.stats{display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:8px;
font-size:13px;color:var(--mut);}
footer{color:var(--mut);font-size:12px;margin-top:36px;line-height:1.7;}
.voile{position:fixed;inset:0;background:var(--voile);display:flex;
align-items:flex-end;justify-content:center;z-index:50;}
.fiche{background:var(--bg);border-radius:20px 20px 0 0;max-width:660px;
width:100%;max-height:88vh;overflow:auto;padding:24px 20px 32px;}
@media(min-width:640px){.voile{align-items:center;padding:24px;}
.fiche{border-radius:20px;}}
.fx{float:right;font-size:14px;padding:8px 13px;border:1px solid var(--line);
border-radius:999px;background:var(--card);color:var(--ink);cursor:pointer;}
.fh{font-family:var(--mono);font-size:11px;letter-spacing:0.08em;
text-transform:uppercase;color:var(--mut);margin:24px 0 10px;font-weight:500;}
.fgrille{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));
gap:10px;}
.fstat{background:var(--bg);border-radius:14px;padding:16px;}
.fstat b{font-size:24px;display:block;font-family:var(--mono);font-weight:500;}
.fstat span{font-size:12px;color:var(--mut);line-height:1.35;display:block;
margin-top:5px;}
.fstat.acc{background:var(--brand-tint);}
.fstat.acc b,.fstat.acc span{color:var(--brand);}
.fbtn{display:inline-block;margin-top:10px;margin-right:8px;padding:11px 17px;
border:1.5px solid var(--line);border-radius:999px;background:var(--card);
text-decoration:none;color:var(--ink);font-weight:600;font-size:14px;
cursor:pointer;}
.fbtn:hover{border-color:var(--mut);}
.fbtn.acc{background:var(--brand);border-color:var(--brand);color:var(--brand-on);}
.gq{font-weight:600;margin-top:12px;}
.qbtn{display:block;width:100%;text-align:left;margin-top:8px;font:inherit;
padding:12px 15px;border:1px solid var(--line);border-radius:12px;
background:var(--card);color:var(--ink);cursor:pointer;font-size:14.5px;}
.qbtn:hover{border-color:var(--mut);}
.gres{background:var(--okbg);border-radius:12px;padding:14px 16px;
font-size:14px;margin-top:10px;line-height:1.6;}
.gnote{font-size:12px;color:var(--mut);margin-top:8px;line-height:1.5;}
.alarm{background:var(--badbg);color:var(--bad);border-radius:12px;
padding:12px 15px;font-size:13px;margin:10px 0;line-height:1.55;}
.tr{display:flex;gap:2px;align-items:flex-end;height:26px;margin-top:10px;}
.tr i{flex:1;background:var(--line);border-radius:2px;min-height:2px;}
.tr i.now{background:var(--charge);}
.trh i{background:var(--brand);opacity:.4;}
.trh i.der{opacity:1;}
.trcap{color:var(--mut);font-size:12.5px;margin-top:6px;line-height:1.5;}
.trcap b{color:var(--ink);}
.aff{display:flex;align-items:flex-end;gap:6px;height:104px;margin-top:18px;}
.aff>div{flex:1;display:flex;flex-direction:column;align-items:center;gap:7px;
height:100%;justify-content:flex-end;}
.aff .ab{width:100%;border-radius:5px 5px 2px 2px;}
.aff .al{font-family:var(--mono);font-size:10px;color:var(--mut);}
.binfo{background:var(--infobg);border-radius:14px;padding:15px 17px;
margin-top:10px;line-height:1.55;color:var(--info);font-size:13.5px;}
.bvert{background:var(--okbg);border-radius:14px;padding:15px 17px;margin-top:8px;
line-height:1.55;color:var(--ok);font-size:13.5px;}
.timel{display:grid;grid-template-columns:92px 1fr;gap:16px;padding:12px 0;
border-bottom:1px solid var(--line);font-size:14.5px;}
.timel:last-child{border-bottom:none;}
.timel span:first-child{font-family:var(--mono);font-size:13px;color:var(--mut);}
.duo3{display:grid;grid-template-columns:1.35fr 1fr;gap:16px;align-items:start;}
@media(max-width:840px){.duo3{grid-template-columns:1fr;}}
.retour{font-family:var(--mono);font-size:12px;color:var(--mut);background:none;
border:none;padding:0;cursor:pointer;}
#carte svg{width:100%;height:auto;background:var(--card);
border:1px solid var(--line);border-radius:18px;display:block;}
.cartenote{font-size:12px;color:var(--mut);margin:8px 0 0;line-height:2;}
.cartenote button{padding:5px 12px;font-size:12px;border-radius:999px;
border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer;}
.compbar{position:fixed;left:50%;bottom:14px;transform:translateX(-50%);
background:var(--ink);color:var(--bg);border-radius:999px;
padding:8px 10px 8px 16px;display:flex;gap:10px;align-items:center;
font-size:13.5px;z-index:40;white-space:nowrap;}
.compbar button{font-size:13px;padding:6px 13px;border:none;border-radius:999px;
background:var(--bg);color:var(--ink);cursor:pointer;}
.compwrap{overflow-x:auto;margin-top:12px;}
.comptab{width:100%;border-collapse:collapse;font-size:13px;}
.comptab th,.comptab td{padding:9px 8px;border-bottom:1px solid var(--line);
text-align:left;vertical-align:top;}
.comptab th{font-size:12px;line-height:1.35;}
.comptab td{font-family:var(--mono);font-size:12.5px;}
.comptab td:first-child,.comptab th:first-child{color:var(--mut);
font-size:12px;min-width:105px;font-family:inherit;}
.comptab td.best{background:var(--okbg);color:var(--ok);font-weight:600;}
.methodo p{font-size:13.5px;line-height:1.6;margin-top:6px;}
.methodo p.fh{margin-top:18px;}
.methodo p.name{margin-top:0;}
.fav{color:var(--charge);}
.regchip{color:var(--brand);font-weight:600;cursor:pointer;}
.resrow{display:flex;justify-content:space-between;font-size:14px;
padding:5px 0;}
.resrow span:first-child{color:var(--mut);}
.resrow .mono{font-size:14px;}
.hr{height:1px;background:var(--line);margin:14px 0;}
</style>
</head>
<body>
<main>
<div class="entete">
  <div class="marque" id="titre" title="Revenir à l'accueil">
    <img src="__LOGO__" alt="">
    <div><b>Boussole Santé Québec</b>
    <div class="tag">urgences en temps réel</div></div>
  </div>
  <div class="onglets">
    <button type="button" class="onglet on" id="ong1">1 · Orienter</button>
    <button type="button" class="onglet" id="ong2">2 · Choisir</button>
    <button type="button" class="onglet" id="ong3">3 · Y aller</button>
  </div>
</div>
<div class="chips" id="chips"></div>
<div id="notice"></div>
<div id="ecran1">
  <div class="c20" style="padding:36px 32px">
    <div class="kick">Commencez ici</div>
    <h1 class="h1g">Où aller, maintenant&nbsp;?</h1>
    <p class="sous">L'urgence n'est pas toujours le bon endroit — la Boussole
    vous montre où l'attente est la plus courte près de chez vous, à partir
    des données publiques du MSSS.</p>
    <div class="triage">
      <div class="tcard t911">
        <div class="tt">Urgence vitale</div>
        <div class="td">Douleur à la poitrine, perte de conscience,
        saignement majeur</div>
        <div class="tn">Appelez le 911</div>
      </div>
      <button type="button" class="tcard t811" id="t-guide">
        <div class="tt">Doute, conseil, autre besoin</div>
        <div class="td">Fièvre, symptômes flous, question sur un médicament</div>
        <div class="tn">Info-Santé 811 · guide&nbsp;→</div>
      </button>
      <button type="button" class="tcard tbrand" id="t-soins">
        <div class="tt">Besoin de voir quelqu'un</div>
        <div class="td">Blessure, infection, état qui ne s'améliore pas</div>
        <div class="tn">Voir où aller&nbsp;→</div>
      </button>
    </div>
    <p class="gnote" style="margin-top:14px">Détresse ou idées suicidaires&nbsp;:
    appelez ou textez le <b>988</b>, 24&nbsp;h/7. En cas de doute sur votre
    état&nbsp;: <b>811</b>, une infirmière répond en tout temps.</p>
  </div>
  <div class="duo">
    <div class="c20" id="recomm"></div>
    <div class="c20" id="reseau"></div>
  </div>
</div>
<div id="ecran2" style="display:none">
<div id="guide-src" style="display:none">
    <p class="gnote">Ce guide ne donne pas d'avis médical et n'évalue pas vos
    symptômes : vous choisissez vous-même la situation qui vous ressemble.
    Vos réponses ne sont ni enregistrées ni transmises. En cas de doute,
    appelez le <b>811</b> — une infirmière, 24&nbsp;h/7, gratuit.</p>
    <div class="alarm"><b>Composez le 911 maintenant</b> en cas de :
    difficulté importante à respirer &middot; douleur ou serrement à la
    poitrine &middot; visage affaissé, bras faible ou parole difficile
    (signes d'AVC) &middot; perte de conscience ou confusion soudaine &middot;
    saignement qui ne s'arrête pas &middot; réaction allergique qui
    s'aggrave. En détresse ou idées suicidaires : appelez ou textez le
    <b>988</b>, 24&nbsp;h/7.</div>
</div>
<div class="bar">
  <input id="q" type="search" placeholder="Hôpital ou ville…">
  <select id="reg"><option value="">Toutes les régions</option></select>
  <select id="sort">
    <option value="distance">Trier : distance (le plus proche d'abord)</option>
    <option value="total">Trier : temps total (trajet + attente)</option>
    <option value="taux">Trier : occupation des civières</option>
    <option value="attente_pec">Trier : personnes en attente</option>
    <option value="dms_ambulatoire">Trier : séjour moyen en salle d'attente</option>
    <option value="nom">Trier : nom</option>
  </select>
  <button id="loc" type="button">&#128205; Autour de moi</button>
  <button id="vue" type="button">&#128506; Carte</button>
  <button id="gtoggle" type="button">&#129517; Guide</button>
</div>
<div class="ligneres">
  <span id="nres"></span>
  <span class="legdots">
    <span><span class="dot" style="background:var(--fluide)"></span>fluide</span>
    <span><span class="dot" style="background:var(--charge)"></span>chargé</span>
    <span><span class="dot" style="background:var(--deborde)"></span>débordé</span>
  </span>
</div>
<div id="carte" style="display:none"></div>
<div id="list"></div>
</div>
<div id="ecran3" style="display:none"></div>
<div id="compbar" class="compbar" style="display:none"></div>
<footer>
Source : Fichier horaire de la situation à l'urgence, Console provinciale des urgences (CPU),
ministère de la Santé et des Services sociaux, via Données Québec.<br>
Si un hôpital est absent, il se peut que le MSSS ait suspendu temporairement la diffusion de sa région.<br>Outil d'information seulement — les cas prioritaires sont toujours vus en premier,
peu importe l'établissement. En cas d'urgence vitale, composez le 911.<br>
<a id="lien-apropos" href="#" style="color:var(--info);font-weight:600">À propos —
ce que la Boussole fait (et ne fait pas)</a> ·
<a id="lien-methodo" href="#" style="color:var(--info);font-weight:600">Méthodologie,
sources et vie privée</a>__SOUTIEN__
</footer>
<div id="apropos-src" style="display:none">
<p class="name" style="font-size:18px;padding-right:90px">Ce que la Boussole
fait — et ne fait pas</p>

<p class="fh">Ce qu'elle fait</p>
<p><b>Informer en temps réel.</b> L'état des urgences du Québec diffusées par
le MSSS, actualisé chaque heure&nbsp;: personnes en attente d'un médecin,
personnes sur place, civières, séjours moyens — et un portrait d'ensemble du
réseau (fluide / chargé / débordé).</p>
<p><b>Orienter selon le besoin.</b> Trier l'urgence vitale (911, 988) et le
doute (811), décrire factuellement les ressources — pharmacien, 811, GAP,
cliniques, CLSC (avec les points de service les plus proches), urgence — et
recommander une urgence générale à distance raisonnable, en expliquant
pourquoi.</p>
<p><b>Aider à décider.</b> Distances et temps de route estimés, fourchette de
temps sur place pour un cas non prioritaire ajustée à l'achalandage, historique
des dernières heures, affluence typique par heure, «&nbsp;quand partir&nbsp;»,
comparateur, carte, favoris.</p>
<p><b>Respecter l'utilisateur.</b> Gratuite, sans compte, sans publicité, sans
traçage&nbsp;; la position ne quitte jamais l'appareil&nbsp;; fonctionne hors
ligne&nbsp;; sources et calculs publics.</p>

<p class="fh">Ce qu'elle ne fait pas</p>
<p><b>Côté médical — la ligne rouge.</b> Aucun diagnostic, aucun avis médical,
aucune évaluation de symptômes&nbsp;: la Boussole ne demande jamais
«&nbsp;où avez-vous mal&nbsp;?&nbsp;». Elle ne prédit pas votre attente
personnelle&nbsp;: au triage, la priorité clinique passe avant tout. Elle ne
remplace ni le 911, ni le 811, ni un professionnel de la santé.</p>
<p><b>Côté technique — limites assumées.</b> Distances à vol d'oiseau (pas
d'itinéraire réel)&nbsp;; pas de notifications&nbsp;; pas de filtres
«&nbsp;24&nbsp;h&nbsp;» ou «&nbsp;pédiatrie&nbsp;» (absents des données
MSSS)&nbsp;; pas de prise de rendez-vous&nbsp;; les hôpitaux dont le MSSS
suspend la diffusion n'apparaissent pas&nbsp;; de légers écarts avec Québec.ca
sont possibles.</p>
<p><b>Côté données personnelles — par conception.</b> Elle ne collecte, ne
stocke et ne transmet aucune donnée de santé ni de position. Elle ne sait
même pas combien de personnes l'utilisent.</p>

<p class="fh">En une phrase</p>
<p>La Boussole vous dit <b>où le réseau en est</b> et <b>où il est raisonnable
d'aller</b> — jamais <b>ce que vous avez</b> ni <b>combien de temps vous
attendrez, vous</b>.</p>
</div>
<div id="methodo-src" style="display:none">
<p class="name" style="font-size:18px;padding-right:90px">Méthodologie, sources et vie privée</p>

<p class="fh">D'où viennent les données</p>
<p>Les chiffres des urgences proviennent du <b>Fichier horaire de la situation à
l'urgence</b> (Console provinciale des urgences) du ministère de la Santé et des
Services sociaux, diffusé en données ouvertes sur Données Québec (licence
CC-BY&nbsp;4.0) et mis à jour chaque heure. Les coordonnées, adresses et points
de service CLSC viennent du <b>répertoire cartographique M02</b> du même
ministère. La Boussole n'invente aucun chiffre&nbsp;: elle les met en forme.</p>
<p>Le MSSS suspend parfois la diffusion d'une région (au moment d'écrire ces
lignes&nbsp;: Mauricie–Centre-du-Québec et Nord-de-l'Île-de-Montréal). Ces
hôpitaux existent et fonctionnent&nbsp;— ils sont simplement absents des
données, et donc de la Boussole. De petits écarts avec Québec.ca sont normaux
(heures de publication différentes).</p>

<p class="fh">Ce que veulent dire les chiffres</p>
<p><b>En attente d'un médecin</b>&nbsp;: personnes inscrites à l'urgence dont la
prise en charge médicale n'a pas commencé. <b>Personnes sur place</b>&nbsp;:
tout le monde présent à l'urgence, y compris les personnes déjà prises en
charge. <b>Civières occupées / fonctionnelles</b>&nbsp;: un taux au-dessus de
100&nbsp;% signifie plus de patients sur civière que de places prévues.
<b>Séjour moyen</b>&nbsp;: durée moyenne de séjour observée la veille — pour
les patients ambulatoires (salle d'attente) ou sur civière.</p>

<p class="fh">Comment la Boussole calcule</p>
<p><b>La jauge d'achalandage</b>&nbsp;: moyenne de deux pressions — l'occupation
des civières (en&nbsp;%) et l'attente (2 personnes en attente par civière
fonctionnelle&nbsp;=&nbsp;100&nbsp;%). Vert sous 85, orange de 85 à 124, rouge
à partir de 125.</p>
<p><b>« Temps sur place estimé »</b>&nbsp;: le séjour moyen ambulatoire d'hier,
ajusté par l'achalandage du moment (le nombre de personnes en attente comparé à
la moyenne habituelle à cette heure, facteur borné entre 0,6 et 1,8), présenté
en fourchette de ±25&nbsp;%. Sans historique suffisant, la fourchette repose
sur le séjour d'hier seulement.</p>
<p><b>« Quand partir&nbsp;? »</b>&nbsp;: l'heure la plus calme des 12 prochaines
heures selon le profil moyen de cet hôpital (au moins 3 jours d'observations),
proposée seulement si elle est au moins 25&nbsp;% plus calme que l'heure
actuelle.</p>
<p><b>Distances et temps de route</b>&nbsp;: distance à vol d'oiseau
multipliée par 1,3, à une vitesse moyenne de 30 à 80&nbsp;km/h selon la
distance. C'est une approximation&nbsp;: sur les longues distances (fleuve,
traversiers), le trajet réel peut être bien plus long. <b>Temps total</b>&nbsp;=
temps de route + séjour moyen estimé.</p>
<p><b>Tendances et dernières heures</b>&nbsp;: la Boussole conserve chaque
relevé horaire dans une petite base locale. « Dernières heures » montre les
relevés réels des 48 dernières heures&nbsp;; « Attente typique » montre la
moyenne par heure de la journée. « En hausse / en baisse » compare le dernier
relevé à celui d'environ 3 heures avant (écart d'au moins 3 personnes et
20&nbsp;%).</p>
<p><b>Résumé provincial</b>&nbsp;: l'occupation moyenne est pondérée (toutes
les civières occupées divisées par toutes les civières fonctionnelles), sur
les seules urgences diffusées par le MSSS.</p>
<p><b>«&nbsp;Recommandé près de vous&nbsp;»</b>&nbsp;: toujours l'urgence
générale la plus proche de votre position. Si, dans un rayon réaliste, un
hôpital plus éloigné afficherait un temps total estimé d'au moins
45&nbsp;minutes de moins, il est mentionné comme <b>alternative
optionnelle</b> — jamais comme recommandation principale, car ces
estimations ne comptent ni le trafic réel ni leur propre marge
d'erreur.</p>
<p><b>Urgences spécialisées</b>&nbsp;: les urgences à vocation particulière
(santé mentale, cardiologie-pneumologie, pédiatrie) sont reconnues par leur
nom, étiquetées dans la liste et exclues de la carte
«&nbsp;Recommandé près de vous&nbsp;», qui ne propose que des urgences
générales. Elles restent visibles et consultables partout ailleurs.</p>

<p class="fh">Ce que la Boussole ne fait pas</p>
<p>Elle ne donne <b>aucun avis médical</b> et ne prédit pas votre attente
personnelle&nbsp;: au triage, les cas prioritaires passent toujours en premier,
peu importe l'établissement et peu importe nos chiffres. Le guide
«&nbsp;Où aller&nbsp;?&nbsp;» décrit les ressources publiques&nbsp;; il ne pose
aucune question de santé et n'évalue aucun symptôme. En cas d'urgence vitale,
composez le 911&nbsp;; en cas de doute, le 811.</p>

<p class="fh">Vie privée</p>
<p>Aucun compte, aucun serveur de la Boussole, aucun traçage. Votre position
est utilisée <b>uniquement dans votre navigateur</b> pour calculer des
distances&nbsp;— elle n'est jamais transmise. La position GPS n'est jamais
enregistrée&nbsp;; si vous choisissez votre ville dans la liste, ce choix est
retenu sur votre appareil seulement, pour que la position soit active dès la
prochaine visite. Vos favoris sont de simples noms d'hôpitaux gardés sur
votre appareil. Le bouton «&nbsp;Itinéraire&nbsp;» ouvre votre application de
cartes avec la <b>destination seulement</b>.</p>
</div>
</main>
<script>
let DATA=[],META={},POS=null;
/* Favoris : de simples noms d'h\\u00f4pitaux, gard\\u00e9s uniquement sur
   l'appareil (localStorage) \\u2014 aucune donn\\u00e9e de sant\\u00e9. */
let FAV=new Set();
try{FAV=new Set(JSON.parse(localStorage.getItem('bousfav')||'[]'));}catch(e){}
function toggleFav(nom){
  FAV.has(nom)?FAV.delete(nom):FAV.add(nom);
  try{localStorage.setItem('bousfav',JSON.stringify([...FAV]));}catch(e){}
}
/* Fourchette \\u00ab temps sur place \\u00bb partag\\u00e9e entre la carte et
   la fiche : DMS ambulatoire d'hier \\u00d7 facteur d'achalandage. */
function estBornes(d){
  if(d.dms_ambulatoire==null)return null;
  const now=new Date().getHours();
  const typ=(META.tjours>=3&&d.trend)?d.trend[now]:null;
  let fac=1,ratio=null;
  if(typ!=null&&typ>0&&d.attente_pec!=null){
    ratio=d.attente_pec/typ;
    fac=Math.min(1.8,Math.max(0.6,ratio));
  }
  const est=d.dms_ambulatoire*fac;
  return {lo:est*45,hi:est*75,typ:typ,ratio:ratio};
}
/* Indice d'achalandage de la jauge : moyenne de l'occupation des
   civi\\u00e8res (%) et de la pression d'attente (2 personnes en attente
   par civi\\u00e8re fonctionnelle = 100 %). */
function indice(d){
  const p=[];
  if(d.taux!=null)p.push(Math.min(d.taux,200));
  if(d.attente_pec!=null&&d.civ_fonct)
    p.push(Math.min(100*d.attente_pec/(2*d.civ_fonct),200));
  if(!p.length)return null;
  return Math.round(p.reduce((a,b)=>a+b,0)/p.length);
}
function coulJauge(i){return i<85?'var(--fluide)':(i<125?'var(--charge)':'var(--deborde)');}
function etatDe(i){return i==null?'':(i<85?'Fluide':(i<125?'Charg\u00e9':'D\u00e9bord\u00e9'));}
/* Pastilles d'en-t\\u00eate : fra\\u00eecheur du relev\\u00e9, nombre
   d'urgences, \\u00e9tat de la position. */
function chips(){
  const c=document.getElementById('chips');
  let h='';
  if(META.source==='msss'&&META.releve){
    const t=new Date(META.releve);
    const min=Math.max(0,Math.round((Date.now()-t.getTime())/60000));
    if(!isNaN(min))
      h+='<span class="chip '+(min<=75?'ok':'warn')+'">Relev\\u00e9 '+
        (min<60?'il y a '+min+' min':'de '+META.releve.slice(11))+'</span>';
  }else{
    h+='<span class="chip warn">Donn\\u00e9es de d\\u00e9monstration</span>';
  }
  h+='<span class="chip">'+DATA.length+' urgences</span>';
  h+='<span class="chip" id="chip-pos">'+
    (POS?(POSVILLE?'\\uD83D\\uDCCD '+POSVILLE:'\\u2713 Position activ\\u00e9e')
        :'\\uD83D\\uDCCD Activer ma position')+'</span>';
  c.innerHTML=h;
  const cp=document.getElementById('chip-pos');
  if(cp)cp.addEventListener('click',()=>{
    if(!POS)locate();
    else ouvrirChoixVille('choix');  /* GPS ou changement de ville */
  });
}
/* R\\u00e9sum\\u00e9 provincial : le pouls du Qu\\u00e9bec, calcul\\u00e9
   localement \\u00e0 partir des donn\\u00e9es d\\u00e9j\\u00e0 charg\\u00e9es.
   L'occupation moyenne est pond\\u00e9r\\u00e9e (total occup\\u00e9es /
   total fonctionnelles), pas une moyenne de pourcentages. */
function fraicheurMin(){
  if(!META.releve)return null;
  const m=Math.round((Date.now()-new Date(META.releve).getTime())/60000);
  return isNaN(m)?null:Math.max(0,m);
}
/* Carte \u00ab Le r\u00e9seau en ce moment \u00bb de l'\u00e9cran 1 :
   d\u00e9compte fluide/charg\u00e9/d\u00e9bord\u00e9 + moyenne pond\u00e9r\u00e9e. */
function reseauHtml(){
  const k='<div class="kick">Le r\u00e9seau en ce moment</div>';
  if(!DATA.length||META.source!=='msss')
    return k+'<p class="gnote" style="margin-top:12px">Donn\u00e9es de d\u00e9monstration.</p>';
  let occ=0,fonct=0,att=0,nf=0,nc=0,nd=0;
  const rT={},rN={};
  DATA.forEach(d=>{
    if(d.civ_occ!=null&&d.civ_fonct){occ+=d.civ_occ;fonct+=d.civ_fonct;}
    if(d.attente_pec!=null)att+=d.attente_pec;
    const i=indice(d);
    if(i!=null){if(i<85)nf++;else if(i<125)nc++;else nd++;}
    if(d.taux!=null){rT[d.region]=(rT[d.region]||0)+d.taux;
      rN[d.region]=(rN[d.region]||0)+1;}
  });
  const taux=fonct?Math.round(100*occ/fonct):null;
  const regs=Object.keys(rT).filter(r=>rN[r]>=2)
    .map(r=>({r:r,m:Math.round(rT[r]/rN[r])}))
    .sort((a,b)=>b.m-a.m).slice(0,3);
  const min=fraicheurMin();
  return k+
    '<div style="display:flex;align-items:baseline;gap:10px;margin-top:16px">'+
    '<span class="mono" style="font-size:38px;font-weight:500;line-height:1">'+DATA.length+'</span>'+
    '<span style="font-size:14px;color:var(--mut)">urgences suivies</span></div>'+
    '<div class="hr"></div>'+
    '<div class="resrow"><span>Fluide</span><span class="mono" style="color:var(--fluide)">'+nf+'</span></div>'+
    '<div class="resrow"><span>Charg\u00e9</span><span class="mono" style="color:var(--charge)">'+nc+'</span></div>'+
    '<div class="resrow"><span>D\u00e9bord\u00e9</span><span class="mono" style="color:var(--deborde)">'+nd+'</span></div>'+
    '<div class="hr"></div>'+
    '<div class="resrow"><span>Occupation moyenne</span><span class="mono">'+(taux!=null?taux+' %':'n/d')+'</span></div>'+
    '<div class="resrow"><span>En attente d\u2019un m\u00e9decin</span><span class="mono">'+Math.round(att).toLocaleString('fr-CA')+'</span></div>'+
    (regs.length?'<p class="trcap" style="margin-top:10px">R\u00e9gions charg\u00e9es\u00a0: '+
      regs.map(x=>'<span class="regchip" data-region="'+x.r+'">'+x.r+' ('+x.m+'\u00a0%)</span>').join(' \u00b7 ')+'</p>':'')+
    '<p class="trcap mono" style="margin-top:8px;font-size:11px">'+
    (min!=null?'Relev\u00e9 il y a '+min+' min \u00b7 source MSSS':'Source MSSS')+'</p>';
}
/* Urgences sp\u00e9cialis\u00e9es : list\u00e9es et \u00e9tiquet\u00e9es,
   mais jamais recommand\u00e9es d'office \u00e0 tout le monde. */
function vocation(d){
  const n=d.installation.toUpperCase();
  if(n.includes('SANT\u00c9 MENTALE')||n.includes('DOUGLAS'))
    return 'sant\u00e9 mentale';
  if(n.includes('CARDIOLOGIE')||n.includes('PNEUMOLOGIE'))
    return 'cardiologie-pneumologie';
  if(n.includes('SAINTE-JUSTINE')||n.includes('POUR ENFANTS'))
    return 'p\u00e9diatrique';
  return null;
}
/* Recommandation raisonnable : on ne consid\u00e8re que les urgences
   g\u00e9n\u00e9rales dans un rayon r\u00e9aliste (le plus proche + 60 km,
   minimum 80 km \u2014 assez pour les r\u00e9gions rurales), et un
   h\u00f4pital plus \u00e9loign\u00e9 que le plus proche doit faire gagner
   au moins 45 minutes de temps total, sinon le gain est plus petit que la
   marge d'erreur de nos estimations. */
function meilleurGeneral(){
  const gens=DATA.filter(d=>!vocation(d));
  if(!POS){
    let b=null;
    gens.forEach(d=>{if(d._total!=null&&(b==null||d._total<b._total))b=d;});
    return b;
  }
  let proche=null;
  gens.forEach(d=>{if(d._km!=null&&(proche==null||d._km<proche._km))proche=d;});
  if(!proche)return null;
  const dmax=Math.max(proche._km+60,80);
  let best=null;
  gens.forEach(d=>{
    if(d._km==null||d._km>dmax||d._total==null)return;
    if(best==null||d._total<best._total)best=d;
  });
  if(!best)return proche;
  if(best!==proche&&proche._total!=null&&best._total>proche._total-45)
    return proche;
  return best;
}
function plusProcheGeneral(){
  let p=null;
  DATA.forEach(d=>{
    if(vocation(d)||d._km==null)return;
    if(p==null||d._km<p._km)p=d;
  });
  return p;
}
/* Carte \u00ab Recommand\u00e9 pr\u00e8s de vous \u00bb : le temps total
   estim\u00e9 le plus court depuis la position, urgences g\u00e9n\u00e9rales
   seulement. */
function recommHtml(){
  const k='<div class="kick">Recommand\u00e9 pr\u00e8s de vous</div>';
  if(META.source!=='msss'||!DATA.length)
    return k+'<p class="gnote" style="margin-top:12px">Disponible avec les donn\u00e9es en direct.</p>';
  if(!POS){
    return k+'<p style="margin-top:14px;font-size:15px;line-height:1.5">Activez '+
      'votre position (ou choisissez votre ville) pour voir o\u00f9 le temps '+
      'total \u2014 trajet + attente \u2014 est le plus court pour vous.</p>'+
      '<button type="button" class="pill pill-b" style="margin-top:16px" '+
      'data-loc="1">\\uD83D\\uDCCD Activer ma position</button>';
  }
  const proche=plusProcheGeneral();
  if(!proche)return k+'<p class="gnote" style="margin-top:12px">Pas assez de donn\u00e9es.</p>';
  const b=estBornes(proche);
  const totalTxt=proche._total!=null?fmtMin(proche._total)
    :(b?fmtMin((b.lo+b.hi)/2):'\u2014');
  let h=k+
    '<div style="margin-top:16px;display:flex;align-items:flex-start;justify-content:space-between;gap:20px;flex-wrap:wrap">'+
    '<div style="min-width:190px;flex:1">'+
    '<div style="font-size:21px;font-weight:600;letter-spacing:-0.02em;line-height:1.2">'+proche.installation+'</div>'+
    '<div style="margin-top:6px;font-size:14px;color:var(--mut)">'+
    Math.round(proche._km)+' km \u00b7 ~'+fmtMin(proche._route)+' de route \u00b7 '+
    'l\u2019urgence g\u00e9n\u00e9rale la plus proche de chez vous</div></div>'+
    '<div style="text-align:right"><div class="bigmono" style="font-size:32px">'+totalTxt+'</div>'+
    '<div style="font-size:12px;color:var(--mut);margin-top:4px">temps total estim\u00e9</div></div></div>'+
    (b?'<p class="trcap">Dont '+fmtMin(b.lo)+' \u2013 '+fmtMin(b.hi)+' sur place '+
      '(cas non prioritaire \u2014 le triage passe toujours en premier).</p>':'')+
    '<div style="margin-top:16px;display:flex;gap:10px;flex-wrap:wrap">'+
    '<button type="button" class="pill pill-n" data-fiche="'+proche._i+'">Voir la fiche</button>'+
    '<button type="button" class="pill pill-o" data-go2="1">Comparer les autres</button></div>';
  /* Une option plus lointaine n'est qu'une ALTERNATIVE, jamais le choix par
     d\u00e9faut \u2014 et on invite \u00e0 la prudence (trafic non compt\u00e9). */
  const best=meilleurGeneral();
  if(best&&best!==proche&&best._total!=null&&proche._total!=null){
    h+='<div class="hr"></div>'+
      '<p class="trcap"><b>Si vous pouvez faire la route\u00a0:</b> '+
      best.installation+', \u00e0 '+Math.round(best._km)+' km (~'+
      fmtMin(best._route)+'), afficherait un temps total \u2248 '+
      fmtMin(best._total)+' \u2014 soit \u2248 '+
      fmtMin(proche._total-best._total)+' de moins. Estimation \u00e0 prendre '+
      'avec prudence\u00a0: le trafic r\u00e9el n\u2019est pas compt\u00e9. '+
      '<span class="regchip" data-fiche="'+best._i+'">Voir sa fiche</span></p>';
  }
  return h;
}
function majAccueil(){
  const r=document.getElementById('reseau'),c=document.getElementById('recomm');
  if(r)r.innerHTML=reseauHtml();
  if(c)c.innerHTML=recommHtml();
}
let SCREEN=1,FICHE_SEL=null;
function goScreen(n){
  SCREEN=n;
  [1,2,3].forEach(k=>{
    document.getElementById('ecran'+k).style.display=(k===n)?'':'none';
    document.getElementById('ong'+k).classList.toggle('on',k===n);
  });
  window.scrollTo(0,0);
}
function goAccueil(){goScreen(1);}
function goAppli(){goScreen(2);}
/* Villes de repli si la g\\u00e9olocalisation du navigateur est refus\\u00e9e
   ou indisponible. Tout reste local : rien n'est jamais transmis. */
const VILLES=[
["Alma",48.55,-71.65],["Amos",48.566,-78.116],["Baie-Comeau",49.216,-68.148],
["Chibougamau",49.911,-74.365],["Drummondville",45.883,-72.483],
["Gasp\\u00e9",48.833,-64.487],["Gatineau",45.477,-75.701],["Granby",45.4,-72.733],
["Joliette",46.023,-73.439],["Laval",45.606,-73.712],["L\\u00e9vis",46.738,-71.246],
["Longueuil",45.531,-73.518],["Matane",48.85,-67.533],["Montr\\u00e9al",45.502,-73.567],
["Qu\\u00e9bec",46.813,-71.208],["Repentigny",45.742,-73.45],["Rimouski",48.449,-68.524],
["Rouyn-Noranda",48.236,-79.024],["Saguenay",48.428,-71.069],
["Saint-Georges",46.117,-70.667],["Saint-Hyacinthe",45.631,-72.957],
["Saint-Jean-sur-Richelieu",45.301,-73.258],["Saint-J\\u00e9r\\u00f4me",45.781,-74.004],
["Sept-\\u00celes",50.2,-66.382],["Shawinigan",46.567,-72.744],
["Sherbrooke",45.404,-71.893],["Terrebonne",45.706,-73.647],
["Trois-Rivi\\u00e8res",46.343,-72.543],["Val-d'Or",48.098,-77.783],
["Victoriaville",46.05,-71.967]];
/* Distance \\u00e0 vol d'oiseau (formule de haversine), en km. */
function hav(a1,o1,a2,o2){const r=Math.PI/180,R=6371,
  dA=(a2-a1)*r,dO=(o2-o1)*r,
  h=Math.sin(dA/2)**2+Math.cos(a1*r)*Math.cos(a2*r)*Math.sin(dO/2)**2;
  return 2*R*Math.asin(Math.sqrt(h));}
function fmtMin(m){if(m==null)return 'n/d';m=Math.round(m);
  return m<60?m+' min':Math.floor(m/60)+' h '+String(m%60).padStart(2,'0');}
let POSVILLE=null; /* nom de la ville si la position vient de la liste */
function setPosition(lat,lon,ville){
  POS=[lat,lon];
  POSVILLE=ville||null;
  DATA.forEach(d=>{
    if(d.lat==null){d._km=null;d._route=null;d._total=null;return;}
    d._km=hav(lat,lon,d.lat,d.lon);
    /* Estimation grossi\\u00e8re du temps de route : distance routi\\u00e8re
       \\u2248 1,3 \\u00d7 vol d'oiseau ; vitesse moyenne de 30 \\u00e0 80 km/h
       selon la distance (ville \\u2192 autoroute). */
    const kmRoute=d._km*1.3, v=30+50*Math.min(d._km,50)/50;
    d._route=kmRoute/v*60;
    d._total=d.dms_ambulatoire!=null?d._route+d.dms_ambulatoire*60:null;
  });
  const lb=document.getElementById('loc');
  if(lb)lb.textContent='Position activ\\u00e9e \\u2713';
  chips();
  /* Si un r\\u00e9sultat du guide affichant les CLSC est ouvert, on le
     rafra\\u00eechit avec les distances maintenant connues. */
  if((LASTG==='rclin'||LASTG==='rgap')&&document.getElementById('gq'))ggo(LASTG);
  majAccueil();
  /* D\\u00e8s que la position est connue, on montre le plus proche d'abord
     (sauf si l'utilisateur avait d\\u00e9j\\u00e0 choisi le temps total). */
  const sel=document.getElementById('sort');
  if(sel.value!=='total')sel.value='distance';
  if(!document.getElementById('posnote')){
    const n=document.createElement('p');n.id='posnote';n.className='sub';
    n.textContent='Distances \\u00e0 vol d\\u2019oiseau et temps de route estim\\u00e9s \\u2014 '+
      'sur de longues distances, le trajet r\\u00e9el (fleuve, traversiers, d\\u00e9tours) '+
      'peut \\u00eatre beaucoup plus long. Votre position reste dans votre navigateur : '+
      'elle n\\u2019est jamais transmise. Si vous choisissez votre ville dans la '+
      'liste, elle est retenue sur cet appareil seulement, pour la prochaine visite.';
    document.getElementById('notice').before(n);
  }
  render();
}
function villeMemo(){
  try{return localStorage.getItem('bousville');}catch(e){return null;}
}
function fallbackVilles(pre){
  const b=document.getElementById('loc');
  if(!b||document.getElementById('ville'))return;
  const s=document.createElement('select');s.id='ville';
  s.innerHTML='<option value="">Votre ville\\u2026</option>'+
    VILLES.map((v,i)=>'<option value="'+i+'">'+v[0]+'</option>').join('');
  s.addEventListener('change',()=>{const v=VILLES[+s.value];
    if(v){
      /* On retient la ville sur l'appareil : au prochain lancement, la
         position sera active toute seule. Rien ne quitte le t\\u00e9l\\u00e9phone. */
      try{localStorage.setItem('bousville',v[0]);}catch(e){}
      setPosition(v[1],v[2],v[0]);
    }});
  if(pre!=null)s.value=String(pre);
  b.replaceWith(s);
}
function reprendreVille(){
  /* Ville retenue lors d'une visite pr\\u00e9c\\u00e9dente \\u2192 position
     automatique d\\u00e8s l'ouverture. */
  const vn=villeMemo();
  if(!vn)return false;
  const i=VILLES.findIndex(v=>v[0]===vn);
  if(i<0)return false;
  fallbackVilles(i);
  setPosition(VILLES[i][1],VILLES[i][2],vn);
  return true;
}
function gpsDirect(){
  if(!navigator.geolocation){ouvrirChoixVille('echec');return;}
  navigator.geolocation.getCurrentPosition(
    p=>setPosition(p.coords.latitude,p.coords.longitude),
    ()=>ouvrirChoixVille('echec'),
    {timeout:8000,maximumAge:600000});
}
function ouvrirChoixVille(motif){
  /* Fen\\u00eatre de position : GPS exact OU choix de ville, visible depuis
     n'importe quel \\u00e9cran. motif='echec' quand le GPS vient d'\\u00e9chouer. */
  if(document.getElementById('voile'))return;
  const v=document.createElement('div');v.className='voile';v.id='voile';
  const intro=(motif==='echec')
    ?'La localisation exacte n\\u2019a pas pu \\u00eatre obtenue \\u2014 '+
     'refus\\u00e9e, bloqu\\u00e9e, ou indisponible (sur une page ouverte en '+
     'local, le navigateur l\\u2019interdit\\u00a0; sur le site en ligne, elle '+
     'fonctionne). Choisissez votre ville\\u00a0: les distances partiront de '+
     'son centre, et elle sera retenue sur cet appareil. Rien n\\u2019est transmis.'
    :'Utilisez votre position exacte (le navigateur demandera votre '+
     'permission), ou choisissez votre ville\\u00a0: elle sera retenue sur '+
     'cet appareil. Dans les deux cas, rien ne quitte votre appareil.';
  v.innerHTML='<div class="fiche"><button type="button" class="fx" '+
    'id="fermer">\\u2715 Fermer</button>'+
    '<p class="name" style="font-size:17px">Votre position</p>'+
    '<p class="gnote">'+intro+'</p>'+
    '<button type="button" class="pill pill-b" id="gpsov" '+
    'style="display:block;width:100%;margin-top:12px">'+
    (motif==='echec'?'R\\u00e9essayer la localisation exacte'
      :'\\uD83D\\uDCCD Utiliser ma position exacte')+'</button>'+
    '<select id="villeov" style="width:100%;margin-top:10px">'+
    '<option value="">\\u2026 ou choisissez votre ville</option>'+
    VILLES.map((x,i)=>'<option value="'+i+'">'+x[0]+'</option>').join('')+
    '</select></div>';
  if(POSVILLE){
    const i=VILLES.findIndex(x=>x[0]===POSVILLE);
    if(i>=0)v.querySelector('#villeov').value=String(i);
  }
  v.addEventListener('click',e=>{
    if(e.target.id==='gpsov'){v.remove();gpsDirect();return;}
    if(e.target===v||e.target.id==='fermer')v.remove();});
  v.querySelector('#villeov').addEventListener('change',e=>{
    const x=VILLES[+e.target.value];
    if(!x)return;
    try{localStorage.setItem('bousville',x[0]);}catch(err){}
    if(!document.getElementById('ville'))fallbackVilles(+e.target.value);
    else document.getElementById('ville').value=e.target.value;
    setPosition(x[1],x[2],x[0]);
    v.remove();
  });
  document.body.appendChild(v);
}
function locate(){
  if(!navigator.geolocation){
    if(!reprendreVille())ouvrirChoixVille('echec');
    return;
  }
  const b=document.getElementById('loc');
  if(b)b.textContent='Localisation\\u2026';
  navigator.geolocation.getCurrentPosition(
    p=>setPosition(p.coords.latitude,p.coords.longitude),
    ()=>{
      if(b)b.textContent='\\uD83D\\uDCCD Autour de moi';
      if(!reprendreVille())ouvrirChoixVille('echec');
    },
    {timeout:8000,maximumAge:600000});
}
/* Tendances : mini-graphique des 24 heures + phrase simple, calcul\\u00e9s
   \\u00e0 partir des moyennes horaires re\\u00e7ues du script Python. */
function trendHtml(d){
  if(META.tjours<3||!d.trend)return '';
  const t=d.trend,vals=t.filter(v=>v!=null);
  if(vals.length<8)return '';
  const max=Math.max(...vals)||1;
  const now=new Date().getHours();
  let bars='';
  for(let h=0;h<24;h+=2){
    const a=t[h],b2=t[(h+1)%24];
    const v=(a!=null&&b2!=null)?(a+b2)/2:(a!=null?a:b2);
    const pc=v==null?4:Math.round(8+92*v/max);
    const c=v==null?'var(--line)':
      (v>max*0.8?'var(--deborde)':(v>max*0.55?'var(--charge)':'var(--fluide)'));
    bars+='<div><div class="ab" style="height:'+pc+'%;background:'+c+
      ((h===now||h+1===now)?';outline:2px solid var(--ink);outline-offset:1px':'')+
      '" title="'+h+' h \u2013 '+((h+2)%24)+' h : '+
      (v==null?'n/d':Math.round(v)+' en attente en moyenne')+'"></div>'+
      '<div class="al">'+h+'h</div></div>';
  }
  let creux=null,pointe=null;
  t.forEach((v,h)=>{if(v!=null){
    if(creux==null||v<t[creux])creux=h;
    if(pointe==null||v>t[pointe])pointe=h;}});
  return '<div class="fh">Affluence par heure \u2014 profil des '+META.tjours+
    ' derniers jours</div><div class="aff">'+bars+'</div>'+
    '<p class="trcap">Creux habituel vers <b>'+creux+' h</b>, pointe vers <b>'+
    pointe+' h</b> \u00b7 barre encadr\u00e9e = maintenant \u00b7 survolez '+
    'pour le d\u00e9tail.</p>';
}
/* Guide d'orientation : informatif seulement. Aucune question de sant\\u00e9,
   aucun calcul de gravit\\u00e9 \\u2014 la personne choisit la situation qui
   lui ressemble et on d\\u00e9crit ce que chaque ressource publique offre.
   Rien n'est enregistr\\u00e9 : l'\\u00e9tat vit dans la page, c'est tout. */
const GQ={
 q1:{q:'Quelle situation vous ressemble le plus\\u00a0?',o:[
  ['Question sur un m\\u00e9dicament, renouvellement, ou probl\\u00e8me mineur courant (rhume, piq\\u00fbre, infection urinaire d\\u00e9j\\u00e0 connue\\u2026)','rpharm'],
  ['Je ne suis pas certain de la gravit\\u00e9 \\u2014 je veux l\\u2019avis d\\u2019un professionnel','r811'],
  ['J\\u2019aimerais une consultation dans les prochains jours','q2'],
  ['J\\u2019ai besoin de soins aujourd\\u2019hui (sans signe d\\u2019alarme ci-dessus)','rjour']]},
 q2:{q:'Avez-vous un m\\u00e9decin de famille ou une clinique habituelle\\u00a0?',o:[
  ['Oui','rclin'],['Non','rgap']]}
};
const GRES={
 rpharm:'<b>Direction\\u00a0: votre pharmacie.</b> Souvent sans rendez-vous, '+
  'le pharmacien peut \\u00e9valuer et traiter plusieurs probl\\u00e8mes de '+
  'sant\\u00e9 courants, prescrire certains m\\u00e9dicaments, renouveler ou '+
  'ajuster une ordonnance et vacciner. Apportez votre liste de '+
  'm\\u00e9dicaments. Si votre situation d\\u00e9passe son champ '+
  'd\\u2019exercice, il vous orientera vers la bonne ressource.',
 r811:'<b>Appelez le 811.</b> Option 1\\u00a0: Info-Sant\\u00e9 \\u2014 une '+
  'infirmi\\u00e8re vous conseille, 24\\u00a0h/7, gratuitement. Option '+
  '2\\u00a0: Info-Social \\u2014 soutien psychosocial. On vous dira quoi '+
  'surveiller et o\\u00f9 aller selon votre situation.',
 rgap:'<b>Le Guichet d\\u2019acc\\u00e8s \\u00e0 la premi\\u00e8re ligne '+
  '(GAP).</b> Sans m\\u00e9decin de famille, composez le <b>811, option '+
  '3</b>\\u00a0: on vous dirige vers le bon professionnel (m\\u00e9decin, '+
  'infirmi\\u00e8re praticienne, pharmacien, physioth\\u00e9rapeute\\u2026), '+
  'souvent en quelques jours.',
 rclin:'<b>Votre clinique d\\u2019abord.</b> Appelez votre clinique ou '+
  'prenez rendez-vous en ligne sur Rendez-vous sant\\u00e9 Qu\\u00e9bec '+
  '(rvsq.gouv.qc.ca). Votre CLSC offre aussi plusieurs services sans '+
  'm\\u00e9decin (soins infirmiers, pr\\u00e9l\\u00e8vements, vaccination).',
 rjour:'<b>Trois portes possibles aujourd\\u2019hui\\u00a0:</b><br>'+
  '\\u2022 <b>811</b> \\u2014 conseil d\\u2019une infirmi\\u00e8re en '+
  'quelques minutes, 24\\u00a0h/7.<br>'+
  '\\u2022 <b>Cliniques</b> \\u2014 des places le jour m\\u00eame via '+
  'rvsq.gouv.qc.ca ou le 811 option 3.<br>'+
  '\\u2022 <b>Urgence</b> \\u2014 ouverte \\u00e0 tous, 24\\u00a0h/7\\u00a0: '+
  'la liste montre l\\u2019attente en direct, et les cas '+
  'prioritaires sont toujours vus en premier. '+
  '<button type="button" class="qbtn" data-go="voir">Voir les urgences '+
  'pr\\u00e8s de chez moi</button>'
};
/* CLSC les plus proches : affich\\u00e9s dans les r\\u00e9sultats
   \\u00ab clinique \\u00bb et \\u00ab GAP \\u00bb du guide. Le calcul de
   distance se fait localement, comme pour les h\\u00f4pitaux. */
function clscHtml(){
  if(!META.clsc||!META.clsc.length)return '';
  if(!POS){
    return '<p class="trcap">Astuce\\u00a0: activez votre position '+
      '(pastille en haut de la page) pour voir ici les CLSC les plus proches.</p>';
  }
  const prox=META.clsc
    .map(c=>({n:c.n,a:c.a,lat:c.lat,lon:c.lon,km:hav(POS[0],POS[1],c.lat,c.lon)}))
    .sort((a,b)=>a.km-b.km).slice(0,3);
  return '<p class="fh">CLSC les plus proches</p>'+prox.map(c=>
    '<div class="fstat" style="margin-top:6px"><b style="font-size:14px;font-family:inherit">'+c.n+'</b>'+
    '<span>'+c.a+' \\u00b7 \\u2248 '+Math.round(c.km)+' km</span>'+
    '<a class="fbtn" style="margin-top:8px;font-size:12.5px;padding:6px 12px" '+
    'target="_blank" rel="noopener" '+
    'href="https://www.google.com/maps/dir/?api=1&destination='+c.lat+','+c.lon+
    '">Itin\\u00e9raire</a></div>').join('')+
    '<p class="trcap">Les services offerts varient d\\u2019un point de service '+
    '\\u00e0 l\\u2019autre\\u00a0: appelez avant de vous d\\u00e9placer.</p>';
}
let LASTG=null;
function ggo(id){
  LASTG=id;
  const el=document.getElementById('gq');
  if(!el)return;
  if(GQ[id]){
    el.innerHTML='<p class="gq">'+GQ[id].q+'</p>'+
      GQ[id].o.map(o=>'<button type="button" class="qbtn" data-go="'+o[1]+'">'+
        o[0]+'</button>').join('');
  }else{
    el.innerHTML='<div class="gres">'+GRES[id]+'</div>'+
      ((id==='rclin'||id==='rgap')?clscHtml():'')+
      '<p class="gnote">Si un signe d\\u2019alarme appara\\u00eet '+
      '(encadr\\u00e9 rouge ci-dessus), composez le 911. Ce guide est '+
      'informatif\\u00a0: il ne remplace pas un avis m\\u00e9dical.</p>'+
      '<button type="button" class="qbtn" data-go="q1">\\u21ba Recommencer</button>';
  }
}
function openGuide(){
  if(document.getElementById('voile'))return;
  const v=document.createElement('div');v.className='voile';v.id='voile';
  v.innerHTML='<div class="fiche"><button type="button" class="fx" '+
    'id="fermer">\\u2715 Fermer</button>'+
    '<p class="name" style="font-size:18px;padding-right:90px">O\\u00f9 aller\\u00a0? '+
    '\\u2014 guide des ressources (informatif)</p>'+
    document.getElementById('guide-src').innerHTML+
    '<div id="gq"></div></div>';
  v.addEventListener('click',e=>{
    const go=e.target.getAttribute&&e.target.getAttribute('data-go');
    if(go){
      if(go==='voir'){
        v.remove();
        goScreen(2);
        document.getElementById('sort').value='distance';
        if(!POS)locate();
        render();
        return;
      }
      ggo(go);
      return;
    }
    if(e.target===v||e.target.id==='fermer')v.remove();
  });
  document.body.appendChild(v);
  ggo('q1');
}
function badge(t){
  if(t==null)return '<span class="badge" style="background:var(--line);color:var(--mut)">n/d</span>';
  const cls=t<100?'b-ok':(t<130?'b-warn':'b-bad');
  return '<span class="badge '+cls+'">'+t+' %</span>';
}
/* Fiche d\\u00e9taill\\u00e9e d'un h\\u00f4pital : tout ce qu'il faut pour
   d\\u00e9cider, en mots simples. S'ouvre au clic sur une carte. */
function fmt(v,suf){return v==null?'n/d':Math.round(v*10)/10+(suf||'');}
const nrm=s=>(s||'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLowerCase()
  .replace(/\\bsainte\\b/g,'ste').replace(/\\bsaint\\b/g,'st')
  .replace(/\\bhopital\\b/g,'hop').replace(/[^a-z0-9]+/g,' ').trim();
function fstat(val,lab){
  return val==null?'':'<div class="fstat"><b>'+val+'</b><span>'+lab+'</span></div>';
}
/* Estimation « si vous y allez maintenant » : s\\u00e9jour moyen d'hier,
   ajust\\u00e9 par l'achalandage du moment quand l'historique le permet.
   Toujours pr\\u00e9sent\\u00e9 en fourchette et clairement \\u00e9tiquet\\u00e9
   comme indicatif \\u2014 jamais comme une promesse. */
function estimationHtml(d){
  const now=new Date().getHours();
  const b=estBornes(d);
  const typ=b?b.typ:null,ratio=b?b.ratio:null;
  let h='';
  if(b){
    h+='<div class="binfo">Si vous y allez maintenant\\u00a0\\u2014 cas non prioritaire<br>'+
      '<b style="font-size:17px">entre '+fmtMin(b.lo)+' et '+fmtMin(b.hi)+' sur place</b>'+
      (ratio!=null?'<br>'+(ratio<0.8?'Plus calme':(ratio>1.25?'Plus occup\\u00e9':'Achalandage normal'))+
        ' que d\\u2019habitude \\u00e0 cette heure-ci ('+fmt(d.attente_pec)+
        ' en attente, moyenne\\u00a0: '+typ+')':'')+
      '</div>';
  }
  /* Le meilleur moment pour partir, d'apr\\u00e8s les derniers jours. */
  if(typ!=null){
    let best=null;
    for(let k=1;k<=12;k++){const hh=(now+k)%24;
      if(d.trend[hh]!=null&&(best==null||d.trend[hh]<d.trend[best]))best=hh;}
    if(best!=null&&d.trend[best]<=typ*0.75){
      h+='<div class="bvert"><b>Quand partir\\u00a0?</b> D\\u2019apr\\u00e8s les '+
        META.tjours+' derniers jours, c\\u2019est habituellement plus calme vers '+
        '<b>'+best+'\\u00a0h</b> (\\u2248 '+d.trend[best]+' en attente, contre '+
        typ+' en moyenne \\u00e0 cette heure-ci).</div>';
    }else{
      h+='<div class="bvert"><b>Quand partir\\u00a0?</b> Vous \\u00eates dans '+
        'une p\\u00e9riode plut\\u00f4t calme pour cet h\\u00f4pital\\u00a0: '+
        'pas de meilleur moment \\u00e9vident d\\u2019ici 12 h.</div>';
    }
  }else if(d.dms_ambulatoire!=null){
    h+='<p class="trcap">La suggestion du \\u00ab\\u00a0meilleur moment pour '+
      'partir\\u00a0\\u00bb appara\\u00eetra apr\\u00e8s 3 jours de collecte '+
      '(accumul\\u00e9\\u00a0: '+META.tjours+' jour'+(META.tjours>1?'s':'')+').</p>';
  }
  if(!h)return '';
  return h+
    '<p class="gnote">Estimation indicative, fond\\u00e9e sur le s\\u00e9jour '+
    'moyen d\\u2019hier et l\\u2019achalandage du moment. Votre attente '+
    'r\\u00e9elle d\\u00e9pend surtout de la priorit\\u00e9 de votre cas au '+
    'triage\\u00a0: les cas urgents passent toujours en premier.</p>';
}
/* Historique r\\u00e9el des derni\\u00e8res 24-48 h : les relev\\u00e9s
   effectivement observ\\u00e9s, avec une phrase de tendance simple. */
function histoHtml(d){
  if(!META.hrel||!META.hrel.length||!d.histo)return '';
  const rel=META.hrel,vals=d.histo;
  const pts=vals.map((v,i)=>({v:v,r:rel[i]})).filter(p=>p.v!=null);
  if(pts.length<3)return '';
  const max=Math.max(...pts.map(p=>p.v))||1;
  const n=new Date();
  const auj=n.getFullYear()+'-'+String(n.getMonth()+1).padStart(2,'0')+
    '-'+String(n.getDate()).padStart(2,'0');
  let bars='';
  vals.forEach((v,i)=>{
    const px=v==null?2:Math.round(2+22*v/max);
    bars+='<i style="height:'+px+'px"'+
      (i===vals.length-1?' class="der"':'')+' title="'+
      (rel[i].slice(0,10)===auj?'':'la veille, ')+rel[i].slice(11,16)+
      ' : '+(v==null?'n/d':v+' en attente')+'"></i>';
  });
  const der=pts[pts.length-1],ref=pts[Math.max(0,pts.length-4)];
  let tend='';
  if(der!==ref){
    const dv=der.v-ref.v;
    if(dv>=3&&der.v>=ref.v*1.2)
      tend='En hausse depuis '+ref.r.slice(11,16)+' ('+ref.v+' \\u2192 '+der.v+').';
    else if(dv<=-3&&der.v<=ref.v*0.8)
      tend='En baisse depuis '+ref.r.slice(11,16)+' ('+ref.v+' \\u2192 '+der.v+').';
    else tend='Plut\\u00f4t stable ces derni\\u00e8res heures.';
  }
  return '<p class="fh">Derni\\u00e8res heures \\u2014 personnes en attente</p>'+
    '<div class="tr trh">'+bars+'</div>'+
    '<p class="trcap">'+(tend?'<b>'+tend+'</b> ':'')+
    'Relev\\u00e9s r\\u00e9els ('+pts.length+' heures observ\\u00e9es \\u00b7 '+
    'barre fonc\\u00e9e = le plus r\\u00e9cent \\u00b7 survolez pour le d\\u00e9tail).</p>';
}
function openFiche(d){
  FICHE_SEL=d;
  const rel=META.releve?META.releve.replace('T',' \u00e0 '):'';
  const b=estBornes(d);
  let g='<div class="c20" style="padding:30px">'+
    '<button type="button" class="retour" data-back="1">\u2190 Retour aux '+
    'r\u00e9sultats</button>'+
    '<div style="margin-top:14px;font-size:27px;font-weight:600;'+
    'letter-spacing:-0.025em;line-height:1.12">'+
    (FAV.has(d.installation)?'<span class="fav">\u2605</span> ':'')+
    d.installation+'</div>'+
    '<div style="margin-top:8px;font-size:14.5px;color:var(--mut)">'+
    [(vocation(d)?'Urgence sp\u00e9cialis\u00e9e \u2014 '+vocation(d):null),
     d.etablissement,d.adresse,
     (POS&&d._km!=null?Math.round(d._km)+' km \u00b7 ~'+fmtMin(d._route)+
      ' de route':null)].filter(Boolean).join(' \u00b7 ')+'</div>'+
    '<div class="fgrille" style="margin-top:24px">'+
    (b?'<div class="fstat acc"><b>'+fmtMin(b.lo)+'\u2013'+fmtMin(b.hi)+'</b>'+
      '<span>temps sur place estim\u00e9 (cas non prioritaire)</span></div>':'')+
    fstat(fmt(d.attente_pec),'personnes en attente d\u2019un m\u00e9decin')+
    fstat(d.taux!=null?d.taux+' %':null,'civi\u00e8res occup\u00e9es ('+
      fmt(d.civ_occ)+' sur '+fmt(d.civ_fonct)+')')+
    fstat(fmt(d.presents),'personnes sur place')+
    fstat(d.civ_24h||null,'sur civi\u00e8re depuis plus de 24 h')+
    '</div>'+
    estimationHtml(d)+
    '<div class="fh">Ce qui vous attend</div>'+
    '<div class="timel"><span>0\u201315 min</span><span>Inscription et '+
    'triage infirmier</span></div>'+
    '<div class="timel"><span>ensuite</span><span>Salle d\u2019attente \u2014 '+
    'priorit\u00e9 selon la gravit\u00e9, pas l\u2019ordre '+
    'd\u2019arriv\u00e9e</span></div>'+
    '<div class="timel"><span>'+(b?fmtMin(b.lo)+'\u2013'+fmtMin(b.hi):'variable')+
    '</span><span>Sortie estim\u00e9e pour un cas ambulatoire (s\u00e9jour '+
    'd\u2019hier, ajust\u00e9 \u00e0 l\u2019achalandage du moment)</span></div>'+
    '</div>';
  const blocsHisto=histoHtml(d)+trendHtml(d);
  if(blocsHisto||META.tjours<3){
    g+='<div class="c20" style="margin-top:16px;padding:26px">'+blocsHisto+
      (META.tjours<3?'<p class="trcap">Le profil d\u2019affluence par heure '+
        'appara\u00eetra apr\u00e8s 3 jours de collecte (accumul\u00e9\u00a0: '+
        META.tjours+' jour'+(META.tjours>1?'s':'')+').</p>':'')+'</div>';
  }
  const a='<div class="c20" style="padding:24px">'+
    (d.lat!=null?'<a class="pill pill-b" style="display:block;text-align:center;'+
      'text-decoration:none" target="_blank" rel="noopener" '+
      'href="https://www.google.com/maps/dir/?api=1&destination='+d.lat+','+d.lon+
      '">Ouvrir l\u2019itin\u00e9raire</a>':'')+
    '<button type="button" class="pill pill-o" style="display:block;width:100%;'+
    'margin-top:10px" data-fav="'+d.installation.replace(/"/g,'&quot;')+'">'+
    (FAV.has(d.installation)?'\u2605 Retirer des favoris':'\u2606 Ajouter aux favoris')+
    '</button></div>'+
    '<div class="c20" style="padding:22px;margin-top:12px">'+
    '<div class="kick">\u00c0 apporter</div>'+
    '<p style="margin-top:10px;font-size:14px;line-height:1.55">Carte '+
    'd\u2019assurance maladie \u00b7 liste de vos m\u00e9dicaments \u00b7 '+
    'carte d\u2019h\u00f4pital si vous en avez une.</p>'+
    '<div class="hr"></div>'+
    '<p class="mono" style="font-size:11px;color:var(--mut);line-height:1.6">'+
    'Donn\u00e9es MSSS'+(rel?' \u00b7 relev\u00e9 de '+rel:'')+
    '. Estimations indicatives, non m\u00e9dicales. Les cas prioritaires '+
    'passent toujours en premier. Urgence vitale\u00a0: 911.</p></div>';
  document.getElementById('ecran3').innerHTML=
    '<div class="duo3"><div>'+g+'</div><div>'+a+'</div></div>';
  goScreen(3);
}
/* Comparateur : jusqu'\\u00e0 3 h\\u00f4pitaux c\\u00f4te \\u00e0 c\\u00f4te.
   S\\u00e9lection en m\\u00e9moire seulement \\u2014 rien d'enregistr\\u00e9. */
let COMP=new Set();
function toggleComp(nom){
  if(COMP.has(nom))COMP.delete(nom);
  else if(COMP.size<3)COMP.add(nom);
  majCompBar();render();
}
function majCompBar(){
  const b=document.getElementById('compbar');
  if(!COMP.size){b.style.display='none';return;}
  b.style.display='';
  b.innerHTML=COMP.size+'/3 s\\u00e9lectionn\\u00e9'+(COMP.size>1?'s':'')+
    (COMP.size>=2?' <button type="button" id="compgo">Comparer</button>':'')+
    ' <button type="button" id="compclear">\\u2715</button>';
  document.getElementById('compclear').addEventListener('click',()=>{
    COMP.clear();majCompBar();render();});
  const go=document.getElementById('compgo');
  if(go)go.addEventListener('click',openComp);
}
function openComp(){
  const sel=DATA.filter(d=>COMP.has(d.installation));
  if(sel.length<2)return;
  const lignes=[
    ['Occupation des civi\\u00e8res',d=>d.taux!=null?{t:d.taux+' %',n:d.taux}:null],
    ['En attente d\\u2019un m\\u00e9decin',d=>d.attente_pec!=null?{t:fmt(d.attente_pec),n:d.attente_pec}:null],
    ['Personnes sur place',d=>d.presents!=null?{t:fmt(d.presents),n:d.presents}:null],
    ['Sur civi\\u00e8re depuis 24 h+',d=>d.civ_24h!=null?{t:fmt(d.civ_24h),n:d.civ_24h}:null],
    ['Temps sur place estim\\u00e9',d=>{const b=estBornes(d);
      return b?{t:fmtMin(b.lo)+' \\u2013 '+fmtMin(b.hi),n:(b.lo+b.hi)/2}:null;}],
    ['S\\u00e9jour moyen hier',d=>d.dms_ambulatoire!=null?{t:fmt(d.dms_ambulatoire,' h'),n:d.dms_ambulatoire}:null]
  ];
  if(POS){
    lignes.push(['Distance',d=>d._km!=null?{t:Math.round(d._km)+' km',n:d._km}:null]);
    lignes.push(['Temps de route',d=>d._route!=null?{t:'~'+fmtMin(d._route),n:d._route}:null]);
    lignes.push(['Temps total estim\\u00e9',d=>d._total!=null?{t:fmtMin(d._total),n:d._total}:null]);
  }
  let t='<div class="compwrap"><table class="comptab"><tr><th></th>'+
    sel.map(d=>'<th>'+d.installation+'</th>').join('')+'</tr>';
  lignes.forEach(l=>{
    const vals=sel.map(d=>l[1](d));
    if(vals.every(v=>v==null))return;
    /* Toutes ces mesures se lisent \\u00ab plus bas = mieux \\u00bb. */
    const nums=vals.filter(v=>v!=null).map(v=>v.n);
    const mini=Math.min(...nums);
    t+='<tr><td>'+l[0]+'</td>'+vals.map(v=>
      v==null?'<td>n/d</td>':
      '<td'+(v.n===mini&&nums.length>1?' class="best"':'')+'>'+v.t+'</td>').join('')+'</tr>';
  });
  t+='<tr><td></td>'+sel.map(d=>'<td>'+
    '<button type="button" class="fbtn" style="font-size:12.5px;padding:6px 10px" '+
    'data-fiche="'+d._i+'">Fiche</button></td>').join('')+'</tr></table></div>';
  const v=document.createElement('div');v.className='voile';v.id='voile';
  v.innerHTML='<div class="fiche"><button type="button" class="fx" id="fermer">'+
    '\\u2715 Fermer</button><p class="name" style="font-size:17px">Comparaison</p>'+t+
    '<p class="gnote">La meilleure valeur de chaque ligne est surlign\\u00e9e. '+
    'Estimations indicatives \\u2014 les cas prioritaires passent toujours '+
    'en premier. En cas d\\u2019urgence vitale, composez le 911.</p></div>';
  v.addEventListener('click',e=>{
    const fi=e.target.getAttribute&&e.target.getAttribute('data-fiche');
    if(fi!=null){v.remove();openFiche(DATA[+fi]);return;}
    if(e.target===v||e.target.id==='fermer')v.remove();});
  document.body.appendChild(v);
}
/* Carte g\\u00e9ographique : SVG dessin\\u00e9 dans la page \\u00e0 partir
   des lat/lon \\u2014 aucune tuile externe, rien ne sort du navigateur.
   Le cadre par d\\u00e9faut couvre le sud habit\\u00e9 du Qu\\u00e9bec ;
   un filtre de r\\u00e9gion ou de recherche recadre automatiquement. */
let VUE='liste',ZOOMALL=false;
function renderCarte(rows){
  const el=document.getElementById('carte');
  const pts=rows.filter(d=>d.lat!=null);
  if(!pts.length){
    el.innerHTML='<p class="ligne">Aucun h\\u00f4pital \\u00e0 afficher.</p>';
    return;
  }
  const filtre=document.getElementById('reg').value||
               document.getElementById('q').value.trim();
  let latMin,latMax,lonMin,lonMax,vis=pts,hors=0;
  if(!filtre&&!ZOOMALL){
    latMin=44.95;latMax=50.55;lonMin=-79.9;lonMax=-61.2;
    vis=pts.filter(d=>d.lat>=latMin&&d.lat<=latMax&&
                      d.lon>=lonMin&&d.lon<=lonMax);
    hors=pts.length-vis.length;
  }else{
    latMin=Math.min(...pts.map(d=>d.lat));latMax=Math.max(...pts.map(d=>d.lat));
    lonMin=Math.min(...pts.map(d=>d.lon));lonMax=Math.max(...pts.map(d=>d.lon));
    const dla=(latMax-latMin)||0.5,dlo=(lonMax-lonMin)||0.5;
    latMin-=dla*.08;latMax+=dla*.08;lonMin-=dlo*.06;lonMax+=dlo*.06;
  }
  const k=Math.cos((latMin+latMax)/2*Math.PI/180);
  const W=700;
  const H=Math.max(240,Math.min(760,
    Math.round(W*(latMax-latMin)/((lonMax-lonMin)*k))));
  const X=lon=>((lon-lonMin)/(lonMax-lonMin)*W).toFixed(1);
  const Y=lat=>((latMax-lat)/(latMax-latMin)*H).toFixed(1);
  let s='<svg viewBox="0 0 '+W+' '+H+'" xmlns="http://www.w3.org/2000/svg" '+
    'role="img" aria-label="Carte des urgences du Qu\\u00e9bec">';
  vis.forEach(d=>{
    const i=indice(d);
    s+='<circle data-i="'+d._i+'" cx="'+X(d.lon)+'" cy="'+Y(d.lat)+
      '" r="7" style="fill:'+(i!=null?coulJauge(i):'var(--mut)')+
      ';stroke:var(--card);cursor:pointer" stroke-width="1.6" '+
      'opacity="0.93"><title>'+d.installation+
      (d.attente_pec!=null?' \\u2014 '+fmt(d.attente_pec)+' en attente':'')+
      (d.taux!=null?' \\u00b7 civi\\u00e8res '+d.taux+' %':'')+
      '</title></circle>';
  });
  if(POS&&POS[0]>latMin&&POS[0]<latMax&&POS[1]>lonMin&&POS[1]<lonMax){
    s+='<circle cx="'+X(POS[1])+'" cy="'+Y(POS[0])+
      '" r="9" fill="none" style="stroke:var(--info)" stroke-width="3">'+
      '<title>Votre position</title></circle>'+
      '<circle cx="'+X(POS[1])+'" cy="'+Y(POS[0])+
      '" r="3" style="fill:var(--info)"/>';
  }
  s+='</svg>';
  el.innerHTML=s+'<p class="cartenote">'+vis.length+' urgences affich\\u00e9es'+
    (hors?' \\u00b7 '+hors+' plus au nord, hors du cadre':'')+
    ' \\u00b7 touchez un point pour ouvrir sa fiche'+
    (!filtre?' \\u00b7 <button type="button" id="zoomall">'+
      (ZOOMALL?'Recentrer sur le sud':'Voir tout le Qu\\u00e9bec')+
      '</button>':'')+'</p>';
}
function render(){
  const q=nrm(document.getElementById('q').value.trim());
  const reg=document.getElementById('reg').value;
  const sort=document.getElementById('sort').value;
  let rows=DATA.filter(d=>
    (!reg||d.region===reg)&&
    (!q||nrm(d.installation+' '+d.etablissement+' '+d.region).includes(q)));
  rows.sort((a,b)=>{
    if(sort==='nom')return a.installation.localeCompare(b.installation,'fr');
    if(sort==='total')return (a._total??Infinity)-(b._total??Infinity);
    if(sort==='distance')return (a._km??Infinity)-(b._km??Infinity);
    return (b[sort]??-1)-(a[sort]??-1);
  });
  const byReg={};
  if(sort==='total'&&POS){
    /* Classement par proximit\\u00e9 : une seule liste, sans regroupement. */
    byReg['Du plus court au plus long (temps total estim\\u00e9)']=rows;
  }else if(sort==='distance'&&POS){
    byReg['Du plus proche au plus \\u00e9loign\\u00e9']=rows;
  }else{
    /* Vue par r\\u00e9gion : les favoris \\u00e9pingl\\u00e9s en premier. */
    const favs=rows.filter(d=>FAV.has(d.installation));
    if(favs.length)byReg['\\u2605 Mes favoris']=favs;
    rows.filter(d=>!FAV.has(d.installation))
      .forEach(d=>{(byReg[d.region]=byReg[d.region]||[]).push(d);});
  }
  let html='';
  Object.keys(byReg).sort((a,b)=>{
    const fa=a.startsWith('\\u2605'),fb=b.startsWith('\\u2605');
    if(fa!==fb)return fa?-1:1;
    return a.localeCompare(b,'fr');
  }).forEach(r=>{
    html+='<p class="rg">'+r+'</p>';
    byReg[r].forEach(d=>{
      const bo=estBornes(d);
      const ind=indice(d);
      const coul=ind!=null?coulJauge(ind):'var(--mut)';
      const et=etatDe(ind);
      const voc=vocation(d);
      const type='Urgence'+(voc?' '+voc:'')+((POS&&d._km!=null)
        ?' \u00b7 '+Math.round(d._km)+' km':' \u00b7 '+d.region);
      const droite=bo
        ?'<div style="flex-shrink:0"><div class="attq" style="color:'+coul+'">'+
          fmtMin(bo.lo)+'\u2013'+fmtMin(bo.hi)+'</div>'+
          '<div class="attqs">sur place (est.)'+
          (d.attente_pec!=null?' \u00b7 '+fmt(d.attente_pec)+' en file':'')+'</div></div>'
        :'<div style="flex-shrink:0">'+badge(d.taux)+
          (d.attente_pec!=null?'<div class="attqs">'+fmt(d.attente_pec)+' en file</div>':'')+'</div>';
      const infos=[et||null,(d.taux!=null?d.taux+' % d\u2019occupation':null)];
      if(POS&&d._total!=null&&sort==='total')
        infos.push('temps total \u2248 '+fmtMin(d._total));
      html+='<div class="card" data-i="'+d._i+'">'+
        '<div class="row"><div style="text-align:left"><div class="ktype">'+type+'</div>'+
        '<div class="name">'+
        (FAV.has(d.installation)?'<span class="fav">\u2605</span> ':'')+
        d.installation+'</div></div>'+droite+'</div>'+
        (ind!=null?'<div class="jauge"><i style="width:'+
          Math.min(100,Math.round(ind/1.5))+'%;background:'+coul+'"></i></div>':'')+
        '<div class="basrow"><span>'+infos.filter(Boolean).join(' \u00b7 ')+'</span>'+
        '<span><span class="det">D\u00e9tails \u2192</span> \u00b7 '+
        '<span class="cmp'+(COMP.has(d.installation)?' on':'')+'" data-comp="'+
        d.installation.replace(/"/g,'&quot;')+'">'+
        (COMP.has(d.installation)?'\u2713 \u00c0 comparer':'+ Comparer')+
        '</span></span></div></div>';
    });
  });
  document.getElementById('list').innerHTML=html||'<p class="gnote">Aucun r\\u00e9sultat.</p>';
  const libelles={distance:'distance',total:'temps total (trajet + attente)',
    taux:'occupation des civi\\u00e8res',attente_pec:'personnes en attente',
    dms_ambulatoire:'s\\u00e9jour moyen',nom:'nom'};
  const nr=document.getElementById('nres');
  if(nr)nr.innerHTML=rows.length+' lieu'+(rows.length>1?'x':'')+
    ' \\u00b7 tri\\u00e9s par <b>'+(libelles[sort]||sort)+'</b>';
  const enCarte=(VUE==='carte');
  document.getElementById('list').style.display=enCarte?'none':'';
  document.getElementById('carte').style.display=enCarte?'':'none';
  if(enCarte)renderCarte(rows);
}
Promise.resolve(__PAYLOAD__).then(j=>{
  DATA=j.data;META=j;
  DATA.forEach((d,i)=>{d._i=i;});
  chips();
  setInterval(chips,60000);
  document.getElementById('titre').addEventListener('click',()=>goScreen(1));
  document.getElementById('ong1').addEventListener('click',()=>goScreen(1));
  document.getElementById('ong2').addEventListener('click',()=>goScreen(2));
  document.getElementById('ong3').addEventListener('click',()=>{
    if(!FICHE_SEL)FICHE_SEL=plusProcheGeneral()||meilleurGeneral()||DATA[0];
    if(FICHE_SEL)openFiche(FICHE_SEL);
  });
  document.getElementById('t-soins').addEventListener('click',()=>{
    goScreen(2);
    document.getElementById('sort').value='distance';
    if(!POS)locate();else render();
  });
  document.getElementById('t-guide').addEventListener('click',openGuide);
  document.getElementById('sort').value='taux';
  document.getElementById('reseau').addEventListener('click',e=>{
    const r=e.target.getAttribute&&e.target.getAttribute('data-region');
    if(!r)return;
    goScreen(2);
    document.getElementById('reg').value=r;
    render();
  });
  document.getElementById('recomm').addEventListener('click',e=>{
    const g=a=>e.target.getAttribute&&e.target.getAttribute(a);
    if(g('data-loc')){locate();return;}
    const fi=g('data-fiche');
    if(fi!=null){openFiche(DATA[+fi]);return;}
    if(g('data-go2')){
      goScreen(2);
      document.getElementById('sort').value='total';
      render();
    }
  });
  document.getElementById('ecran3').addEventListener('click',e=>{
    const g=a=>e.target.getAttribute&&e.target.getAttribute(a);
    if(g('data-back')){goScreen(2);return;}
    const nom=g('data-fav');
    if(nom!=null){
      toggleFav(nom);
      e.target.textContent=FAV.has(nom)
        ?'\u2605 Retirer des favoris':'\u2606 Ajouter aux favoris';
      render();
    }
  });
  document.getElementById('lien-apropos').addEventListener('click',e=>{
    e.preventDefault();
    const v=document.createElement('div');v.className='voile';v.id='voile';
    v.innerHTML='<div class="fiche methodo"><button type="button" class="fx" '+
      'id="fermer">\u2715 Fermer</button>'+
      document.getElementById('apropos-src').innerHTML+'</div>';
    v.addEventListener('click',ev=>{
      if(ev.target===v||ev.target.id==='fermer')v.remove();});
    document.body.appendChild(v);
  });
  document.getElementById('lien-methodo').addEventListener('click',e=>{
    e.preventDefault();
    const v=document.createElement('div');v.className='voile';v.id='voile';
    v.innerHTML='<div class="fiche methodo"><button type="button" class="fx" '+
      'id="fermer">\\u2715 Fermer</button>'+
      document.getElementById('methodo-src').innerHTML+'</div>';
    v.addEventListener('click',ev=>{
      if(ev.target===v||ev.target.id==='fermer')v.remove();});
    document.body.appendChild(v);
  });
  if(j.error)document.getElementById('notice').innerHTML=
    '<div class="notice">'+j.error+'</div>';
  const regs=[...new Set(j.data.map(d=>d.region))].sort((a,b)=>a.localeCompare(b,'fr'));
  const sel=document.getElementById('reg');
  regs.forEach(r=>{const o=document.createElement('option');o.value=o.textContent=r;sel.appendChild(o);});
  ['q','reg','sort'].forEach(id=>document.getElementById(id)
    .addEventListener('input',render));
  const locBtn=document.getElementById('loc');
  if(locBtn)locBtn.addEventListener('click',locate);
  document.getElementById('sort').addEventListener('change',e=>{
    if((e.target.value==='total'||e.target.value==='distance')&&!POS)locate();});
  document.getElementById('gtoggle').addEventListener('click',openGuide);
  document.getElementById('list').addEventListener('click',e=>{
    const nom=e.target.getAttribute&&e.target.getAttribute('data-comp');
    if(nom!=null){toggleComp(nom);return;}
    const c=e.target.closest&&e.target.closest('.card');
    if(!c)return;
    const d=DATA[+c.getAttribute('data-i')];
    if(d)openFiche(d);
  });
  document.getElementById('vue').addEventListener('click',()=>{
    VUE=(VUE==='liste')?'carte':'liste';
    document.getElementById('vue').textContent=
      VUE==='liste'?'\\uD83D\\uDDFA Carte':'\\u2630 Liste';
    render();
  });
  document.getElementById('carte').addEventListener('click',e=>{
    if(e.target.id==='zoomall'){ZOOMALL=!ZOOMALL;render();return;}
    const c=e.target.closest&&e.target.closest('[data-i]');
    if(!c)return;
    const d=DATA[+c.getAttribute('data-i')];
    if(d)openFiche(d);
  });
  document.addEventListener('keydown',e=>{
    if(e.key==='Escape'){const v=document.getElementById('voile');if(v)v.remove();}
  });
  reprendreVille();
  majAccueil();
  /* Permission GPS d\\u00e9j\\u00e0 accord\\u00e9e lors d'une visite
     pass\\u00e9e ? On l'utilise sans rien demander : la position exacte
     remplace la ville m\\u00e9moris\\u00e9e, silencieusement. */
  if(navigator.permissions&&navigator.geolocation){
    navigator.permissions.query({name:'geolocation'}).then(st=>{
      if(st.state==='granted'){
        navigator.geolocation.getCurrentPosition(
          p=>setPosition(p.coords.latitude,p.coords.longitude),
          ()=>{},{timeout:8000,maximumAge:600000});
      }
    }).catch(()=>{});
  }
  render();
});
</script>
</body>
</html>
"""


def make_payload():
    c = get_data()
    # Tendances : seulement avec de vraies données (les mélanger aux chiffres
    # fictifs de la démo n'aurait aucun sens).
    jours, profils = load_trends() if c["source"] == "msss" else (0, {})
    hrel, histo = load_recent() if c["source"] == "msss" else ([], {})
    for d in c["data"]:
        d["trend"] = profils.get(d["installation"])
        d["histo"] = histo.get(d["installation"])
    # Les CLSC (information publique, indépendante des urgences) servent au
    # guide « Où aller ? » pour proposer les points de service proches.
    clsc = load_coords()[2]
    payload = json.dumps({"data": c["data"], "source": c["source"],
                          "fetched": c["t"], "error": c["error"],
                          "releve": c["releve"], "tjours": jours,
                          "hrel": hrel, "clsc": clsc},
                         ensure_ascii=False)
    return c, payload


LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "logo_rosace.png")
_logo_uri = None


def logo_uri():
    """Le logo (rosace 128 px) en data-URI, pour des pages autonomes."""
    global _logo_uri
    if _logo_uri is None:
        try:
            import base64
            with open(LOGO_PATH, "rb") as f:
                _logo_uri = ("data:image/png;base64,"
                             + base64.b64encode(f.read()).decode("ascii"))
        except Exception:
            _logo_uri = ""  # pas de logo : l'image sera simplement vide
    return _logo_uri


def soutien_html():
    """Le lien « Soutenir » du pied de page — rien du tout si pas d'adresse."""
    if not SOUTIEN_URL:
        return ""
    return ('<br><a href="' + SOUTIEN_URL + '" target="_blank" rel="noopener" '
            'style="color:var(--brand);font-weight:600">&#9829; Soutenir la '
            'Boussole</a> — l\'application est gratuite, sans publicité ni '
            'traçage ; votre soutien finance son hébergement et ses '
            'améliorations.')


def build_page():
    """Page autonome (double-clic) : données incluses, pas de service worker."""
    _, payload = make_payload()
    return (PAGE.replace("__PAYLOAD__", payload).replace("__PWA__", "")
            .replace("__LOGO__", logo_uri())
            .replace("__SOUTIEN__", soutien_html()))


# ---------------------------------------------------------------------------
# PWA : site hébergeable (index.html + donnees.json + manifeste + service
# worker + icônes), généré par --site, servi en local par --servir.
# ---------------------------------------------------------------------------
PWA_HEAD = """<link rel="manifest" href="manifest.webmanifest">
<meta name="theme-color" content="#f6f5f1" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#181913" media="(prefers-color-scheme: dark)">
<link rel="icon" href="icone-192.png">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<script>
if('serviceWorker' in navigator)
  addEventListener('load',()=>navigator.serviceWorker.register('sw.js'));
</script>"""

# En mode site, la page va chercher donnees.json (regénéré chaque heure par
# l'hébergeur) ; hors ligne, le service worker sert la dernière version vue.
PAYLOAD_FETCH = ("fetch('donnees.json').then(r=>r.json()).catch(()=>({"
                 "data:[],source:'demo',fetched:0,tjours:0,releve:null,clsc:[],hrel:[],"
                 "error:'Impossible de charger les donn\\u00e9es \\u2014 "
                 "v\\u00e9rifiez votre connexion, puis actualisez.'}))")

MANIFEST = """{
  "name": "Boussole sant\\u00e9",
  "short_name": "Boussole",
  "description": "\\u00c9tat des urgences du Qu\\u00e9bec, guide des ressources et temps d'attente.",
  "lang": "fr-CA",
  "start_url": "./",
  "scope": "./",
  "display": "standalone",
  "background_color": "#f6f5f1",
  "theme_color": "#f6f5f1",
  "icons": [
    {"src": "icone-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
    {"src": "icone-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
  ]
}
"""

SW_JS = """/* Boussole sant\\u00e9 \\u2014 service worker.
   Strat\\u00e9gie : r\\u00e9seau d'abord (donn\\u00e9es fra\\u00eeches),
   cache en secours (hors ligne : derni\\u00e8re version vue). */
const CACHE='boussole-v18';
const SHELL=['./','manifest.webmanifest','icone-192.png','icone-512.png'];
self.addEventListener('install',e=>{
  e.waitUntil(caches.open(CACHE).then(c=>c.addAll(SHELL))
    .then(()=>self.skipWaiting()));
});
self.addEventListener('activate',e=>{
  e.waitUntil(caches.keys().then(ks=>Promise.all(
    ks.filter(k=>k!==CACHE).map(k=>caches.delete(k))))
    .then(()=>self.clients.claim()));
});
self.addEventListener('fetch',e=>{
  if(new URL(e.request.url).origin!==location.origin)return;
  e.respondWith(
    fetch(e.request).then(r=>{
      const copie=r.clone();
      caches.open(CACHE).then(c=>c.put(e.request,copie));
      return r;
    }).catch(()=>caches.match(e.request,{ignoreSearch:true})
      .then(r=>r||caches.match('./')))
  );
});
"""


def _png(size, pixel):
    """Encodeur PNG minimal (RGBA), en pur Python — pour les icônes."""
    rows = bytearray()
    for y in range(size):
        rows.append(0)  # filtre « aucun » pour la ligne
        for x in range(size):
            rows.extend(pixel(x, y))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows)))
            + chunk(b"IEND", b""))


def make_icon(size):
    """Icône « boussole » : disque vert, aiguille nord rouge / sud blanche."""
    creme, vert = (246, 245, 241, 255), (15, 110, 86, 255)
    rouge, blanc = (163, 45, 45, 255), (255, 255, 255, 255)
    c = size / 2.0

    def pixel(x, y):
        dx, dy = x - c, y - c
        if dx * dx + dy * dy > (0.42 * size) ** 2:
            return creme                     # fond plein : compatible maskable
        if abs(dx) + abs(dy) < 0.30 * size:  # aiguille en losange
            if dx * dx + dy * dy < (0.045 * size) ** 2:
                return vert                  # pivot central
            return rouge if dy < 0 else blanc
        return vert

    return _png(size, pixel)


def build_site():
    """Génère le dossier site/ complet ; refuse de publier la démo."""
    c, payload = make_payload()
    if c["source"] != "msss":
        print("Site non généré :", c["error"] or "données réelles indisponibles")
        sys.exit(1)
    site = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site")
    os.makedirs(site, exist_ok=True)
    shell = (PAGE.replace("__PAYLOAD__", PAYLOAD_FETCH)
             .replace("__PWA__", PWA_HEAD).replace("__LOGO__", logo_uri())
             .replace("__SOUTIEN__", soutien_html()))
    ecrits = []
    for nom, contenu in [("index.html", shell), ("donnees.json", payload),
                         ("manifest.webmanifest", MANIFEST), ("sw.js", SW_JS)]:
        with open(os.path.join(site, nom), "w", encoding="utf-8") as f:
            f.write(contenu)
        ecrits.append(nom)
    for nom, taille in [("icone-192.png", 192), ("icone-512.png", 512),
                        ("apple-touch-icon.png", 180)]:
        chemin = os.path.join(site, nom)
        if not os.path.exists(chemin):  # les icônes ne changent jamais
            with open(chemin, "wb") as f:
                f.write(make_icon(taille))
            ecrits.append(nom)
    return site, ecrits, c


if __name__ == "__main__":
    if SITE_MODE or SERVE_MODE:
        print(time.strftime("[%Y-%m-%d %H:%M:%S]"), "génération du site PWA…")
        site, ecrits, c = build_site()
        print("Site généré dans :", site)
        print("Fichiers :", ", ".join(ecrits))
        print("Installations :", len(c["data"]), "· relevé :", c["releve"])
        if _cache["hist"]:
            print(_cache["hist"])
        if SERVE_MODE:
            Handler = functools.partial(
                http.server.SimpleHTTPRequestHandler, directory=site)
            adresse = "0.0.0.0" if LAN_MODE else "127.0.0.1"
            with http.server.ThreadingHTTPServer((adresse, 8765), Handler) as srv:
                print()
                print("PWA locale : http://localhost:8765")
                if LAN_MODE:
                    # L'adresse du Mac sur le réseau local, pour le téléphone.
                    import socket
                    try:
                        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        s.connect(("8.8.8.8", 80))  # aucun paquet envoyé
                        ip = s.getsockname()[0]
                        s.close()
                        print("Sur votre téléphone (même Wi-Fi) : "
                              "http://{}:8765".format(ip))
                    except Exception:
                        print("Téléphone : http://<adresse-IP-du-Mac>:8765 "
                              "(voir Réglages > Wi-Fi sur le Mac)")
                    print("Note : par le Wi-Fi, la géolocalisation automatique "
                          "est bloquée par le navigateur — choisissez votre "
                          "ville dans la liste.")
                print("(Ctrl+C pour arrêter le serveur)")
                webbrowser.open("http://localhost:8765")
                try:
                    srv.serve_forever()
                except KeyboardInterrupt:
                    print("\nServeur arrêté.")
        sys.exit(0)
    if COLLECT_MODE:
        # Une ligne d'horodatage par passage : le journal de collecte
        # (boussole_collecte.log) reste lisible sur la durée.
        print(time.strftime("[%Y-%m-%d %H:%M:%S]"), "collecte automatique")
    else:
        print("Boussole santé — prototype phase 1")
        print("Mode :", "démonstration" if DEMO_MODE else "données MSSS en direct")
        print("Téléchargement et préparation de la page…")
    html = build_page()

    if COLLECT_MODE and _cache["source"] != "msss":
        # Pas de vraies données : on n'écrase pas la page existante avec la
        # démo et on signale l'échec (visible dans le journal de collecte).
        print("  échec —", _cache["error"] or "source indisponible")
        sys.exit(1)

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "boussole_sante.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    if not COLLECT_MODE:
        print("Page créée :", out)
    print("Installations trouvées :", len(_cache["data"]))
    if _ssl_origin and not COLLECT_MODE:
        print("Certificats HTTPS :", _ssl_origin)
    if _cache["hist"]:
        print(_cache["hist"])
    if _cache["error"]:
        print("Note :", _cache["error"])
    if not COLLECT_MODE:
        webbrowser.open("file://" + out)
        print("La page s'ouvre dans votre navigateur.")
        print("Pour actualiser les données, relancez simplement : python3 boussole.py")
