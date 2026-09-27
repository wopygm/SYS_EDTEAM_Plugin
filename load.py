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
PLUGIN_VERSION = "2.2"

SUPABASE_URL = "https://oailvdigfdoyfcydmabb.supabase.co"
SUPABASE_KEY = "sb_publishable_AASqgRggHdIGttZHPGaWkA_VqrhuYNg"

status_label = None
systeme_actuel = "SYSTÈME INCONNU"
cmdr_actuel = None
scan_en_cours = False
dernier_solde_fc = None
cached_user_id = None
invalid_api_key = False
dernier_solde_vaisseau = None
dernier_etat_cible = "LOST"
dernier_faction_escadron_envoyee = None

# Cache pilote (Egress shield : 600s)
pilot_cache = {
    'user_id': None,
    'escadron_id': None,
    'faction_alliee': None,
    'faction_choisie': None,
    'puissance_nom': None,
    'puissance_rang': 0,
    'puissance_merites_cycle': 0,
    'puissance_merites_total': 0,
    'ts': 0
}

# Matériaux industriels de colonisation / chantiers
MATERIAUX_COLONISATION = {
    'gold', 'titanium', 'beryllium', 'steel', 'aluminium', 'copper', 'lithium',
    'cmmcomposite', 'ceramiccomposites', 'polymers', 'insulatingmembrane',
    'coolinghoses', 'powergenerators', 'waterpurifiers', 'structuralregulators',
    'atmosphericprocessors', 'buildingfabricators', 'computercomponents',
    'superconductors', 'semiconductors', 'emergencypowercells',
    'reinforcedmountingplate', 'microcontrollers', 'semiconductor', 'superconductor'
}

def trouver_journal_dir():
    if config and hasattr(config, 'get'):
        jdir = config.get('journaldir')
        if jdir: return jdir
    if platform.system() == 'Windows':
        return os.path.join(os.environ.get('USERPROFILE', ''), 'Saved Games', 'Frontier Developments', 'Elite Dangerous')
    return None

def lire_cle():
    try:
        chemin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "edteam_key.txt")
        if os.path.exists(chemin):
            with open(chemin, 'r') as f:
                return f.read().strip()
    except: pass
    return ""

def sauvegarder_cle(cle):
    try:
        chemin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "edteam_key.txt")
        with open(chemin, 'w') as f:
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

def get_user_id():
    global cached_user_id, invalid_api_key
    if cached_user_id: 
        return cached_user_id
    if invalid_api_key:
        return None
        
    cle = lire_cle()
    if not cle:
        mettre_a_jour_interface(">_ BLOQUÉ : AUCUNE CLÉ DANS EDMC", "red")
        return None
    try:
        res = requests.get(f"{SUPABASE_URL}/rest/v1/profils?select=user_id", headers=get_headers(), timeout=5)
        if res.status_code == 200:
            data = res.json()
            if len(data) > 0:
                cached_user_id = data[0].get('user_id')
                return cached_user_id
            else:
                mettre_a_jour_interface(">_ BLOQUÉ : CLÉ NON RECONNUE", "red")
                invalid_api_key = True
                return None
        else:
            mettre_a_jour_interface(f">_ ERREUR BDD : {res.status_code}", "red")
            if res.status_code in [401, 403]:
                invalid_api_key = True
            return None
    except:
        mettre_a_jour_interface(">_ BLOQUÉ : ERREUR RÉSEAU", "red")
    return None

def obtenir_infos_pilote(force=False):
    """Charge et met en cache pour 10 minutes les allégeances et la faction alliée."""
    global pilot_cache
    maintenant = time.time()
    if not force and pilot_cache['user_id'] and (maintenant - pilot_cache['ts'] < 600):
        return pilot_cache

    uid = get_user_id()
    if not uid: return pilot_cache

    try:
        res = requests.get(
            f"{SUPABASE_URL}/rest/v1/profils?user_id=eq.{uid}&select=user_id,escadron_id,faction_choisie,puissance_nom,puissance_rang,puissance_merites_cycle,puissance_merites_total",
            headers=get_headers(),
            timeout=5
        )
        if res.status_code == 200 and res.json():
            row = res.json()[0]
            pilot_cache['user_id'] = uid
            pilot_cache['escadron_id'] = row.get('escadron_id') or ''
            pilot_cache['faction_choisie'] = row.get('faction_choisie') or ''
            pilot_cache['puissance_nom'] = row.get('puissance_nom')
            pilot_cache['puissance_rang'] = row.get('puissance_rang', 0)
            pilot_cache['puissance_merites_cycle'] = row.get('puissance_merites_cycle', 0)
            pilot_cache['puissance_merites_total'] = row.get('puissance_merites_total', 0)
            pilot_cache['ts'] = maintenant

            faction_trouvee = None
            if pilot_cache['escadron_id']:
                esc_id = pilot_cache['escadron_id'].strip()
                res_esc = requests.get(
                    f"{SUPABASE_URL}/rest/v1/escadrons?id=eq.{urllib.parse.quote(esc_id)}&select=nom_faction_officielle",
                    headers=get_headers(),
                    timeout=5
                )
                if res_esc.status_code == 200 and res_esc.json():
                    faction_trouvee = res_esc.json()[0].get('nom_faction_officielle')

            if not faction_trouvee and pilot_cache['faction_choisie']:
                faction_trouvee = pilot_cache['faction_choisie']

            pilot_cache['faction_alliee'] = faction_trouvee
    except Exception as e:
        logging.error(f"[SYS_EDTEAM] Erreur cache pilote : {e}")

    return pilot_cache

def patch_parametres(payload):
    uid = get_user_id()
    if not uid: return
    try:
        res = requests.get(f"{SUPABASE_URL}/rest/v1/radar_commercial?select=id,station_name&target_commodity=eq.PARAM_UPDATE&user_id=eq.{uid}", headers=get_headers(), timeout=5)
        if res.status_code == 200 and len(res.json()) > 0:
            row = res.json()[0]
            try: existing = json.loads(row.get('station_name', '{}'))
            except: existing = {}
            existing.update(payload)
            requests.patch(f"{SUPABASE_URL}/rest/v1/radar_commercial?id=eq.{row['id']}", headers=get_headers(), json={"station_name": json.dumps(existing)}, timeout=5)
        else:
            data = {"user_id": uid, "system_name": "SYS_CORE", "station_name": json.dumps(payload), "target_commodity": "PARAM_UPDATE", "type_operation": "STATUS", "prix_unitaire": 0, "volume_disponible": 0, "distance": 0, "prix_moyen": 0}
            requests.post(f"{SUPABASE_URL}/rest/v1/radar_commercial", headers=get_headers(), json=data, timeout=5)
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
        res = requests.get(f"{SUPABASE_URL}/rest/v1/radar_commercial?select=id&target_commodity=eq.{target}&user_id=eq.{uid}", headers=get_headers(), timeout=5)
        if res.status_code == 200 and len(res.json()) > 0:
            requests.patch(f"{SUPABASE_URL}/rest/v1/radar_commercial?id=eq.{res.json()[0]['id']}", headers=get_headers(), json=payload, timeout=5)
        else:
            requests.post(f"{SUPABASE_URL}/rest/v1/radar_commercial", headers=get_headers(), json=payload, timeout=5)
    except: pass

def maj_generique_batch(items):
    """Variante groupee de maj_generique_global : un seul GET pour verifier l'existence
    de plusieurs lignes (target_commodity=in.(...)) au lieu d'un GET par thread.
    items : liste de dicts {target, system, station, type_op, val, vol}."""
    uid = get_user_id()
    if not uid or not items: return
    try:
        in_list = ",".join(it["target"] for it in items)
        res = requests.get(f"{SUPABASE_URL}/rest/v1/radar_commercial?select=id,target_commodity&target_commodity=in.({in_list})&user_id=eq.{uid}", headers=get_headers(), timeout=5)
        existants = {}
        if res.status_code == 200:
            for row in res.json():
                existants[row['target_commodity']] = row['id']

        for it in items:
            payload = {
                "user_id": uid,
                "system_name": str(it["system"]),
                "station_name": str(it["station"]),
                "target_commodity": it["target"],
                "type_operation": it["type_op"],
                "prix_unitaire": int(it.get("val", 0)),
                "volume_disponible": int(it.get("vol", 0)),
                "distance": 0,
                "prix_moyen": 0
            }
            if it["target"] in existants:
                requests.patch(f"{SUPABASE_URL}/rest/v1/radar_commercial?id=eq.{existants[it['target']]}", headers=get_headers(), json=payload, timeout=5)
            else:
                requests.post(f"{SUPABASE_URL}/rest/v1/radar_commercial", headers=get_headers(), json=payload, timeout=5)
    except: pass

def maj_powerplay(puissance, rang, merites_cycle, merites_total):
    """Met à jour le statut Powerplay 2.0 uniquement en cas de changement effectif."""
    uid = get_user_id()
    if not uid: return
    infos = obtenir_infos_pilote()
    
    if (infos.get('puissance_nom') == puissance and 
        infos.get('puissance_rang') == rang and 
        abs(infos.get('puissance_merites_cycle', 0) - merites_cycle) < 5):
        return

    infos['puissance_nom'] = puissance
    infos['puissance_rang'] = rang
    infos['puissance_merites_cycle'] = merites_cycle
    infos['puissance_merites_total'] = merites_total

    # NE PAS ecrire directement dans 'profils' (aucune policy RLS UPDATE dessus,
    # l'ecriture serait bloquee silencieusement). On passe par 'radar_commercial'
    # comme les autres stats (finances, rangs) : un trigger cote serveur
    # (automatisation_finances_radar) repercute ensuite vers profils.puissance_*.
    payload = {
        "user_id": uid,
        "system_name": "QG_DATA",
        "station_name": str(puissance) if puissance else '',
        "target_commodity": "QG_POWERPLAY",
        "type_operation": "INFO",
        "prix_unitaire": int(rang),
        "volume_disponible": int(merites_cycle),
        "prix_moyen": float(merites_total),
        "distance": 0
    }
    try:
        res = requests.get(f"{SUPABASE_URL}/rest/v1/radar_commercial?select=id&target_commodity=eq.QG_POWERPLAY&user_id=eq.{uid}", headers=get_headers(), timeout=5)
        if res.status_code == 200 and len(res.json()) > 0:
            requests.patch(f"{SUPABASE_URL}/rest/v1/radar_commercial?id=eq.{res.json()[0]['id']}", headers=get_headers(), json=payload, timeout=5)
        else:
            requests.post(f"{SUPABASE_URL}/rest/v1/radar_commercial", headers=get_headers(), json=payload, timeout=5)
    except: pass

def notifier_journal_activite(type_act, details_txt, couleur_txt="#00F0FF"):
    """Injection d'une brève marquante dans le QG (0 octet d'Egress via return=minimal)."""
    uid = get_user_id()
    if not uid: return
    infos = obtenir_infos_pilote()
    esc_id = infos.get('escadron_id') or "INDEPENDANT"
    cmdr = cmdr_actuel or "CMDR"
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/journal_activite",
            headers=get_headers(),
            json={
                "escadron_id": esc_id,
                "user_id": uid,
                "cmdr_nom": cmdr,
                "type_action": type_act,
                "details": details_txt,
                "couleur": couleur_txt
            },
            timeout=5
        )
    except: pass

def obtenir_parametres():
    cle = lire_cle()
    try:
        res = requests.get(f"{SUPABASE_URL}/rest/v1/radar_commercial?select=system_name,station_name&target_commodity=eq.APP_PARAMS", headers=get_headers(), timeout=5)
        if res.status_code == 200:
            for row in res.json():
                if str(row.get('system_name')).strip() == str(cle).strip():
                    return json.loads(row.get('station_name', '{}'))
    except: pass
    return {"cibles_achat": ["Gold"], "moyennes_galactiques": {}, "mode_flotte": "FC"}

def obtenir_moyennes_galactiques():
    try:
        res = requests.get(f"{SUPABASE_URL}/rest/v1/moyennes_galactiques?select=marchandise,prix_moyen", headers=get_headers(), timeout=5)
        if res.status_code == 200:
            return {m.get('marchandise'): m.get('prix_moyen', 0) for m in res.json()}
    except: pass
    return {}

def recuperer_dernier_systeme_connu():
    global systeme_actuel
    if systeme_actuel != "SYSTÈME INCONNU" and systeme_actuel != "Sol": return systeme_actuel
    try:
        res = requests.get(f"{SUPABASE_URL}/rest/v1/radar_commercial?select=system_name&target_commodity=eq.SYSTEM_STATUS", headers=get_headers(), timeout=5)
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
        except: pass
        time.sleep(60)

def check_for_updates():
    global status_label
    time.sleep(3)
    try:
        url_version = "https://raw.githubusercontent.com/wopygm/SYS_EDTEAM_Plugin/main/version.txt"
        req = urllib.request.Request(url_version, headers={'User-Agent': 'EDMC-Plugin-Updater'})
        with urllib.request.urlopen(req) as response:
            latest_version = response.read().decode('utf-8').strip()

        if latest_version != PLUGIN_VERSION:
            try: status_label.config(text=f"Téléchargement MAJ v{latest_version}...", fg="orange")
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
                        target_path = os.path.join(this_dir, os.path.join(*parts[1:]))
                        os.makedirs(os.path.dirname(target_path), exist_ok=True)
                        with z.open(file_info) as source, open(target_path, "wb") as target:
                            target.write(source.read())

            try: status_label.config(text=f"MAJ v{latest_version} OK ! Redémarrez EDMC.", fg="#00FF66")
            except: pass
    except Exception as e:
        logging.error(f"SYS_EDTEAM : Erreur maj : {e}")

def plugin_start3(plugin_dir):
    threading.Thread(target=heartbeat_loop, daemon=True).start()
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
    try:
        h = get_headers()
        h["Prefer"] = "resolution=merge-duplicates"
        payloads = []
        now_iso = datetime.now(timezone.utc).isoformat()
        
        for c in conflits:
            if str(c.get("Status", "")).lower() == "pending": continue
            f1, f2 = c.get("Faction1", {}), c.get("Faction2", {})
            f1_nom, f2_nom = f1.get("Name", ""), f2.get("Name", "")
            if not f1_nom or not f2_nom: continue
                
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
            res = requests.post(f"{SUPABASE_URL}/rest/v1/conflits_systemes?on_conflict=id", headers=h, json=payloads, timeout=5)
            if res.status_code in [200, 201, 204]:
                mettre_a_jour_interface(f">_ BGS : ÉCLAIREUR ({systeme.upper()})", "#00FF66")
    except Exception as e:
        logging.error(f"[SYS_EDTEAM] Erreur synchro conflits : {e}")

# ==========================================
# ROUTEUR PRINCIPAL
# ==========================================
def journal_entry(cmdr, is_beta, system, station, entry, state):
    global systeme_actuel, cmdr_actuel
    cmdr_actuel = cmdr
    if system and system != systeme_actuel: systeme_actuel = system
    event = entry.get('event')

    # Suivi CZ
    if not hasattr(journal_entry, 'cz_cache'):
        journal_entry.cz_cache = {'intensity': 'S', 'points': 1, 'faction': '', 'won': False}

    if event in ['SupercruiseEntry', 'FSDJump', 'CarrierJump', 'Location']:
        journal_entry.cz_cache = {'intensity': 'S', 'points': 1, 'faction': '', 'won': False}

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

    elif event == 'FactionKillBond':
        faction_alliee = entry.get('AwardingFaction')
        if faction_alliee:
            journal_entry.cz_cache['faction'] = faction_alliee
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
            threading.Thread(target=maj_generique_global, args=("SYSTEM_STATUS", systeme_actuel, "JUMP", "INFO")).start()
            
        if event in ['FSDJump', 'Location']:
            factions = entry.get('Factions', [])
            reps = {f['Name']: f['MyReputation'] for f in factions if 'MyReputation' in f}
            if reps:
                threading.Thread(target=maj_generique_global, args=("QG_REPUTATIONS", "QG_DATA", json.dumps(reps), "INFO")).start()

            # Capture automatique de la faction officielle de l'escadron (visible uniquement
            # dans les systemes ou cette faction est active, via le flag SquadronFaction:true)
            global dernier_faction_escadron_envoyee
            faction_escadron = next((f.get('Name') for f in factions if f.get('SquadronFaction') is True), None)
            if faction_escadron and faction_escadron != dernier_faction_escadron_envoyee:
                infos_pilote = obtenir_infos_pilote()
                esc_id = infos_pilote.get('escadron_id', '')
                if esc_id:
                    dernier_faction_escadron_envoyee = faction_escadron
                    payload_esc = json.dumps({"escadron_id": esc_id, "faction": faction_escadron})
                    threading.Thread(target=maj_generique_global, args=("QG_SQUADRON_FACTION", "QG_DATA", payload_esc, "INFO")).start()

            conflits = entry.get('Conflicts', [])
            sys_nom = entry.get('StarSystem') or systeme_actuel
            if conflits and sys_nom:
                threading.Thread(target=sync_conflits_supabase, args=(sys_nom, conflits), daemon=True).start()

        threading.Thread(target=maj_generique_global, args=("TARGETED_CMDR", "SYS_CORE", "LOST", "INFO")).start()

    elif event == 'LoadGame':
        ship_name = entry.get('ShipName', 'VAISSEAU TACTIQUE')
        ship_model = entry.get('Ship_Localised', entry.get('Ship', 'INCONNU')).title()
        possede_fc = entry.get('FleetCarrierID') is not None
        
        threading.Thread(target=patch_parametres, args=({"vaisseau_nom": ship_name.upper(), "vaisseau_modele": ship_model, "possede_fc": possede_fc},)).start()
        threading.Thread(target=maj_generique_global, args=("CMDR_NAME", "SYS_CORE", cmdr, "STATUS")).start()
        threading.Thread(target=maj_generique_global, args=("QG_ACTIVE_SHIP_ID", "QG_DATA", str(entry.get('ShipID', '0')), "INFO")).start()
        threading.Thread(target=obtenir_infos_pilote, args=(True,)).start()

    # ==========================================
    # POWERPLAY 2.0
    # ==========================================
    elif event == 'Powerplay':
        power = entry.get('Power')
        rank = entry.get('Rank', 0)
        merits_cycle = entry.get('Merits', 0)
        merits_total = entry.get('TotalMerits', merits_cycle)
        if power:
            threading.Thread(target=maj_powerplay, args=(power, rank, merits_cycle, merits_total)).start()

    elif event in ['PowerplayLeave', 'PowerplayDefect']:
        new_power = entry.get('NewPower') if event == 'PowerplayDefect' else None
        threading.Thread(target=maj_powerplay, args=(new_power, 0, 0, 0)).start()

    # ==========================================
    # FLEET CARRIER (solde)
    # ==========================================
    elif event == 'CarrierStats':
        solde_fc = entry.get('Finance', {}).get('CarrierBalance')
        if solde_fc is not None:
            threading.Thread(target=maj_generique_global, args=("FC_BALANCE", "FINANCE", "FC", "FINANCE", solde_fc)).start()

    # ==========================================
    # INTERCEPTION ESCADRON / INDÉPENDANT
    # ==========================================
    elif event == 'SquadronStartup':
        squad_name = entry.get('SquadronName', '')
        squad_rank = entry.get('CurrentRank', 0)
        
        if squad_name:
            payload = json.dumps({"nom": squad_name, "rank": squad_rank})
            threading.Thread(target=maj_generique_global, args=("SQUADRON_INFO", "QG_DATA", payload, "INFO")).start()
            mettre_a_jour_interface(f">_ ESCADRON : {squad_name}", "#00FF66")
        else:
            payload = json.dumps({"nom": "", "rank": 0, "independant": True})
            threading.Thread(target=maj_generique_global, args=("SQUADRON_INFO", "QG_DATA", payload, "INFO")).start()
            mettre_a_jour_interface(">_ STATUT : INDÉPENDANT", "#00F0FF")
        threading.Thread(target=obtenir_infos_pilote, args=(True,)).start()

    # CIBLAGE TACTIQUE
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
                if dernier_etat_cible != "LOST":
                    dernier_etat_cible = "LOST"
                    threading.Thread(target=maj_generique_global, args=("TARGETED_CMDR", "SYS_CORE", "LOST", "INFO")).start()
        else:
            if dernier_etat_cible != "LOST":
                dernier_etat_cible = "LOST"
                threading.Thread(target=maj_generique_global, args=("TARGETED_CMDR", "SYS_CORE", "LOST", "INFO")).start()

    if entry.get('Notoriety') is not None:
        threading.Thread(target=maj_generique_global, args=("QG_NOTORIETE", "QG_DATA", "NOTORIETE", "INFO", entry.get('Notoriety'))).start()

    if event in ['Rank', 'Progress']:
        prefix = "RANK" if event == 'Rank' else "PROG"
        items = []
        for r in ['Combat', 'Trade', 'Explore', 'Federation', 'Empire', 'Exobiologist']:
            if entry.get(r) is not None:
                items.append({"target": f"QG_{prefix}_{r.upper()[:6]}", "system": "QG_DATA", "station": r.upper(), "type_op": "INFO", "val": entry.get(r)})
        val_mercenary = entry.get('Soldier') if entry.get('Soldier') is not None else entry.get('Mercenary')
        if val_mercenary is not None:
            items.append({"target": f"QG_{prefix}_MERCEN", "system": "QG_DATA", "station": "MERCENARY", "type_op": "INFO", "val": val_mercenary})
        if items:
            # Un seul thread, un seul GET groupe (au lieu de jusqu'a 7 threads x 1 GET chacun)
            threading.Thread(target=maj_generique_batch, args=(items,)).start()

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
        crime = entry.get('Crime', {})
        if crime.get('Notoriety') is not None:
            threading.Thread(target=maj_generique_global, args=("QG_NOTORIETE", "QG_DATA", "NOTORIETE", "INFO", crime.get('Notoriety'))).start()

    # ==========================================
    # MODULE BGS, SOUTIEN LIBRE & COLONISATION
    # ==========================================
    if not hasattr(journal_entry, 'bgs_cache'): 
        journal_entry.bgs_cache = {'missions': {}, 'station_faction': '', 'ordres': [], 'ordres_ts': 0}
    
    if entry.get('event') in ['Docked', 'Location', 'ApproachSettlement']:
        faction_info = entry.get('StationFaction') or entry.get('SystemFaction')
        if isinstance(faction_info, dict):
            journal_entry.bgs_cache['station_faction'] = faction_info.get('Name', '')
        elif isinstance(faction_info, str):
            journal_entry.bgs_cache['station_faction'] = faction_info

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

    bgs_events = [
        'MissionCompleted', 'MissionFailed', 'MissionAbandoned', 
        'MarketSell', 'RedeemVoucher', 'SellExplorationData', 
        'MultiSellExplorationData', 'SellOrganicData', 'CommitCrime', 
        'CollectItem', 'CollectItems', 'DataDownloaded', 'BackpackChange', 
        'Music', 'ReceiveText', 'Embark', 'BookDropship', 'CargoDepot'
    ]

    if entry.get('event') in bgs_events:
        def process_bgs_complet():
            user_id = get_user_id()
            if not user_id: return
            
            infos_pilote = obtenir_infos_pilote()
            faction_alliee = infos_pilote.get('faction_alliee', '')
            
            evt = entry.get('event')
            actions = []
            
            f_station = journal_entry.bgs_cache.get('station_faction', '')
            if not f_station and state:
                st_state = state.get('StationFaction')
                if isinstance(st_state, dict): f_station = st_state.get('Name', '')
                elif isinstance(st_state, str): f_station = st_state

            victoire_cz = False
            if evt == 'Music':
                track = str(entry.get('MusicTrack', '')).lower()
                if 'conflictzone' in track and ('win' in track or 'victory' in track):
                    victoire_cz = True
            elif evt == 'ReceiveText':
                msg = str(entry.get('Message', '')).lower()
                if '$military_passthrough' in msg or 'warzone_pointrace_win' in msg:
                    victoire_cz = True
            elif evt in ['Embark', 'BookDropship']:
                victoire_cz = True

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

            elif evt in ['MissionFailed', 'MissionAbandoned']:
                m_id = entry.get('MissionID')
                m_info = journal_entry.bgs_cache.get('missions', {}).get(m_id, {})
                if not (isinstance(m_info, dict) and m_info.get('deja_compte')):
                    if isinstance(m_info, dict): m_info['deja_compte'] = True
                    f = m_info.get('faction') if isinstance(m_info, dict) else (m_info or '')
                    s = m_info.get('system') if isinstance(m_info, dict) else systeme_actuel
                    if f: 
                        actions.append({"faction": f, "type": "ECHECS", "valeur": 1, "system": s, "is_combat": False})

            elif evt == 'RedeemVoucher' and entry.get('Type') in ['bounty', 'CombatBond']:
                for f_info in entry.get('Factions', []): 
                    actions.append({"faction": f_info.get('Faction', ''), "type": "SECURITE", "valeur": f_info.get('Amount', 0), "system": systeme_actuel, "is_combat": True})
                if entry.get('Faction') and entry.get('Amount'): 
                    actions.append({"faction": entry.get('Faction', ''), "type": "SECURITE", "valeur": entry.get('Amount', 0), "system": systeme_actuel, "is_combat": True})

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

            # Commerce & Matériaux de colonisation
            elif evt == 'MarketSell':
                val = entry.get('TotalSale', 0)
                count = entry.get('Count', 0)
                mat_raw = str(entry.get('Type', '')).lower().replace(' ', '').replace('$', '').replace('_name;', '')
                mat_nom = entry.get('Type_Localised') or entry.get('Type', 'Matériaux')
                
                # Détection colonisation au tonnage
                if count > 0 and mat_raw in MATERIAUX_COLONISATION and f_station:
                    actions.append({
                        "faction": f_station, 
                        "type": "COLONISATION", 
                        "valeur": count, 
                        "details": str(mat_nom), 
                        "system": systeme_actuel, 
                        "is_combat": False
                    })

                if val > 0: 
                    actions.append({
                        "faction": f_station, 
                        "type": "CONTREBANDE" if (entry.get('Stolen', False) or entry.get('IllegalGoods', False)) else "ECONOMIE", 
                        "valeur": val, 
                        "system": systeme_actuel, 
                        "is_combat": False
                    })

            # Déchargement de fret / dépôts de chantiers coloniaux
            elif evt == 'CargoDepot' and entry.get('UpdateType') == 'Deliver':
                count = entry.get('Count', 0)
                mat_nom = entry.get('CargoType_Localised') or entry.get('CargoType', 'Fret colonial')
                if count > 0 and f_station:
                    actions.append({
                        "faction": f_station,
                        "type": "COLONISATION",
                        "valeur": count,
                        "details": str(mat_nom),
                        "system": systeme_actuel,
                        "is_combat": False
                    })

            elif evt == 'CommitCrime' and 'murder' in str(entry.get('CrimeType', '')).lower():
                faction_victime = entry.get('Faction', '') or f_station
                if faction_victime: 
                    actions.append({"faction": faction_victime, "type": "MEURTRES", "valeur": 1, "system": systeme_actuel, "is_combat": True})

            elif evt in ['CollectItem', 'CollectItems']:
                if 'powerregulator' in entry.get('Name', '').lower() and f_station:
                    actions.append({"faction": f_station, "type": "VOLS", "valeur": 1, "system": systeme_actuel, "is_combat": False})

            elif evt == 'DataDownloaded' and f_station:
                actions.append({"faction": f_station, "type": "PIRATAGE", "valeur": 1, "system": systeme_actuel, "is_combat": False})

            elif evt == 'BackpackChange':
                for ajout in entry.get('Added', []):
                    nom_ajout = ajout.get('Name', '').lower()
                    if 'powerregulator' in nom_ajout and f_station:
                        actions.append({"faction": f_station, "type": "VOLS", "valeur": ajout.get('Count', 1), "system": systeme_actuel, "is_combat": False})
                    elif ajout.get('Type') == 'Data' and f_station:
                        actions.append({"faction": f_station, "type": "PIRATAGE", "valeur": ajout.get('Count', 1), "system": systeme_actuel, "is_combat": False})
            
            # Transmission filtrée (Egress Shield)
            try:
                maintenant = time.time()
                if maintenant - journal_entry.bgs_cache.get('ordres_ts', 0) > 300:
                    res_ordres = requests.get(f"{SUPABASE_URL}/rest/v1/ordres_bgs?statut=eq.ACTIF&select=id,faction_cible,systeme_cible,type_ordre", headers=get_headers(), timeout=5)
                    if res_ordres.status_code == 200:
                        journal_entry.bgs_cache['ordres'] = res_ordres.json()
                        journal_entry.bgs_cache['ordres_ts'] = maintenant

                ordres_actifs = journal_entry.bgs_cache.get('ordres', [])

                for action in actions:
                    sys_cible_action = action.get('system', systeme_actuel).strip().lower()
                    act_faction = str(action.get('faction', '')).strip().lower()
                    ordre_valide = None

                    if action['type'] == 'CZ_VICTOIRES' and act_faction:
                        ordre_valide = next((o for o in ordres_actifs if o.get('type_ordre') == 'GUERRE' and o.get('systeme_cible', '').strip().lower() == sys_cible_action and o.get('faction_cible', '').strip().lower() == act_faction), None)
                    elif act_faction:
                        ordre_valide = next((o for o in ordres_actifs if o.get('faction_cible', '').strip().lower() == act_faction and o.get('systeme_cible', '').strip().lower() == sys_cible_action), None)

                    # Est-ce un soutien libre pour notre faction alliée ?
                    est_action_libre = False
                    if not ordre_valide and faction_alliee and act_faction == faction_alliee.strip().lower():
                        est_action_libre = True

                    if ordre_valide or est_action_libre:
                        if ordre_valide:
                            if ordre_valide.get('type_ordre') == 'ELECTION' and action.get('is_combat', False): continue
                            if ordre_valide.get('type_ordre') == 'GUERRE' and action['type'] == 'MISSIONS':
                                if action.get('is_combat', False):
                                    dest = action.get('dest_system', sys_cible_action).strip().lower()
                                    if dest != ordre_valide.get('systeme_cible', '').strip().lower() or action.get('is_pirate', False): continue
                                valeur_finale = 1
                            else:
                                valeur_finale = action['valeur']
                        else:
                            valeur_finale = action['valeur']

                        payload = {
                            "user_id": user_id, 
                            "ordre_id": ordre_valide['id'] if ordre_valide else None, 
                            "type_action": action['type'], 
                            "valeur": valeur_finale, 
                            "systeme": action.get('system', systeme_actuel),
                            "faction": action.get('faction', ''),
                            "details": action.get('details', None),
                            "date_action": entry.get('timestamp', datetime.now(timezone.utc).isoformat())
                        }

                        res_post = requests.post(f"{SUPABASE_URL}/rest/v1/efforts_bgs", headers=get_headers(), json=payload, timeout=5)
                        
                        if res_post.status_code in [200, 201, 204]:
                            if ordre_valide:
                                mettre_a_jour_interface(f">_ BGS [ORDRE #{ordre_valide['id']}] : {action['type']} +{int(valeur_finale)}", "#00FF66")
                            elif action['type'] == 'COLONISATION':
                                mettre_a_jour_interface(f">_ BÂTISSEUR : +{int(valeur_finale)} T DE FRET", "#FFD700")
                            else:
                                mettre_a_jour_interface(f">_ SOUTIEN LIBRE : {action['type']} (+{int(valeur_finale)})", "#00FF66")

                            # Brèves QG narratives (seuils sélectifs)
                            if action['type'] == 'COLONISATION' and valeur_finale >= 200:
                                threading.Thread(target=notifier_journal_activite, args=("COLONISATION", f"Livraison logistique : +{int(valeur_finale)} t ({action.get('details', 'Matériaux')})", "#FFD700")).start()
                            elif action['type'] == 'CZ_VICTOIRES':
                                threading.Thread(target=notifier_journal_activite, args=("COMBAT", f"Victoire en zone de conflit ({action.get('intensite', 'S')}) à {systeme_actuel}", "#FF3333")).start()
                            elif action['type'] == 'MISSIONS' and valeur_finale >= 3:
                                threading.Thread(target=notifier_journal_activite, args=("SOUTIEN", f"Mission navale validée (+{int(valeur_finale)} INF) pour la flotte", "#00FF66")).start()
                            elif action['type'] == 'SCIENCE' and valeur_finale >= 5000000:
                                threading.Thread(target=notifier_journal_activite, args=("SCIENCE", f"Données stellaires / bio versées ({valeur_finale/1000000:.1f}M CR)", "#00F0FF")).start()
                            elif action['type'] == 'SECURITE' and valeur_finale >= 2000000:
                                threading.Thread(target=notifier_journal_activite, args=("SECURITE", f"Primes et obligations encaissées ({valeur_finale/1000000:.1f}M CR)", "#FF6600")).start()
                            elif action['type'] == 'ECONOMIE' and valeur_finale >= 5000000:
                                threading.Thread(target=notifier_journal_activite, args=("COMMERCE", f"Apport commercial de grande envergure ({valeur_finale/1000000:.1f}M CR)", "#00F0FF")).start()
                        else:
                            mettre_a_jour_interface(f">_ REJET EFFORT : {res_post.status_code}", "red")
                    else:
                        # Action pour une faction tierce sans intérêt : silence radio, 0 requête
                        pass
            except Exception as e:
                logging.error(f"[SYS_EDTEAM] Erreur transmission BGS : {e}")

        threading.Thread(target=process_bgs_complet).start()