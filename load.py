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
PLUGIN_VERSION = "2.0"

SUPABASE_URL = "https://oailvdigfdoyfcydmabb.supabase.co"
SUPABASE_KEY = "sb_publishable_AASqgRggHdIGttZHPGaWkA_VqrhuYNg"

status_label = None
systeme_actuel = "SYSTÈME INCONNU"
cmdr_actuel = None # <-- NOUVELLE VARIABLE
scan_en_cours = False
dernier_solde_fc = None
cached_user_id = None
invalid_api_key = False
dernier_solde_vaisseau = None
dernier_etat_cible = "LOST"

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
        "Prefer": "return=minimal",
        "x-commandant-key": cle if cle else "NO_KEY",
        "User-Agent": f"SYS.EDTEAM/{cle}" if cle else "SYS.EDTEAM/NO_KEY"
    }

# ==========================================
# LE PONT DE COMMUNICATION
# ==========================================
def patch_parametres(payload):
    uid = get_user_id()
    if not uid: return
    try:
        res = requests.get(f"{SUPABASE_URL}/rest/v1/radar_commercial?select=id,station_name&target_commodity=eq.PARAM_UPDATE&user_id=eq.{uid}", headers=get_headers())
        if res.status_code == 200 and len(res.json()) > 0:
            row = res.json()[0]
            try: existing = json.loads(row.get('station_name', '{}'))
            except: existing = {}
            existing.update(payload)
            requests.patch(f"{SUPABASE_URL}/rest/v1/radar_commercial?id=eq.{row['id']}", headers=get_headers(), json={"station_name": json.dumps(existing)})
        else:
            data = {"user_id": uid, "system_name": "SYS_CORE", "station_name": json.dumps(payload), "target_commodity": "PARAM_UPDATE", "type_operation": "STATUS", "prix_unitaire": 0, "volume_disponible": 0, "distance": 0, "prix_moyen": 0}
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
        res = requests.get(f"{SUPABASE_URL}/rest/v1/radar_commercial?select=id&target_commodity=eq.{target}&user_id=eq.{uid}", headers=get_headers())
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
    global cached_user_id, invalid_api_key
    if cached_user_id: 
        return cached_user_id
    if invalid_api_key:
        return None # <-- LE BOUCLIER : On stoppe l'hémorragie ici
        
    cle = lire_cle()
    if not cle:
        mettre_a_jour_interface(">_ BLOQUÉ : AUCUNE CLÉ DANS EDMC", "red")
        return None
    try:
        res = requests.get(f"{SUPABASE_URL}/rest/v1/profils?select=user_id", headers=get_headers())
        if res.status_code == 200:
            data = res.json()
            if len(data) > 0:
                cached_user_id = data[0].get('user_id')
                return cached_user_id
            else:
                mettre_a_jour_interface(">_ BLOQUÉ : CLÉ NON RECONNUE", "red")
                invalid_api_key = True # <-- VERROUILLAGE
                return None
        else:
            mettre_a_jour_interface(f">_ ERREUR BDD : {res.status_code}", "red")
            if res.status_code in [401, 403]:
                invalid_api_key = True # <-- VERROUILLAGE
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

def heartbeat_loop():
    global dernier_solde_vaisseau
    while True:
        try:
            timestamp = str(int(time.time()))
            maj_generique_global("HEARTBEAT", "SYS_CORE", timestamp, "STATUS")
            
            jdir = trouver_journal_dir()
            if jdir and os.path.exists(os.path.join(jdir, 'Status.json')):
                with open(os.path.join(jdir, 'Status.json'), 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    nouveau_solde = data.get('Balance')
                    if nouveau_solde is not None and nouveau_solde != dernier_solde_vaisseau: 
                        maj_generique_global("SHIP_BALANCE", "FINANCE", "BANK", "FINANCE", val=nouveau_solde)
                        dernier_solde_vaisseau = nouveau_solde
        except: 
            pass
        time.sleep(60) # <-- On passe de 30s à 60s

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
    threading.Thread(target=heartbeat_loop, daemon=True).start()
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

def sync_conflits_supabase(systeme, conflits):
    """Envoie tous les conflits actifs détectés par l'éclaireur vers Supabase."""
    try:
        h = get_headers()
        h["Prefer"] = "resolution=merge-duplicates"
        
        payloads = []
        now_iso = datetime.now(timezone.utc).isoformat()
        
        for c in conflits:
            # On ignore uniquement si le conflit est expressément marqué en attente (pending)
            statut = str(c.get("Status", "")).lower()
            if statut == "pending":
                continue
            
            f1 = c.get("Faction1", {})
            f2 = c.get("Faction2", {})
            f1_nom = f1.get("Name", "")
            f2_nom = f2.get("Name", "")
            
            if not f1_nom or not f2_nom:
                continue
                
            cle_conflit = f"{systeme}_{min(f1_nom, f2_nom)}_{max(f1_nom, f2_nom)}".replace(" ", "_")
            
            payloads.append({
                "id": cle_conflit,
                "systeme": systeme,
                "type_conflit": c.get("WarType", "inconnu"),
                "faction1_nom": f1_nom,
                "faction1_score": f1.get("WonDays", 0),
                "faction1_enjeu": f1.get("Stake", ""),
                "faction2_nom": f2_nom,
                "faction2_score": f2.get("WonDays", 0),
                "faction2_enjeu": f2.get("Stake", ""),
                "mis_a_jour": now_iso
            })
            
        if payloads:
            res = requests.post(
                f"{SUPABASE_URL}/rest/v1/conflits_systemes?on_conflict=id",
                headers=h,
                json=payloads,
                timeout=5
            )
            if res.status_code in [200, 201, 204]:
                mettre_a_jour_interface(f">_ BGS : ÉCLAIREUR ({systeme.upper()})", "#00FF66")
            else:
                mettre_a_jour_interface(f">_ REJET BDD CONFLIT : {res.status_code}", "red")
        else:
            mettre_a_jour_interface(f">_ BGS : 0 CONFLIT RETENU", "orange")
    except Exception as e:
        mettre_a_jour_interface(f">_ ERREUR SCRIPT CONFLIT", "red")
        logging.error(f"[SYS_EDTEAM] Erreur synchro conflits : {e}")

# ==========================================
# ROUTEUR PRINCIPAL
# ==========================================
def journal_entry(cmdr, is_beta, system, station, entry, state):
    global systeme_actuel, cmdr_actuel
    cmdr_actuel = cmdr # <-- ON ENREGISTRE TON NOM
    if system and system != systeme_actuel: systeme_actuel = system
    event = entry.get('event')

    # ==========================================
    # SUIVI TACTIQUE DES ZONES DE CONFLIT (CZ)
    # ==========================================
    if not hasattr(journal_entry, 'cz_cache'):
        journal_entry.cz_cache = {'intensity': 'S', 'points': 1, 'faction': '', 'won': False}

    # Réinitialisation à la sortie ou au saut
    if event in ['SupercruiseEntry', 'FSDJump', 'CarrierJump', 'Location']:
        journal_entry.cz_cache = {'intensity': 'S', 'points': 1, 'faction': '', 'won': False}

    # Détection de l'intensité au drop
    elif event == 'SupercruiseDestinationDrop':
        drop_type = str(entry.get('Type', '')).lower()
        if 'warzone' in drop_type or 'conflict' in drop_type:
            journal_entry.cz_cache['won'] = False
            if 'high' in drop_type:
                journal_entry.cz_cache['intensity'] = 'H'
                journal_entry.cz_cache['points'] = 1.6
            elif 'med' in drop_type:
                journal_entry.cz_cache['intensity'] = 'M'
                journal_entry.cz_cache['points'] = 1.3
            else:
                journal_entry.cz_cache['intensity'] = 'S'
                journal_entry.cz_cache['points'] = 1.0

    # Mémorisation du camp soutenu via les primes de combat et déduction de l'intensité au sol
    elif event == 'FactionKillBond':
        faction_alliee = entry.get('AwardingFaction')
        if faction_alliee:
            journal_entry.cz_cache['faction'] = faction_alliee
            
            # Si on tue une cible de grande valeur (Capitaine, SpecOps...), on monte l'intensité
            prime = entry.get('Reward', 0)
            actuel_pts = journal_entry.cz_cache.get('points', 1.0)
            
            if prime >= 30000 and actuel_pts < 1.6:
                journal_entry.cz_cache['intensity'] = 'H'
                journal_entry.cz_cache['points'] = 1.6
            elif prime >= 15000 and actuel_pts < 1.3:
                journal_entry.cz_cache['intensity'] = 'M'
                journal_entry.cz_cache['points'] = 1.3
    
    if event in ['FSDJump', 'Location', 'CarrierJump', 'SupercruiseEntry', 'SupercruiseExit']:
        if event in ['FSDJump', 'Location', 'CarrierJump']:
            mettre_a_jour_interface(f">_ POSITION ACTUELLE : {systeme_actuel.upper()}", "#00F0FF")
            # ÉMISSION CHIRURGICALE POUR COVAS (Uniquement au changement de système)
            threading.Thread(target=maj_generique_global, args=("SYSTEM_STATUS", systeme_actuel, "JUMP", "INFO")).start()
            
        # NOUVEAU : Capture des réputations des factions locales
        if event in ['FSDJump', 'Location']:
            factions = entry.get('Factions', [])
            reps = {}
            for f in factions:
                if 'MyReputation' in f:
                    reps[f['Name']] = f['MyReputation']
            if reps:
                threading.Thread(target=maj_generique_global, args=("QG_REPUTATIONS", "QG_DATA", json.dumps(reps), "INFO")).start()

                # INTERCEPTION ÉCLAIREUR : CONFLITS BGS (GUERRES & ÉLECTIONS)
            conflits = entry.get('Conflicts', [])
            sys_nom = entry.get('StarSystem') or systeme_actuel
            if conflits and sys_nom:
                threading.Thread(target=sync_conflits_supabase, args=(sys_nom, conflits), daemon=True).start()

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
    # MODULE CIBLAGE TACTIQUE OPTIMISÉ (PARE-FEU RÉSEAU)
    # ==========================================
    elif event == 'ShipTargeted':
        global dernier_etat_cible
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
                if payload != dernier_etat_cible:
                    dernier_etat_cible = payload
                    threading.Thread(target=maj_generique_global, args=("TARGETED_CMDR", "SYS_CORE", payload, "INFO")).start()
                    affichage_tag = f" [{squad_tag}]" if squad_tag else ""
                    mettre_a_jour_interface(f">_ CIBLE : CMDR {nom_joueur}{affichage_tag}", "#FF3333")
            else:
                # Cible non-joueur : transmission de "LOST" uniquement si on ciblait un joueur auparavant
                if dernier_etat_cible != "LOST":
                    dernier_etat_cible = "LOST"
                    threading.Thread(target=maj_generique_global, args=("TARGETED_CMDR", "SYS_CORE", "LOST", "INFO")).start()
        else:
            # Déverrouillage complet : transmission de "LOST" uniquement si nécessaire
            if dernier_etat_cible != "LOST":
                dernier_etat_cible = "LOST"
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
        
        threading.Thread(target=patch_parametres, args=({"vaisseau_nom": ship_name.upper(), "vaisseau_modele": ship_model},)).start()
        threading.Thread(target=maj_generique_global, args=("QG_REBUY", "QG_DATA", "ASSURANCE", "INFO", entry.get('Rebuy', 0))).start()
        threading.Thread(target=maj_generique_global, args=("QG_ACTIVE_SHIP_ID", "QG_DATA", ship_id, "INFO")).start()

    elif event == 'Statistics':
        bank = entry.get('Bank_Account', {})
        threading.Thread(target=maj_generique_global, args=("QG_WEALTH", "QG_DATA", "WEALTH", "INFO", bank.get('Current_Wealth', 0))).start()
        threading.Thread(target=maj_generique_global, args=("QG_SHIPS_VALUE", "QG_DATA", "SHIPS", "INFO", bank.get('Spent_On_Ships', 0) + bank.get('Spent_On_Outfitting', 0))).start()
        
        # --- AJOUT : Capture de la notoriété au démarrage ---
        crime = entry.get('Crime', {})
        if crime.get('Notoriety') is not None:
            threading.Thread(target=maj_generique_global, args=("QG_NOTORIETE", "QG_DATA", "NOTORIETE", "INFO", crime.get('Notoriety'))).start()

    # ==========================================
    # MODULE BGS (SÉCURISÉ : GUERRES, HAUSSE & OPÉRATIONS)
    # ==========================================
    if not hasattr(journal_entry, 'bgs_cache'): 
        journal_entry.bgs_cache = {'missions': {}, 'station_faction': '', 'ordres': [], 'ordres_ts': 0}
    
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
        'CollectItem', 'CollectItems', 'DataDownloaded', 'BackpackChange', 
        'Music', 'ReceiveText', 'Embark', 'BookDropship'
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

            # A. VICTOIRE EN ZONE DE CONFLIT (SPATIALE & TERRESTRE)
            victoire_cz = False
            
            # 1. Combat au sol (Odyssey - piste musicale si disponible)
            if evt == 'Music':
                track = str(entry.get('MusicTrack', '')).lower()
                if 'conflictzone' in track and ('win' in track or 'victory' in track):
                    victoire_cz = True

            # 2. Combat spatial (Déclencheur d'origine restauré)
            elif evt == 'ReceiveText':
                msg = str(entry.get('Message', '')).lower()
                # On réactive ton déclencheur qui marche à tous les coups
                if '$military_passthrough' in msg or 'warzone_pointrace_win' in msg:
                    victoire_cz = True

            # 3. Combat au sol (Déclencheur de repli)
            elif evt in ['Embark', 'BookDropship']:
                victoire_cz = True

            # 4. Validation avec verrou anti-doublon (Exige d'avoir touché des primes)
            f_combat = journal_entry.cz_cache.get('faction', '')
            if victoire_cz and not journal_entry.cz_cache.get('won', False) and f_combat != '':
                journal_entry.cz_cache['won'] = True
                pts = journal_entry.cz_cache.get('points', 1.0)
                intensite = journal_entry.cz_cache.get('intensity', 'S')

                actions.append({
                    "faction": f_combat, 
                    "type": "CZ_VICTOIRES", 
                    "valeur": pts, 
                    "intensite": intensite,
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
                maintenant = time.time()
                # Bouclier Egress : Lecture des ordres limitée à 1 fois toutes les 5 minutes
                if maintenant - journal_entry.bgs_cache.get('ordres_ts', 0) > 300:
                    res_ordres = requests.get(f"{SUPABASE_URL}/rest/v1/ordres_bgs?statut=eq.ACTIF&select=id,faction_cible,systeme_cible,type_ordre", headers=get_headers(), timeout=5)
                    
                    # DIAGNOSTIC 1 : Supabase refuse-t-il la lecture des ordres ?
                    if res_ordres.status_code != 200:
                        mettre_a_jour_interface(f">_ ERREUR LECTURE ORDRE : {res_ordres.status_code}", "red")
                        return
                        
                    journal_entry.bgs_cache['ordres'] = res_ordres.json()
                    journal_entry.bgs_cache['ordres_ts'] = maintenant
                
                ordres_actifs = journal_entry.bgs_cache.get('ordres', [])
                
                # DIAGNOSTIC 2 : La liste des ordres est-elle vide pour le plugin ?
                if len(ordres_actifs) == 0:
                    mettre_a_jour_interface(">_ AUCUN ORDRE ACTIF TROUVÉ (RLS?)", "orange")
                    return

                for action in actions:
                    sys_cible_action = action.get('system', systeme_actuel).strip().lower()
                    ordre_valide = None

                    # CAS 1 : Victoire en Zone de Conflit (Stricte sur Système ET Faction)
                    if action['type'] == 'CZ_VICTOIRES':
                        if action.get('faction'):
                            ordre_valide = next((o for o in ordres_actifs if o.get('type_ordre') == 'GUERRE' and o.get('systeme_cible', '').strip().lower() == sys_cible_action and o.get('faction_cible', '').strip().lower() == action['faction'].strip().lower()), None)

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
                                intensite_label = action.get('intensite', 'S')
                                noms_cz = {'H': 'HAUTE', 'M': 'MOYENNE', 'S': 'FAIBLE'}
                                label_cz = noms_cz.get(intensite_label, intensite_label)
                                mettre_a_jour_interface(f">_ BGS : CZ {label_cz} +{action['valeur']} PTS", "#FFD700")
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