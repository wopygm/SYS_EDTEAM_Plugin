import os
import urllib.request
import zipfile
import io
import logging
import time
import math
import json
import re
import hashlib
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
PLUGIN_VERSION = "2.6"

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
    global cached_user_id, invalid_api_key
    try:
        chemin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "edteam_key.txt")
        with open(chemin, 'w') as f:
            f.write(cle.strip())
    except: pass
    # Nouvelle cle saisie : on repart de zero (sinon une cle refusee bloquait le plugin jusqu'au prochain redemarrage d'EDMC)
    cached_user_id = None
    invalid_api_key = False

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

# ==========================================
# POWERPLAY : RELEVE DIRECT (v2.4)
# Le jeu ecrit PowerplayMerits a chaque gain (TotalMerits). On garde le dernier total en memoire et on l'envoie
# en UNE requete (fonction serveur pp_releve : aucune lecture, aucune ligne radar), au plus toutes les 5 minutes.
# Au demarrage, on relit les journaux recents pour rattraper ce qui a ete joue sans EDMC, cycle par cycle.
# ==========================================
PP_INTERVALLE_ENVOI = 300
pp_etat = {'puissance': None, 'rang': 0, 'total': 0, 'a_envoyer': False, 'dernier_envoi': 0, 'connu': False}
pp_historique_attente = []
pp_rpc_absente = False
pp_verrou = threading.Lock()

def pp_cloture_cycle(dt):
    """Jeudi (UTC) qui cloture le cycle Powerplay contenant dt (le cycle change le jeudi 07:00 UTC). Meme regle que cloture_cycle_pp() en base."""
    decale = (dt - timedelta(hours=7)).date()
    return decale + timedelta(days=((3 - decale.weekday()) % 7) or 7)

def pp_enregistrer(puissance, rang=None, total=None):
    """Memorise le dernier etat Powerplay vu dans le journal. L'envoi se fait plus tard, jamais a chaque evenement."""
    with pp_verrou:
        nouveau = (puissance,
                   pp_etat['rang'] if rang is None else rang,
                   pp_etat['total'] if total is None else total)
        if not pp_etat['connu'] or nouveau != (pp_etat['puissance'], pp_etat['rang'], pp_etat['total']):
            pp_etat['puissance'], pp_etat['rang'], pp_etat['total'] = nouveau
            pp_etat['a_envoyer'] = True
        pp_etat['connu'] = True

def pp_envoyer(force=False):
    """Envoie l'etat Powerplay (et l'historique en attente) si necessaire. Limite : 1 envoi par PP_INTERVALLE_ENVOI, sauf force."""
    global pp_historique_attente, pp_rpc_absente
    with pp_verrou:
        if not pp_etat['connu']: return
        if not pp_etat['a_envoyer'] and not pp_historique_attente: return
        if not force and time.time() - pp_etat['dernier_envoi'] < PP_INTERVALLE_ENVOI: return
        etat = dict(pp_etat)
        historique = pp_historique_attente
        pp_historique_attente = []
        pp_etat['a_envoyer'] = False
        pp_etat['dernier_envoi'] = time.time()

    def _rearmer():
        global pp_historique_attente
        with pp_verrou:
            pp_etat['a_envoyer'] = True
            if historique: pp_historique_attente = historique + pp_historique_attente

    if not get_user_id():
        _rearmer()
        return

    if not pp_rpc_absente:
        try:
            res = requests.post(
                f"{SUPABASE_URL}/rest/v1/rpc/pp_releve",
                headers=get_headers(),
                json={"p_puissance": etat['puissance'] or '', "p_rang": int(etat['rang'] or 0),
                      "p_total": int(etat['total'] or 0), "p_historique": historique},
                timeout=8
            )
            if res.status_code in (200, 201, 204): return
            if res.status_code == 404:
                pp_rpc_absente = True   # fonction pas encore creee en base : repli sur l'ancienne voie (v2.3)
            else:
                _rearmer()
                return
        except:
            _rearmer()
            return

    # Repli v2.3 (fonction serveur absente)
    if etat['puissance']:
        maj_powerplay(etat['puissance'], etat['rang'], etat['total'], etat['total'])
    else:
        maj_powerplay(None, 0, 0, 0)

def pp_scanner_journaux(jours=21):
    """Relit les journaux recents : rattrape les merites gagnes sans EDMC (dernier total de chaque cycle, horodate)."""
    global pp_historique_attente
    try:
        jdir = trouver_journal_dir()
        if not jdir or not os.path.isdir(jdir): return
        limite = time.time() - jours * 86400
        fichiers = sorted(f for f in os.listdir(jdir)
                          if f.startswith('Journal.') and f.endswith('.log')
                          and os.path.getmtime(os.path.join(jdir, f)) >= limite)
        nom, rang, total = None, 0, None
        par_cycle = {}
        for nom_fichier in fichiers:
            with open(os.path.join(jdir, nom_fichier), 'r', encoding='utf-8', errors='ignore') as f:
                for ligne in f:
                    if '"event":"Powerplay' not in ligne: continue
                    try: e = json.loads(ligne)
                    except: continue
                    ev = e.get('event')
                    if ev == 'Powerplay':
                        nom, rang, total = e.get('Power'), e.get('Rank', rang), e.get('Merits')
                    elif ev == 'PowerplayMerits':
                        nom, total = e.get('Power', nom), e.get('TotalMerits')
                    elif ev == 'PowerplayRank':
                        rang = e.get('Rank', rang)
                        continue
                    elif ev == 'PowerplayLeave':
                        nom, rang, total = None, 0, None
                        continue
                    elif ev == 'PowerplayDefect':
                        nom, rang, total = e.get('NewPower'), 0, 0
                        continue
                    else:
                        continue
                    if nom and total is not None:
                        try: dt = datetime.strptime(e.get('timestamp', ''), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                        except: continue
                        par_cycle[pp_cloture_cycle(dt)] = {"ts": e.get('timestamp'), "puissance": nom, "rang": int(rang or 0), "total": int(total)}
        if not nom or total is None: return

        with pp_verrou:
            pp_historique_attente = list(par_cycle.values())
        if not pp_etat['connu']:   # un evenement en direct est plus recent que la relecture : il prime
            pp_enregistrer(nom, int(rang or 0), int(total))
        pp_envoyer(force=True)
    except Exception as e:
        logging.error(f"[SYS_EDTEAM] Erreur relecture Powerplay : {e}")

# ==========================================
# COLONISATION : chantiers, livraisons et systemes revendiques, releves automatiquement dans les journaux de jeu.
# Un evenement est traite par col_traiter_evenement() (en direct comme a la relecture, aucun appel reseau) ;
# l'envoi (col_envoyer) regroupe : UNE ligne par chantier et par envoi, au plus un envoi toutes les COL_INTERVALLE_ENVOI secondes.
# Au premier lancement, tous les journaux sont relus une fois (historique) ; ensuite seulement ce qui date depuis le dernier envoi.
# Le serveur (fonction col_releve, script SQL 21) ignore les pilotes sans escadron et les doublons.
# ==========================================
COL_INTERVALLE_ENVOI = 180
COL_LOT_CHANTIERS = 40
COL_LOT_SYSTEMES = 40
COL_LOT_LIVRAISONS = 400
COL_LOT_INSTALLATIONS = 100
COL_MARQUEUR = 'col_marqueur.json'
COL_ECHECS_MAX = 5

col_verrou = threading.Lock()
col_attente = {'chantiers': {}, 'systemes': {}, 'livraisons': [], 'installations': {}}
col_stations = {}      # MarketID -> {'station', 'type', 'systeme', 'adresse'} (vu a l'amarrage)
col_positions = {}     # SystemAddress -> {'nom', 'x', 'y', 'z'} (vu aux sauts)
col_courant = {'adresse': None, 'nom': None}
col_etat = {'dernier_envoi': 0, 'rpc_absente': False, 'dernier_ts': '', 'echecs': 0, 'inst_absente': False}
col_faction = {'nom': None}   # faction de l'escadron (flag SquadronFaction:true vu aux sauts)
col_inst_envoyees = set()     # MarketID d'installations deja envoyees pendant cette session

def col_chemin(nom):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), nom)

def col_trace(msg):
    """Diagnostic : n'ecrit QUE si un fichier col_debug.txt existe deja a cote de load.py (le creer vide pour activer). Sinon, sans effet."""
    try:
        if not os.path.exists(col_chemin('col_debug.txt')):
            return
        with open(col_chemin('col_debug.txt'), 'a', encoding='utf-8') as f:
            f.write(datetime.now().strftime('%H:%M:%S') + ' ' + str(msg) + chr(10))
    except Exception:
        pass

def col_nom_marchandise(nom_interne, localise):
    if localise:
        return str(localise)
    n = str(nom_interne or '').strip('$;')
    if n.lower().endswith('_name'):
        n = n[:-5]
    return n

def col_maj_ts(ts):
    if ts and str(ts) > col_etat['dernier_ts']:
        col_etat['dernier_ts'] = str(ts)

def col_traiter_evenement(entry):
    """Traite UN evenement de journal. Ne fait aucun appel reseau."""
    ev = entry.get('event')
    ts = entry.get('timestamp')

    if ev in ('FSDJump', 'Location', 'CarrierJump'):
        adr = entry.get('SystemAddress')
        nom = entry.get('StarSystem')
        if adr and nom:
            pos = entry.get('StarPos')
            x = y = z = None
            if isinstance(pos, (list, tuple)) and len(pos) == 3:
                x, y, z = pos[0], pos[1], pos[2]
            col_positions[adr] = {'nom': nom, 'x': x, 'y': y, 'z': z,
                                  'faction': (entry.get('SystemFaction') or {}).get('Name'), 'faction_ts': ts}
            col_courant['adresse'] = adr
            col_courant['nom'] = nom
        for f_ in (entry.get('Factions') or []):
            if isinstance(f_, dict) and f_.get('SquadronFaction') is True and f_.get('Name'):
                col_faction['nom'] = f_.get('Name')
                break
        return

    if ev == 'Docked':
        mid = entry.get('MarketID')
        typ = entry.get('StationType') or ''
        nom_st = entry.get('StationName') or ''
        if mid and ('ConstructionDepot' in typ or 'ColonisationShip' in nom_st or 'Construction Site' in nom_st):
            col_stations[mid] = {'station': nom_st, 'type': typ, 'systeme': entry.get('StarSystem'), 'adresse': entry.get('SystemAddress')}
        elif (mid and col_faction['nom'] and typ != 'FleetCarrier' and not nom_st.startswith('$') and entry.get('SystemAddress')
              and mid not in col_inst_envoyees and (entry.get('StationFaction') or {}).get('Name') == col_faction['nom']):
            # Station terminee de la faction de l'escadron : simple CANDIDATE. Le serveur (col_installations, SQL 24) ne la retient que si le
            # systeme est deja connu comme colonise (revendication ou chantier vu) ; les systemes BGS classiques sont jetes la-bas.
            with col_verrou:
                col_attente['installations'][mid] = {'market_id': mid, 'systeme_adresse': entry.get('SystemAddress'),
                                                     'systeme_nom': entry.get('StarSystem'), 'nom': nom_st, 'type_station': typ, 'ts': ts,
                                                     'faction': (col_positions.get(entry.get('SystemAddress')) or {}).get('faction'),
                                                     'faction_ts': (col_positions.get(entry.get('SystemAddress')) or {}).get('faction_ts')}
        return

    if ev == 'ColonisationSystemClaim':
        adr = entry.get('SystemAddress')
        nom = entry.get('StarSystem')
        if adr and nom:
            p = col_positions.get(adr) or {}
            with col_verrou:
                col_attente['systemes'][adr] = {'adresse': adr, 'nom': nom, 'x': p.get('x'), 'y': p.get('y'), 'z': p.get('z'),
                                                'revendique': True, 'revendique_le': ts}
        col_maj_ts(ts)
        return

    if ev == 'ColonisationConstructionDepot':
        mid = entry.get('MarketID')
        if not mid:
            return
        st = col_stations.get(mid) or {}
        adr = st.get('adresse') or col_courant['adresse']
        nom_sys = st.get('systeme') or col_courant['nom']
        march = []
        for r in (entry.get('ResourcesRequired') or []):
            march.append({'nom': r.get('Name'),
                          'nom_fr': col_nom_marchandise(r.get('Name'), r.get('Name_Localised')),
                          'requis': int(r.get('RequiredAmount') or 0),
                          'livre': int(r.get('ProvidedAmount') or 0),
                          'paiement': int(r.get('Payment') or 0)})
        chantier = {'market_id': mid, 'systeme_adresse': adr, 'systeme_nom': nom_sys,
                    'station_nom': st.get('station'), 'type_station': st.get('type'),
                    'progression': float(entry.get('ConstructionProgress') or 0),
                    'complet': bool(entry.get('ConstructionComplete')), 'echec': bool(entry.get('ConstructionFailed')),
                    'ts': ts, 'marchandises': march}
        with col_verrou:
            col_attente['chantiers'][mid] = chantier          # le plus recent remplace le precedent
            if adr and nom_sys and adr not in col_attente['systemes']:
                p = col_positions.get(adr) or {}
                col_attente['systemes'][adr] = {'adresse': adr, 'nom': nom_sys, 'x': p.get('x'), 'y': p.get('y'), 'z': p.get('z'),
                                                'revendique': False}
        col_maj_ts(ts)
        return

    if ev == 'ColonisationContribution':
        mid = entry.get('MarketID')
        if not mid:
            return
        lignes = []
        for c in (entry.get('Contributions') or []):
            t = int(c.get('Amount') or 0)
            if t > 0:
                lignes.append({'market_id': mid, 'marchandise': c.get('Name'),
                               'marchandise_nom': col_nom_marchandise(c.get('Name'), c.get('Name_Localised')),
                               'tonnes': t, 'ts': ts})
        if lignes:
            with col_verrou:
                col_attente['livraisons'].extend(lignes)
        col_maj_ts(ts)
        return

def col_lire_marqueur():
    try:
        with open(col_chemin(COL_MARQUEUR), 'r', encoding='utf-8') as f:
            return json.load(f).get('dernier_ts') or ''
    except Exception:
        return ''

def col_ecrire_marqueur(ts):
    if not ts:
        return
    try:
        with open(col_chemin(COL_MARQUEUR), 'w', encoding='utf-8') as f:
            json.dump({'dernier_ts': ts}, f)
    except Exception:
        pass

def col_envoyer_installations(installations):
    """Second appel (fonction col_installations, SQL 24) : stations terminees candidates. Remises en attente si l'envoi echoue."""
    if not installations or col_etat['inst_absente']:
        return
    def _rearmer_i(lst):
        with col_verrou:
            for x in lst:
                if x['market_id'] not in col_inst_envoyees and x['market_id'] not in col_attente['installations']:
                    col_attente['installations'][x['market_id']] = x
    if not get_user_id():
        _rearmer_i(installations)
        return
    i_i = 0
    try:
        while i_i < len(installations):
            lot_i = installations[i_i:i_i + COL_LOT_INSTALLATIONS]
            res = requests.post(
                f"{SUPABASE_URL}/rest/v1/rpc/col_installations",
                headers=get_headers(),
                json={'p_installations': lot_i},
                timeout=20
            )
            col_trace('col_installations : %d stations -> HTTP %s %s' % (len(lot_i), res.status_code, (res.text or '')[:160]))
            if res.status_code in (200, 201, 204):
                for x in lot_i:
                    col_inst_envoyees.add(x['market_id'])
                i_i += len(lot_i)
                continue
            if res.status_code == 404:
                col_etat['inst_absente'] = True      # fonction pas encore creee en base : on ne reessaie pas
                return
            raise RuntimeError('HTTP ' + str(res.status_code))
    except Exception as e:
        col_trace('installations : envoi en echec : %r' % (e,))
        _rearmer_i(installations[i_i:])

def col_envoyer(force=False):
    """Envoie ce qui est en attente, par lots. Au plus un envoi toutes les COL_INTERVALLE_ENVOI secondes, sauf force."""
    if col_etat['rpc_absente']:
        return
    with col_verrou:
        if not (col_attente['chantiers'] or col_attente['systemes'] or col_attente['livraisons'] or col_attente['installations']):
            return
        if not force and time.time() - col_etat['dernier_envoi'] < COL_INTERVALLE_ENVOI:
            return
        chantiers = list(col_attente['chantiers'].values())
        systemes = list(col_attente['systemes'].values())
        livraisons = list(col_attente['livraisons'])
        installations = list(col_attente['installations'].values())
        col_attente['installations'] = {}
        col_attente['chantiers'] = {}
        col_attente['systemes'] = {}
        col_attente['livraisons'] = []
        col_etat['dernier_envoi'] = time.time()

    col_envoyer_installations(installations)

    # positions connues depuis (un systeme peut etre revendique avant qu'on y saute)
    for s_ in systemes:
        if s_.get('x') is None:
            p_ = col_positions.get(s_['adresse'])
            if p_ and p_.get('x') is not None:
                s_['x'] = p_.get('x')
                s_['y'] = p_.get('y')
                s_['z'] = p_.get('z')

    # faction qui controle le systeme (la plus recente vue aux sauts)
    for s_ in systemes:
        p_ = col_positions.get(s_['adresse']) or {}
        if p_.get('faction'):
            s_['faction'] = p_.get('faction')
            s_['faction_ts'] = p_.get('faction_ts')

    def _rearmer(ch, sy, li):
        col_etat['echecs'] += 1
        if col_etat['echecs'] >= COL_ECHECS_MAX:
            col_trace('abandon apres %d echecs : %d chantiers, %d systemes, %d livraisons non envoyes' % (col_etat['echecs'], len(ch), len(sy), len(li)))
            col_etat['echecs'] = 0
            return
        with col_verrou:
            for c in ch:
                if c['market_id'] not in col_attente['chantiers']:     # un releve plus recent est peut-etre deja arrive
                    col_attente['chantiers'][c['market_id']] = c
            for s in sy:
                if s['adresse'] not in col_attente['systemes']:
                    col_attente['systemes'][s['adresse']] = s
            col_attente['livraisons'] = li + col_attente['livraisons']

    if not get_user_id():
        col_trace('envoi differe : get_user_id() vide (cle inconnue ou reseau)')
        _rearmer(chantiers, systemes, livraisons)
        return

    i_c = 0
    i_s = 0
    i_l = 0
    try:
        while i_c < len(chantiers) or i_s < len(systemes) or i_l < len(livraisons):
            lot_c = chantiers[i_c:i_c + COL_LOT_CHANTIERS]
            lot_s = systemes[i_s:i_s + COL_LOT_SYSTEMES]
            lot_l = livraisons[i_l:i_l + COL_LOT_LIVRAISONS]
            res = requests.post(
                f"{SUPABASE_URL}/rest/v1/rpc/col_releve",
                headers=get_headers(),
                json={'p_systemes': lot_s, 'p_chantiers': lot_c, 'p_livraisons': lot_l},
                timeout=20
            )
            col_trace('col_releve : %d chantiers, %d systemes, %d livraisons -> HTTP %s %s' % (len(lot_c), len(lot_s), len(lot_l), res.status_code, (res.text or '')[:160]))
            if res.status_code in (200, 201, 204):
                i_c += len(lot_c)
                i_s += len(lot_s)
                i_l += len(lot_l)
                continue
            if res.status_code == 404:
                col_etat['rpc_absente'] = True      # fonction pas encore creee en base : on ne reessaie pas
                return
            raise RuntimeError('HTTP ' + str(res.status_code))
    except Exception as e:
        col_trace('envoi en echec : %r' % (e,))
        _rearmer(chantiers[i_c:], systemes[i_s:], livraisons[i_l:])
        return
    col_etat['echecs'] = 0

def col_scanner_journaux():
    """Relit les journaux : tout l'historique la premiere fois, ensuite seulement depuis le dernier envoi (moins 2 jours)."""
    try:
        debut_scan = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        jdir = trouver_journal_dir()
        col_trace('relecture : dossier des journaux = %s' % jdir)
        if not jdir or not os.path.isdir(jdir):
            return
        marqueur = col_lire_marqueur()
        limite = None
        if marqueur:
            try:
                limite = datetime.strptime(marqueur, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() - 2 * 86400
            except Exception:
                limite = None
        fichiers = sorted(f for f in os.listdir(jdir)
                          if f.startswith('Journal.') and f.endswith('.log')
                          and (limite is None or os.path.getmtime(os.path.join(jdir, f)) >= limite))
        mots = ('"event":"Coloni', '"event":"Docked"', '"event":"FSDJump"', '"event":"Location"', '"event":"CarrierJump"')
        nb = 0
        for nom_fichier in fichiers:
            with open(os.path.join(jdir, nom_fichier), 'r', encoding='utf-8', errors='ignore') as f:
                for ligne in f:
                    if not any(m in ligne for m in mots):
                        continue
                    try:
                        e = json.loads(ligne)
                    except Exception:
                        continue
                    col_traiter_evenement(e)
                    nb += 1
        with col_verrou:
            n_c = len(col_attente['chantiers'])
            n_s = len(col_attente['systemes'])
            n_l = len(col_attente['livraisons'])
        col_trace('relecture : %d fichiers, %d evenements utiles, en attente : %d chantiers, %d systemes, %d livraisons (marqueur = %s)' % (len(fichiers), nb, n_c, n_s, n_l, marqueur or 'aucun'))
        col_envoyer(force=True)
        with col_verrou:
            reste = len(col_attente['chantiers']) + len(col_attente['systemes']) + len(col_attente['livraisons'])
        if reste == 0 and col_etat['echecs'] == 0 and not col_etat['rpc_absente']:
            col_ecrire_marqueur(debut_scan)      # relecture complete et envoyee : la prochaine ne remontera que depuis ce moment (moins 2 jours)
            col_trace('relecture terminee, marqueur = %s' % debut_scan)
    except Exception as e:
        col_trace('relecture : EXCEPTION %r' % (e,))

# ==========================================
# BUDGET PERSONNEL : revenus et charges releves dans les journaux de jeu (script SQL 23).
# Chaque evenement d'argent est classe (bud_classer), ajoute aux totaux du CYCLE (jeudi 07:00 UTC) et, pour les 8 derniers cycles,
# au livre de compte. Le plugin envoie les totaux COMPLETS des cycles modifies (le serveur les remplace) et les nouvelles lignes,
# au plus toutes les 5 minutes. L'etat est garde dans budget_etat.json (a cote de load.py) : un redemarrage ne compte jamais deux fois
# un evenement (filtre sur l'horodatage du dernier evenement compte). Tout reste PRIVE : le serveur ne rend ces lignes qu'a leur pilote.
# Le solde suivi est recale a chaque lancement du jeu (LoadGame) : la difference est enregistree comme "ecart", jamais cachee.
# ==========================================
BUD_INTERVALLE_ENVOI = 300
BUD_LOT_LIGNES = 400
BUD_LOT_CYCLES = 120
BUD_LOTS_PAR_PASSE = 10
BUD_MAX_LIGNES = 3000
BUD_FICHIER = 'budget_etat.json'
BUD_REVENUS = ('commerce', 'missions', 'primes', 'exploration', 'minage', 'autres_revenus')
BUD_EVENEMENTS_ARGENT = frozenset((
    'MissionCompleted', 'RedeemVoucher', 'SellExplorationData', 'MultiSellExplorationData', 'SellOrganicData', 'MarketSell',
    'MarketBuy', 'SearchAndRescue', 'CommunityGoalReward', 'PowerplaySalary', 'ModuleSell', 'ModuleSellRemote', 'ShipyardSell',
    'SellDrones', 'SellMicroResources', 'ModuleBuy', 'ModuleBuyAndStore', 'ShipyardBuy', 'RefuelAll', 'RefuelPartial', 'Repair',
    'RepairAll', 'RestockVehicle', 'BuyAmmo', 'BuyDrones', 'Resurrect', 'PayFines', 'PayLegacyFines', 'PayBounties',
    'FetchRemoteModule', 'ShipyardTransfer', 'BookTaxi', 'BookDropship', 'NpcCrewPaidWage', 'CrewHire', 'BuyExplorationData',
    'BuyTradeData', 'BuyMicroResources', 'BuyWeapon', 'BuySuit', 'UpgradeSuit', 'UpgradeWeapon', 'SellWeapon', 'SellSuit',
    'MissionFailed', 'PowerplayFastTrack', 'ModuleRetrieve', 'ModuleStore',
    'CarrierBankTransfer', 'CarrierBuy', 'LoadGame'))
BUD_EVENEMENTS_CONTEXTE = frozenset(('Location', 'FSDJump', 'CarrierJump', 'Docked', 'Undocked'))
bud_re_journal = re.compile('"event":"(' + '|'.join(sorted(BUD_EVENEMENTS_ARGENT | BUD_EVENEMENTS_CONTEXTE)) + ')"')

bud_verrou = threading.RLock()
bud_etat = {'dernier_ts': '', 'refs': [], 'solde': None, 'cycles': {}, 'sales': [], 'lignes': [], 'lu_jusqua': ''}
bud_ctx = {'systeme': '', 'station': ''}
bud_file = []          # evenements vus en direct pendant la relecture des journaux
bud_flags = {'pret': False, 'absente': False, 'dernier_envoi': 0, 'dernier_sauvetage': 0, 'modifie': False, 'echecs': 0}

def bud_chemin():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), BUD_FICHIER)

def bud_trace(msg):
    """Diagnostic : n'ecrit QUE si un fichier bud_debug.txt existe deja a cote de load.py (le creer vide pour activer)."""
    try:
        chemin = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bud_debug.txt')
        if not os.path.exists(chemin):
            return
        with open(chemin, 'a', encoding='utf-8') as f:
            f.write(datetime.now().strftime('%H:%M:%S') + ' ' + str(msg) + chr(10))
    except Exception:
        pass

def bud_semaine(ts):
    """Jeudi (date ISO) qui cloture le cycle contenant cet instant UTC : meme regle que cloture_cycle_pp cote serveur."""
    dt = datetime.strptime(str(ts)[:19], '%Y-%m-%dT%H:%M:%S') - timedelta(hours=7)
    d = dt.date()
    delta = (3 - d.weekday()) % 7
    if delta == 0:
        delta = 7
    return (d + timedelta(days=delta)).isoformat()

def bud_fenetre_debut():
    """Debut de la fenetre du livre : debut du cycle courant moins 7 cycles (8 cycles au total), comme budget_fenetre_debut()."""
    cur = datetime.strptime(bud_semaine(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')), '%Y-%m-%d')
    return (cur - timedelta(days=56) + timedelta(hours=7)).strftime('%Y-%m-%dT%H:%M:%SZ')

def bud_charger_etat():
    try:
        with open(bud_chemin(), 'r', encoding='utf-8') as f:
            d = json.load(f)
        with bud_verrou:
            for cle in ('dernier_ts', 'lu_jusqua'):
                bud_etat[cle] = str(d.get(cle) or '')
            bud_etat['refs'] = list(d.get('refs') or [])
            bud_etat['solde'] = d.get('solde')
            bud_etat['cycles'] = dict(d.get('cycles') or {})
            bud_etat['sales'] = list(d.get('sales') or [])
            bud_etat['lignes'] = list(d.get('lignes') or [])
    except Exception:
        pass

def bud_sauver_etat():
    """Ecriture atomique de l'etat (totaux, horodatage du dernier evenement compte, lignes en attente)."""
    try:
        with bud_verrou:
            donnees = json.dumps(bud_etat)
            bud_flags['modifie'] = False
            bud_flags['dernier_sauvetage'] = time.time()
        tmp = bud_chemin() + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(donnees)
        os.replace(tmp, bud_chemin())
    except Exception as e:
        bud_trace('sauvegarde impossible : %r' % (e,))

def _bn(v):
    try:
        return int(v)
    except Exception:
        return 0

def bud_classer(e):
    """Un evenement du journal -> liste de (categorie, montant POSITIF, libelle). Liste vide si aucun mouvement d'argent."""
    ev = e.get('event')
    out = []
    def add(cat, montant, lib):
        m = _bn(montant)
        if m > 0:
            out.append((cat, m, str(lib)[:120]))
    nom_march = e.get('Type_Localised') or e.get('Type') or ''
    if ev == 'MissionCompleted':
        nom = e.get('LocalisedName') or e.get('Name') or ''
        add('missions', e.get('Reward'), 'Mission : ' + str(nom))
        add('autres_charges', e.get('Donated'), 'Don (mission) : ' + str(nom))
    elif ev == 'RedeemVoucher':
        typ = str(e.get('Type') or '').lower()
        if typ == 'bounty':
            add('primes', e.get('Amount'), 'Encaissement de primes')
        elif typ == 'combatbond':
            add('primes', e.get('Amount'), "Encaissement de bons de combat")
        else:
            add('autres_revenus', e.get('Amount'), 'Encaissement de bons : ' + typ)
    elif ev in ('SellExplorationData', 'MultiSellExplorationData'):
        total = e.get('TotalEarnings')
        if total is None:
            total = _bn(e.get('BaseValue')) + _bn(e.get('Bonus'))
        add('exploration', total, "Vente de données d'exploration")
    elif ev == 'SellOrganicData':
        total = e.get('TotalEarnings')
        if total is None:
            total = sum(_bn(b.get('Value')) + _bn(b.get('Bonus')) for b in (e.get('BioData') or []) if isinstance(b, dict))
        add('exploration', total, 'Vente de données organiques')
    elif ev == 'MarketSell':
        cat = 'minage' if _bn(e.get('AvgPricePaid')) == 0 else 'commerce'
        add(cat, e.get('TotalSale'), 'Vente ' + str(nom_march) + ' x' + str(_bn(e.get('Count'))))
    elif ev == 'MarketBuy':
        add('achats', e.get('TotalCost'), 'Achat ' + str(nom_march) + ' x' + str(_bn(e.get('Count'))))
    elif ev == 'SearchAndRescue':
        add('autres_revenus', e.get('Reward'), 'Sauvetage : ' + str(e.get('Name_Localised') or e.get('Name') or ''))
    elif ev == 'CommunityGoalReward':
        add('autres_revenus', e.get('Reward'), 'Objectif communautaire : ' + str(e.get('Name') or ''))
    elif ev == 'PowerplaySalary':
        add('autres_revenus', e.get('Salary') or e.get('Amount'), 'Salaire Powerplay')
    elif ev in ('ModuleSell', 'ModuleSellRemote'):
        add('autres_revenus', e.get('SellPrice'), 'Revente de module')
    elif ev == 'ShipyardSell':
        add('autres_revenus', e.get('ShipPrice'), 'Revente de vaisseau')
    elif ev == 'SellDrones':
        add('autres_revenus', e.get('TotalSale'), 'Vente de drones')
    elif ev == 'SellMicroResources':
        add('autres_revenus', e.get('Price'), 'Vente de micro-ressources')
    elif ev in ('SellWeapon', 'SellSuit'):
        add('autres_revenus', e.get('Price'), 'Revente d\'équipement à pied')
    elif ev == 'ModuleBuy':
        add('modules', e.get('BuyPrice'), 'Achat de module')
        add('autres_revenus', e.get('SellPrice'), "Revente de l'ancien module")
    elif ev == 'ModuleBuyAndStore':
        add('modules', e.get('BuyPrice'), 'Achat de module (stockage)')
    elif ev == 'ShipyardBuy':
        add('modules', e.get('ShipPrice'), 'Achat de vaisseau : ' + str(e.get('ShipType_Localised') or e.get('ShipType') or ''))
        add('autres_revenus', e.get('SellPrice'), "Revente de l'ancien vaisseau")
    elif ev in ('BuyWeapon', 'BuySuit'):
        add('modules', e.get('Price'), "Achat d'équipement à pied : " + str(e.get('Name_Localised') or e.get('Name') or ''))
    elif ev in ('UpgradeSuit', 'UpgradeWeapon'):
        add('modules', e.get('Cost'), "Amélioration d'équipement à pied")
    elif ev == 'ModuleStore':
        add('modules', e.get('Cost'), 'Stockage de module')
    elif ev in ('RefuelAll', 'RefuelPartial'):
        add('carburant', e.get('Cost'), 'Ravitaillement')
    elif ev in ('Repair', 'RepairAll'):
        add('reparations', e.get('Cost'), 'Réparation')
    elif ev in ('RestockVehicle', 'BuyAmmo'):
        add('munitions', e.get('Cost'), 'Munitions et recharges')
    elif ev == 'BuyDrones':
        add('munitions', e.get('TotalCost'), 'Achat de drones')
    elif ev == 'Resurrect':
        add('assurance', e.get('Cost'), 'Rachat après destruction')
    elif ev in ('PayFines', 'PayLegacyFines'):
        add('amendes', e.get('Amount'), "Paiement d'amendes")
    elif ev == 'PayBounties':
        add('amendes', e.get('Amount'), 'Paiement de primes sur votre tête')
    elif ev == 'MissionFailed':
        add('amendes', e.get('Fine'), 'Pénalité de mission échouée')
    elif ev == 'FetchRemoteModule':
        add('transferts', e.get('TransferCost'), 'Transfert de module')
    elif ev == 'ShipyardTransfer':
        add('transferts', e.get('TransferPrice'), 'Transfert de vaisseau')
    elif ev == 'ModuleRetrieve':
        add('transferts', e.get('Cost'), 'Récupération de module')
    elif ev in ('BookTaxi', 'BookDropship'):
        add('transferts', e.get('Cost'), 'Taxi' if ev == 'BookTaxi' else 'Transport en navette')
    elif ev == 'NpcCrewPaidWage':
        add('autres_charges', e.get('Amount'), "Salaire d'équipage")
    elif ev == 'CrewHire':
        add('autres_charges', e.get('Cost'), "Embauche d'équipage")
    elif ev in ('BuyExplorationData', 'BuyTradeData'):
        add('autres_charges', e.get('Cost'), 'Achat de données')
    elif ev == 'BuyMicroResources':
        add('autres_charges', e.get('Price'), 'Achat de micro-ressources')
    elif ev == 'PowerplayFastTrack':
        add('autres_charges', e.get('Cost'), 'Powerplay : accélération')
    return out

def bud_lieu():
    s, st = bud_ctx['systeme'], bud_ctx['station']
    return (st + ' / ' + s if st and s else (s or st or ''))[:120]

def bud_ajouter(sem, ts, cat, montant, lib, ref):
    """Ajoute un mouvement au cycle (et au livre si dans la fenetre). montant POSITIF sauf pour 'ecart' (signe). Appel sous bud_verrou."""
    c = bud_etat['cycles'].setdefault(sem, {'t': {}, 'debut': None, 'fin': None})
    revenu = cat in BUD_REVENUS
    signe = montant if (cat == 'ecart' or revenu) else -montant
    t = c['t'].setdefault(cat, {'m': 0, 'n': 0})
    t['m'] += montant
    t['n'] += 1
    avant = bud_etat['solde']
    if avant is not None:
        if c['debut'] is None:
            c['debut'] = avant
        bud_etat['solde'] = avant + signe
        c['fin'] = bud_etat['solde']
    if sem not in bud_etat['sales']:
        bud_etat['sales'].append(sem)
    if ts >= bud_fenetre_debut():
        bud_etat['lignes'].append({'ts': ts, 'k': cat, 'm': signe, 'l': lib, 'lieu': bud_lieu(), 'ref': ref})
        if len(bud_etat['lignes']) > BUD_MAX_LIGNES:
            del bud_etat['lignes'][:len(bud_etat['lignes']) - BUD_MAX_LIGNES]
    bud_flags['modifie'] = True

def bud_deja_compte(ts, ref):
    """Vrai si cet evenement a deja ete compte ; sinon l'enregistre comme dernier evenement compte. Appel sous bud_verrou."""
    dernier = bud_etat['dernier_ts']
    if ts < dernier:
        return True
    if ts == dernier:
        if ref in bud_etat['refs']:
            return True
        bud_etat['refs'].append(ref)
        return False
    bud_etat['dernier_ts'] = ts
    bud_etat['refs'] = [ref]
    return False

def bud_appliquer(entry):
    """Traite UN evenement (en direct ou a la relecture), dans l'ordre chronologique. Appel sous bud_verrou."""
    ev = entry.get('event')
    ts = str(entry.get('timestamp') or '')
    if ev in ('Location', 'FSDJump', 'CarrierJump'):
        bud_ctx['systeme'] = str(entry.get('StarSystem') or '')
        bud_ctx['station'] = ''
        return
    if ev == 'Docked':
        bud_ctx['systeme'] = str(entry.get('StarSystem') or bud_ctx['systeme'])
        bud_ctx['station'] = str(entry.get('StationName') or '')
        return
    if ev == 'Undocked':
        bud_ctx['station'] = ''
        return
    if ev not in BUD_EVENEMENTS_ARGENT or len(ts) < 19:
        return
    ref = hashlib.md5(json.dumps(entry, sort_keys=True).encode('utf-8')).hexdigest()[:12]
    sem = bud_semaine(ts)

    if ev == 'LoadGame':
        credits = entry.get('Credits')
        if credits is None:
            return
        credits = _bn(credits)
        if bud_deja_compte(ts, ref):
            return
        if bud_etat['solde'] is None:
            c = bud_etat['cycles'].setdefault(sem, {'t': {}, 'debut': None, 'fin': None})
            if c['debut'] is None:
                c['debut'] = credits
            c['fin'] = credits
            bud_etat['solde'] = credits
            if sem not in bud_etat['sales']:
                bud_etat['sales'].append(sem)
            bud_flags['modifie'] = True
        else:
            ecart = credits - _bn(bud_etat['solde'])
            if ecart != 0:
                bud_ajouter(sem, ts, 'ecart', ecart, 'Écart constaté au lancement du jeu (mouvements non relevés)', ref)
            bud_etat['solde'] = credits
        return

    if ev in ('CarrierBankTransfer', 'CarrierBuy'):
        # Mouvement entre le compte du pilote et sa porte-flotte (hors budget pour l'instant) : on suit seulement le solde.
        if bud_deja_compte(ts, ref):
            return
        if ev == 'CarrierBankTransfer' and entry.get('PlayerBalance') is not None:
            bud_etat['solde'] = _bn(entry.get('PlayerBalance'))      # le journal donne le solde exact apres le virement
            bud_flags['modifie'] = True
        elif bud_etat['solde'] is not None:
            if ev == 'CarrierBankTransfer':
                bud_etat['solde'] = _bn(bud_etat['solde']) - _bn(entry.get('Deposit')) + _bn(entry.get('Withdraw'))
            else:
                bud_etat['solde'] = _bn(bud_etat['solde']) - _bn(entry.get('Price'))
            bud_flags['modifie'] = True
        return

    mouvements = bud_classer(entry)
    if not mouvements or bud_deja_compte(ts, ref):
        return
    for cat, montant, lib in mouvements:
        bud_ajouter(sem, ts, cat, montant, lib, ref)

def bud_traiter_evenement(entry):
    """Appelee pour chaque evenement en direct. Pendant la relecture des journaux, les evenements attendent dans bud_file."""
    ev = entry.get('event')
    if ev not in BUD_EVENEMENTS_ARGENT and ev not in BUD_EVENEMENTS_CONTEXTE:
        return
    with bud_verrou:
        if not bud_flags['pret']:
            if len(bud_file) < 5000:
                bud_file.append(entry)
            return
        bud_appliquer(entry)

def bud_scanner_journaux():
    """Au demarrage : relit les journaux (tout l'historique la premiere fois, ensuite depuis la derniere relecture moins 2 jours),
    puis traite les evenements arrives en direct pendant ce temps. Le filtre d'horodatage evite tout double comptage."""
    try:
        bud_charger_etat()
        debut_scan = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        jdir = trouver_journal_dir()
        if jdir and os.path.isdir(jdir):
            limite = None
            lu = bud_etat['lu_jusqua']
            if lu:
                try:
                    limite = datetime.strptime(lu, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp() - 2 * 86400
                except Exception:
                    limite = None
            fichiers = sorted(f for f in os.listdir(jdir)
                              if f.startswith('Journal.') and f.endswith('.log')
                              and (limite is None or os.path.getmtime(os.path.join(jdir, f)) >= limite))
            nb = 0
            for nom_fichier in fichiers:
                with open(os.path.join(jdir, nom_fichier), 'r', encoding='utf-8', errors='ignore') as f:
                    for ligne in f:
                        if not bud_re_journal.search(ligne):
                            continue
                        try:
                            e = json.loads(ligne)
                        except Exception:
                            continue
                        with bud_verrou:
                            bud_appliquer(e)
                        nb += 1
            with bud_verrou:
                bud_etat['lu_jusqua'] = debut_scan
                bud_flags['modifie'] = True
            bud_trace('relecture : %d fichiers, %d evenements utiles, %d cycles, %d lignes en attente' % (len(fichiers), nb, len(bud_etat['cycles']), len(bud_etat['lignes'])))
    except Exception as e:
        bud_trace('relecture : EXCEPTION %r' % (e,))
    finally:
        with bud_verrou:
            en_attente = list(bud_file)
            del bud_file[:]
            for e in en_attente:
                try:
                    bud_appliquer(e)
                except Exception as ex:
                    bud_trace('evenement ignore : %r' % (ex,))
            bud_flags['pret'] = True
        bud_sauver_etat()
        try:
            bud_envoyer(force=True)
        except Exception:
            pass

def bud_envoyer(force=False):
    """Envoie les totaux des cycles modifies (le serveur les remplace) et les nouvelles lignes du livre, par lots. Au plus un envoi toutes les 5 minutes."""
    if bud_flags['absente'] or not bud_flags['pret']:
        return
    with bud_verrou:
        if not (bud_etat['sales'] or bud_etat['lignes']):
            return
        if not force and time.time() - bud_flags['dernier_envoi'] < BUD_INTERVALLE_ENVOI:
            return
        bud_flags['dernier_envoi'] = time.time()
    if not get_user_id():
        bud_trace('envoi differe : get_user_id() vide (cle inconnue ou reseau)')
        return
    for _ in range(BUD_LOTS_PAR_PASSE):
        with bud_verrou:
            sales = list(bud_etat['sales'])[:BUD_LOT_CYCLES]
            cycles = []
            for s in sales:
                c = bud_etat['cycles'].get(s)
                if not c:
                    continue
                item = {'semaine': s, 't': dict(c['t'])}
                if c.get('debut') is not None:
                    item['debut'] = c['debut']
                if c.get('fin') is not None:
                    item['fin'] = c['fin']
                cycles.append(item)
            lignes = list(bud_etat['lignes'][:BUD_LOT_LIGNES])
            if not cycles and not lignes:
                return
            for s in sales:
                if s in bud_etat['sales']:
                    bud_etat['sales'].remove(s)
        try:
            res = requests.post(
                f"{SUPABASE_URL}/rest/v1/rpc/budget_releve",
                headers=get_headers(),
                json={'p_cycles': cycles, 'p_lignes': lignes},
                timeout=20
            )
            bud_trace('budget_releve : %d cycles, %d lignes -> HTTP %s %s' % (len(cycles), len(lignes), res.status_code, (res.text or '')[:160]))
            if res.status_code in (200, 201, 204):
                with bud_verrou:
                    del bud_etat['lignes'][:len(lignes)]
                    bud_flags['modifie'] = True
                bud_flags['echecs'] = 0
                continue
            if res.status_code == 404:
                bud_flags['absente'] = True      # fonction pas encore creee en base : on ne reessaie pas
                with bud_verrou:
                    for s in sales:
                        if s not in bud_etat['sales']:
                            bud_etat['sales'].append(s)
                return
            raise RuntimeError('HTTP ' + str(res.status_code))
        except Exception as e:
            bud_trace('envoi en echec : %r' % (e,))
            bud_flags['echecs'] += 1
            with bud_verrou:
                for s in sales:
                    if s not in bud_etat['sales']:
                        bud_etat['sales'].append(s)
            return

def bud_sauver_si_besoin():
    """Appelee par la boucle de fond : sauvegarde l'etat s'il a change (au plus toutes les 30 secondes)."""
    if bud_flags['modifie'] and time.time() - bud_flags['dernier_sauvetage'] >= 30:
        bud_sauver_etat()

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
            # Plus de HEARTBEAT : la presence n'est plus affichee cote site (economie d'Egress).
            # La boucle ne fait plus que surveiller le solde (lecture fichier local, ecriture seulement si change).
            jdir = trouver_journal_dir()
            if jdir and os.path.exists(os.path.join(jdir, 'Status.json')):
                with open(os.path.join(jdir, 'Status.json'), 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    nouveau_solde = data.get('Balance')
                    if nouveau_solde is not None and nouveau_solde != dernier_solde_vaisseau: 
                        maj_generique_global("SHIP_BALANCE", "FINANCE", "BANK", "FINANCE", val=nouveau_solde)
                        dernier_solde_vaisseau = nouveau_solde
        except: pass
        try: pp_envoyer()   # merites Powerplay : envoi si du (au plus toutes les 5 min)
        except: pass
        try: col_envoyer()  # colonisation : envoi regroupe si du (au plus toutes les 3 min)
        except: pass
        try: bud_envoyer()  # budget : totaux des cycles modifies et nouvelles lignes du livre (au plus toutes les 5 min)
        except: pass
        try: bud_sauver_si_besoin()
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
    threading.Thread(target=pp_scanner_journaux, daemon=True).start()
    threading.Thread(target=col_scanner_journaux, daemon=True).start()
    threading.Thread(target=bud_scanner_journaux, daemon=True).start()
    return "SYS.EDTEAM"

def plugin_stop():
    try: pp_envoyer(force=True)
    except: pass
    try: col_envoyer(force=True)
    except: pass
    try: bud_envoyer(force=True)
    except: pass
    try: bud_sauver_etat()
    except: pass

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

    # Colonisation : releve des chantiers, livraisons et systemes (aucun appel reseau ici)
    try: col_traiter_evenement(entry)
    except Exception as e: col_trace('evenement ignore : %r' % (e,))

    # Budget : revenus et charges (aucun appel reseau ici)
    try: bud_traiter_evenement(entry)
    except Exception as e: bud_trace('evenement ignore : %r' % (e,))

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
        # 'Merits' = TOTAL des merites (pas ceux du cycle : le jeu n'envoie pas de compteur de cycle)
        power = entry.get('Power')
        if power:
            pp_enregistrer(power, entry.get('Rank', 0), entry.get('Merits', 0))
            threading.Thread(target=pp_envoyer, daemon=True).start()

    elif event == 'PowerplayMerits':
        # Gain de merites en direct : memorise seulement, la boucle de fond envoie au plus toutes les 5 minutes
        if entry.get('Power') and entry.get('TotalMerits') is not None:
            pp_enregistrer(entry.get('Power'), None, entry.get('TotalMerits'))

    elif event == 'PowerplayRank':
        if entry.get('Power') and entry.get('Rank') is not None:
            pp_enregistrer(entry.get('Power'), entry.get('Rank'), None)

    elif event in ['PowerplayLeave', 'PowerplayDefect']:
        new_power = entry.get('NewPower') if event == 'PowerplayDefect' else None
        pp_enregistrer(new_power, 0, 0)
        threading.Thread(target=pp_envoyer, kwargs={'force': True}, daemon=True).start()

    elif event == 'Shutdown':
        pp_envoyer(force=True)

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

                    # Opération noire contre une AUTRE faction que celle de l'escadron : alimente le titre « L'Exécuteur » (sans ordre)
                    est_operation_noire = False
                    if (not ordre_valide and not est_action_libre and faction_alliee and act_faction
                            and act_faction != faction_alliee.strip().lower()
                            and action['type'] in ('MEURTRES', 'VOLS', 'PIRATAGE', 'CONTREBANDE')):
                        est_operation_noire = True

                    if ordre_valide or est_action_libre or est_operation_noire:
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
                            elif est_operation_noire:
                                mettre_a_jour_interface(f">_ OPÉRATION NOIRE : {action['type']} (+{int(valeur_finale)})", "#FF3333")
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