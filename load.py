import os
import urllib.request
import zipfile
import io
import logging
import time
import math
import json
import platform
import threading
import tkinter as tk
from datetime import datetime, timezone, timedelta
import requests
import urllib.parse
from config import config

try:
    import config
except ImportError:
    config = None

plugin_name = "SYS.EDTEAM"
PLUGIN_VERSION = "1.2"

SUPABASE_URL = "https://oailvdigfdoyfcydmabb.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im9haWx2ZGlnZmRveWZjeWRtYWJiIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODQ2MjQzNTAsImV4cCI6MjEwMDIwMDM1MH0.rWEATcSWDyyyeKXWAkCySCZPwTsIFgDRJ7KB1u4OE00"

status_label = None
systeme_actuel = "SYSTÈME INCONNU"
cmdr_actuel = None # <-- NOUVELLE VARIABLE
scan_en_cours = False
dernier_solde_fc = None
inventaire_fc_local = None
inventaire_lock = threading.Lock()

def trouver_journal_dir():
    if config and hasattr(config, 'get'):
        jdir = config.get('journaldir')
        if jdir: return jdir
    if platform.system() == 'Windows':
        return os.path.join(os.environ.get('USERPROFILE', ''), 'Saved Games', 'Frontier Developments', 'Elite Dangerous')
    return None

def lire_cle():
    try:
        if os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "edteam_key.txt")):
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "edteam_key.txt"), 'r') as f:
                return f.read().strip()
    except: pass
    return ""

def sauvegarder_cle(cle):
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "edteam_key.txt"), 'w') as f:
            f.write(cle.strip())
    except: pass

def get_headers():
    cle = lire_cle()
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "x-commandant-key": cle if cle else "NO_KEY",
        "User-Agent": f"SYS.EDTEAM/{cle}" if cle else "SYS.EDTEAM/NO_KEY"
    }

# ==========================================
# LE PONT DE COMMUNICATION
# ==========================================
def patch_parametres(payload):
    try:
        res = requests.get(f"{SUPABASE_URL}/rest/v1/radar_commercial?target_commodity=eq.PARAM_UPDATE", headers=get_headers())
        if res.status_code == 200 and len(res.json()) > 0:
            row = res.json()[0]
            try: existing = json.loads(row.get('station_name', '{}'))
            except: existing = {}
            existing.update(payload)
            requests.patch(f"{SUPABASE_URL}/rest/v1/radar_commercial?id=eq.{row['id']}", headers=get_headers(), json={"station_name": json.dumps(existing)})
        else:
            data = {"system_name": "SYS_CORE", "station_name": json.dumps(payload), "target_commodity": "PARAM_UPDATE", "type_operation": "STATUS", "prix_unitaire": 0, "volume_disponible": 0, "distance": 0, "prix_moyen": 0}
            requests.post(f"{SUPABASE_URL}/rest/v1/radar_commercial", headers=get_headers(), json=data)
    except: pass

def maj_generique_global(target, system, station, type_op, val=0, vol=0):
    uid = get_user_id()
    if not uid: return
    
    payload = {
        "user_id": uid,
        "system_name": str(system), 
        "station_name": str(station), 
        "target_commodity": target, 
        "type_operation": type_op, 
        "prix_unitaire": int(val), 
        "volume_disponible": int(vol), 
        "distance": 0, 
        "prix_moyen": 0
    }
    try:
        # On ajoute user_id dans la recherche pour ne pas écraser les autres pilotes
        res = requests.get(f"{SUPABASE_URL}/rest/v1/radar_commercial?target_commodity=eq.{target}&user_id=eq.{uid}", headers=get_headers())
        if res.status_code == 200 and len(res.json()) > 0:
            requests.patch(f"{SUPABASE_URL}/rest/v1/radar_commercial?id=eq.{res.json()[0]['id']}", headers=get_headers(), json=payload)
        else:
            requests.post(f"{SUPABASE_URL}/rest/v1/radar_commercial", headers=get_headers(), json=payload)
    except: pass

def obtenir_parametres():
    cle = lire_cle()
    try:
        res = requests.get(f"{SUPABASE_URL}/rest/v1/radar_commercial?target_commodity=eq.APP_PARAMS", headers=get_headers())
        if res.status_code == 200:
            for row in res.json():
                if str(row.get('system_name')).strip() == str(cle).strip():
                    return json.loads(row.get('station_name', '{}'))
    except: pass
    return {"cibles_achat": ["Gold"], "moyennes_galactiques": {}, "mode_flotte": "FC"}

def obtenir_moyennes_galactiques():
    try:
        res = requests.get(f"{SUPABASE_URL}/rest/v1/moyennes_galactiques", headers=get_headers())
        if res.status_code == 200:
            return {m.get('marchandise'): m.get('prix_moyen', 0) for m in res.json()}
    except: pass
    return {}

def get_user_id():
    cle = lire_cle()
    if not cle:
        mettre_a_jour_interface(">_ BLOQUÉ : AUCUNE CLÉ DANS EDMC", "red")
        return None
    try:
        # On interroge directement TON profil sécurisé par ta clé secrète
        res = requests.get(f"{SUPABASE_URL}/rest/v1/profils?select=user_id", headers=get_headers())
        if res.status_code == 200:
            data = res.json()
            if len(data) > 0:
                return data[0].get('user_id')
            else:
                mettre_a_jour_interface(">_ BLOQUÉ : CLÉ NON RECONNUE", "red")
                return None
        else:
            mettre_a_jour_interface(f">_ ERREUR BDD : {res.status_code}", "red")
            return None
    except:
        mettre_a_jour_interface(">_ BLOQUÉ : ERREUR RÉSEAU", "red")
    return None

def recuperer_dernier_systeme_connu():
    global systeme_actuel
    if systeme_actuel != "SYSTÈME INCONNU" and systeme_actuel != "Sol": return systeme_actuel
    try:
        res = requests.get(f"{SUPABASE_URL}/rest/v1/radar_commercial?target_commodity=eq.SYSTEM_STATUS", headers=get_headers())
        if res.status_code == 200 and len(res.json()) > 0:
            sys_db = res.json()[0].get('system_name', '')
            if sys_db and sys_db not in ["SYSTÈME INCONNU", "SYS_CORE", "SHIP", "FINANCE", "Sol"]:
                systeme_actuel = sys_db
                return systeme_actuel
    except: pass
    return "Sol"

def mettre_a_jour_interface(texte, couleur):
    global status_label
    if status_label:
        try: status_label.after(0, lambda t=texte, c=couleur: status_label.config(text=t, fg=c))
        except: pass
    sys_a_sauver = recuperer_dernier_systeme_connu()
    threading.Thread(target=maj_generique_global, args=("SYSTEM_STATUS", sys_a_sauver, texte, "INFO")).start()

def heartbeat_loop():
    while True:
        try:
            timestamp = str(int(time.time()))
            maj_generique_global("HEARTBEAT", "SYS_CORE", timestamp, "STATUS")
            
            jdir = trouver_journal_dir()
            if jdir and os.path.exists(os.path.join(jdir, 'Status.json')):
                with open(os.path.join(jdir, 'Status.json'), 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if data.get('Balance') is not None: 
                        maj_generique_global("SHIP_BALANCE", "FINANCE", "BANK", "FINANCE", val=data.get('Balance'))
        except: 
            pass
        time.sleep(10)

def ecoute_commandes_distantes():
    global scan_en_cours
    while True:
        try:
            res = requests.get(f"{SUPABASE_URL}/rest/v1/commandes_terminal?statut=eq.EN_ATTENTE", headers=get_headers())
            if res.status_code == 200 and len(res.json()) > 0:
                mon_id = get_user_id()
                if not mon_id:
                    time.sleep(4)
                    continue
                    
                commande_trouvee = False
                for cmd in res.json():
                    if str(cmd.get('user_id')) == str(mon_id):
                        commande_trouvee = True
                        requests.patch(f"{SUPABASE_URL}/rest/v1/commandes_terminal?id=eq.{cmd['id']}", headers=get_headers(), json={"statut": "TRAITEE"})
                        if not scan_en_cours:
                            action = cmd.get('type_commande')
                            if action == 'SCAN_ACHAT': threading.Thread(target=processus_scan_achats).start()
                            elif action == 'SCAN_VENTE': threading.Thread(target=processus_scan_ventes).start()
                            elif action == 'SCAN_PREDICTIF': threading.Thread(target=processus_scan_predictif).start()
                            
                if not commande_trouvee:
                    pass
        except: pass
        time.sleep(4)

def surveiller_marche_en_fond():
    global systeme_actuel
    jdir = trouver_journal_dir()
    if not jdir: return
    dernier_mtime = 0
    market_path = os.path.join(jdir, 'Market.json')
    status_path = os.path.join(jdir, 'Status.json')
    
    while True:
        try:
            if os.path.exists(status_path):
                with open(status_path, 'r', encoding='utf-8') as f:
                    s_data = json.load(f)
                    if 'SystemName' in s_data: systeme_actuel = s_data['SystemName']

            if os.path.exists(market_path):
                mtime = os.path.getmtime(market_path)
                if mtime != dernier_mtime:
                    dernier_mtime = mtime
                    analyser_marche_detecte(market_path, systeme_actuel)
        except: pass
        time.sleep(1)

def analyser_marche_detecte(market_file, sys_actuel):
    try:
        mettre_a_jour_interface(">_ ANALYSE LOCALE EN COURS...", "#FFD700")
        time.sleep(0.3)
        with open(market_file, 'r', encoding='utf-8') as f: market_data = json.load(f)
        items = market_data.get('Items') or []
        sta_exacte = market_data.get('StationName', "STATION INCONNUE")
        sys_exact = market_data.get('StarSystem', sys_actuel)
        
        user_id = get_user_id()
        parametres = obtenir_parametres()
        moyennes_modifiees = False
        payload_local = []

        moyennes_a_sauver = []

        # On scanne TOUT le marché sans limite
        for item in items:
            nom_brut = item.get('Name', '') or ''
            if not nom_brut: continue
            
            nom_marchandise = formater_nom_marchandise(nom_brut)
            if not nom_marchandise: continue # <-- LE BOUCLIER : Rejette tout ce qui n'est pas dans le catalogue
            
            stock_reel = item.get('Stock', 0)
            prix_moyen_jeu = item.get('MeanPrice', 0)

            # L'aspirateur global pour la table commune
            if prix_moyen_jeu > 0:
                moyennes_a_sauver.append({"marchandise": nom_marchandise, "prix_moyen": prix_moyen_jeu})

            # On prépare l'affichage local classique
            if stock_reel > 0:
                payload_local.append({
                    "user_id": user_id,
                    "system_name": sys_exact, 
                    "station_name": sta_exacte, 
                    "target_commodity": nom_marchandise, 
                    "type_operation": "PREDICTIF_LOCAL", 
                    "prix_unitaire": item.get('BuyPrice', 0), 
                    "volume_disponible": stock_reel, 
                    "date_maj": datetime.now(timezone.utc).isoformat(),
                    "distance": 0, 
                    "prix_moyen": prix_moyen_jeu
                })

        # TRANSMISSION DE L'ENCYCLOPÉDIE (UPSERT)
        if moyennes_a_sauver:
            try:
                h = get_headers()
                h["Prefer"] = "resolution=merge-duplicates"
                res_db = requests.post(f"{SUPABASE_URL}/rest/v1/moyennes_galactiques?on_conflict=marchandise", headers=h, json=moyennes_a_sauver)
                
                # Si Supabase refuse, on affiche le code d'erreur sur l'interface EDMC
                if res_db.status_code not in [200, 201, 204]:
                    mettre_a_jour_interface(f">_ REJET BDD : ERREUR {res_db.status_code}", "red")
            except: pass

        # VÉRIFICATION : Est-ce que cette station est une cible de nos radars ?
        station_est_cible = False
        if user_id and payload_local:
            try:
                res_check = requests.get(
                    f"{SUPABASE_URL}/rest/v1/radar_commercial",
                    headers=get_headers(),
                    params={
                        "user_id": f"eq.{user_id}",
                        "system_name": f"eq.{sys_exact}",
                        "type_operation": "in.(ACHAT,VENTE,PREDICTIF)"
                    }
                )
                if res_check.status_code == 200:
                    for cible in res_check.json():
                        # On vérifie si le nom brut du jeu est contenu dans le nom formaté avec le [PAD]
                        if sta_exacte.lower() in cible.get('station_name', '').lower():
                            station_est_cible = True
                            break
            except: pass

        # ENREGISTREMENT CONDITIONNEL
        if payload_local and user_id and station_est_cible:
            sta_safe = sta_exacte.replace(' ', '%20')
            requests.delete(f"{SUPABASE_URL}/rest/v1/radar_commercial?type_operation=eq.PREDICTIF_LOCAL&station_name=eq.{sta_safe}&user_id=eq.{user_id}", headers=get_headers())
            requests.post(f"{SUPABASE_URL}/rest/v1/radar_commercial", headers=get_headers(), json=payload_local)
            mettre_a_jour_interface(f">_ RAPPORT TRANSMIS : {sta_exacte}", "#00FF66")
        else:
            # La station n'est pas ciblée : on a juste aspiré les moyennes silencieusement
            mettre_a_jour_interface(">_ MOYENNES ASPIRÉES (HORS CIBLE)", "gray")
            
    except:
        mettre_a_jour_interface(">_ ERREUR ANALYSE", "red")

def formater_nom_marchandise(nom_brut):
    # Le catalogue strict des marchandises de masse (Filtre anti-déchets)
    marchandises_utiles = [
        "Advanced Catalysers", "Advanced Medicines", "Agronomic Treatment", "Algae", "Aluminium", 
        "Animal Meat", "Animal Monitors", "Aquaponic Systems", "Atmospheric Extractors", "Auto-Fabricators", 
        "Basic Medicines", "Bauxite", "Beer", "Bertrandite", "Beryllium", "Bioreducing Lichen", "Biowaste", 
        "Bismuth", "Bootleg Liquor", "Bromellite", "Building Fabricators", "Ceramic Composites", 
        "Chemical Waste", "Clothing", "Cobalt", "Coffee", "Coltan", "Combat Stabilisers", 
        "Computer Components", "Conductive Fabrics", "Consumer Technology", "Cooling Hoses", "Copper", 
        "Crop Harvesters", "Cryolite", "Diagnostics Sensor", "Domestic Appliances", "Earth Relics", 
        "Emergency Power Cells", "Evacuation Shelter", "Exhaust Manifold", "Explosives", "Fish", 
        "Food Cartridges", "Fruit and Vegetables", "Gallite", "Gallium", "Geological Equipment", 
        "Gold", "Goshenite", "Grain", "Hazardous Environment Suits", "Helium", "Hydrogen Fuel", 
        "Hydrogen Peroxide", "Imperial Slaves", "Indite", "Indium", "Insulating Membrane", 
        "Ion Distributors", "Jadeite", "Land Enrichment Systems", "Leather", "Lepidolite", 
        "Liquid Oxygen", "Liquor", "Lithium", "Low Temperature Diamonds", "Magnetic Emitter Coil", 
        "Marine Equipment", "Medical Diagnostic Equipment", "Micro Controllers", "Micro-Weavers", 
        "Microbial Furnaces", "Mineral Extractors", "Mineral Oil", "Moissanite", "Monazite", 
        "Musgravite", "Narcotics", "Nerve Agents", "Non-Lethal Weapons", "Osmium", "Painite", 
        "Palladium", "Performance Enhancers", "Personal Effects", "Personal Weapons", "Pesticides", 
        "Platinum", "Polymers", "Power Generators", "Power Transfer Conduits", "Progenitor Cells", 
        "Radiation Baffle", "Reactive Armour", "Reinforced Baffling", "Resonating Separators", 
        "Robotics", "Rutile", "Scrap", "Semiconductors", "Silver", "Slaves", "Structural Regulators", 
        "Superconductors", "Survival Equipment", "Synthetic Fabrics", "Synthetic Meat", "Synthetic Reagents", 
        "Taaffeite", "Tantalum", "Tea", "Titanium", "Tobacco", "Tritium", "Uraninite", "Void Opals", 
        "Water", "Water Purifiers", "Wine"
    ]
    
    nom_propre = nom_brut.lower().replace("$", "").replace("_name;", "")
    
    for m in marchandises_utiles:
        if m.lower().replace(" ", "").replace("-", "") == nom_propre:
            return m
            
    # Si c'est une marchandise rare, inconnue ou de l'Odyssey, on retourne None pour la rejeter
    return None

def formater_donnees(sta, marchandise, mode, prix, volume, prix_moyen, user_id=None):
    pad = 'L' if sta.get('has_large_pad') else '?'
    etat = sta.get('controlling_minor_faction_state', 'None').lower()
    etats = {"infrastructure failure": "INFRA. DÉFAILLANTE", "boom": "BOOM ÉCO.", "bust": "CRISE", "outbreak": "ÉPIDÉMIE", "investment": "INVESTISSEMENT", "blight": "FLÉAU"}
    tag = f" [{etats.get(etat, etat.upper())}]" if etat in etats else ""
    
    return {
        "user_id": user_id,
        "system_name": sta.get('system_name'), 
        "station_name": f"{sta.get('name')} [PAD {pad}]{tag}", 
        "type_operation": mode, 
        "target_commodity": marchandise, 
        "prix_unitaire": prix, 
        "volume_disponible": volume, 
        "date_maj": sta.get('market_updated_at', 'Inconnue'), 
        "distance": sta.get('distance', 0), 
        "prix_moyen": prix_moyen
    }

def finaliser_scan(cibles_finales):
    global scan_en_cours
    if not cibles_finales: mettre_a_jour_interface("Scan terminé : 0 station trouvée.", "orange")
    else:
        mettre_a_jour_interface("Récupération EDSM & Transmission BDD...", "orange")
        for data in cibles_finales:
            try: requests.post(f"{SUPABASE_URL}/rest/v1/radar_commercial", headers=get_headers(), json=data)
            except: pass
        mettre_a_jour_interface("Liaison BDD terminée.", "#00FF00")
    time.sleep(3)
    mettre_a_jour_interface(f">_ POSITION ACTUELLE : {recuperer_dernier_systeme_connu().upper()}", "#00F0FF")
    scan_en_cours = False

def processus_scan_achats():
    global scan_en_cours, systeme_actuel
    moyennes_gal = obtenir_moyennes_galactiques()
    scan_en_cours = True
    mettre_a_jour_interface("Amorçage des senseurs...", "orange")

    try:
        user_id = get_user_id()
        if user_id:
            requests.delete(f"{SUPABASE_URL}/rest/v1/radar_commercial?type_operation=eq.ACHAT&user_id=eq.{user_id}", headers=get_headers())
        parametres = obtenir_parametres()
        cibles_vip = [c for c in parametres.get('cibles_achat', []) if c.strip() and "AUCUNE" not in c.upper()]
        if not cibles_vip:
            mettre_a_jour_interface("Scan annulé : Aucune cible.", "red"); time.sleep(3); mettre_a_jour_interface(f">_ POSITION ACTUELLE : {systeme_actuel.upper()}", "#00F0FF"); scan_en_cours = False; return
        cibles_finales = []
        for i, marchandise in enumerate(cibles_vip):
            achats_temporaires = []
            for (min_dist, max_dist) in [(0, 200), (200, 400), (400, 600), (600, 800), (800, 1000), (1000, 1200)]:
                mettre_a_jour_interface(f"Scan ACHAT [{i+1}/{len(cibles_vip)}] {marchandise} : {min_dist}-{max_dist} AL", "orange")
                for page in range(0, 10):
                    try:
                        res = requests.post("https://spansh.co.uk/api/stations/search", json={"filters": {"commodities": {"value": [marchandise]}, "distance": {"min": min_dist, "max": max_dist}, "has_market": {"value": True}, "is_fleet_carrier": {"value": False}, "has_large_pad": {"value": True}}, "reference_system": recuperer_dernier_systeme_connu(), "size": 250, "page": page}, timeout=10)
                        if res.status_code != 200 or not res.json().get('results'): break 
                        for sta in res.json().get('results', []):
                            if "carrier" in sta.get('type', '').lower() or not sta.get('market_updated_at'): continue
                            try:
                                if (datetime.now(timezone.utc) - datetime.fromisoformat(sta['market_updated_at'].replace('Z', '+00:00'))).total_seconds() / 3600.0 > 48: continue
                            except: continue
                            for item in (sta.get('market') or []):
                                if item.get('commodity') == marchandise and item.get('supply', 0) >= 5000:
                                    prix_moyen_spansh = item.get('mean_price', 0)
                                    prix_moyen_final = prix_moyen_spansh if prix_moyen_spansh > 0 else moyennes_gal.get(marchandise, 0)
                                    achats_temporaires.append(formater_donnees(sta, marchandise, "ACHAT", item.get('buy_price', 0), item.get('supply', 0), prix_moyen_final, user_id))
                                    break
                    except: break 
            cibles_finales.extend(sorted(achats_temporaires, key=lambda x: x['prix_unitaire'])[:15])
        finaliser_scan(cibles_finales)
    except:
        mettre_a_jour_interface("Erreur Scan Achat", "red")
        scan_en_cours = False

def processus_scan_ventes():
    global scan_en_cours, systeme_actuel
    scan_en_cours = True
    mettre_a_jour_interface("Amorçage des senseurs...", "orange")
    try:
        user_id = get_user_id()
        if user_id:
            requests.delete(f"{SUPABASE_URL}/rest/v1/radar_commercial?type_operation=eq.VENTE&user_id=eq.{user_id}", headers=get_headers())
        parametres = obtenir_parametres()
        res_fc = requests.get(f"{SUPABASE_URL}/rest/v1/inventaire_fc?quantite=gt.0", headers=get_headers())
        res_ship = requests.get(f"{SUPABASE_URL}/rest/v1/radar_commercial?type_operation=eq.SHIP_CARGO", headers=get_headers())
        
        stocks_consolides = {}
        for item in (res_fc.json() if res_fc.status_code == 200 else []):
            if item.get('marchandise'): stocks_consolides[item['marchandise']] = stocks_consolides.get(item['marchandise'], 0) + item.get('quantite', 0)
        for item in (res_ship.json() if res_ship.status_code == 200 else []):
            if item.get('target_commodity'): stocks_consolides[item['target_commodity']] = stocks_consolides.get(item['target_commodity'], 0) + item.get('volume_disponible', 0)
            
        marchandises_en_soute = [m[0] for m in sorted(stocks_consolides.items(), key=lambda x: x[1], reverse=True)][:4]

        if not marchandises_en_soute:
            mettre_a_jour_interface("Soute vide. Scan annulé.", "red"); time.sleep(3); mettre_a_jour_interface(f">_ POSITION ACTUELLE : {systeme_actuel.upper()}", "#00F0FF"); scan_en_cours = False; return
            
        cibles_finales = []
        for i, marchandise in enumerate(marchandises_en_soute):
            ventes_temporaires = []
            for (min_dist, max_dist) in [(0, 200), (200, 400), (400, 600), (600, 800), (800, 1000), (1000, 1200)]:
                mettre_a_jour_interface(f"Scan VENTE [{i+1}/{len(marchandises_en_soute)}] {marchandise} : {min_dist}-{max_dist} AL", "orange")
                for page in range(0, 10):
                    try:
                        res = requests.post("https://spansh.co.uk/api/stations/search", json={"filters": {"commodities": {"value": [marchandise]}, "distance": {"min": min_dist, "max": max_dist}, "has_market": {"value": True}, "is_fleet_carrier": {"value": False}, "has_large_pad": {"value": True}}, "reference_system": recuperer_dernier_systeme_connu(), "size": 250, "page": page}, timeout=10)
                        if res.status_code != 200 or not res.json().get('results'): break 
                        for sta in res.json().get('results', []):
                            if "carrier" in sta.get('type', '').lower() or not sta.get('market_updated_at'): continue
                            try:
                                if (datetime.now(timezone.utc) - datetime.fromisoformat(sta['market_updated_at'].replace('Z', '+00:00'))).total_seconds() / 3600.0 > 48: continue
                            except: continue
                            for item in (sta.get('market') or []):
                                if item.get('commodity') == marchandise and item.get('demand', 0) >= 5000:
                                    prix_moyen_spansh = item.get('mean_price', 0)
                                    prix_moyen_final = prix_moyen_spansh if prix_moyen_spansh > 0 else moyennes_gal.get(marchandise, 0)
                                    ventes_temporaires.append(formater_donnees(sta, marchandise, "VENTE", item.get('sell_price', 0), item.get('demand', 0), prix_moyen_final, user_id))
                                    break
                    except: break 
            cibles_finales.extend(sorted(ventes_temporaires, key=lambda x: x['prix_unitaire'], reverse=True)[:5])
        finaliser_scan(cibles_finales)
    except:
        mettre_a_jour_interface("Erreur Scan Vente", "red")
        scan_en_cours = False

def processus_scan_predictif():
    global scan_en_cours, systeme_actuel
    scan_en_cours = True
    mettre_a_jour_interface("Amorçage Radar Prédictif (BGS)...", "orange")
    try:
        user_id = get_user_id()
        if user_id:
            # 1. On efface tes anciennes cibles prédictives
            requests.delete(f"{SUPABASE_URL}/rest/v1/radar_commercial?type_operation=eq.PREDICTIF&user_id=eq.{user_id}", headers=get_headers())
            
            # 2. PURGE INTELLIGENTE : On efface tes marchés locaux vieux de plus de 3 jours
            limite_memoire = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
            requests.delete(f"{SUPABASE_URL}/rest/v1/radar_commercial?type_operation=eq.PREDICTIF_LOCAL&user_id=eq.{user_id}&date_maj=lt.{limite_memoire}", headers=get_headers())
        cibles_finales = []
        for (min_dist, max_dist) in [(0, 500), (500, 1000), (1000, 1500), (1500, 2000)]:
            mettre_a_jour_interface(f"Scan PRÉDICTIF : {min_dist}-{max_dist} AL", "orange")
            for page in range(0, 10):
                try:
                    res = requests.post("https://spansh.co.uk/api/stations/search", json={"filters": {"primary_economy": {"value": ["Extraction", "Refinery", "Agriculture", "High Tech"]}, "distance": {"min": min_dist, "max": max_dist}, "has_large_pad": {"value": True}, "is_fleet_carrier": {"value": False}}, "reference_system": recuperer_dernier_systeme_connu(), "size": 250, "page": page}, timeout=10)
                    if res.status_code != 200 or not res.json().get('results'): break 
                    for sta in res.json().get('results', []):
                        if "carrier" in sta.get('type', '').lower(): continue
                        etat_faction = sta.get('controlling_minor_faction_state', '').lower()
                        if etat_faction not in ['infrastructure failure', 'bust', 'blight', 'outbreak', 'boom', 'investment']: continue
                        date_maj = sta.get('updated_at') or sta.get('market_updated_at')
                        if not date_maj: continue
                        try:
                            if (datetime.now(timezone.utc) - datetime.fromisoformat(date_maj.replace('Z', '+00:00'))).total_seconds() / 3600.0 > 48: continue
                        except: continue
                        cibles_finales.append(formater_donnees(sta, "MÉTAUX/MINÉRAUX", "PREDICTIF", 0, 0, 0, user_id))
                except: break 
        finaliser_scan(sorted(cibles_finales, key=lambda x: x['distance'])[:15])
    except:
        mettre_a_jour_interface("Erreur Scan Prédictif", "red")
        scan_en_cours = False

def recalcul_distances_mathematiques(pos_vaisseau):
    try:
        vx, vy, vz = pos_vaisseau[0], pos_vaisseau[1], pos_vaisseau[2]
        res = requests.get(f"{SUPABASE_URL}/rest/v1/radar_commercial?select=id,x,y,z&type_operation=in.(ACHAT,VENTE)", headers=get_headers())
        if res.status_code == 200:
            for cible in res.json():
                cx, cy, cz = cible.get('x', 0), cible.get('y', 0), cible.get('z', 0)
                if cx == 0 and cy == 0 and cz == 0: continue 
                requests.patch(f"{SUPABASE_URL}/rest/v1/radar_commercial?id=eq.{cible['id']}", headers=get_headers(), json={"distance": math.sqrt((cx - vx)**2 + (cy - vy)**2 + (cz - vz)**2)})
            mettre_a_jour_interface(f"Distances recalibrées", "#00FF00")
            time.sleep(2)
            mettre_a_jour_interface(f">_ POSITION ACTUELLE : {systeme_actuel.upper()}", "gray")
    except: pass

def enregistrer_transaction(system, station, entry, type_op):
    try:
        user_id = get_user_id()
        if not user_id: return
        
        payload = {
            "user_id": user_id,
            "type_operation": type_op,
            "systeme": system if system else "INCONNU",
            "station": station if station else "INCONNUE",
            "marchandise": formater_nom_marchandise(entry.get('Type', '')),
            "quantite": int(entry.get('Count', 0)),
            "prix_unitaire": int(entry.get('BuyPrice', 0)) if type_op == 'ACHAT' else int(entry.get('SellPrice', 0)),
            "total": int(entry.get('TotalCost', 0)) if type_op == 'ACHAT' else int(entry.get('TotalSale', 0))
        }
        
        requests.post(f"{SUPABASE_URL}/rest/v1/journal_transactions", headers=get_headers(), json=payload)
    except: pass

def check_for_updates():
    global status_label  # <-- Permet de modifier le texte sur l'interface d'EDMC
    # On attend 3 secondes pour laisser l'interface d'EDMC se construire
    import time
    time.sleep(3)
    try:
        url_version = "https://raw.githubusercontent.com/wopygm/SYS_EDTEAM_Plugin/main/version.txt"
        req = urllib.request.Request(url_version, headers={'User-Agent': 'EDMC-Plugin-Updater'})
        with urllib.request.urlopen(req) as response:
            latest_version = response.read().decode('utf-8').strip()

        if latest_version != PLUGIN_VERSION:
            logging.info(f"SYS_EDTEAM : Mise à jour trouvée ! (v{PLUGIN_VERSION} -> v{latest_version})")
            
            # --- MESSAGE 1 : DÉBUT DE LA MAJ ---
            try:
                status_label.config(text=f"Téléchargement MAJ v{latest_version}...", fg="orange")
            except: pass
            
            url_zip = "https://github.com/wopygm/SYS_EDTEAM_Plugin/archive/refs/heads/main.zip"
            req_zip = urllib.request.Request(url_zip, headers={'User-Agent': 'EDMC-Plugin-Updater'})
            with urllib.request.urlopen(req_zip) as response_zip:
                zip_data = response_zip.read()
            
            this_dir = os.path.dirname(os.path.realpath(__file__))
            with zipfile.ZipFile(io.BytesIO(zip_data)) as z:
                for file_info in z.infolist():
                    if file_info.is_dir() or file_info.filename.endswith("version.txt"):
                        continue
                    
                    parts = file_info.filename.split('/')
                    if len(parts) > 1:
                        relative_path = os.path.join(*parts[1:])
                        target_path = os.path.join(this_dir, relative_path)
                        
                        os.makedirs(os.path.dirname(target_path), exist_ok=True)
                        with z.open(file_info) as source, open(target_path, "wb") as target:
                            target.write(source.read())

            logging.info("SYS_EDTEAM : Mise à jour terminée avec succès.")
            
            # --- MESSAGE 2 : FIN DE LA MAJ ---
            try:
                status_label.config(text=f"MAJ v{latest_version} OK ! Redémarrez EDMC.", fg="#00FF66")
            except: pass
            
    except Exception as e:
        logging.error(f"SYS_EDTEAM : Erreur lors de la maj : {e}")

# ==========================================
# BOOT SEQUENCE
# ==========================================
def plugin_start3(plugin_dir):
    threading.Thread(target=ecoute_commandes_distantes, daemon=True).start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=surveiller_marche_en_fond, daemon=True).start()
    # On lance la vérification dans un processus séparé pour ne pas figer EDMC
    threading.Thread(target=check_for_updates, daemon=True).start()
    
    return "SYS.EDTEAM"

def plugin_app(parent):
    global status_label
    frame = tk.Frame(parent)
    tk.Label(frame, text=f"SYS_EDTEAM v{PLUGIN_VERSION}", font=("Helvetica", 10, "bold"), fg="#00FF66").grid(row=0, column=0, sticky=tk.W)
    status_label = tk.Label(frame, text="En attente des senseurs...", fg="gray")
    status_label.grid(row=1, column=0, sticky=tk.W)
    return frame

import myNotebook as nb
def plugin_prefs(parent, cmdr, is_beta):
    frame = nb.Frame(parent)
    cle_api_var = tk.StringVar(value=lire_cle())
    tk.Label(frame, text="SYS.EDTEAM - Télémétrie Sécurisée", font=("Helvetica", 10, "bold"), fg="#FF7100").grid(column=0, row=0, columnspan=2, sticky=tk.W, pady=5)
    tk.Label(frame, text="Clé d'Accès :").grid(column=0, row=1, sticky=tk.W)
    entry = tk.Entry(frame, textvariable=cle_api_var, width=45)
    entry.grid(column=1, row=1, sticky=tk.W, padx=10)
    cle_api_var.trace_add("write", lambda *args: sauvegarder_cle(cle_api_var.get()))
    return frame

# ==========================================
# ROUTEUR PRINCIPAL
# ==========================================
def journal_entry(cmdr, is_beta, system, station, entry, state):
    global systeme_actuel, cmdr_actuel
    cmdr_actuel = cmdr # <-- ON ENREGISTRE TON NOM
    if system and system != systeme_actuel: systeme_actuel = system
    event = entry.get('event')
    
    if event in ['FSDJump', 'Location', 'CarrierJump', 'SupercruiseEntry', 'SupercruiseExit']:
        if event in ['FSDJump', 'Location', 'CarrierJump']:
            mettre_a_jour_interface(f">_ POSITION ACTUELLE : {systeme_actuel.upper()}", "#00F0FF")
            
        # NOUVEAU : Capture des réputations des factions locales
        if event in ['FSDJump', 'Location']:
            factions = entry.get('Factions', [])
            reps = {}
            for f in factions:
                if 'MyReputation' in f:
                    reps[f['Name']] = f['MyReputation']
            if reps:
                threading.Thread(target=maj_generique_global, args=("QG_REPUTATIONS", "QG_DATA", json.dumps(reps), "INFO")).start()

        # 🚨 LE CORRECTIF : On force l'effacement de la cible
        threading.Thread(target=maj_generique_global, args=("TARGETED_CMDR", "SYS_CORE", "LOST", "INFO")).start()

    elif event == 'LoadGame':
        ship_name = entry.get('ShipName', 'VAISSEAU TACTIQUE')
        ship_model = entry.get('Ship_Localised', entry.get('Ship', 'INCONNU')).title()
        possede_fc = entry.get('FleetCarrierID') is not None
        
        threading.Thread(target=patch_parametres, args=({"vaisseau_nom": ship_name.upper(), "vaisseau_modele": ship_model, "possede_fc": possede_fc},)).start()
        
        threading.Thread(target=maj_generique_global, args=("CMDR_NAME", "SYS_CORE", cmdr, "STATUS")).start()
        threading.Thread(target=maj_generique_global, args=("QG_ACTIVE_SHIP_ID", "QG_DATA", str(entry.get('ShipID', '0')), "INFO")).start()

    # ==========================================
    # NOUVEAU MODULE : INTERCEPTION ESCADRON
    # ==========================================
    elif event == 'SquadronStartup':
        squad_name = entry.get('SquadronName', '')
        squad_rank = entry.get('CurrentRank', 0)
        
        if squad_name:
            payload = json.dumps({"nom": squad_name, "rank": squad_rank})
            threading.Thread(target=maj_generique_global, args=("SQUADRON_INFO", "QG_DATA", payload, "INFO")).start()
            mettre_a_jour_interface(f">_ ESCADRON DÉTECTÉ : {squad_name}", "#00FF66")

    # ==========================================
    # NOUVEAU MODULE : CIBLAGE TACTIQUE (COVAS)
    # ==========================================
    elif event == 'ShipTargeted':
        target_locked = entry.get('TargetLocked', False)
        
        if target_locked:
            pilot_name_loc = entry.get('PilotName_Localised', '')
            pilot_name = entry.get('PilotName', '')
            
            nom_joueur = ""
            if pilot_name_loc.upper().startswith("CMDR "):
                nom_joueur = pilot_name_loc[5:].strip().upper()
            elif "$cmdr_decorate" in pilot_name.lower():
                nom_joueur = pilot_name.split('=')[-1].replace(';', '').strip().upper()
                
            if nom_joueur:
                squad_tag = entry.get('SquadronID', '')
                payload = json.dumps({"nom": nom_joueur, "tag": squad_tag})
                threading.Thread(target=maj_generique_global, args=("TARGETED_CMDR", "SYS_CORE", payload, "INFO")).start()
                
                affichage_tag = f" [{squad_tag}]" if squad_tag else ""
                mettre_a_jour_interface(f">_ CIBLE : CMDR {nom_joueur}{affichage_tag}", "#FF3333")
            else:
                # Cible verrouillée mais non-joueur (PNJ, drone, balise...)
                threading.Thread(target=maj_generique_global, args=("TARGETED_CMDR", "SYS_CORE", "LOST", "INFO")).start()
        else:
            # Déverrouillage complet (espace vide, station, astre sélectionné...)
            threading.Thread(target=maj_generique_global, args=("TARGETED_CMDR", "SYS_CORE", "LOST", "INFO")).start()


    # ==========================================
    # STATUT LÉGAL
    # ==========================================
    if entry.get('Notoriety') is not None:
        threading.Thread(target=maj_generique_global, args=("QG_NOTORIETE", "QG_DATA", "NOTORIETE", "INFO", entry.get('Notoriety'))).start()
    

    if event in ['Rank', 'Progress']:
        
        # ==========================================
        # 1. RANGS ET PROGRESSION
        # ==========================================
        if event == 'Rank':
            for r in ['Combat', 'Trade', 'Explore', 'Federation', 'Empire', 'Exobiologist']:
                if entry.get(r) is not None: 
                    threading.Thread(target=maj_generique_global, args=(f"QG_RANK_{r.upper()[:6]}", "QG_DATA", r.upper(), "INFO", entry.get(r))).start()
            
            val_mercenary = entry.get('Soldier') if entry.get('Soldier') is not None else entry.get('Mercenary')
            if val_mercenary is not None:
                threading.Thread(target=maj_generique_global, args=("QG_RANK_MERCEN", "QG_DATA", "MERCENARY", "INFO", val_mercenary)).start()
                    
        elif event == 'Progress':
            for r in ['Combat', 'Trade', 'Explore', 'Federation', 'Empire', 'Exobiologist']:
                if entry.get(r) is not None: 
                    threading.Thread(target=maj_generique_global, args=(f"QG_PROG_{r.upper()[:6]}", "QG_DATA", r.upper(), "INFO", entry.get(r))).start()
            
            prog_mercenary = entry.get('Soldier') if entry.get('Soldier') is not None else entry.get('Mercenary')
            if prog_mercenary is not None:
                threading.Thread(target=maj_generique_global, args=("QG_PROG_MERCEN", "QG_DATA", "MERCENARY", "INFO", prog_mercenary)).start()
            
            if entry.get('Soldier') is not None:
                threading.Thread(target=maj_generique_global, args=("QG_PROG_MERCEN", "QG_DATA", "MERCENARY", "INFO", entry.get('Soldier'))).start()

    elif event == 'Loadout':
        ship_name = entry.get('ShipName', 'VAISSEAU TACTIQUE')
        ship_id = str(entry.get('ShipID', '0'))
        ship_model = entry.get('Ship_Localised', entry.get('Ship', 'INCONNU')).title()
        cargo_cap = entry.get('CargoCapacity', 0)
        
        # --- ICI : On a retiré le vaisseau_id du payload ---
        threading.Thread(target=patch_parametres, args=({"vaisseau_nom": ship_name.upper(), "vaisseau_modele": ship_model, "vaisseau_capacite": cargo_cap},)).start()
        
        threading.Thread(target=maj_generique_global, args=("QG_REBUY", "QG_DATA", "ASSURANCE", "INFO", entry.get('Rebuy', 0))).start()
        
        modules = [{"slot": m.get('Slot', ''), "nom": m.get('Item_Localised', m.get('Item', '')).replace('_', ' ').title(), "grade": m.get('Engineering', {}).get('Level', 0)} for m in entry.get('Modules', [])]
        threading.Thread(target=maj_generique_global, args=("QG_MODULES_" + ship_id, "QG_DATA", json.dumps(modules), "INFO")).start()
        threading.Thread(target=maj_generique_global, args=("QG_ACTIVE_SHIP_ID", "QG_DATA", ship_id, "INFO")).start()

    elif event == 'StoredShips':
        flotte = [{"id": str(s.get('ShipID')), "nom": s.get('Name', 'INCONNU'), "modele": s.get('ShipType_Localised', s.get('ShipType', 'INCONNU')).title(), "systeme": s.get('StarSystem', systeme_actuel)} for s in entry.get('ShipsHere', []) + entry.get('ShipsRemote', [])]
        threading.Thread(target=maj_generique_global, args=("QG_FLEET", "QG_DATA", json.dumps(flotte), "INFO")).start()

    elif event == 'Statistics':
        bank = entry.get('Bank_Account', {})
        threading.Thread(target=maj_generique_global, args=("QG_WEALTH", "QG_DATA", "WEALTH", "INFO", bank.get('Current_Wealth', 0))).start()
        threading.Thread(target=maj_generique_global, args=("QG_SHIPS_VALUE", "QG_DATA", "SHIPS", "INFO", bank.get('Spent_On_Ships', 0) + bank.get('Spent_On_Outfitting', 0))).start()
        
        # --- AJOUT : Capture de la notoriété au démarrage ---
        crime = entry.get('Crime', {})
        if crime.get('Notoriety') is not None:
            threading.Thread(target=maj_generique_global, args=("QG_NOTORIETE", "QG_DATA", "NOTORIETE", "INFO", crime.get('Notoriety'))).start()

    elif event == 'CargoTransfer':
        for t in entry.get('Transfers', []):
            ch = t.get('Count', 0) if t.get('Direction') == 'tocarrier' else -t.get('Count', 0)
            maj_bdd_inventaire(formater_nom_marchandise(t.get('Type', '')), ch, relatif=True)
            
    elif event == 'CarrierStats':
        cap = entry.get('SpaceUsage', {}).get('FreeSpace', 25000) + entry.get('SpaceUsage', {}).get('Cargo', 0)
        threading.Thread(target=patch_parametres, args=({"fc_nom": f"[{entry.get('Callsign', 'XXXX')}] {entry.get('Name', 'CARRIER INCONNU')}", "fc_capacite": cap if cap > 0 else 25000, "possede_fc": True},)).start()
        
        fc_balance = entry.get('Finance', {}).get('CarrierBalance')
        if fc_balance is not None: 
            threading.Thread(target=maj_generique_global, args=("FC_BALANCE", "FINANCE", "BANK", "FINANCE", fc_balance)).start()
        
        stats_fc = {"fuel": entry.get('FuelLevel', 0), "crew": entry.get('Crew', []), "finance": entry.get('Finance', {})}
        threading.Thread(target=maj_generique_global, args=("QG_CARRIER_STATS", "QG_DATA", json.dumps(stats_fc), "INFO")).start()
        
    elif event == 'Cargo':
        if entry.get('Vessel') != 'SRV':
            try:
                requests.delete(f"{SUPABASE_URL}/rest/v1/radar_commercial?type_operation=eq.SHIP_CARGO", headers=get_headers())
                for item in entry.get('Inventory', []):
                    if item.get('Count', 0) > 0: 
                        maj_generique_global(formater_nom_marchandise(item.get('Name', '')), "SHIP", "SHIP", "SHIP_CARGO", vol=item.get('Count', 0))
            except: pass

    elif event == 'MarketBuy': threading.Thread(target=enregistrer_transaction, args=(system, station, entry, "ACHAT")).start()
    elif event == 'MarketSell': threading.Thread(target=enregistrer_transaction, args=(system, station, entry, "VENTE")).start()

    # ==========================================
    # MODULE BGS (SÉCURISÉ : GUERRES, HAUSSE & OPÉRATIONS)
    # ==========================================
    if not hasattr(journal_entry, 'bgs_cache'): 
        journal_entry.bgs_cache = {'missions': {}, 'station_faction': ''}
    
    # 1. Extraction robuste de la faction de la station (Texte ou Dictionnaire)
    if entry.get('event') in ['Docked', 'Location', 'ApproachSettlement']:
        faction_info = entry.get('StationFaction') or entry.get('SystemFaction')
        if isinstance(faction_info, dict):
            journal_entry.bgs_cache['station_faction'] = faction_info.get('Name', '')
        elif isinstance(faction_info, str):
            journal_entry.bgs_cache['station_faction'] = faction_info

    # 2. Mémorisation des missions acceptées
    if entry.get('event') == 'MissionAccepted': 
        m_id = entry.get('MissionID')
        m_name = entry.get('Name', '').lower()
        est_combat = any(k in m_name for k in ['massacre', 'assassin', 'kill', 'pirat', 'combat', 'destroy', 'skimmer'])
        est_pirate = any(k in m_name for k in ['pirat', 'deserter', 'anarchy'])
        
        if m_id:
            journal_entry.bgs_cache['missions'][m_id] = {
                'faction': entry.get('Faction', ''),
                'system': system or systeme_actuel,
                'dest_system': entry.get('DestinationSystem', system or systeme_actuel),
                'is_combat': est_combat,
                'is_pirate': est_pirate
            }

    # 3. Liste complète des événements surveillés
    bgs_events = [
        'MissionCompleted', 'MissionFailed', 'MissionAbandoned', 
        'MarketSell', 'RedeemVoucher', 'SellExplorationData', 
        'MultiSellExplorationData', 'SellOrganicData', 'CommitCrime', 
        'CollectItem', 'CollectItems', 'DataDownloaded', 'BackpackChange', 'Music'
    ]

    if entry.get('event') in bgs_events:
        def process_bgs_complet():
            user_id = get_user_id()
            if not user_id: return
            
            evt = entry.get('event')
            actions = []
            
            # Récupération de la faction de la station (avec repli sur l'état EDMC)
            f_station = journal_entry.bgs_cache.get('station_faction', '')
            if not f_station and state:
                st_state = state.get('StationFaction')
                if isinstance(st_state, dict): f_station = st_state.get('Name', '')
                elif isinstance(st_state, str): f_station = st_state

            # A. VICTOIRE EN ZONE DE CONFLIT (CZ SPATIALE & TERRESTRE)
            if evt == 'Music':
                track = str(entry.get('MusicTrack', '')).lower()
                if 'conflictzone' in track and ('win' in track or 'victory' in track):
                    actions.append({
                        "faction": "", 
                        "type": "CZ_VICTOIRES", 
                        "valeur": 1, 
                        "system": systeme_actuel, 
                        "is_combat": True
                    })

            # B. VALIDATION DE MISSION
            elif evt == 'MissionCompleted': 
                inf_val = 1
                for fe in entry.get('FactionEffects', []):
                    if fe.get('Faction') == entry.get('Faction', ''):
                        for inf in fe.get('Influence', []):
                            if isinstance(inf.get('Influence'), str) and '+' in inf.get('Influence'):
                                inf_val = inf.get('Influence').count('+')
                                break
                        break
                
                m_id = entry.get('MissionID')
                m_info = journal_entry.bgs_cache.get('missions', {}).get(m_id, {})
                m_faction = m_info.get('faction') if isinstance(m_info, dict) else (m_info or entry.get('Faction', ''))
                m_system = m_info.get('system') if isinstance(m_info, dict) else systeme_actuel
                m_dest_system = m_info.get('dest_system') if isinstance(m_info, dict) else systeme_actuel
                m_is_combat = m_info.get('is_combat', False) if isinstance(m_info, dict) else False
                m_is_pirate = m_info.get('is_pirate', False) if isinstance(m_info, dict) else False

                actions.append({
                    "faction": m_faction or entry.get('Faction', ''), 
                    "type": "MISSIONS", 
                    "valeur": inf_val,
                    "system": m_system or systeme_actuel,
                    "dest_system": m_dest_system or systeme_actuel,
                    "is_combat": m_is_combat,
                    "is_pirate": m_is_pirate
                })

            # C. MISSIONS ÉCHOUÉES OU ABANDONNÉES
            elif evt in ['MissionFailed', 'MissionAbandoned']:
                m_id = entry.get('MissionID')
                m_info = journal_entry.bgs_cache.get('missions', {}).get(m_id, {})
                
                # Système anti-doublon : on ignore si la mission a déjà été marquée
                if isinstance(m_info, dict) and m_info.get('deja_compte'):
                    pass
                else:
                    if isinstance(m_info, dict):
                        m_info['deja_compte'] = True # On verrouille pour le prochain événement
                        
                    f = m_info.get('faction') if isinstance(m_info, dict) else (m_info or '')
                    s = m_info.get('system') if isinstance(m_info, dict) else systeme_actuel
                    if f: 
                        actions.append({"faction": f, "type": "ECHECS", "valeur": 1, "system": s, "is_combat": False})

            # D. OBLIGATIONS DE COMBAT ET PRIMES
            elif evt == 'RedeemVoucher' and entry.get('Type') in ['bounty', 'CombatBond']:
                for f_info in entry.get('Factions', []): 
                    actions.append({"faction": f_info.get('Faction', ''), "type": "SECURITE", "valeur": f_info.get('Amount', 0), "system": systeme_actuel, "is_combat": True})
                if entry.get('Faction') and entry.get('Amount'): 
                    actions.append({"faction": entry.get('Faction', ''), "type": "SECURITE", "valeur": entry.get('Amount', 0), "system": systeme_actuel, "is_combat": True})

            # E. DONNÉES SCIENTIFIQUES (Cartographie & Exobiologie Vista Genomics)
            elif evt in ['SellExplorationData', 'MultiSellExplorationData', 'SellOrganicData']:
                val = 0
                if evt == 'SellOrganicData':
                    val = entry.get('TotalEarnings', 0)
                    if not val and 'BioData' in entry:
                        val = sum((b.get('Value', 0) + b.get('Bonus', 0)) for b in entry.get('BioData', []))
                else:
                    val = entry.get('TotalEarnings', entry.get('BaseValue', 0))
                    if evt == 'SellExplorationData' and 'TotalEarnings' not in entry:
                        val += entry.get('Bonus', 0)

                if val > 0: 
                    actions.append({"faction": f_station, "type": "SCIENCE", "valeur": val, "system": systeme_actuel, "is_combat": False})

            # F. COMMERCE & MARCHÉ NOIR
            elif evt == 'MarketSell':
                val = entry.get('TotalSale', 0)
                if val > 0: 
                    actions.append({"faction": f_station, "type": "CONTREBANDE" if (entry.get('Stolen', False) or entry.get('IllegalGoods', False)) else "ECONOMIE", "valeur": val, "system": systeme_actuel, "is_combat": False})

            # G. CRIMES ET SABOTAGES
            elif evt == 'CommitCrime' and 'murder' in str(entry.get('CrimeType', '')).lower():
                faction_victime = entry.get('Faction', '') or f_station
                if faction_victime: 
                    actions.append({"faction": faction_victime, "type": "MEURTRES", "valeur": 1, "system": systeme_actuel, "is_combat": True})

            elif evt in ['CollectItem', 'CollectItems']:
                nom_item = entry.get('Name', '').lower()
                if 'powerregulator' in nom_item and f_station:
                    actions.append({"faction": f_station, "type": "VOLS", "valeur": 1, "system": systeme_actuel, "is_combat": False})

            elif evt == 'DataDownloaded':
                if f_station:
                    actions.append({"faction": f_station, "type": "PIRATAGE", "valeur": 1, "system": systeme_actuel, "is_combat": False})

            # LE FILET DE SÉCURITÉ ODYSSEY (Écoute des ajouts silencieux dans le sac à dos)
            elif evt == 'BackpackChange':
                ajouts = entry.get('Added', [])
                for ajout in ajouts:
                    nom_ajout = ajout.get('Name', '').lower()
                    type_ajout = ajout.get('Type', '')
                    
                    # 1. Détection du régulateur volé
                    if 'powerregulator' in nom_ajout and f_station:
                         actions.append({"faction": f_station, "type": "VOLS", "valeur": ajout.get('Count', 1), "system": systeme_actuel, "is_combat": False})
                    
                    # 2. Détection du piratage de données (On filtre les consommables classiques)
                    elif type_ajout == 'Data' and f_station:
                         actions.append({"faction": f_station, "type": "PIRATAGE", "valeur": ajout.get('Count', 1), "system": systeme_actuel, "is_combat": False})
            
            # 4. LIAISON ET TRANSMISSION VERS SUPABASE
            try:
                res_ordres = requests.get(f"{SUPABASE_URL}/rest/v1/ordres_bgs?statut=eq.ACTIF&select=id,faction_cible,systeme_cible,type_ordre", headers=get_headers(), timeout=5)
                
                # DIAGNOSTIC 1 : Supabase refuse-t-il la lecture des ordres ?
                if res_ordres.status_code != 200:
                    mettre_a_jour_interface(f">_ ERREUR LECTURE ORDRE : {res_ordres.status_code}", "red")
                    return
                
                ordres_actifs = res_ordres.json()
                
                # DIAGNOSTIC 2 : La liste des ordres est-elle vide pour le plugin ?
                if len(ordres_actifs) == 0:
                    mettre_a_jour_interface(">_ AUCUN ORDRE ACTIF TROUVÉ (RLS?)", "orange")
                    return

                for action in actions:
                    sys_cible_action = action.get('system', systeme_actuel).strip().lower()
                    ordre_valide = None

                    # CAS 1 : Victoire en Zone de Conflit (Stricte sur le système)
                    if action['type'] == 'CZ_VICTOIRES':
                        ordre_valide = next((o for o in ordres_actifs if o.get('type_ordre') == 'GUERRE' and o.get('systeme_cible', '').strip().lower() == sys_cible_action), None)

                    # CAS 2 : Actions standards (Stricte sur la Faction ET le Système)
                    else:
                        if action['faction']:
                            ordre_valide = next((o for o in ordres_actifs if o.get('faction_cible', '').strip().lower() == action['faction'].strip().lower() and o.get('systeme_cible', '').strip().lower() == sys_cible_action), None)

                    if ordre_valide:
                        # Filtres ignorés
                        if ordre_valide.get('type_ordre') == 'ELECTION' and action.get('is_combat', False): continue
                        if ordre_valide.get('type_ordre') == 'GUERRE' and action['type'] == 'MISSIONS':
                            if action.get('is_combat', False):
                                dest = action.get('dest_system', sys_cible_action).strip().lower()
                                sys_guerre = ordre_valide.get('systeme_cible', '').strip().lower()
                                if dest != sys_guerre or action.get('is_pirate', False): continue
                            valeur_finale = 1
                        else:
                            valeur_finale = action['valeur']

                        # ENVOI À SUPABASE
                        res_post = requests.post(
                            f"{SUPABASE_URL}/rest/v1/efforts_bgs", 
                            headers=get_headers(), 
                            json={
                                "user_id": user_id, 
                                "ordre_id": ordre_valide['id'], 
                                "type_action": action['type'], 
                                "valeur": valeur_finale, 
                                "date_action": entry.get('timestamp', datetime.now(timezone.utc).isoformat())
                            }, 
                            timeout=5
                        )
                        
                        # DIAGNOSTIC 3 : Supabase a-t-il accepté l'insertion ?
                        if res_post.status_code in [200, 201, 204]:
                            if action['type'] == 'CZ_VICTOIRES':
                                mettre_a_jour_interface(">_ BGS : VICTOIRE CZ ENREGISTRÉE !", "#FFD700")
                            else:
                                mettre_a_jour_interface(f">_ BGS : {action['type']} ENREGISTRÉ", "#00FF66")
                        else:
                            mettre_a_jour_interface(f">_ REJET EFFORT : {res_post.status_code}", "red")
                    else:
                        # DIAGNOSTIC 4 : Le système ou la faction ne correspond pas
                        mettre_a_jour_interface(f">_ BGS : AUCUN ORDRE CORRESPONDANT", "orange")
            except Exception as e:
                mettre_a_jour_interface(f">_ ERREUR SCRIPT : {str(e)[:15]}", "red")

        threading.Thread(target=process_bgs_complet).start()

def maj_bdd_inventaire(marchandise, quantite, relatif=True):
    global inventaire_fc_local
    if not marchandise:
        return
        
    user_id = get_user_id()
    if not user_id:
        return

    try:
        with inventaire_lock:
            if inventaire_fc_local is None:
                inventaire_fc_local = {}
                res = requests.get(f"{SUPABASE_URL}/rest/v1/inventaire_fc?user_id=eq.{user_id}", headers=get_headers())
                if res.status_code == 200:
                    for item in res.json(): 
                        inventaire_fc_local[item.get('marchandise')] = item.get('quantite', 0)
                        
            nouvelle_qte = max(0, inventaire_fc_local.get(marchandise, 0) + quantite) if relatif else quantite
            inventaire_fc_local[marchandise] = nouvelle_qte

        if nouvelle_qte <= 0:
            requests.delete(
                f"{SUPABASE_URL}/rest/v1/inventaire_fc?user_id=eq.{user_id}&marchandise=eq.{urllib.parse.quote(marchandise)}", 
                headers=get_headers()
            )
        else:
            h = get_headers()
            h["Prefer"] = "resolution=merge-duplicates"
            payload = {
                "user_id": user_id,
                "marchandise": marchandise,
                "quantite": nouvelle_qte,
                "derniere_maj": datetime.now(timezone.utc).isoformat()
            }
            requests.post(
                f"{SUPABASE_URL}/rest/v1/inventaire_fc?on_conflict=user_id,marchandise", 
                headers=h, 
                json=payload
            )
    except Exception as e:
        pass