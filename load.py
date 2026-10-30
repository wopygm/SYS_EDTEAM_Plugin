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
PLUGIN_VERSION = "2.51"

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

def trouver_journal_dir():
    """Dossier des journaux du jeu. On demande d'abord a EDMC (reglage du pilote, puis son dossier par defaut), puis aux chemins usuels de Windows
    (y compris un dossier Saved Games deplace dans OneDrive). Le premier qui EXISTE est retenu ; sinon on renvoie le premier candidat (comme avant).
    NB : dans ce fichier, le nom `config` designe le MODULE config d'EDMC (l'objet de reglages est config.config) : jusqu'a la 2.20 le reglage d'EDMC
    n'etait donc JAMAIS lu et seul le chemin par defaut de Windows etait essaye."""
    candidats = []
    def _ajouter(v):
        try:
            v = str(v).strip() if v else ''
        except Exception:
            v = ''
        if v and v not in candidats:
            candidats.append(v)
    objets = []
    if config:
        objets.append(getattr(config, 'config', None))      # l'objet de reglages d'EDMC (config.config)
        objets.append(config)                               # au cas ou `config` serait deja cet objet (ancien EDMC)
    for cfg in objets:
        if cfg is None:
            continue
        for nom in ('get_str', 'get'):
            try:
                if hasattr(cfg, nom):
                    _ajouter(getattr(cfg, nom)('journaldir'))
            except Exception:
                pass
        for nom in ('default_journal_dir_path', 'default_journal_dir'):
            try:
                _ajouter(getattr(cfg, nom, None))
            except Exception:
                pass
    if platform.system() == 'Windows':
        _ajouter(os.path.join(os.environ.get('USERPROFILE', ''), 'Saved Games', 'Frontier Developments', 'Elite Dangerous'))
        for var in ('OneDrive', 'OneDriveConsumer', 'OneDriveCommercial'):
            od = os.environ.get(var)
            if od:
                _ajouter(os.path.join(od, 'Saved Games', 'Frontier Developments', 'Elite Dangerous'))
    for c in candidats:
        try:
            if os.path.isdir(c):
                return c
        except Exception:
            pass
    return candidats[0] if candidats else None

def lister_journaux(jdir):
    """Fichiers Journal.*.log du dossier, du plus ancien au plus recent d'apres leur DATE DE MODIFICATION (puis le nom).
    Le tri par nom seul est faux quand un dossier contient d'anciens journaux au vieux format (Journal.220207204012.01.log) : ils passent apres
    les noms recents (Journal.2026-10-13T204012.01.log) et le plugin lisait alors une vieille session terminee (jeu 'non lance')."""
    def _cle(f):
        try:
            return (os.path.getmtime(os.path.join(jdir, f)), f)
        except OSError:
            return (0, f)
    return sorted((f for f in os.listdir(jdir) if f.startswith('Journal.') and f.endswith('.log') and f.count('.') >= 3), key=_cle)

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
# POWERPLAY PAR SYSTEME (v2.32, scripts SQL 96 et 97) : etat des systemes observes dans les journaux + merites rattaches au systeme ou se trouve le pilote.
# Aucun appel reseau dans le traitement d'un evenement (ppm_traiter_evenement) : tout est mis en attente, puis envoye par lots (ppm_envoyer, au plus toutes les PPM_INTERVALLE_ENVOI secondes).
# Le serveur ignore les doublons : renvoyer deux fois le meme evenement n'ajoute rien. Les merites sont typés d'apres l'etat du systeme :
#   la puissance du pilote controle le systeme = RENFORCEMENT ; une autre puissance le controle = SAPE ; aucune ne le controle mais des puissances sont presentes = ACQUISITION ; sinon AUTRE (hors mission).
# Au premier lancement de cette version, les 45 derniers jours de journaux sont relus une fois ; ensuite seulement ce qui est plus recent que le dernier envoi (moins 1 heure).
# ==========================================
PPM_INTERVALLE_ENVOI = 120
PPM_LOT_OBS = 300
PPM_LOT_MER = 400
PPM_LOTS_PAR_PASSE = 6
PPM_MAX_ATTENTE = 6000
PPM_RELECTURE = '2.32'          # a changer pour obliger tous les plugins a relire une fois leurs journaux
PPM_JOURS_RELECTURE = 45
PPM_MARQUEUR = 'ppm_marqueur.json'
PPM_ORIGINES = {
    'Bounty': 'PRIMES', 'RedeemVoucher': 'PRIMES', 'FactionKillBond': 'COMBAT', 'MissionCompleted': 'MISSIONS',
    'PowerplayDeliver': 'LIVRAISONS', 'PowerplayCollect': 'LIVRAISONS', 'MarketSell': 'COMMERCE',
    'SellExplorationData': 'EXPLORATION', 'MultiSellExplorationData': 'EXPLORATION', 'SearchAndRescue': 'SAUVETAGE',
    'CommunityGoalReward': 'COMMUNAUTE',
}
PPM_MOTS = tuple('"event":"%s"' % n for n in ('Location', 'FSDJump', 'CarrierJump', 'PowerplayMerits') + tuple(PPM_ORIGINES.keys()))

ppm_verrou = threading.Lock()
ppm_attente = {'obs': [], 'mer': []}
ppm_vu = {}        # adresse -> signature de la derniere observation mise en attente (pas de renvoi d'un etat identique)
ppm_live = {'nom': None, 'adresse': None, 'etat': None, 'evt': None, 'evt_t': 0.0}
ppm_flags = {'dernier_envoi': 0, 'obs_absente': False, 'mer_absente': False, 'echecs': 0, 'dernier_ts': ''}

def ppm_chemin(nom):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), nom)

def ppm_trace(msg):
    """Diagnostic : n'ecrit QUE si un fichier ppm_debug.txt existe deja a cote de load.py (le creer vide pour activer)."""
    try:
        if not os.path.exists(ppm_chemin('ppm_debug.txt')):
            return
        with open(ppm_chemin('ppm_debug.txt'), 'a', encoding='utf-8') as f:
            f.write(datetime.now().strftime('%H:%M:%S') + ' ' + str(msg) + chr(10))
    except Exception:
        pass

def ppm_epoch(ts):
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None

def ppm_type(puissance, etat):
    """Type de merites d'apres l'etat du systeme ou se trouve le pilote (voir l'en-tete du bloc)."""
    if not etat:
        return 'AUTRE'
    ctrl = etat.get('controlling')
    if ctrl and puissance and ctrl == puissance:
        return 'RENFORCEMENT'
    if ctrl:
        return 'SAPE'
    if etat.get('powers'):
        return 'ACQUISITION'
    return 'AUTRE'

def ppm_etat_depuis(e):
    """L'etat Powerplay d'un systeme tel que le journal l'ecrit a l'arrivee (None si le journal n'en dit rien)."""
    if ('PowerplayState' not in e) and ('Powers' not in e) and ('ControllingPower' not in e):
        return None
    powers = e.get('Powers')
    return {
        'controlling': e.get('ControllingPower') or None,
        'powers': [str(p) for p in powers] if isinstance(powers, list) else [],
        'etat': e.get('PowerplayState') or None,
        'progres': e.get('PowerplayStateControlProgress'),
        'renf': e.get('PowerplayStateReinforcement'),
        'sape': e.get('PowerplayStateUndermining'),
    }

def ppm_traiter_evenement(e, st, depuis=0.0):
    """Traite UN evenement (en direct comme a la relecture). st : etat courant (systeme, etat Powerplay, dernier evenement a l'origine d'un merite).
    depuis : horodatage (secondes) en dessous duquel rien n'est mis en attente (l'etat courant est tout de meme tenu a jour)."""
    ev = e.get('event')
    ts = e.get('timestamp')
    if ev in ('Location', 'FSDJump', 'CarrierJump'):
        nom = e.get('StarSystem')
        adr = e.get('SystemAddress')
        if not nom or adr is None:
            return
        st['nom'] = nom
        st['adresse'] = adr
        st['evt'] = None
        etat = ppm_etat_depuis(e)
        st['etat'] = etat
        if etat is None:
            return
        t = ppm_epoch(ts)
        if t is None or t < depuis:
            return
        signature = (etat['controlling'], etat['etat'], etat['progres'], etat['renf'], etat['sape'])
        with ppm_verrou:
            if ppm_vu.get(adr) == signature or len(ppm_attente['obs']) >= PPM_MAX_ATTENTE:
                return
            ppm_vu[adr] = signature
            ppm_attente['obs'].append({'adresse': int(adr), 'nom': str(nom), 'ts': ts, 'controlling': etat['controlling'], 'powers': etat['powers'],
                                       'etat': etat['etat'], 'progres': etat['progres'], 'renf': etat['renf'], 'sape': etat['sape']})
            if ts and str(ts) > ppm_flags['dernier_ts']:
                ppm_flags['dernier_ts'] = str(ts)
        return
    if ev == 'PowerplayMerits':
        try:
            gain = int(e.get('MeritsGained') or 0)
            total = e.get('TotalMerits')
            if gain <= 0 or total is None or not st.get('nom'):
                return
            t = ppm_epoch(ts)
            if t is None or t < depuis:
                return
            origine = 'AUTRE'
            if st.get('evt') and t - float(st.get('evt_t') or 0) <= 3.0:
                origine = PPM_ORIGINES.get(st['evt'], 'AUTRE')
            ligne = {'ts': ts, 'systeme': str(st['nom']), 'adresse': int(st['adresse']) if st.get('adresse') is not None else None,
                     'merites': gain, 'total': int(total), 'type': ppm_type(e.get('Power'), st.get('etat')), 'origine': origine}
            with ppm_verrou:
                if len(ppm_attente['mer']) < PPM_MAX_ATTENTE:
                    ppm_attente['mer'].append(ligne)
                    if ts and str(ts) > ppm_flags['dernier_ts']:
                        ppm_flags['dernier_ts'] = str(ts)
        except Exception:
            pass
        return
    if ev in PPM_ORIGINES:
        st['evt'] = ev
        st['evt_t'] = ppm_epoch(ts) or 0.0

def ppm_post(rpc, payload, drapeau):
    """Un appel de fonction serveur. Vrai si le lot est traite (ou si la fonction n'existe pas encore : on ne reessaie pas)."""
    try:
        res = requests.post(f"{SUPABASE_URL}/rest/v1/rpc/{rpc}", headers=get_headers(), json=payload, timeout=20)
        ppm_trace('%s : %d lignes -> HTTP %s %s' % (rpc, len(payload.get('p_lot') or []), res.status_code, (res.text or '')[:120]))
        if res.status_code in (200, 201, 204):
            return True
        if res.status_code == 404:
            ppm_flags[drapeau] = True
            return True
        return False
    except Exception as ex:
        ppm_trace('%s : echec %r' % (rpc, ex))
        return False

def ppm_lire_marqueur():
    try:
        with open(ppm_chemin(PPM_MARQUEUR), 'r', encoding='utf-8') as f:
            d = json.load(f)
        if d.get('rel') != PPM_RELECTURE:
            return ''
        return d.get('dernier_ts') or ''
    except Exception:
        return ''

def ppm_ecrire_marqueur(ts):
    if not ts:
        return
    try:
        with open(ppm_chemin(PPM_MARQUEUR), 'w', encoding='utf-8') as f:
            json.dump({'dernier_ts': ts, 'rel': PPM_RELECTURE}, f)
    except Exception:
        pass

def ppm_envoyer(force=False):
    """Envoie ce qui est en attente, par lots (observations puis merites). Au plus un envoi toutes les PPM_INTERVALLE_ENVOI secondes, sauf force. Renvoie vrai si tout est parti."""
    if ppm_flags['obs_absente'] and ppm_flags['mer_absente']:
        return True
    with ppm_verrou:
        if not ppm_attente['obs'] and not ppm_attente['mer']:
            return True
        if not force and time.time() - ppm_flags['dernier_envoi'] < PPM_INTERVALLE_ENVOI:
            return False
        ppm_flags['dernier_envoi'] = time.time()
    if not get_user_id():
        ppm_trace('envoi differe : get_user_id() vide')
        return False
    for _ in range(PPM_LOTS_PAR_PASSE):
        with ppm_verrou:
            lot_o = list(ppm_attente['obs'][:PPM_LOT_OBS])
            lot_m = list(ppm_attente['mer'][:PPM_LOT_MER])
        if not lot_o and not lot_m:
            break
        ok_o = True
        ok_m = True
        if lot_o and not ppm_flags['obs_absente']:
            ok_o = ppm_post('pp_observer', {'p_lot': lot_o}, 'obs_absente')
        if lot_m and not ppm_flags['mer_absente']:
            ok_m = ppm_post('pp_merites_releve', {'p_lot': lot_m}, 'mer_absente')
        with ppm_verrou:
            if lot_o and ok_o:
                del ppm_attente['obs'][:len(lot_o)]
            if lot_m and ok_m:
                del ppm_attente['mer'][:len(lot_m)]
        if not (ok_o and ok_m):
            ppm_flags['echecs'] += 1
            return False
    ppm_flags['echecs'] = 0
    with ppm_verrou:
        reste = len(ppm_attente['obs']) + len(ppm_attente['mer'])
        dernier = ppm_flags['dernier_ts']
    if reste == 0:
        ppm_ecrire_marqueur(dernier)
    return reste == 0

def ppm_scanner_journaux():
    """Relit les journaux : 45 jours la premiere fois, ensuite seulement depuis le dernier envoi (moins 1 heure). L'etat courant (systeme, puissance qui le controle) est reconstitue
    en suivant les evenements dans l'ordre ; seuls les evenements assez recents sont mis en attente."""
    try:
        jdir = trouver_journal_dir()
        ppm_trace('relecture : dossier des journaux = %s' % jdir)
        if not jdir or not os.path.isdir(jdir):
            return
        marqueur = ppm_lire_marqueur()
        maintenant = time.time()
        depuis = maintenant - PPM_JOURS_RELECTURE * 86400
        if marqueur:
            t = ppm_epoch(marqueur)
            if t is not None:
                depuis = max(depuis, t - 3600)
        fichiers = [f for f in lister_journaux(jdir) if os.path.getmtime(os.path.join(jdir, f)) >= depuis - 2 * 86400]
        st = {'nom': None, 'adresse': None, 'etat': None, 'evt': None, 'evt_t': 0.0}
        nb = 0
        for nom_fichier in fichiers:
            with open(os.path.join(jdir, nom_fichier), 'r', encoding='utf-8', errors='ignore') as f:
                for ligne in f:
                    if not any(m in ligne for m in PPM_MOTS):
                        continue
                    try:
                        e = json.loads(ligne)
                    except Exception:
                        continue
                    if not isinstance(e, dict):
                        continue
                    ppm_traiter_evenement(e, st, depuis)
                    nb += 1
        with ppm_verrou:
            n_o = len(ppm_attente['obs'])
            n_m = len(ppm_attente['mer'])
        ppm_trace('relecture : %d fichiers, %d evenements utiles, en attente : %d observations, %d merites (marqueur = %s)' % (len(fichiers), nb, n_o, n_m, marqueur or 'aucun'))
        for _ in range(40):
            if ppm_envoyer(force=True):
                break
            if ppm_flags['echecs'] > 0:
                break
        ppm_trace('relecture terminee')
    except Exception as ex:
        ppm_trace('relecture : EXCEPTION %r' % (ex,))

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
COL_RELECTURE_COMPLETE = '2.27'     # a changer pour obliger tous les plugins a relire une fois tous leurs journaux
COL_ECHECS_MAX = 5
COL_DELAI_LIVRAISON = 20     # secondes : apres la DERNIERE livraison d'une serie, envoi rapide (au lieu d'attendre jusqu'a 3 a 4 minutes)

col_verrou = threading.Lock()
col_attente = {'chantiers': {}, 'systemes': {}, 'livraisons': [], 'installations': {}, 'economies': {}}
col_stations = {}      # MarketID -> {'station', 'type', 'systeme', 'adresse'} (vu a l'amarrage)
col_positions = {}     # SystemAddress -> {'nom', 'x', 'y', 'z'} (vu aux sauts)
col_courant = {'adresse': None, 'nom': None}
col_etat = {'dernier_envoi': 0, 'rpc_absente': False, 'dernier_ts': '', 'echecs': 0, 'inst_absente': False, 'eco_absente': False}
col_faction = {'nom': None}   # faction de l'escadron (flag SquadronFaction:true vu aux sauts)
col_inst_envoyees = set()     # MarketID d'installations deja envoyees pendant cette session
col_minuteur = {'t': None}    # minuteur d'envoi rapide apres une livraison (remis a zero a chaque nouvelle livraison de la serie)
col_verrou_minuteur = threading.Lock()
col_eco_envoyees = {}         # MarketID -> economies (texte JSON) deja envoyees pendant cette session

def col_economies_liste(entry):
    """Economies d'une station d'apres l'evenement Docked (StationEconomies) : [[nom, proportion], ...], les plus fortes d'abord, 4 au plus."""
    out = []
    for x in (entry.get('StationEconomies') or []):
        if isinstance(x, dict) and x.get('Name'):
            nom = re.sub(r'[^A-Za-z]', '', str(x.get('Name')).replace('$economy_', ''))[:20]
            try:
                part = round(float(x.get('Proportion') or 0), 2)
            except Exception:
                part = 0
            if nom:
                out.append([nom, part])
    return sorted(out, key=lambda a: -a[1])[:4]

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

def col_nom_faction(v):
    """Nom d'une faction : un objet {'Name': ...} aujourd'hui, mais une simple chaine dans les anciens journaux (2022)."""
    if isinstance(v, dict):
        return v.get('Name')
    return v if isinstance(v, str) and v else None

def col_livraison_recente(ts):
    """Vrai si l'evenement date de moins de 2 minutes : une livraison relue dans un ancien journal ne declenche aucun envoi rapide."""
    try:
        t = live_ts(ts)
        return t is not None and 0 <= (datetime.now(timezone.utc) - t).total_seconds() < 120
    except Exception:
        return False


def col_envoi_rapide():
    """Appele par le minuteur, ~COL_DELAI_LIVRAISON s apres la derniere livraison : envoie tout ce qui est en attente (livraisons ET etat a jour du chantier)."""
    try:
        col_envoyer(force=True)
    except Exception:
        pass


def col_programmer_envoi():
    """Apres une livraison : un envoi dans COL_DELAI_LIVRAISON s. Chaque nouvelle livraison de la serie remet le compte a zero, donc toutes les marchandises
    livrees a la suite partent dans UN seul envoi (et le chantier a alors son etat final)."""
    try:
        with col_verrou_minuteur:
            if col_minuteur['t'] is not None:
                col_minuteur['t'].cancel()
            t = threading.Timer(COL_DELAI_LIVRAISON, col_envoi_rapide)
            t.daemon = True
            col_minuteur['t'] = t
            t.start()
    except Exception:
        pass


def col_traiter_evenement(entry):
    """Traite UN evenement de journal. Ne fait aucun appel reseau lui-meme (une livraison recente programme seulement un envoi differe de quelques secondes)."""
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
                                  'faction': col_nom_faction(entry.get('SystemFaction')), 'faction_ts': ts}
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
              and mid not in col_inst_envoyees and col_nom_faction(entry.get('StationFaction')) == col_faction['nom']):
            # Station terminee de la faction de l'escadron : simple CANDIDATE. Le serveur (col_installations, SQL 24) ne la retient que si le
            # systeme est deja connu comme colonise (revendication ou chantier vu) ; les systemes BGS classiques sont jetes la-bas.
            with col_verrou:
                col_attente['installations'][mid] = {'market_id': mid, 'systeme_adresse': entry.get('SystemAddress'),
                                                     'systeme_nom': entry.get('StarSystem'), 'nom': nom_st, 'type_station': typ, 'ts': ts,
                                                     'faction': (col_positions.get(entry.get('SystemAddress')) or {}).get('faction'),
                                                     'faction_ts': (col_positions.get(entry.get('SystemAddress')) or {}).get('faction_ts')}
        if (mid and col_faction['nom'] and typ != 'FleetCarrier' and not nom_st.startswith('$') and 'Construction Site' not in nom_st
                and 'ConstructionDepot' not in typ and col_nom_faction(entry.get('StationFaction')) == col_faction['nom']):
            eco = col_economies_liste(entry)
            if eco and col_eco_envoyees.get(mid) != json.dumps(eco):
                with col_verrou:
                    col_attente['economies'][mid] = {'market_id': mid, 'economies': eco, 'systeme_adresse': entry.get('SystemAddress'), 'nom': nom_st}     # nom + systeme : le serveur retrouve la ligne meme si le MarketID de la station differe de celui du chantier (SQL 89)
        return

    if ev == 'ColonisationSystemClaim':
        adr = entry.get('SystemAddress')
        nom = entry.get('StarSystem')
        if adr and not nom:
            nom = (col_positions.get(adr) or {}).get('nom')            # evenement sans nom de systeme : on reprend celui vu aux sauts
        elif nom and not adr:
            adr = next((a for a, p_ in col_positions.items() if p_.get('nom') == nom), None)
        col_trace('revendication vue : %s | systeme=%s adresse=%s | champs=%s' % (ts, nom, adr, ','.join(sorted(entry.keys()))))
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
            if col_livraison_recente(ts):
                col_programmer_envoi()
        col_maj_ts(ts)
        return

def col_lire_marqueur():
    try:
        with open(col_chemin(COL_MARQUEUR), 'r', encoding='utf-8') as f:
            d = json.load(f)
        if d.get('rel') != COL_RELECTURE_COMPLETE:
            return ''          # marqueur d'une version precedente : une relecture complete est refaite une fois (le serveur ignore les doublons)
        return d.get('dernier_ts') or ''
    except Exception:
        return ''

def col_ecrire_marqueur(ts):
    if not ts:
        return
    try:
        with open(col_chemin(COL_MARQUEUR), 'w', encoding='utf-8') as f:
            json.dump({'dernier_ts': ts, 'rel': COL_RELECTURE_COMPLETE}, f)
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

def col_envoyer_economies(stations):
    """Economie des stations terminees (fonction col_economies, SQL 88) : indice pour identifier le type d'un avant-poste. Remises en attente si l'envoi echoue."""
    if not stations or col_etat['eco_absente']:
        return
    def _rearmer_e(lst):
        with col_verrou:
            for x in lst:
                if x['market_id'] not in col_attente['economies']:
                    col_attente['economies'][x['market_id']] = x
    if not get_user_id():
        _rearmer_e(stations)
        return
    i_e = 0
    try:
        while i_e < len(stations):
            lot_e = stations[i_e:i_e + COL_LOT_INSTALLATIONS]
            res = requests.post(
                f"{SUPABASE_URL}/rest/v1/rpc/col_economies",
                headers=get_headers(),
                json={'p_stations': lot_e},
                timeout=20
            )
            col_trace('col_economies : %d stations -> HTTP %s %s' % (len(lot_e), res.status_code, (res.text or '')[:160]))
            if res.status_code in (200, 201, 204):
                for x in lot_e:
                    col_eco_envoyees[x['market_id']] = json.dumps(x['economies'])
                i_e += len(lot_e)
                continue
            if res.status_code == 404:
                col_etat['eco_absente'] = True      # fonction pas encore creee en base : on ne reessaie pas
                return
            raise RuntimeError('HTTP ' + str(res.status_code))
    except Exception as e:
        col_trace('economies : envoi en echec : %r' % (e,))
        _rearmer_e(stations[i_e:])

def col_envoyer(force=False):
    """Envoie ce qui est en attente, par lots. Au plus un envoi toutes les COL_INTERVALLE_ENVOI secondes, sauf force."""
    if col_etat['rpc_absente']:
        return
    with col_verrou:
        if not (col_attente['chantiers'] or col_attente['systemes'] or col_attente['livraisons'] or col_attente['installations'] or col_attente['economies']):
            return
        if not force and time.time() - col_etat['dernier_envoi'] < COL_INTERVALLE_ENVOI:
            return
        chantiers = list(col_attente['chantiers'].values())
        systemes = list(col_attente['systemes'].values())
        livraisons = list(col_attente['livraisons'])
        installations = list(col_attente['installations'].values())
        economies = list(col_attente['economies'].values())
        col_attente['installations'] = {}
        col_attente['economies'] = {}
        col_attente['chantiers'] = {}
        col_attente['systemes'] = {}
        col_attente['livraisons'] = []
        col_etat['dernier_envoi'] = time.time()

    col_envoyer_installations(installations)
    col_envoyer_economies(economies)

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
                    if not isinstance(e, dict):
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

# =====================================================================================================
# TABLEAU DE BORD EN JEU (script SQL 77) : session en direct du pilote (soute, registre des achats et ventes, missions, position, rang).
# Le plugin LIT LUI-MEME le journal du jeu (toutes les 2 s, uniquement les lignes ajoutees ; au lancement, tous les morceaux de la partie en cours) : il ne depend plus d'EDMC
# pour recevoir les evenements (v2.9 : un Loadout n'arrivait pas). L'etat vit en MEMOIRE ; un instantane compact (quelques ko) part par live_releve(),
# seulement quand quelque chose a change, au plus un envoi toutes les 30 s (un envoi de securite toutes les 5 min tant que le jeu tourne).
# Les ecritures ne comptent pas dans l'egress ; la page lit sa seule ligne (session_live) en temps reel.
# Diagnostic : creer un fichier vide live_debug.txt a cote de load.py pour activer la trace.
# =====================================================================================================
LIVE_INTERVALLE_ENVOI = 30
LIVE_INTERVALLE_MINEUR = 60     # secondes : simple changement de position (sauts), qui ne merite pas un envoi rapide
LIVE_KEEPALIVE = 600
LIVE_MINEURS = frozenset(('FSDTarget', 'FSDJump', 'CarrierJump', 'BackpackChange', 'MiningRefined', 'ProspectedAsteroid', 'LaunchDrone', 'SAASignalsFound'))
LIVE_REGISTRE_MAX = 30
LIVE_MISSIONS_MAX = 40
LIVE_MISSIONS_JOURS = 14     # profondeur de recherche des missions dont la prise est dans une partie precedente
live_inconnues = set()       # missions de la liste « Missions » du jeu dont on ne connait pas encore les details
live_essayees = set()        # missions deja cherchees dans les anciens journaux (on ne les relit pas a chaque passe)
LIVE_TERMINEES_MAX = 30
CMB_SEULS = frozenset(('ShipTargeted', 'Bounty', 'FactionKillBond', 'PVPKill', 'Died', 'Resurrect', 'UnderAttack', 'ShieldState', 'HullDamage', 'UseConsumable', 'RedeemVoucher',
                       'SupercruiseDestinationDrop', 'ApproachSettlement', 'CommitCrime'))     # onglet Combat (2.38) : traites par cmb_traiter, envoyes a part (SQL 103)
LIVE_EVENEMENTS = frozenset((
    'Fileheader', 'LoadGame', 'Loadout', 'Location', 'FSDJump', 'CarrierJump', 'Docked', 'Undocked',
    'MarketBuy', 'MarketSell', 'Cargo', 'MissionAccepted', 'MissionCompleted', 'MissionFailed',
    'MissionAbandoned', 'MissionRedirected', 'Missions', 'Rank', 'Progress', 'Promotion', 'Shutdown',
    'Disembark', 'Embark', 'SuitLoadout', 'Backpack', 'BackpackChange',
    'LaunchSRV', 'DockSRV', 'SRVDestroyed', 'MiningRefined', 'ProspectedAsteroid', 'LaunchDrone', 'CargoTransfer', 'SAASignalsFound',
    'NpcCrewPaidWage', 'NpcCrewRank', 'CrewAssign', 'CrewHire', 'CrewFire', 'LaunchFighter', 'DockFighter', 'FighterDestroyed', 'FighterRebuilt',
    'FSSDiscoveryScan', 'FSSAllBodiesFound', 'FSSBodySignals', 'Scan', 'SAAScanComplete', 'SellExplorationData', 'MultiSellExplorationData', 'ScanOrganic', 'SellOrganicData',     # exploration + exobiologie (2.30)
    'StartJump', 'FSDTarget',     # annonce « saut hyperespace » aux passagers (2.33 / 2.34)
    'ReceiveText', 'Interdicted', 'EscapeInterdiction', 'DataScanned'     # DataScanned : balise de tourisme scannee. Interception en supercruise avec des passagers a bord : annonce + sons de la page (2.35)
)) | CMB_SEULS
live_re_journal = re.compile('"event":"(' + '|'.join(sorted(LIVE_EVENEMENTS)) + ')"')

live_verrou = threading.RLock()
live_etat = {}
live_flags = {'pret': False, 'absente': False, 'sale': False, 'dernier_envoi': 0, 'echecs': 0, 'empreinte': '', 'urgent': False, 'immediat': False}


def live_trace(msg):
    """Diagnostic : n'ecrit QUE si un fichier live_debug.txt existe deja a cote de load.py (le creer vide pour activer)."""
    try:
        chemin = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'live_debug.txt')
        if not os.path.exists(chemin):
            return
        with open(chemin, 'a', encoding='utf-8') as f:
            f.write(datetime.now().strftime('%H:%M:%S') + ' ' + str(msg) + chr(10))
    except Exception:
        pass


LIVE_RHINO_CAP = 72     # emplacements de la soute du Rhino (le journal ne donne pas la capacite d'un VRS : valeur constatee en jeu)


def live_minage_neuf():
    """Etat du minage de la session : soute du Rhino (minerais raffines depuis le dernier transfert), totaux par mode, dernier asteroide prospecte, drones."""
    return {'srv': '', 'rhino': {'det': {}, 'cap': LIVE_RHINO_CAP, 'dt': None}, 'tot': {'rhino': {}, 'vaisseau': {}}, 'deb': {}, 'fin': {}, 'noms': {}, 'pros': None, 'dr': {}, 'sig': {}, 'hot': {}}


def live_cle_minerai(t):
    """'$osmium_name;' -> 'osmium' (meme cle que les transferts de soute et les ventes)."""
    s = str(t or '').strip().lower()
    if s.startswith('$'):
        s = s[1:]
    if s.endswith('_name;'):
        s = s[:-6]
    return s


def live_minage_mode(m):
    return 'rhino' if 'rhino' in str(m.get('srv') or '') else 'vaisseau'


def live_minage_raffine(e):
    """Un minerai raffine (un evenement par unite) : au Rhino s'il est sorti, sinon au vaisseau."""
    m = live_etat['minage']
    k = live_cle_minerai(e.get('Type'))
    if not k:
        return
    ts = str(e.get('timestamp') or '')
    if len(m['noms']) < 40:
        m['noms'][k] = live_texte(e.get('Type_Localised') or k)
    mode = live_minage_mode(m)
    tot = m['tot'][mode]
    tot[k] = tot.get(k, 0) + 1
    if mode not in m['deb']:
        m['deb'][mode] = ts
    m['fin'][mode] = ts
    if mode == 'rhino':
        det = m['rhino']['det']
        det[k] = det.get(k, 0) + 1


def live_minage_transfert(e):
    """Transfert de soute entre le VRS et le vaisseau : 'toship' vide le Rhino, 'tosrv' le remplit."""
    m = live_etat['minage']
    det = m['rhino']['det']
    change = False
    for tr in (e.get('Transfers') or []):
        k = live_cle_minerai(tr.get('Type'))
        n = _bn(tr.get('Count'))
        sens = str(tr.get('Direction') or '').lower()
        if not k or n <= 0:
            continue
        if sens == 'toship':
            det[k] = det.get(k, 0) - n
            change = True
        elif sens == 'tosrv':
            det[k] = det.get(k, 0) + n
            change = True
    for k in [k for k, v in det.items() if v <= 0]:
        del det[k]
    if change:
        m['rhino']['dt'] = str(e.get('timestamp') or '')


def live_soute_transfert(e):
    """Un transfert VRS <-> vaisseau change la soute du VAISSEAU, mais Cargo.json ne decrit que le VRS tant qu'on est dedans : on tient donc la soute du vaisseau a jour ici."""
    et = live_etat
    sv = et.get('soute_vaisseau') or {'Vessel': 'Ship', 'Inventory': []}
    inv = {}
    for i in (sv.get('Inventory') or []):
        inv[str(i.get('Name') or '').lower()] = dict(i)
    noms = et['minage']['noms']
    for tr in (e.get('Transfers') or []):
        k = live_cle_minerai(tr.get('Type'))
        n = _bn(tr.get('Count'))
        sens = str(tr.get('Direction') or '').lower()
        if not k or n <= 0:
            continue
        it = inv.get(k)
        if it is None:
            it = {'Name': k, 'Name_Localised': noms.get(k) or k.capitalize(), 'Count': 0}
            inv[k] = it
        if sens == 'toship':
            it['Count'] = _bn(it.get('Count')) + n
        elif sens == 'tosrv':
            it['Count'] = _bn(it.get('Count')) - n
    et['soute_vaisseau'] = {'Vessel': 'Ship', 'Inventory': [v for v in inv.values() if _bn(v.get('Count')) > 0]}


def live_minage_signaux(e):
    """Scan de surface d'un corps (SAASignalsFound) : nombre d'emplacements d'extraction planetaire du corps."""
    nom = live_texte(e.get('BodyName'))
    if not nom:
        return
    if nom.endswith(' Ring'):
        # anneau scanne : points chauds = [[minerai (nom anglais), nombre], ...] (les signaux $SAA_... ne sont pas des minerais)
        hot = []
        for s in (e.get('Signals') or []):
            t = str(s.get('Type') or '')
            if t and not t.startswith('$') and _bn(s.get('Count')) > 0:
                hot.append([t, _bn(s.get('Count'))])
        hots = live_etat['minage']['hot']
        if hot:
            hots[nom] = hot[:12]
            if len(hots) > 24:
                del hots[next(iter(hots))]
        return
    n = 0
    for s in (e.get('Signals') or []):
        if 'PlanetaryMiningLocation' in str(s.get('Type') or ''):
            n = max(n, _bn(s.get('Count')))
    sig = live_etat['minage']['sig']
    if n > 0:
        sig[nom] = n
        if len(sig) > 60:
            del sig[next(iter(sig))]


def live_minage_prospecte(e):
    """Dernier asteroide prospecte (vaisseau) : composition, richesse, matiere de noyau eventuelle."""
    mats = []
    for x in (e.get('Materials') or [])[:8]:
        try:
            mats.append([live_texte(x.get('Name_Localised') or x.get('Name')), round(float(x.get('Proportion') or 0), 1)])
        except Exception:
            pass
    try:
        reste = int(round(float(e.get('Remaining') or 0)))
    except Exception:
        reste = 0
    live_etat['minage']['pros'] = {'t': str(e.get('timestamp') or ''), 'mat': mats, 'ct': live_texte(e.get('Content_Localised') or e.get('Content')),
                                   'reste': reste, 'coeur': live_texte(e.get('MotherlodeMaterial_Localised') or e.get('MotherlodeMaterial'))}


def live_minage_instantane(m):
    """Minage pour l'instantane : rien (None) tant qu'on n'a pas mine, pour ne pas alourdir l'envoi."""
    if not (m.get('deb') or m['rhino']['det'] or m.get('pros') or m.get('dr') or m.get('sig') or m.get('hot')):
        return None
    return {'srv': 1 if live_minage_mode(m) == 'rhino' and m.get('srv') else 0,
            'rhino': {'det': dict(m['rhino']['det']), 'cap': m['rhino']['cap'], 'dt': m['rhino']['dt']},
            'tot': {'rhino': dict(m['tot']['rhino']), 'vaisseau': dict(m['tot']['vaisseau'])},
            'deb': dict(m['deb']), 'fin': dict(m['fin']), 'noms': dict(m['noms']), 'pros': m.get('pros'), 'dr': dict(m['dr']), 'sig': dict(m.get('sig') or {}), 'hot': dict(m.get('hot') or {})}


def live_nouvel_etat():
    return {
        'debut': None, 'en_ligne': False, 'systeme': '', 'station': '', 'amarre': False, 'pos': None,
        'vaisseau': {'nom': '', 'modele': '', 'cap': 0, 'saut': 0, 'cab': [], 'mods': []},
        'paye': {},                      # marchandise -> [quantite, cout total] : prix moyen paye
        'registre': [], 'tot': {'achats': 0, 'ventes': 0, 'benef': 0, 'tonnes': 0, 'nb': 0},
        'missions': {}, 'terminees': [], 'rep': {}, 'rang': {}, 'prog': {},
        'pied': {'on': False, 'suit': '', 'loadout': '', 'mods': [], 'armes': {}, 'sac': {'data': {}, 'comp': {}, 'items': {}, 'conso': {}}},     # a pied : combinaison, armes, sac a dos
        'minage': live_minage_neuf(),
        'equipage': {},                  # equipage PNJ : identifiant -> {n, g (rang de combat), r (role)}
        'chasseur': None,                # chasseur du hangar : {n, pc (pilote par le joueur), e : vol | hangar | detruit}
        'cible': None,                   # systeme vise pour le prochain saut (dernier FSDTarget non atteint)
        'chargement': None,              # dernier transfert de marchandises VERS le vaisseau (ex. soute du Rhino) : {t, n} ; la page joue le son de chargement
        'tourisme': None,                # derniere balise de tourisme scannee AVEC des passagers a bord : {t, sys} ; la page joue les appareils photo et l'annonce
        'interception': None,            # derniere interception subie AVEC des passagers a bord : {t, e : debut | echappe | capture} ; la page joue l'annonce et les sons
        'saut': None                     # dernier saut hyperespace fait AVEC des passagers a bord : {t (horodatage du journal), sys (destination)} ; la page annonce aux passagers
    }


live_etat.update(live_nouvel_etat())


def live_ts(ts):
    try:
        return datetime.strptime(str(ts)[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
    except Exception:
        return None


def live_texte(s):
    return str(s or '').replace('&nbsp;', ' ').replace(chr(160), ' ').strip()


# places par cabine de passagers : (taille, classe) -> places (donnees Coriolis : classe 1 = Eco, 2 = Affaires, 3 = Premiere, 4 = Luxe)
LIVE_CABINES = {(2, 1): 2, (3, 1): 4, (4, 1): 8, (5, 1): 16, (6, 1): 32, (3, 2): 3, (4, 2): 6, (5, 2): 10, (6, 2): 16,
                (4, 3): 3, (5, 3): 6, (6, 3): 12, (5, 4): 4, (6, 4): 8}


def live_cabines(modules):
    """Liste des cabines de passagers du vaisseau, [[classe, places], ...], de la classe la plus haute a la plus basse (cabine inconnue = ignoree)."""
    res = []
    try:
        for mod in (modules or []):
            m = re.match(r'^int_(mkii_)?passengercabin_size([0-9])_class([0-9])$', str(mod.get('Item') or '').lower())
            if m:
                places = LIVE_CABINES.get((int(m.group(2)), int(m.group(3))))
                if places:
                    if m.group(1):
                        places = int(places * 1.5)
                    res.append([int(m.group(3)), places])
    except Exception:
        return []
    res.sort(key=lambda c: (-c[0], -c[1]))
    return res


# a pied (Odyssey) : combinaison, armes et sac a dos. Le jeu ne donne PAS les outils equipes ni la capacite du sac.
LIVE_SAC_TYPES = {'item': 'items', 'component': 'comp', 'consumable': 'conso', 'data': 'data'}


def live_costume(e):
    p = live_etat['pied']
    p['suit'] = str(e.get('SuitName') or '').lower()
    p['loadout'] = live_texte(e.get('LoadoutName'))
    p['mods'] = [str(x) for x in (e.get('SuitMods') or [])][:12]
    armes = {}
    cles = {'PrimaryWeapon1': 'p1', 'PrimaryWeapon2': 'p2', 'SecondaryWeapon': 's'}
    for mod in (e.get('Modules') or []):
        k = cles.get(str(mod.get('SlotName') or ''))
        if k:
            armes[k] = {'n': live_texte(mod.get('ModuleName_Localised') or mod.get('ModuleName')), 'c': _bn(mod.get('Class')),
                        'm': [str(x) for x in (mod.get('WeaponMods') or [])][:8]}
    p['armes'] = armes


def live_sac_complet(e):
    """Contenu complet du sac a dos (evenement Backpack ; si l'evenement est vide, on lit Backpack.json)."""
    src = e
    if not any(k in e for k in ('Items', 'Components', 'Consumables', 'Data')):
        src = live_lire_fichier('Backpack.json') or {}
    sac = {'data': {}, 'comp': {}, 'items': {}, 'conso': {}}
    for cle, cat in (('Items', 'items'), ('Components', 'comp'), ('Consumables', 'conso'), ('Data', 'data')):
        for it in (src.get(cle) or []):
            nom = live_texte(it.get('Name_Localised') or it.get('Name'))
            n = _bn(it.get('Count'))
            if nom and n > 0:
                sac[cat][nom] = sac[cat].get(nom, 0) + n
    live_etat['pied']['sac'] = sac


def live_sac_change(e):
    sac = live_etat['pied']['sac']
    for cle, signe in (('Added', 1), ('Removed', -1)):
        for it in (e.get(cle) or []):
            cat = LIVE_SAC_TYPES.get(str(it.get('Type') or '').lower())
            nom = live_texte(it.get('Name_Localised') or it.get('Name'))
            if not cat or not nom:
                continue
            n = sac[cat].get(nom, 0) + signe * _bn(it.get('Count'))
            if n > 0:
                sac[cat][nom] = n
            else:
                sac[cat].pop(nom, None)


# modules du vaisseau (Loadout) : emplacement, module, allume, niveau d'ingenierie ; detail (plan, effet, ingenieur, modifications) seulement pour les modules OPTIONNELS
LIVE_SLOTS_IGNORES = re.compile(r'^(Decal|ShipName|ShipID|PaintJob|VesselVoice|Bobble|Nameplate|WeaponColour|EngineColour|ShipKit|ShipCockpit|CargoHatch|Hologram)')
LIVE_SLOTS_OPTIONNELS = re.compile(r'^(Slot[0-9]+_Size[0-9]+|Military[0-9]+|Passenger[0-9]+|Cargo[0-9]+|PlanetaryApproachSuite)$')


def live_modules(modules):
    res = []
    try:
        for mod in (modules or []):
            slot = str(mod.get('Slot') or '')
            if not slot or LIVE_SLOTS_IGNORES.match(slot):
                continue
            o = {'s': slot, 'i': str(mod.get('Item') or '').lower(), 'o': 1 if mod.get('On', True) else 0}
            g = mod.get('Engineering')
            if isinstance(g, dict):
                o['g'] = int(g.get('Level') or 0)
                if LIVE_SLOTS_OPTIONNELS.match(slot):
                    o['b'] = str(g.get('BlueprintName') or '')
                    o['x'] = str(g.get('ExperimentalEffect') or '')
                    o['e'] = live_texte(g.get('Engineer'))
                    o['m'] = [[str(x.get('Label') or ''), round(float(x.get('OriginalValue') or 0), 3), round(float(x.get('Value') or 0), 3), 1 if x.get('LessIsGood') else 0] for x in (g.get('Modifiers') or [])][:10]
            res.append(o)
    except Exception:
        return []
    return res[:90]


def live_type_mission(nom):
    n = str(nom or '').lower()
    for cle, mot in (('don', 'altruism'), ('passagers', 'passenger'), ('courrier', 'courier'), ('collecte', 'collect'), ('livraison', 'delivery'),
                     ('sauvetage', 'salvage'), ('minage', 'mining'), ('massacre', 'massacre'), ('assassinat', 'assassinate'), ('exploration', 'sightsee')):
        if mot in n:
            return cle
    return 'autre'


def live_ajouter_registre(ligne):
    reg = live_etat['registre']
    reg.append(ligne)
    if len(reg) > LIVE_REGISTRE_MAX:
        del reg[:len(reg) - LIVE_REGISTRE_MAX]


live_rangs = {}                 # identifiant PNJ -> rang de combat : le journal ne l'ecrit qu'a un CHANGEMENT de rang, on le retrouve donc dans les anciens journaux
live_rangs_cherches = set()
LIVE_RANG_FICHIERS_MAX = 400    # nombre maximal de journaux relus (du plus recent au plus ancien) pour retrouver un rang


def live_rangs_sauver():
    try:
        with open(col_chemin('crew_rangs.json'), 'w', encoding='utf-8') as f:
            json.dump(live_rangs, f)
    except Exception:
        pass


def live_rang_historique(cid):
    """Dernier rang de combat connu d'un membre d'equipage PNJ : memoire, fichier crew_rangs.json, puis anciens journaux (une seule recherche par lancement)."""
    if cid in live_rangs:
        return live_rangs[cid]
    if cid in live_rangs_cherches:
        return -1
    live_rangs_cherches.add(cid)
    try:
        chemin = col_chemin('crew_rangs.json')
        if not live_rangs and os.path.exists(chemin):
            with open(chemin, 'r', encoding='utf-8') as f:
                for k, v in json.load(f).items():
                    live_rangs[str(k)] = int(v)
        if cid in live_rangs:
            return live_rangs[cid]
        jdir = trouver_journal_dir()
        if jdir and os.path.isdir(jdir):
            for nom in list(reversed(lister_journaux(jdir)))[:LIVE_RANG_FICHIERS_MAX]:
                trouve = -1
                try:
                    with open(os.path.join(jdir, nom), 'r', encoding='utf-8', errors='ignore') as f:
                        for ligne in f:
                            if '"NpcCrewRank"' in ligne and cid in ligne:
                                try:
                                    j = json.loads(ligne)
                                except Exception:
                                    continue
                                if str(j.get('NpcCrewId')) == cid:
                                    trouve = _bn(j.get('RankCombat'))
                except OSError:
                    continue
                if trouve >= 0:
                    live_rangs[cid] = trouve
                    live_rangs_sauver()
                    break
    except Exception:
        pass
    return live_rangs.get(cid, -1)


def live_equipage(e):
    """Equipage PNJ du commandant : present des qu'il est paye (a chaque lancement du jeu), retire s'il est renvoye."""
    ev = e.get('event')
    eq = live_etat['equipage']
    cid = str(e.get('NpcCrewId') if e.get('NpcCrewId') is not None else e.get('CrewID'))
    if cid in ('None', ''):
        return
    if ev == 'CrewFire':
        eq.pop(cid, None)
        return
    m = eq.setdefault(cid, {'n': '', 'g': -1, 'r': ''})
    if m['g'] < 0 and ev not in ('NpcCrewRank', 'CrewHire'):
        m['g'] = live_rang_historique(cid)
    nom = live_texte(e.get('NpcCrewName') or e.get('Name'))
    if nom:
        m['n'] = nom
    if ev == 'NpcCrewRank':
        m['g'] = _bn(e.get('RankCombat'))
        live_rangs[cid] = m['g']
        live_rangs_sauver()
    elif ev == 'CrewHire':
        m['g'] = _bn(e.get('CombatRank'))
        live_rangs[cid] = m['g']
        live_rangs_sauver()
    elif ev == 'CrewAssign':
        m['r'] = str(e.get('Role') or '')[:20]


def live_chasseur(e):
    """Chasseur du hangar : lance (par un PNJ ou par le joueur), revenu au hangar, detruit (puis reconstruit)."""
    ev = e.get('event')
    ch = live_etat.get('chasseur')
    if ev == 'LaunchFighter':
        live_etat['chasseur'] = {'n': live_texte(e.get('_Localised') or e.get('') or 'Fighter')[:40], 'pc': bool(e.get('PlayerControlled')), 'e': 'vol'}
    elif ch:
        ch['e'] = 'detruit' if ev == 'FighterDestroyed' else 'hangar'


# ==========================================
# EXPLORATION (onglet Exploration du tableau de bord) : trajet de l'expedition, corps du systeme en cours, valeur ESTIMEE des donnees non vendues.
# Les valeurs viennent de la formule publiee par la communaute (EDDiscovery, forum Frontier « Exploration value formulae »), verifiee sur les ventes du proprietaire :
# exacte pour les etoiles, BORNE BASSE pour les planetes scannees au FSS (jamais de surestimation sur 37 ventes), non verifiee pour les cartographies (DSS).
# Les ceintures d'asteroides (« Belt Cluster ») sont payees par le jeu mais absentes de la formule : seulement comptees.
# L'etat est garde dans explo_etat.json (a cote de load.py) : il survit a un relancement du jeu ; au premier lancement il est reconstitue depuis les journaux recents.
# ==========================================
EXPLO_FICHIER = 'explo_etat.json'
EXPLO_TRAJET_MAX = 160            # points du trajet envoyes au site (au-dela, les plus anciens sont eclaircis)
EXPLO_MARQUES_MAX = 40
EXPLO_DECOUVERTES_MAX = 40        # corps listes dans le journal « ce que j'ai decouvert » (les plus recents)
EXPLO_JOURS_RATTRAPAGE = 45       # premier lancement : journaux relus sur ces derniers jours
EXPLO_BALISE = False               # les scans de balise de navigation comptent-ils ? (a confirmer par les ventes)
EXPLO_SEUIL_OR = 400000           # « trouvaille de grande valeur » : valeur estimee (cartographie efficace comprise) a partir de ce montant
EXPLO_EVENEMENTS = frozenset(('FSSDiscoveryScan', 'FSSAllBodiesFound', 'FSSBodySignals', 'SAASignalsFound', 'Scan', 'SAAScanComplete',
                              'SellExplorationData', 'MultiSellExplorationData', 'ScanOrganic', 'SellOrganicData', 'FSDJump', 'CarrierJump', 'Location'))
explo_re_journal = re.compile('"event":"(' + '|'.join(sorted(EXPLO_EVENEMENTS)) + ')"')
explo_mem = {'v': 1, 'ts': '', 'sys': None, 'corps': {}, 'trajet': [], 'marques': [], 'sauts': 0, 'dist': 0.0, 'debut': '', 'vente': None, 'pos': None, 'bio': {'ech': {}, 'vente': None}}
EXPLO_ECH_MAX = 60               # echantillons d'exobiologie envoyes au site (les plus recents)
explo_etat_init = {'fait': False, 'sale': False, 'sauve': 0.0}


def explo_k_etoile(t):
    t = str(t or '')
    if t in ('N', 'H'):
        return 22628.0
    if t == 'SupermassiveBlackHole':
        return 33.5678
    if t.startswith('D'):
        return 14057.0
    return 1200.0


def explo_k_planete(classe, tf):
    c = str(classe or '').lower()
    if c == 'metal rich body':
        return 21790.0 + (65631.0 if tf else 0.0)
    if c == 'ammonia world':
        return 96932.0
    if c == 'sudarsky class i gas giant':
        return 1656.0
    if c in ('sudarsky class ii gas giant', 'high metal content body'):
        return 9654.0 + (100677.0 if tf else 0.0)
    if c == 'water world':
        return 64831.0 + (116295.0 if tf else 0.0)
    if c == 'earthlike body':
        return 64831.0 + 116295.0
    return 300.0 + (93328.0 if tf else 0.0)


def explo_ody(v):
    return v + max(v * 0.3, 555.0)


def explo_valeurs(etoile, typ, masse, tf, wasdisc, wasmap, cartographie, efficace):
    """Valeurs estimees d'un corps : (valeur actuelle, valeur potentielle avec cartographie efficace, part « premiere decouverte » de la valeur actuelle)."""
    pd = 1.0 if wasdisc else 2.6
    try:
        m = max(float(masse), 0.0)
    except Exception:
        m = 1.0
    if etoile:
        k = explo_k_etoile(typ)
        v = (k + m * k / 66.25) * pd
        return int(v), int(v), int(v - v / pd)

    base = max(explo_k_planete(typ, tf) * (1.0 + (m ** 0.2) * 0.56591828), 500.0)

    def carte(eff):
        if not wasdisc and not wasmap:
            v = explo_ody(base * 3.699622554) * 2.6
        elif wasdisc and not wasmap:
            v = explo_ody(base * 8.0956)
        else:
            v = explo_ody(base * 3.3333333)
        return v * (1.25 if eff else 1.0)

    actuelle = carte(efficace) if cartographie else base * pd
    potentielle = max(carte(True), actuelle)
    return int(actuelle), int(potentielle), int(actuelle - actuelle / pd)


def explo_cle_codex(brut):
    """'$Codex_Ent_Stratum_Genus_Name;' -> 'Stratum' ; '$Codex_Ent_Stratum_07_Name;' -> 'Stratum_07' (independant de la langue du jeu)."""
    t = str(brut or '').replace('$Codex_Ent_', '').replace('_Genus_Name;', '').replace('_Name;', '').replace(';', '')
    t = re.sub(r'_[A-Z]_?$', '', t)
    return t


def explo_ajouter_point(p):
    t = explo_mem['trajet']
    t.append(p)
    if len(t) > EXPLO_TRAJET_MAX:
        moitie = len(t) // 2
        explo_mem['trajet'] = t[:moitie:2] + t[moitie:]      # les plus anciens sont eclaircis, les recents restent precis


def explo_marque(typ, nom, cle_sys):
    s = explo_mem['sys']
    if not s or not explo_mem['pos']:
        return
    k = '%s|%s' % (typ, nom if typ == 'or' else cle_sys)
    if k in s.setdefault('mk', {}):
        return
    s['mk'][k] = 1
    ms = explo_mem['marques']
    ms.append({'p': explo_mem['pos'], 't': typ, 'n': str(nom)[:40]})
    if len(ms) > EXPLO_MARQUES_MAX:
        del ms[0]


def explo_nouvelle_expedition(ts):
    m = explo_mem
    dernier = m['trajet'][-1] if m['trajet'] else m['pos']
    m['trajet'] = [dernier] if dernier else []
    m['marques'] = []
    m['sauts'] = 0
    m['dist'] = 0.0
    m['debut'] = ts


def explo_court(nom, sysnom):
    n = str(nom or '')
    s = str(sysnom or '')
    if n == s:
        return 'A'
    if s and n.startswith(s):
        n = n[len(s):].strip()
    return n or str(nom)


def explo_corps_systeme(e, cle, c, nom_court):
    """Fiche du corps pour la carte du systeme (envoyee au site) ; seulement si le corps est dans le systeme en cours."""
    s = explo_mem['sys']
    if not s or s.get('adr') != e.get('SystemAddress'):
        return
    pere = None
    par = e.get('Parents')
    if isinstance(par, list) and par and isinstance(par[0], dict) and 'Planet' in par[0]:
        pere = par[0].get('Planet')
    est = 1 if e.get('StarType') else 0
    b = {'i': e.get('BodyID'), 'n': nom_court, 'c': (e.get('StarType') if est else e.get('PlanetClass')) or '', 'e': est,
         'm': round(float(e.get('StellarMass') if est else (e.get('MassEM') or 0)), 4),
         'd': round(float(e.get('DistanceFromArrivalLS') or 0), 1), 'p': pere,
         'a': round(float(e.get('SemiMajorAxis') or 0) / 1.495978707e11, 4),
         'wd': 1 if e.get('WasDiscovered') else 0, 'wm': 1 if e.get('WasMapped') else 0, 'wf': 1 if e.get('WasFootfalled') else 0,
         'tf': 1 if e.get('TerraformState') == 'Terraformable' else 0, 'l': 1 if e.get('Landable') else 0,
         'g': round(float(e.get('SurfaceGravity') or 0) / 9.80665, 2), 'v': c['v'], 'pot': c['pot'], 'cm': c['c']}
    if est:
        b['sp'] = '%s%s' % (e.get('StarType') or '', e.get('Subclass') if e.get('Subclass') is not None else '')
    else:
        rg = e.get('Rings')
        if isinstance(rg, list) and rg:
            b['rg'] = len(rg)                                              # anneaux (dessines autour de la planete)
        at = str(e.get('Atmosphere') or '').lower()
        if at and at != 'no atmosphere':
            b['at'] = at[:30]                                              # atmosphere (halo autour de la planete)
    s.setdefault('b', {})[str(e.get('BodyID'))] = b


def explo_signaux(e, ev):
    s = explo_mem['sys']
    if not s or s.get('adr') != e.get('SystemAddress'):
        return
    bio = geo = 0
    for sg in (e.get('Signals') or []):
        t = (str(sg.get('Type') or '') + str(sg.get('Type_Localised') or '')).lower()
        n = int(sg.get('Count') or 0)
        if 'biolog' in t:
            bio += n
        elif 'geolog' in t:
            geo += n
    d = s.setdefault('sg', {}).setdefault(str(e.get('BodyID')), {})
    if bio:
        d['bio'] = bio
    if geo:
        d['geo'] = geo
    if ev == 'SAASignalsFound' and e.get('Genuses'):
        d['gen'] = [live_texte(g.get('Genus_Localised') or g.get('Genus')) for g in e.get('Genuses')][:8]
        d['gk'] = [explo_cle_codex(g.get('Genus')) for g in e.get('Genuses')][:8]     # cle du genre : « Stratum », « Aleoids »... (independante de la langue du jeu)
    c = explo_mem['corps'].get('%s:%s' % (e.get('SystemAddress'), e.get('BodyID')))
    if c is not None and bio:
        c['bio'] = bio
    if bio:
        explo_marque('bio', live_texte(e.get('BodyName')), '%s|%s' % (e.get('SystemAddress'), e.get('BodyID')))


def explo_appliquer(e, ev, ts):
    m = explo_mem
    if ev in ('FSDJump', 'CarrierJump', 'Location'):
        adr = e.get('SystemAddress')
        nom = live_texte(e.get('StarSystem'))
        if not adr or not nom:
            return
        pos = e.get('StarPos')
        ok = isinstance(pos, list) and len(pos) == 3
        nouveau = not m['sys'] or m['sys'].get('adr') != adr
        if nouveau:
            m['sys'] = {'adr': adr, 'nom': nom, 'nb': None, 'prog': 0, 'tous': False, 'cb': {}, 'b': {}, 'sg': {}, 'mk': {}}
        if ok:
            p = [round(float(v), 1) for v in pos]
            m['pos'] = p
            if not m['trajet'] or (nouveau and m['trajet'][-1] != p):
                explo_ajouter_point(p)
        if ev == 'FSDJump' and nouveau:
            m['sauts'] += 1
            m['dist'] += float(e.get('JumpDist') or 0)
        if not m['debut']:
            m['debut'] = ts
        return

    s = m['sys']
    if ev == 'FSSDiscoveryScan':
        if s and s.get('adr') == e.get('SystemAddress'):
            s['nb'] = e.get('BodyCount')
            s['prog'] = e.get('Progress') or 0
        return
    if ev == 'FSSAllBodiesFound':
        if s and s.get('adr') == e.get('SystemAddress'):
            s['tous'] = True
        return
    if ev in ('FSSBodySignals', 'SAASignalsFound'):
        explo_signaux(e, ev)
        return

    if ev == 'Scan':
        if e.get('ScanType') == 'NavBeaconDetail' and not EXPLO_BALISE:
            return
        nom = live_texte(e.get('BodyName'))
        adr = e.get('SystemAddress')
        bid = e.get('BodyID')
        if not nom or adr is None or bid is None:
            return
        if 'Belt Cluster' in nom:
            if s and s.get('adr') == adr:
                s['cb'][str(bid)] = 1
            return
        est = bool(e.get('StarType'))
        if not est and not e.get('PlanetClass'):
            return                                              # anneaux et autres : pas de valeur dans la formule
        cle = '%s:%s' % (adr, bid)
        typ = e.get('StarType') if est else e.get('PlanetClass')
        masse = e.get('StellarMass') if est else e.get('MassEM')
        tf = e.get('TerraformState') == 'Terraformable'
        sysnom = live_texte(e.get('StarSystem')) or (s or {}).get('nom') or ''
        c = m['corps'].get(cle)
        if c is None:
            c = {'s': sysnom, 'n': explo_court(nom, sysnom), 'e': 1 if est else 0, 't': typ, 'm': masse if masse is not None else 1.0, 'tf': 1 if tf else 0,
                 'wd': 1 if e.get('WasDiscovered') else 0, 'wm': 1 if e.get('WasMapped') else 0, 'c': 0, 'f': 0}
            m['corps'][cle] = c
        c['ts'] = ts
        m['corps'].pop(cle, None)
        m['corps'][cle] = c                                      # le plus recent en dernier (journal du site)
        v, pot, pdv = explo_valeurs(bool(c['e']), c['t'], c['m'], bool(c['tf']), bool(c['wd']), bool(c['wm']), bool(c['c']), bool(c['f']))
        c['v'] = v
        c['p'] = pdv
        c['pot'] = pot
        court = explo_court(nom, sysnom)
        explo_corps_systeme(e, cle, c, court)
        if not c['wd']:
            explo_marque('pd', sysnom, sysnom)
        if pot >= EXPLO_SEUIL_OR:
            explo_marque('or', '%s · %s' % (sysnom, court), sysnom)
        return

    if ev == 'SAAScanComplete':
        cle = '%s:%s' % (e.get('SystemAddress'), e.get('BodyID'))
        c = m['corps'].get(cle)
        if c is None:
            return
        c['c'] = 1
        c['ts'] = ts
        m['corps'].pop(cle, None)
        m['corps'][cle] = c
        c['f'] = 1 if (e.get('ProbesUsed') or 99) <= (e.get('EfficiencyTarget') or 0) else 0
        v, pot, pdv = explo_valeurs(bool(c['e']), c['t'], c['m'], bool(c['tf']), bool(c['wd']), bool(c['wm']), True, bool(c['f']))
        c['v'] = v
        c['p'] = pdv
        c['pot'] = pot
        if s and s.get('adr') == e.get('SystemAddress'):
            b = (s.get('b') or {}).get(str(e.get('BodyID')))
            if b is not None:
                b['v'] = v
                b['pot'] = pot
                b['cm'] = 1
        return

    if ev == 'ScanOrganic':
        bio = m['bio']
        sp = explo_cle_codex(e.get('Species'))
        if not sp:
            return
        cle = '%s:%s:%s' % (e.get('SystemAddress'), e.get('Body'), sp)
        k = {'log': 1, 'sample': 2, 'analyse': 3}.get(str(e.get('ScanType') or '').lower(), 0)
        if not k:
            return
        sy = s.get('nom') if s and s.get('adr') == e.get('SystemAddress') else ''
        bn = ''
        if s and s.get('adr') == e.get('SystemAddress'):
            bn = ((s.get('b') or {}).get(str(e.get('Body'))) or {}).get('n') or ''
        c = bio['ech'].get(cle)
        if c is None:
            c = {'g': live_texte(e.get('Genus_Localised') or e.get('Genus')), 'gk': explo_cle_codex(e.get('Genus')), 's': live_texte(e.get('Species_Localised') or e.get('Species')), 'sc': sp,
                 'va': live_texte(e.get('Variant_Localised') or e.get('Variant')), 'k': 0, 'nv': 0, 'sy': sy, 'bn': bn, 'bi': e.get('Body'), 'adr': e.get('SystemAddress')}
            bio['ech'][cle] = c
        if k == 1 and not e.get('WasLogged'):
            c['nv'] = 1                                      # premiere fois dans le codex du pilote (le jeu donne cette information au premier prelevement)
        if k > c['k']:
            c['k'] = k
        if sy and not c.get('sy'):
            c['sy'] = sy
        if bn and not c.get('bn'):
            c['bn'] = bn
        c['t'] = ts
        pp = explo_position_vivante(ts)
        if pp:                                               # ou le prelevement a ete fait (3 sites par espece : Log, Sample, Analyse) : sert a verifier la distance minimale entre deux sites
            pts = c.setdefault('pts', [None, None, None])
            pts[k - 1] = [pp['la'], pp['lo']]
            c['r'] = pp['r'] or c.get('r', 0)
            c['bf'] = pp['bn'] or c.get('bf', '')
        return

    if ev == 'SellOrganicData':
        if bio_vente_deja(ts):
            return
        bio = m['bio']
        reel = 0
        vendus = []
        for it in (e.get('BioData') or []):
            reel += int(it.get('Value') or 0) + int(it.get('Bonus') or 0)
            sp = explo_cle_codex(it.get('Species'))
            vendus.append(sp)
            for k2 in [k for k, c in bio['ech'].items() if c.get('sc') == sp and c.get('k') == 3][:1]:
                del bio['ech'][k2]
        if reel or vendus:
            bio['vente'] = {'t': ts, 'reel': int(reel), 'sc': vendus[:40], 'n': len(vendus)}
        return

    if ev in ('SellExplorationData', 'MultiSellExplorationData'):
        if m['vente'] and m['vente'].get('t') == ts:
            return                   # vente deja traitee (relecture)
        if ev == 'SellExplorationData':
            noms = set(e.get('Systems') or [])
        else:
            noms = set(x.get('SystemName') for x in (e.get('Discovered') or []))
        cles = [k for k, c in m['corps'].items() if c.get('s') in noms]
        est = sum(m['corps'][k].get('v', 0) for k in cles)
        reel = int(e.get('BaseValue') or 0) + int(e.get('Bonus') or 0)
        if reel or est:
            m['vente'] = {'t': ts, 'est': int(est), 'reel': reel, 'tot': int(e.get('TotalEarnings') or 0), 'n': len(cles)}
        for k in cles:
            del m['corps'][k]
        explo_nouvelle_expedition(ts)                            # les statistiques repartent a zero a chaque vente
        return



def explo_position_vivante(ts):
    """Position du pilote (latitude, longitude, rayon de la planete, nom du corps) lue dans Status.json, seulement pour un evenement vivant (moins de 2 minutes) : jamais pour une relecture de vieux journaux."""
    try:
        d = datetime.strptime(ts, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        if abs((datetime.now(timezone.utc) - d).total_seconds()) > 120:
            return None
    except Exception:
        return None
    st = live_lire_fichier('Status.json') or {}
    try:
        if not (int(st.get('Flags') or 0) & 2097152) or st.get('Latitude') is None or st.get('Longitude') is None:
            return None
        return {'la': round(float(st.get('Latitude')), 5), 'lo': round(float(st.get('Longitude')), 5), 'r': int(st.get('PlanetRadius') or 0),
                'h': int(st.get('Heading') or 0) if st.get('Heading') is not None else None, 'bn': str(st.get('BodyName') or '')[:60]}
    except Exception:
        return None


def bio_vente_deja(ts):
    v = explo_mem['bio'].get('vente')
    return bool(v and v.get('t') == ts)


def explo_traiter(e):
    """Un evenement du journal (appelee sous live_verrou). Les evenements deja pris en compte (relecture apres un relancement) sont ignores."""
    ev = e.get('event')
    if ev not in EXPLO_EVENEMENTS:
        return
    ts = str(e.get('timestamp') or '')
    if ts and explo_mem['ts'] and ts < explo_mem['ts']:
        return                       # strictement plus ancien : deja pris en compte (les rafales de scans partagent la meme seconde : elles passent, les traitements sont sans doublon)
    try:
        explo_appliquer(e, ev, ts)
    except Exception:
        pass
    if ts:
        explo_mem['ts'] = ts
    explo_etat_init['sale'] = True


def explo_instantane():
    """Partie « exploration » de l'instantane envoye au site (None tant que le pilote n'a rien explore)."""
    m = explo_mem
    if not m['sys'] and not m['trajet'] and not m['corps'] and not m['bio']['ech']:
        return None
    s = m['sys'] or {}
    corps = list(m['corps'].values())
    sg = s.get('sg') or {}
    bodies = []
    for k, b in (s.get('b') or {}).items():
        b = dict(b)
        d = sg.get(k)
        if d:
            b.update(d)
        bodies.append(b)
    bodies.sort(key=lambda b: (b.get('d') or 0, b.get('i') or 0))
    dec = []
    for c in reversed(corps[-EXPLO_DECOUVERTES_MAX:]):         # du plus recent au plus ancien ; les champs a zero ne sont pas envoyes
        x = {'s': c.get('s'), 'n': c.get('n') or '?', 't': c.get('t'), 'v': c.get('v', 0), 'pot': c.get('pot', 0), 'ts': c.get('ts')}
        for k, val in (('e', c.get('e')), ('pd', 0 if c.get('wd') else 1), ('c', c.get('c')), ('tf', c.get('tf')), ('bio', c.get('bio'))):
            if val:
                x[k] = val
        dec.append(x)
    ech = sorted(m['bio']['ech'].values(), key=lambda c: c.get('t', ''), reverse=True)[:EXPLO_ECH_MAX]
    bio = {'ech': [{k: c.get(k) for k in ('g', 'gk', 's', 'sc', 'va', 'k', 'nv', 'sy', 'bn', 'bi', 't', 'pts', 'r', 'bf')} for c in ech], 'vente': m['bio'].get('vente')} if (ech or m['bio'].get('vente')) else None
    if bio and any(c.get('pts') for c in ech):
        st = live_lire_fichier('Status.json') or {}              # position actuelle : seulement quand des prelevements existent, et avec l'envoi normal (aucun envoi supplementaire)
        try:
            if int(st.get('Flags') or 0) & 2097152 and st.get('Latitude') is not None and st.get('Longitude') is not None:
                bio['ici'] = {'la': round(float(st['Latitude']), 5), 'lo': round(float(st['Longitude']), 5), 'h': st.get('Heading'), 'bn': str(st.get('BodyName') or '')[:60], 't': str(st.get('timestamp') or '')}
        except Exception:
            pass
    return {'v': 1,
            'dec': dec,
            'bio': bio,
            'sys': {'nom': s.get('nom'), 'nb': s.get('nb'), 'prog': s.get('prog'), 'tous': bool(s.get('tous')), 'ceint': len(s.get('cb') or {}), 'b': bodies} if s else None,
            'nv': {'v': int(sum(c.get('v', 0) for c in corps)), 'pd': int(sum(c.get('p', 0) for c in corps)), 'n': len(corps),
                   'sys': len(set(c.get('s') for c in corps)), 'cartos': sum(1 for c in corps if c.get('c'))},
            'tot': {'sauts': m['sauts'], 'dist': round(m['dist'], 1), 'debut': m['debut']},
            'vente': m['vente']}


def explo_charger():
    """Relit explo_etat.json ; renvoie True si l'etat a ete retrouve."""
    try:
        with open(col_chemin(EXPLO_FICHIER), 'r', encoding='utf-8') as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get('v') == 1 and isinstance(d.get('corps'), dict):
            if any(not (c or {}).get('ts') for c in d['corps'].values()):
                return False                 # etat ecrit avant les dates (version de test) : on le reconstitue depuis les journaux, pour que chaque scan ait sa date
            with live_verrou:
                explo_mem.update(d)
                if not isinstance(explo_mem.get('bio'), dict) or 'ech' not in explo_mem['bio']:
                    explo_mem['bio'] = {'ech': {}, 'vente': None}
            return True
    except Exception:
        pass
    return False


def explo_sauver_si_besoin():
    """Ecrit explo_etat.json si quelque chose a change (au plus toutes les 20 s)."""
    if not explo_etat_init['sale'] or time.time() - explo_etat_init['sauve'] < 20:
        return
    try:
        with live_verrou:
            texte = json.dumps(explo_mem, ensure_ascii=False)
            explo_etat_init['sale'] = False
        chemin = col_chemin(EXPLO_FICHIER)
        with open(chemin + '.tmp', 'w', encoding='utf-8') as f:
            f.write(texte)
        os.replace(chemin + '.tmp', chemin)
        explo_etat_init['sauve'] = time.time()
    except Exception:
        pass


def explo_demarrer(jdir, fichiers):
    """Une seule fois par lancement : charge l'etat sauvegarde ; sinon le reconstitue depuis les journaux des EXPLO_JOURS_RATTRAPAGE derniers jours."""
    if explo_etat_init['fait']:
        return
    explo_etat_init['fait'] = True
    if explo_charger():
        return
    limite = time.time() - EXPLO_JOURS_RATTRAPAGE * 86400
    for nom in fichiers:
        chemin = os.path.join(jdir, nom)
        try:
            if os.path.getmtime(chemin) < limite:
                continue
            with open(chemin, 'r', encoding='utf-8', errors='ignore') as f:
                lignes = f.read().splitlines()
        except OSError:
            continue
        for ligne in lignes:
            if not explo_re_journal.search(ligne):
                continue
            try:
                e = json.loads(ligne)
            except Exception:
                continue
            if isinstance(e, dict):
                with live_verrou:
                    explo_traiter(e)
    explo_etat_init['sale'] = True
    explo_sauver_si_besoin()


def live_mission_combat(e):
    """'M' : massacre / assaut (nombre d'ennemis d'une faction) ; 'A' : assassinat (une cible nommee) ; '' sinon."""
    n = str(e.get('Name') or '').lower()
    if 'assassin' in n:
        return 'A'
    if 'massacre' in n or 'onslaught' in n:
        return 'M'
    return ''


def live_mission_acceptee(e, ts, systeme, station):
    """Mission telle qu'elle est enregistree a sa prise (evenement MissionAccepted) ; systeme / station = ou le pilote se trouvait."""
    return {
        'id': e.get('MissionID'), 'nom': live_texte(e.get('LocalisedName')), 'type': live_type_mission(e.get('Name')), 'faction': live_texte(e.get('Faction')),
        'sys': live_texte(e.get('DestinationSystem')), 'st': live_texte(e.get('DestinationStation')), 'exp': e.get('Expiry'),
        'rec': e.get('Reward'), 'q': _bn(e.get('Count')), 'march': live_texte(e.get('Commodity_Localised')), 'pas': _bn(e.get('PassengerCount')),
        'vip': 1 if e.get('PassengerVIPs') else 0, 'rech': 1 if e.get('PassengerWanted') else 0, 'ptype': live_texte(e.get('PassengerType')),
        'don': _bn(e.get('Donation')), 'inf': e.get('Influence'), 'rep': e.get('Reputation'), 't': ts,
        'os': systeme, 'ost': station,    # ou la mission a ete prise (pour le retour au commanditaire)
        'cm': live_mission_combat(e), 'kc': _bn(e.get('KillCount')), 'tf': live_texte(e.get('TargetFaction')), 'tg': live_texte(e.get('Target')) if live_mission_combat(e) == 'A' else ''     # combat : M massacre, A assassinat
    }


def live_envoi_immediat(ts):
    """Un evenement qui vient de se produire (pas une relecture d'ancien journal) doit etre envoye sans attendre l'intervalle habituel (sons de la page : saut, embarquement, debarquement)."""
    t0 = live_ts(ts)
    if t0 is not None and abs((datetime.now(timezone.utc) - t0).total_seconds()) < 30:
        live_flags['immediat'] = True


def live_est_passagers(m):
    return bool(m) and m.get('type') in ('passagers', 'exploration') and _bn(m.get('pas')) > 0


def live_appliquer(e):
    """Met a jour l'etat de la session avec un evenement du journal. Appelee sous live_verrou."""
    ev = e.get('event')
    ts = str(e.get('timestamp') or '')
    et = live_etat
    if ev in CMB_SEULS:
        cmb_traiter(e)                    # combat : etat propre, envoi a part ; ne marque PAS la session en direct comme modifiee (pas d'envoi de ~9 Ko a chaque tir)
        return
    if ev in EXPLO_EVENEMENTS:
        explo_traiter(e)                  # exploration : etat propre (explo_mem), conserve d'un lancement a l'autre
    if ev == 'Fileheader':
        cmb_traiter(e)                    # nouvelle partie : les primes et bons non encaisses repartent de zero

        if _bn(e.get('part') or 1) <= 1:
            et.update(live_nouvel_etat())
            et['debut'] = ts
        et['en_ligne'] = True
    elif ev == 'LoadGame':
        et['en_ligne'] = True
        if not et['debut']:
            et['debut'] = ts
        et['vaisseau']['modele'] = live_texte(e.get('Ship_Localised') or e.get('Ship'))
        et['vaisseau']['nom'] = live_texte(e.get('ShipName'))
    elif ev == 'Loadout':
        et['vaisseau']['modele'] = live_texte(e.get('Ship') or et['vaisseau']['modele'])
        et['vaisseau']['nom'] = live_texte(e.get('ShipName') or et['vaisseau']['nom'])
        try:
            et['vaisseau']['cap'] = int(e.get('CargoCapacity') or 0)
        except Exception:
            pass
        try:
            et['vaisseau']['saut'] = round(float(e.get('MaxJumpRange') or 0), 1)      # portee de saut a vide : sert a regler la feuille de route
        except Exception:
            pass
        et['vaisseau']['cab'] = live_cabines(e.get('Modules'))                         # cabines de passagers : [[classe, places], ...]
        et['vaisseau']['mods'] = live_modules(e.get('Modules'))                        # modules du vaisseau (hologramme)
    elif ev in ('NpcCrewPaidWage', 'NpcCrewRank', 'CrewAssign', 'CrewHire', 'CrewFire'):
        live_equipage(e)
    elif ev in ('LaunchFighter', 'DockFighter', 'FighterDestroyed', 'FighterRebuilt'):
        live_chasseur(e)
    elif ev == 'StartJump':
        # saut vers un autre systeme (hors supercruise et hors taxi) avec une mission de passagers / tourisme en cours : la page passe l'annonce ; envoi immediat s'il vient de se produire
        if e.get('JumpType') == 'Hyperspace' and not e.get('Taxi') and any(live_est_passagers(m) for m in et['missions'].values()):
            # repli : si la charge du FSD (live_surveiller_fsd) a deja declenche l'annonce il y a moins de 40 s, on ne la relance pas
            t_prec, t_now = live_ts((et.get('saut') or {}).get('t')), live_ts(ts)
            if t_prec is None or t_now is None or abs((t_now - t_prec).total_seconds()) > 40:
                et['saut'] = {'t': ts, 'sys': live_texte(e.get('StarSystem'))}
                live_envoi_immediat(ts)
    elif ev == 'DataScanned':
        # balise de tourisme (mission de tourisme) scannee : la page joue les appareils photo, les passagers excites et l'annonce « nous sommes arrives »
        if 'TouristBeacon' in str(e.get('Type') or '') and any(live_est_passagers(m) for m in et['missions'].values()):
            et['tourisme'] = {'t': ts, 'sys': et.get('systeme') or ''}
            live_envoi_immediat(ts)
    elif ev in ('ReceiveText', 'Interdicted', 'EscapeInterdiction'):
        # debut : le message radio de l'interdicteur PNJ (« ..._StartInterdiction.. »), ~25 s avant le resultat ; fin : Interdicted (Submitted=false : capture, true : soumission) ou EscapeInterdiction
        if any(live_est_passagers(m) for m in et['missions'].values()):
            if ev == 'ReceiveText':
                etat = 'debut' if 'StartInterdiction' in str(e.get('Message') or '') else None
            elif ev == 'EscapeInterdiction':
                etat = 'echappe'
            else:
                etat = 'capture'
            if etat:
                et['interception'] = {'t': ts, 'e': etat}
                live_envoi_immediat(ts)
    elif ev == 'LaunchSRV':
        et['minage']['srv'] = str(e.get('SRVType') or '').lower()
    elif ev in ('DockSRV', 'SRVDestroyed'):
        et['minage']['srv'] = ''
        if ev == 'SRVDestroyed':
            et['minage']['rhino']['det'] = {}
    elif ev == 'MiningRefined':
        live_minage_raffine(e)
    elif ev == 'ProspectedAsteroid':
        live_minage_prospecte(e)
    elif ev == 'SAASignalsFound':
        live_minage_signaux(e)
    elif ev == 'LaunchDrone':
        d = live_texte(e.get('Type')) or '?'
        et['minage']['dr'][d] = et['minage']['dr'].get(d, 0) + 1
    elif ev == 'CargoTransfer':
        live_minage_transfert(e)
        live_soute_transfert(e)
        try:
            n = sum(_bn(x.get('Count')) for x in (e.get('Transfers') or []) if str(x.get('Direction') or '').lower() == 'toship')
        except Exception:
            n = 0
        if n >= 1:
            et['chargement'] = {'t': ts, 'n': n}
            live_envoi_immediat(ts)
    elif ev == 'Cargo':
        if str(e.get('Vessel') or '').upper() == 'SRV' and _bn(e.get('Count')) == 0:
            et['minage']['rhino']['det'] = {}       # soute du VRS vide (transferee, jetee ou perdue)
        elif str(e.get('Vessel') or 'Ship').upper() != 'SRV' and isinstance(e.get('Inventory'), list):
            et['soute_vaisseau'] = {'Vessel': 'Ship', 'Inventory': e.get('Inventory')}
    elif ev == 'Disembark':
        live_etat['pied']['on'] = True      # on descend (du vaisseau, du SRV, d'un taxi ou d'un equipage) : a pied dans tous les cas
        cmb_changement_posture(True)
    elif ev == 'Embark':
        live_etat['pied']['on'] = False
        cmb_changement_posture(False)
    elif ev == 'SuitLoadout':
        live_costume(e)
    elif ev == 'Backpack':
        live_sac_complet(e)
    elif ev == 'BackpackChange':
        live_sac_change(e)
    elif ev == 'FSDTarget':
        et['cible'] = live_texte(e.get('Name'))         # systeme vise pour le prochain saut (le jeu l'ecrit AVANT l'arrivee pour l'etape suivante d'une route)
    elif ev in ('Location', 'FSDJump', 'CarrierJump'):
        if ev != 'Location' and et.get('cible') and et['cible'] == live_texte(e.get('StarSystem')):
            et['cible'] = None                           # cible atteinte (une cible differente = etape suivante deja choisie : on la garde)
        if ev == 'Location' and 'OnFoot' in e:
            et['pied']['on'] = bool(e.get('OnFoot'))
        elif ev != 'Location':
            et['pied']['on'] = False
        if e.get('StarSystem'):
            et['systeme'] = live_texte(e.get('StarSystem'))
        if isinstance(e.get('StarPos'), list) and len(e.get('StarPos')) == 3:
            et['pos'] = [round(float(v), 2) for v in e.get('StarPos')]
        amarre = bool(e.get('Docked')) if ev != 'FSDJump' else False
        et['amarre'] = amarre
        et['station'] = live_texte(e.get('StationName')) if amarre else ''
        try:        # reputation aupres des factions du systeme (MyReputation) : cible « amie » hors zone de conflit
            cmb_g['rep'] = {'sys': live_texte(e.get('StarSystem')), 'm': {str(f.get('Name')): int(round(float(f.get('MyReputation')))) for f in (e.get('Factions') or []) if f.get('Name') and f.get('MyReputation') is not None}}
        except Exception:
            pass
        try:        # factions en guerre dans ce systeme (Conflicts) : aussi a la relecture, pour que l'onglet Combat connaisse la zone apres un redemarrage d'EDMC
            cmb_g['conf'] = {'sys': live_texte(e.get('StarSystem')), 'paires': [[(c.get('Faction1') or {}).get('Name', ''), (c.get('Faction2') or {}).get('Name', '')] for c in (e.get('Conflicts') or []) if str(c.get('Status', '')).lower() != 'pending' and (c.get('Faction1') or {}).get('Name') and (c.get('Faction2') or {}).get('Name')]}
        except Exception:
            pass
    elif ev == 'Docked':
        et['amarre'] = True
        et['station'] = live_texte(e.get('StationName'))
        if e.get('StarSystem'):
            et['systeme'] = live_texte(e.get('StarSystem'))
    elif ev == 'Undocked':
        et['amarre'] = False
        et['station'] = ''
        et['pied']['on'] = False
    elif ev == 'MarketBuy':
        q = _bn(e.get('Count'))
        cout = _bn(e.get('TotalCost'))
        cle = str(e.get('Type') or '').lower()
        p = et['paye'].setdefault(cle, [0, 0])
        p[0] += q
        p[1] += cout
        et['tot']['achats'] += cout
        et['tot']['nb'] += 1
        live_ajouter_registre({'t': ts, 'k': 'A', 'n': live_texte(e.get('Type_Localised') or e.get('Type')), 'q': q, 'p': _bn(e.get('BuyPrice')), 'b': -cout,
                               's': et['station'], 'y': et['systeme']})
    elif ev == 'MarketSell':
        q = _bn(e.get('Count'))
        vente = _bn(e.get('TotalSale'))
        paye_moy = _bn(e.get('AvgPricePaid'))
        benef = vente - paye_moy * q
        cle = str(e.get('Type') or '').lower()
        p = et['paye'].get(cle)
        if p and p[0] > 0:
            unite = p[1] / float(p[0])
            p[0] -= q
            p[1] = int(unite * max(p[0], 0))
            if p[0] <= 0:
                del et['paye'][cle]
        et['tot']['ventes'] += vente
        et['tot']['benef'] += benef
        et['tot']['tonnes'] += q
        et['tot']['nb'] += 1
        ligne = {'t': ts, 'k': 'V', 'n': live_texte(e.get('Type_Localised') or e.get('Type')), 'q': q, 'p': _bn(e.get('SellPrice')), 'b': benef,
                 's': et['station'], 'y': et['systeme']}
        if e.get('BlackMarket'):
            ligne['nm'] = 1
        live_ajouter_registre(ligne)
        live_envoi_immediat(ts)     # la page joue l'annonce de vente tout de suite (2.36)
    elif ev == 'MissionAccepted':
        mid = e.get('MissionID')
        if mid is None:
            return
        et['missions'][str(mid)] = live_mission_acceptee(e, ts, et['systeme'], et['station'])
        if live_est_passagers(et['missions'][str(mid)]):
            live_envoi_immediat(ts)          # son d'embarquement sans attendre l'intervalle
        if len(et['missions']) > LIVE_MISSIONS_MAX:
            plus_ancienne = sorted(et['missions'].items(), key=lambda kv: str(kv[1].get('t') or ''))[0][0]
            del et['missions'][plus_ancienne]
    elif ev == 'MissionRedirected':
        m = et['missions'].get(str(e.get('MissionID')))
        if m:
            if e.get('NewDestinationSystem'):
                m['sys'] = live_texte(e.get('NewDestinationSystem'))
            if e.get('NewDestinationStation'):
                m['st'] = live_texte(e.get('NewDestinationStation'))
    elif ev == 'MissionCompleted':
        mid = str(e.get('MissionID'))
        m = et['missions'].pop(mid, None)
        if live_est_passagers(m):
            live_envoi_immediat(ts)          # son de debarquement des que la mission est rendue
        et['terminees'].append({'id': e.get('MissionID'), 'nom': live_texte(e.get('LocalisedName') or (m or {}).get('nom')), 'faction': live_texte(e.get('Faction')),
                                'rec': _bn(e.get('Reward')) or _bn(e.get('Donated')), 'type': (m or {}).get('type') or live_type_mission(e.get('Name')), 't': ts})
        if len(et['terminees']) > LIVE_TERMINEES_MAX:
            del et['terminees'][:len(et['terminees']) - LIVE_TERMINEES_MAX]
        for fe in (e.get('FactionEffects') or []):
            try:
                nom_f = live_texte(fe.get('Faction'))
                infl = ''
                for it in (fe.get('Influence') or []):
                    infl = str(it.get('Influence') or infl)
                if nom_f:
                    deja = et['rep'].get(nom_f) or {}
                    rep_txt = str(fe.get('Reputation') or '')
                    et['rep'][nom_f] = {'rep': rep_txt, 'infl': infl, 'nm': int(deja.get('nm') or 0) + 1,
                                        'pr': int(deja.get('pr') or 0) + rep_txt.count('+') - rep_txt.count('-'),
                                        'pi': int(deja.get('pi') or 0) + infl.count('+') - infl.count('-')}   # nm = missions, pr / pi = + de reputation / d'influence cumules
            except Exception:
                pass
    elif ev in ('MissionFailed', 'MissionAbandoned'):
        et['missions'].pop(str(e.get('MissionID')), None)
    elif ev == 'Missions':
        # liste des missions en cours a la connexion : on retire celles qui n'y sont plus, on ajoute (minimalement) celles qu'on ne connaissait pas
        actives = {}
        for a in (e.get('Active') or []):
            actives[str(a.get('MissionID'))] = a
        for mid in list(et['missions'].keys()):
            if mid not in actives:
                del et['missions'][mid]
        t0 = live_ts(ts)
        for mid, a in actives.items():
            if mid not in et['missions']:
                exp = None
                if t0 is not None and a.get('Expires') is not None:
                    try:
                        exp = (t0 + timedelta(seconds=int(a.get('Expires')))).strftime('%Y-%m-%dT%H:%M:%SZ')
                    except Exception:
                        exp = None
                et['missions'][mid] = {'id': a.get('MissionID'), 'nom': live_texte(a.get('Name')), 'type': live_type_mission(a.get('Name')), 'faction': '', 'sys': '', 'st': '',
                                       'exp': exp, 'rec': None, 'q': 0, 'march': '', 'pas': 0, 'don': 0, 'inf': None, 'rep': None, 't': ts}
                live_inconnues.add(mid)     # details inconnus (mission prise lors d'une partie precedente) : live_suivre les cherchera dans les anciens journaux
    elif ev == 'Rank':
        et['rang'] = {k: v for k, v in e.items() if k not in ('timestamp', 'event')}
    elif ev == 'Progress':
        et['prog'] = {k: v for k, v in e.items() if k not in ('timestamp', 'event')}
    elif ev == 'Promotion':
        for k, v in e.items():
            if k not in ('timestamp', 'event'):
                et['rang'][k] = v
                et['prog'][k] = 0
    elif ev == 'Shutdown':
        et['en_ligne'] = False
    # 'Cargo' : la soute est relue dans Cargo.json au moment de l'envoi ; l'evenement sert seulement a declencher un envoi
    live_flags['sale'] = True
    if ev not in LIVE_MINEURS:
        live_flags['urgent'] = True


live_suivi = {'prefixe': None, 'fichier': None, 'pos': 0}


def live_traiter_evenement(e):
    """Applique un evenement du journal a l'etat de la session (utilise par la lecture du journal ci-dessous)."""
    if e.get('event') not in LIVE_EVENEMENTS:
        return
    with live_verrou:
        live_appliquer(e)


def live_lire_morceau(chemin, debut):
    """Lit les lignes COMPLETES d'un fichier du journal a partir de la position donnee et les applique ; renvoie la nouvelle position."""
    try:
        taille = os.path.getsize(chemin)
    except OSError:
        return debut
    if taille < debut:
        debut = 0
    if taille == debut:
        return debut
    with open(chemin, 'rb') as f:
        f.seek(debut)
        data = f.read()
    fin = data.rfind(b'\n')
    if fin < 0:
        return debut          # derniere ligne encore incomplete : on attend la suite
    nb = 0
    for ligne in data[:fin + 1].decode('utf-8', errors='ignore').splitlines():
        if not live_re_journal.search(ligne):
            continue
        try:
            e = json.loads(ligne)
        except Exception:
            continue
        if not isinstance(e, dict):
            continue
        live_traiter_evenement(e)
        nb += 1
    if nb:
        live_trace('lu %s : %d evenements utiles (octets %d a %d)' % (os.path.basename(chemin), nb, debut, debut + fin + 1))
    return debut + fin + 1


LIVE_RE_ANCIENNES = re.compile('"event":"(MissionAccepted|MissionRedirected|Location|FSDJump|CarrierJump|Docked|Undocked)"')


def live_retrouver_missions(jdir, fichiers, prefixe, ids):
    """Cherche dans les journaux des parties PRECEDENTES (LIVE_MISSIONS_JOURS derniers jours) la prise de chacune des missions 'ids' (numeros en texte).
    Renvoie {numero: mission}, au meme format qu'une mission prise en direct. Les fichiers sont lus du plus ancien au plus recent : une redirection
    posterieure a la prise est donc prise en compte. Seuls les fichiers qui contiennent un de ces numeros sont analyses."""
    trouvees = {}
    cibles = [str(i).encode('ascii', 'ignore') for i in ids]
    limite = time.time() - LIVE_MISSIONS_JOURS * 86400
    for nom in fichiers:
        if nom.split('.')[1] == prefixe:
            continue                  # la partie en cours est deja lue en entier
        chemin = os.path.join(jdir, nom)
        try:
            if os.path.getmtime(chemin) < limite:
                continue
            with open(chemin, 'rb') as f:
                contenu = f.read()
        except OSError:
            continue
        if not any(c in contenu for c in cibles):
            continue
        systeme = station = ''
        for ligne in contenu.decode('utf-8', errors='ignore').splitlines():
            if not LIVE_RE_ANCIENNES.search(ligne):
                continue
            try:
                e = json.loads(ligne)
            except Exception:
                continue
            if not isinstance(e, dict):
                continue
            ev = e.get('event')
            if ev in ('Location', 'FSDJump', 'CarrierJump'):
                if e.get('StarSystem'):
                    systeme = live_texte(e.get('StarSystem'))
                amarre = bool(e.get('Docked')) if ev != 'FSDJump' else False
                station = live_texte(e.get('StationName')) if amarre else ''
            elif ev == 'Docked':
                station = live_texte(e.get('StationName'))
                if e.get('StarSystem'):
                    systeme = live_texte(e.get('StarSystem'))
            elif ev == 'Undocked':
                station = ''
            elif ev == 'MissionAccepted':
                mid = str(e.get('MissionID'))
                if mid in ids:
                    trouvees[mid] = live_mission_acceptee(e, str(e.get('timestamp') or ''), systeme, station)
            elif ev == 'MissionRedirected':
                m = trouvees.get(str(e.get('MissionID')))
                if m:
                    if e.get('NewDestinationSystem'):
                        m['sys'] = live_texte(e.get('NewDestinationSystem'))
                    if e.get('NewDestinationStation'):
                        m['st'] = live_texte(e.get('NewDestinationStation'))
    return trouvees


def live_restaurer_missions(jdir, fichiers, prefixe):
    """Apres un relancement du jeu, la liste « Missions » ne donne que le numero, le nom interne et le temps restant : on retrouve le reste
    (titre, destination, recompense, passagers, echeance, point de prise) dans les journaux precedents."""
    with live_verrou:
        a_chercher = [mid for mid in live_inconnues if mid not in live_essayees and mid in live_etat['missions'] and not live_etat['missions'][mid].get('sys')]
        live_inconnues.clear()
        live_essayees.update(a_chercher)
    if not a_chercher:
        return
    try:
        trouvees = live_retrouver_missions(jdir, fichiers, prefixe, set(a_chercher))
    except Exception:
        return
    live_trace('missions retrouvees dans les anciens journaux : %d sur %d' % (len(trouvees), len(a_chercher)))
    if not trouvees:
        return
    with live_verrou:
        for mid, m in trouvees.items():
            cur = live_etat['missions'].get(mid)
            if not cur:
                continue                      # terminee ou abandonnee entre-temps
            if cur.get('sys'):                # une redirection recue depuis garde sa destination
                m['sys'] = cur['sys']
                m['st'] = cur.get('st') or m['st']
            live_etat['missions'][mid] = m
        live_flags['sale'] = True
        live_flags['urgent'] = True


def live_suivre():
    """Une passe de suivi du journal : au demarrage (ou a chaque nouvelle partie), relit TOUS les morceaux de la session en cours dans l'ordre, puis lit seulement
    ce qui a ete ajoute depuis. On ne depend plus d'EDMC pour recevoir les evenements : tout ce que le journal contient deja au lancement est pris en compte."""
    jdir = trouver_journal_dir()
    if not jdir or not os.path.isdir(jdir):
        return
    fichiers = lister_journaux(jdir)
    if not fichiers:
        return
    explo_demarrer(jdir, fichiers)      # exploration : etat sauvegarde, ou reconstitution depuis les journaux recents (une seule fois par lancement)
    dernier = fichiers[-1]
    prefixe = dernier.split('.')[1]
    suivi = live_suivi
    if suivi['prefixe'] != prefixe:
        # premiere passe ou nouvelle partie : l'evenement Fileheader du premier morceau remet l'etat a zero
        morceaux = [f for f in fichiers if f.split('.')[1] == prefixe]
        for m in morceaux[:-1]:
            live_lire_morceau(os.path.join(jdir, m), 0)
        suivi['prefixe'] = prefixe
        suivi['fichier'] = morceaux[-1]
        suivi['pos'] = 0
    elif suivi['fichier'] != dernier:
        suivi['fichier'] = dernier      # morceau suivant de la meme partie : l'etat continue
        suivi['pos'] = 0
    suivi['pos'] = live_lire_morceau(os.path.join(jdir, suivi['fichier']), suivi['pos'])
    if live_inconnues:
        live_restaurer_missions(jdir, fichiers, prefixe)


def live_lire_fichier(nom):
    try:
        jdir = trouver_journal_dir()
        if jdir:
            with open(os.path.join(jdir, nom), 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return None


def live_pied_instantane(p):
    """Etat a pied pour l'instantane : seulement quand on est a pied (sinon un simple indicateur), sac en listes [[nom, nombre], ...]."""
    if not p.get('on'):
        return {'on': False}
    return {'on': True, 'suit': p.get('suit', ''), 'loadout': p.get('loadout', ''), 'mods': list(p.get('mods') or []),
            'armes': dict(p.get('armes') or {}),
            'sac': {cat: sorted([[n, q] for n, q in (p.get('sac', {}).get(cat) or {}).items()], key=lambda x: x[0]) for cat in ('data', 'comp', 'items', 'conso')}}


def live_instantane():
    """Construit l'instantane envoye au serveur (aucun appel reseau). La soute vient de Cargo.json, les credits de Status.json."""
    cargo = live_lire_fichier('Cargo.json') or {}
    statut = live_lire_fichier('Status.json') or {}
    with live_verrou:
        et = live_etat
        items = []
        # Cargo.json contient la soute du VRS quand on est dans le VRS (Vessel = SRV) : on envoie alors la derniere soute connue du VAISSEAU
        if str(cargo.get('Vessel') or 'Ship').upper() == 'SRV':
            cargo = et.get('soute_vaisseau') or {}
        elif cargo:
            et['soute_vaisseau'] = cargo
        for it in (cargo.get('Inventory') or []):
            cle = str(it.get('Name') or '').lower()
            p = et['paye'].get(cle)
            item = {'n': live_texte(it.get('Name_Localised') or it.get('Name')), 'q': _bn(it.get('Count')), 'k': str(it.get('Name') or '').lower()}   # k = nom interne (sert a retrouver la marchandise chez Spansh)
            if it.get('MissionID'):
                item['m'] = 1
            if it.get('Stolen'):
                item['v'] = 1
            if p and p[0] > 0 and not it.get('MissionID'):
                item['paye'] = int(p[1] / float(p[0]))
            items.append(item)
        missions = sorted(et['missions'].values(), key=lambda m: str(m.get('exp') or '9999'))
        return {
            'v': 1, 'en_ligne': et['en_ligne'], 'debut': et['debut'], 'maj': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'systeme': et['systeme'], 'station': et['station'], 'amarre': et['amarre'], 'pos': et['pos'],
            'vaisseau': dict(et['vaisseau']), 'credits': statut.get('Balance'),
            'soute': {'cap': et['vaisseau'].get('cap') or 0, 'items': items},
            'registre': list(et['registre']), 'tot': dict(et['tot']),
            'missions': missions, 'terminees': list(et['terminees']), 'rep': dict(et['rep']),
            'rang': dict(et['rang']), 'prog': dict(et['prog']), 'pied': live_pied_instantane(et['pied']),
            'minage': live_minage_instantane(et['minage']),
            'explo': explo_instantane(),
            'equipage': [dict(m) for m in list(et['equipage'].values())[:4]], 'chasseur': dict(et['chasseur']) if et['chasseur'] else None,
            'saut': dict(et['saut']) if et.get('saut') else None,
            'interception': dict(et['interception']) if et.get('interception') else None,
            'tourisme': dict(et['tourisme']) if et.get('tourisme') else None,
            'chargement': dict(et['chargement']) if et.get('chargement') else None
        }


# =====================================================================================================
# COMBAT (onglet « COMBAT » du tableau de bord, script SQL 103) : session de combat, cible verrouillee, morts, primes, bons de guerre.
# Etat en MEMOIRE (cmb, cmb_g), alimente par les evenements du journal (cmb_traiter, sous live_verrou) et par Status.json (cmb_status).
# Il part dans SA PROPRE petite ligne (RPC combat_releve), pas dans l'instantane de la session : un envoi rapide (4 s) ne renvoie donc pas les ~9 Ko de la session.
# Une session de combat commence au premier mort, a la premiere attaque subie ou a la premiere mort du pilote, et finit apres 10 minutes sans evenement de combat.
# Diagnostic : live_debug.txt (meme fichier que la session en direct).
# =====================================================================================================
CMB_FIN_SESSION = 600           # secondes sans evenement de combat : la session est terminee
CMB_ENVOI_MIN = 4               # secondes entre deux envois pendant un VRAI combat (le premier changement part tout de suite, le suivant attend ce delai)
CMB_ENVOI_VERROU = 8            # idem pour un simple verrouillage de cible (supercruise, hors combat)
CMB_ENVOI_LENT = 20             # delai quand plus de CMB_PLAFOND_HEURE envois ont deja eu lieu dans l'heure (protege le quota Supabase)
CMB_PLAFOND_HEURE = 600
cmb_envois = []                 # horodatages des envois de la derniere heure
CMB_KILLS_VAISSEAU = 40
CMB_KILLS_PIED = 120
CMB_NOMS_PIED = 40
CMB_JOURNAL_MAX = 30
CMB_PVP_DUREE = 180             # secondes : apres un signe de PvP (joueur verrouille, tue ou meurtrier), la session reste en mode PvP tant que le combat continue contre lui
CMB_VUS_MAX = 8

cmb = {'s': None}               # session courante (ou la derniere, terminee)
cmb_g = {'pend_p': 0, 'pend_b': 0, 'cible': None, 'bo': True, 'co': None, 'arm': False, 'fg': 0, 'pips': None, 'sante': None, 'imp': 0, 'attaque': 0,
         'lieu': '', 'lieu_sys': '', 'zi': '', 'zn': '', 'zsys': '', 'vus': {}, 'mprog': {},
         'sale': False, 'dernier_envoi': 0.0, 'echecs': 0, 'absente': False, 'empreinte': '', 'fin_a_envoyer': False}


# JOUEURS CROISES (historique partage de l'escadron, SQL 105) : chaque JOUEUR verrouille (pas un PNJ) est note, puis envoye par petits lots (une fois par minute au plus).
# Le serveur ne garde qu'une fiche par joueur et ignore les membres de l'escadron ; la relecture des vieux journaux au demarrage n'ajoute rien (seuls les verrouillages recents comptent).
CROISES_DELAI = 60            # secondes entre deux envois
CROISES_MAX = 40              # fiches par envoi (limite du serveur)
CROISES_RECENT = 900          # un verrouillage plus vieux que 15 minutes n'est pas note (relecture du journal)
croises_verrou = threading.Lock()
croises_g = {'file': {}, 'dernier': 0.0, 'echecs': 0, 'absente': False}


def croises_noter(c, nom, t):
    cle = nom.lower()
    f = {'n': nom, 's': c.get('s', ''), 'sl': c.get('sl', ''), 'rg': c.get('rg', ''), 'f': c.get('f', ''), 'l': c.get('l', ''), 'e': c.get('e', ''), 'pw': c.get('pw', ''),
         'pm': c.get('pm', 0), 'sys': live_etat.get('systeme', ''), 't': int(t), 'st': int(c.get('st') or 0)}      # st = stade du scan (le serveur ne compte pour L'ESPION que le stade 2 ou plus)
    with croises_verrou:
        prec = croises_g['file'].get(cle)
        if prec:
            for k in ('s', 'sl', 'rg', 'f', 'l', 'e', 'pw', 'pm', 'sys'):          # ce que le ciblage precedent savait deja (scan plus complet)
                if not f.get(k) and prec.get(k):
                    f[k] = prec[k]
            f['st'] = max(int(f.get('st') or 0), int(prec.get('st') or 0))          # le scan le plus complet de la file
            croises_g['file'].pop(cle, None)
        croises_g['file'][cle] = f
        while len(croises_g['file']) > 200:
            croises_g['file'].pop(next(iter(croises_g['file'])))


# ALERTE « JOUEUR VERROUILLE » (v2.44) : quand un CMDR (un joueur, pas un PNJ) est verrouille, un tout petit signal (nom, vaisseau, rang, faction) est envoye a la base ; les pages du site
# qui ont l'option « Alerte combat » activee affichent un gros bouton pour basculer sur l'onglet Combat. Un meme joueur n'declenche qu'un signal toutes les 45 s ; l'envoi se fait dans un fil
# a part (le journal du jeu n'attend jamais le reseau) ; la relecture d'un vieux journal ne declenche rien (seul un verrouillage de moins de 20 s compte).
alerte_g = {'nom': '', 't': 0.0, 'absente': False}


def alerte_envoyer(c, nom):
    try:
        if alerte_g['absente'] or not get_user_id():
            return
        maintenant = time.time()
        if nom.lower() == alerte_g['nom'] and maintenant - alerte_g['t'] < 45:
            return
        alerte_g['nom'] = nom.lower()
        alerte_g['t'] = maintenant
        charge = {'p': {'n': str(nom)[:64], 'sl': str(c.get('sl', ''))[:60], 'rg': str(c.get('rg', ''))[:40], 'f': str(c.get('f', ''))[:80], 'pw': str(c.get('pw', ''))[:40],
                        'e': str(c.get('e', ''))[:12], 'st': int(c.get('st') or 0)}}

        def travail():
            try:
                res = requests.post(f"{SUPABASE_URL}/rest/v1/rpc/alerte_cible_poser", headers=get_headers(), json=charge, timeout=5)
                live_trace('alerte_cible_poser : %s -> HTTP %s' % (nom, res.status_code))
                if res.status_code == 404:
                    alerte_g['absente'] = True          # fonction pas encore creee en base : on ne reessaie pas
            except Exception as ex:
                live_trace('alerte joueur verrouille : echec %r' % (ex,))
        threading.Thread(target=travail, daemon=True).start()
    except Exception:
        pass


def croises_envoyer(force=False):
    g = croises_g
    if g['absente'] or not g['file'] or not live_flags['pret']:
        return
    maintenant = time.time()
    if not force and maintenant - g['dernier'] < CROISES_DELAI * (1 + min(g['echecs'], 8)):
        return
    if not get_user_id():
        return
    with croises_verrou:
        lot = list(g['file'].items())[:CROISES_MAX]
    g['dernier'] = maintenant
    try:
        res = requests.post(f"{SUPABASE_URL}/rest/v1/rpc/joueurs_croises_releve", headers=get_headers(), json={'p_joueurs': [v for _, v in lot]}, timeout=10)
        live_trace('joueurs_croises_releve : %d fiche(s) -> HTTP %s %s' % (len(lot), res.status_code, (res.text or '')[:120]))
        if res.status_code in (200, 201, 204):
            with croises_verrou:
                for k, v in lot:
                    if croises_g['file'].get(k) is v:          # une fiche remise a jour entre-temps reste dans la file
                        croises_g['file'].pop(k, None)
            g['echecs'] = 0
            return
        if res.status_code == 404:
            g['absente'] = True                                 # fonction pas encore creee en base : on ne reessaie pas
            return
        raise RuntimeError('HTTP ' + str(res.status_code))
    except Exception as ex:
        live_trace('joueurs croises : envoi en echec %r' % (ex,))
        g['echecs'] += 1


# HANGAR (SQL 106) : la liste des vaisseaux du pilote et l'equipement actuel de chacun, pour la fenetre « Hangar » (fiche pilote) et la page « Mon hangar ».
# Sources du journal : Loadout (equipement complet du vaisseau pilote, a chaque changement), StoredShips (liste des vaisseaux ranges, a l'ouverture du chantier naval),
# ShipyardNew (achat), ShipyardSell / SellShipOnRebuy (vente). Aucune valeur en credits ni position n'est envoyee. Rien n'est envoye tant que rien n'a change ;
# l'envoi est groupe (une fois par minute au plus), le serveur remplace la ligne du pilote. Les modifications d'ingenierie (valeurs) ne sont pas gardees : niveau, plan, effet, ingenieur.
HG_FICHIER = 'hangar_etat.json'
HG_RELECTURE = '2.41'            # a changer pour obliger tous les plugins a relire une fois tous leurs journaux
HG_DELAI = 60                    # secondes entre deux envois
HG_MAX_VAISSEAUX = 60
HG_MAX_MODULES = 90
HG_MAX_OCTETS = 230000           # le serveur refuse au-dela de 250 000
HG_IGNORE = re.compile(r'^(Decal|ShipName|ShipID|PaintJob|VesselVoice|Bobble|Nameplate|WeaponColour|EngineColour|ShipKit|ShipCockpit|CargoHatch|Hologram)')
hg_re_journal = re.compile('"event":"(Loadout|StoredShips|ShipyardNew|ShipyardSell|SellShipOnRebuy)"')
hg_verrou = threading.RLock()
hg_etat = {'ships': {}, 'courant': '', 'marqueur': ''}      # ships : {id (texte) : {'t': type, 'n': nom, 'm': [modules] ou absent, 'ts': horodatage}}
hg_flags = {'pret': False, 'sale': False, 'absente': False, 'dernier': 0.0, 'echecs': 0, 'empreinte': '', 'sauve': 0.0}
hg_file = []                     # evenements vus en direct pendant la relecture des journaux


def hg_chemin():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), HG_FICHIER)


def hg_trace(msg):
    """Diagnostic : n'ecrit QUE si un fichier hangar_debug.txt existe deja a cote de load.py (le creer vide pour activer)."""
    try:
        chemin = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hangar_debug.txt')
        if not os.path.exists(chemin):
            return
        with open(chemin, 'a', encoding='utf-8') as f:
            f.write(datetime.now().strftime('%H:%M:%S') + ' ' + str(msg) + chr(10))
    except Exception:
        pass


def hg_charger_etat():
    try:
        with open(hg_chemin(), 'r', encoding='utf-8') as f:
            d = json.load(f)
        with hg_verrou:
            hg_etat['ships'] = dict(d.get('ships') or {})
            hg_etat['courant'] = str(d.get('courant') or '')
            hg_etat['marqueur'] = str(d.get('marqueur') or '')
    except Exception:
        pass


def hg_sauver_etat():
    """Ecriture atomique de l'etat (a cote de load.py) : evite de relire tous les journaux a chaque demarrage."""
    try:
        with hg_verrou:
            donnees = json.dumps(hg_etat)
            hg_flags['sauve'] = time.time()
        tmp = hg_chemin() + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(donnees)
        os.replace(tmp, hg_chemin())
    except Exception as e:
        hg_trace('sauvegarde impossible : %r' % (e,))


def hg_modules(mods):
    """Modules d'un Loadout, sous forme compacte : emplacement, objet, allume, niveau d'ingenierie, plan, effet experimental, ingenieur."""
    res = []
    try:
        for mod in (mods or []):
            slot = str(mod.get('Slot') or '')
            if not slot or HG_IGNORE.match(slot):
                continue
            o = {'s': slot[:40], 'i': str(mod.get('Item') or '').lower()[:90], 'o': 1 if mod.get('On', True) else 0}
            g = mod.get('Engineering')
            if isinstance(g, dict):
                o['g'] = int(g.get('Level') or 0)
                o['b'] = str(g.get('BlueprintName') or '')[:60]
                o['x'] = str(g.get('ExperimentalEffect') or '')[:60]
                o['e'] = live_texte(g.get('Engineer'))[:40]
            res.append(o)
    except Exception:
        pass
    return res[:HG_MAX_MODULES]


def hg_appliquer(e):
    """Un evenement du journal (sous hg_verrou). Idempotent : le meme evenement vu deux fois donne le meme etat."""
    ev = e.get('event')
    ships = hg_etat['ships']
    ts = str(e.get('timestamp') or '')
    if ev == 'Loadout':
        sid = str(e.get('ShipID', ''))
        if not sid:
            return
        s = ships.setdefault(sid, {})
        s['t'] = str(e.get('Ship') or s.get('t') or '').lower()[:40]
        s['n'] = live_texte(e.get('ShipName') or '')[:60]
        s['m'] = hg_modules(e.get('Modules'))
        s['ts'] = ts
        hg_etat['courant'] = sid
    elif ev == 'StoredShips':
        ids = set()
        for lst in ('ShipsHere', 'ShipsRemote'):
            for x in (e.get(lst) or []):
                try:
                    sid = str(x.get('ShipID', ''))
                except Exception:
                    continue
                if not sid:
                    continue
                ids.add(sid)
                s = ships.setdefault(sid, {})
                s['t'] = str(x.get('ShipType') or s.get('t') or '').lower()[:40]
                if x.get('Name'):
                    s['n'] = live_texte(x.get('Name'))[:60]
        # la liste des vaisseaux ranges fait foi : un vaisseau qui n'y est pas (et n'est pas celui que l'on pilote) n'existe plus
        for sid in list(ships.keys()):
            if sid not in ids and sid != hg_etat['courant']:
                ships.pop(sid, None)
    elif ev == 'ShipyardNew':
        sid = str(e.get('NewShipID', ''))
        if sid and sid not in ships:
            ships[sid] = {'t': str(e.get('ShipType') or '').lower()[:40], 'n': '', 'ts': ts}
    elif ev in ('ShipyardSell', 'SellShipOnRebuy'):
        ships.pop(str(e.get('SellShipID', '')), None)
    else:
        return
    hg_flags['sale'] = True


def hg_traiter_evenement(e):
    """Appelee pour chaque evenement en direct (aucun appel reseau). Pendant la relecture des journaux, les evenements attendent dans hg_file."""
    if e.get('event') not in ('Loadout', 'StoredShips', 'ShipyardNew', 'ShipyardSell', 'SellShipOnRebuy'):
        return
    with hg_verrou:
        if not hg_flags['pret']:
            hg_file.append(e)
            return
        hg_appliquer(e)


def hg_scanner_journaux():
    """Au demarrage : relit TOUS les journaux la premiere fois (ou quand HG_RELECTURE change), ensuite seulement ceux des 2 derniers jours.
    Seul le dernier Loadout de chaque vaisseau compte, donc l'ordre chronologique suffit."""
    try:
        hg_charger_etat()
        complet = hg_etat.get('marqueur') != HG_RELECTURE
        jdir = trouver_journal_dir()
        if jdir and os.path.isdir(jdir):
            limite = None if complet else time.time() - 2 * 86400
            fichiers = sorted(f for f in os.listdir(jdir)
                              if f.startswith('Journal.') and f.endswith('.log')
                              and (limite is None or os.path.getmtime(os.path.join(jdir, f)) >= limite))
            nb = 0
            for nom_fichier in fichiers:
                with open(os.path.join(jdir, nom_fichier), 'r', encoding='utf-8', errors='ignore') as f:
                    for ligne in f:
                        if not hg_re_journal.search(ligne):
                            continue
                        try:
                            e = json.loads(ligne)
                        except Exception:
                            continue
                        with hg_verrou:
                            hg_appliquer(e)
                        nb += 1
            with hg_verrou:
                hg_etat['marqueur'] = HG_RELECTURE
            hg_trace('relecture %s : %d fichiers, %d evenements utiles, %d vaisseaux' % ('complete' if complet else 'recente', len(fichiers), nb, len(hg_etat['ships'])))
    except Exception as ex:
        hg_trace('relecture : EXCEPTION %r' % (ex,))
    finally:
        with hg_verrou:
            en_attente = list(hg_file)
            del hg_file[:]
            for e in en_attente:
                try:
                    hg_appliquer(e)
                except Exception:
                    pass
            hg_flags['pret'] = True
            hg_flags['sale'] = True


def hg_instantane():
    """Liste a envoyer : [{'i': id, 't': type, 'n': nom, 'm': [modules] (absent si equipement inconnu), 'c': 1 pour le vaisseau pilote}], au plus HG_MAX_VAISSEAUX."""
    with hg_verrou:
        items = []
        for sid, s in hg_etat['ships'].items():
            try:
                i = int(sid)
            except Exception:
                continue
            if not s.get('t'):
                continue
            o = {'i': i, 't': s.get('t', ''), 'n': s.get('n', '')}
            if isinstance(s.get('m'), list):
                o['m'] = s['m']
            if sid == hg_etat['courant']:
                o['c'] = 1
            items.append((s.get('ts', ''), o))
    items.sort(key=lambda x: x[0], reverse=True)
    liste = [o for _, o in items][:HG_MAX_VAISSEAUX]
    liste.sort(key=lambda o: o['i'])
    # garde-fou de taille : au besoin, on retire l'equipement des vaisseaux les plus anciens
    anciens = [o for _, o in reversed(items) if 'm' in o]
    while len(json.dumps(liste)) > HG_MAX_OCTETS and anciens:
        anciens.pop(0).pop('m', None)
    return liste


def hg_envoyer(force=False):
    g = hg_flags
    if g['absente'] or not g['pret'] or not g['sale'] or not live_flags['pret']:
        return
    maintenant = time.time()
    if not force and maintenant - g['dernier'] < HG_DELAI * (1 + min(g['echecs'], 8)):
        return
    if not get_user_id():
        return
    liste = hg_instantane()
    if not liste:
        g['sale'] = False
        return
    empreinte = hashlib.md5(json.dumps(liste, sort_keys=True).encode('utf-8')).hexdigest()
    if empreinte == g['empreinte']:
        g['sale'] = False
        return
    g['dernier'] = maintenant
    try:
        res = requests.post(f"{SUPABASE_URL}/rest/v1/rpc/hangar_releve", headers=get_headers(), json={'p_vaisseaux': liste}, timeout=15)
        hg_trace('hangar_releve : %d vaisseau(x) -> HTTP %s %s' % (len(liste), res.status_code, (res.text or '')[:120]))
        if res.status_code in (200, 201, 204):
            g['empreinte'] = empreinte
            g['sale'] = False
            g['echecs'] = 0
            hg_sauver_etat()
            return
        if res.status_code == 404:
            g['absente'] = True                                 # fonction pas encore creee en base : on ne reessaie pas
            return
        raise RuntimeError('HTTP ' + str(res.status_code))
    except Exception as ex:
        hg_trace('envoi en echec %r' % (ex,))
        g['echecs'] += 1


def cmb_nom(s):
    s = live_texte(s)
    m = re.search(r'#name=([^;]*);', s)
    s = (m.group(1) if m else s).strip()
    s = re.sub(r'\$[A-Za-z0-9_]+;\s*', '', s).strip()     # '$ShipName_General; Fearless Blaze' -> 'Fearless Blaze'
    return re.sub(r'^CMD\s+', '', s)                       # 'CMD Dupont' -> 'Dupont' (la page ajoute CMD pour un joueur)


def cmb_actif():
    s = cmb['s']
    return bool(s and s['actif'])


def cmb_marquer():
    """Quelque chose a change : envoi a prevoir, mais seulement pendant un combat (sinon rien ne part)."""
    if cmb_actif():
        cmb_g['sale'] = True


def cmb_journal(s, t, code, texte):
    s['jr'].append([int(t - s['t0']), code, texte])
    if len(s['jr']) > CMB_JOURNAL_MAX:
        del s['jr'][:len(s['jr']) - CMB_JOURNAL_MAX]


def cmb_changement_posture(pied):
    """Embarquement / debarquement : la session de combat en cours (vaisseau ou a pied) est terminee, le prochain combat en ouvre une nouvelle. Appelee sous live_verrou."""
    try:
        s = cmb['s']
        if s and s['actif'] and bool(s['pied']) != bool(pied):
            s['actif'] = False
            cmb_g['fin_a_envoyer'] = True
            cmb_g['sale'] = True
            live_trace('combat : session terminee (changement de posture, %d morts)' % s['nb'])
    except Exception:
        pass


def cmb_session(t, ts):
    """Session de combat en cours (en ouvre une nouvelle au premier evenement de combat, ou apres 10 minutes de silence)."""
    s = cmb['s']
    if s and s['actif'] and (t - s['dernier'] > CMB_FIN_SESSION or bool(s['pied']) != bool(live_etat['pied'].get('on'))):
        s['actif'] = False                  # silence de 10 min, ou autre posture (vaisseau <-> a pied) : nouvelle session
        s = None
    if s is None or not s['actif']:
        s = {'actif': True, 'debut': ts, 't0': t, 'dernier': t, 'mode': '', 'pvp': 0.0, 'pied': False, 'kills': [], 'nb': 0, 'primes': 0, 'bons': 0, 'bt': 0,
             'deces': 0, 'med': 0, 'dg': 0, 'dp': 0, 'vic': [], 'dir': None, 'soutien': '', 'ennemi': '', 'jr': []}
        cmb['s'] = s
        cmb_g['co'] = None
        cmb_g['fin_a_envoyer'] = False
        live_trace('combat : session ouverte (%s)' % ts)
    s['dernier'] = t
    s['pied'] = bool(live_etat['pied'].get('on'))
    cmb_g['sale'] = True
    return s


def cmb_cible_morte(nom, faction, t, recompense):
    c = cmb_g['cible']
    if c and not c.get('m'):
        if (nom and c.get('p') == nom) or (not nom and faction and c.get('f') == faction and t - c.get('t', 0) < 30):
            c['m'] = 1
            c['mr'] = int(recompense or 0)
    if c and c.get('m'):
        cmb_g['vus'].pop(str(c.get('s')) + '|' + str(c.get('p')), None)
    if nom:
        for k in [k for k in cmb_g['vus'] if k.endswith('|' + nom)]:
            cmb_g['vus'].pop(k, None)


def cmb_progres_missions(faction, nom):
    """Avancement estime des missions de massacre (kills de la faction visee) et d'assassinat (nom de la cible) depuis leur acceptation."""
    for m in live_etat['missions'].values():
        cm = m.get('cm')
        if not cm:
            continue
        mid = str(m.get('id'))
        if cm == 'M' and faction and live_texte(m.get('tf')).lower() == faction.lower():
            cmb_g['mprog'][mid] = cmb_g['mprog'].get(mid, 0) + 1
        elif cm == 'A' and nom and live_texte(m.get('tg')).lower() == nom.lower():
            cmb_g['mprog'][mid] = 1


def cmb_kill(s, t, cle, nom, faction, recompense, code):
    s['nb'] += 1
    s['kills'].append([int(t - s['t0']), cle, nom, faction, int(recompense or 0), code])
    if len(s['kills']) > CMB_KILLS_PIED:
        del s['kills'][:len(s['kills']) - CMB_KILLS_PIED]
    cmb_cible_morte(nom, faction if code == 'G' else '', t, recompense)
    cmb_progres_missions(faction, nom)


def cmb_traiter(e):
    """Un evenement du journal. Appelee sous live_verrou ; ne fait aucun appel reseau."""
    ev = e.get('event')
    ts = str(e.get('timestamp') or '')
    t0 = live_ts(ts)
    if t0 is None:
        return
    t = t0.timestamp()
    g = cmb_g
    if ev == 'Fileheader':
        if _bn(e.get('part') or 1) <= 1:
            g['pend_p'] = g['pend_b'] = 0          # les bons non encaisses sont comptes depuis le lancement du jeu
            g['vus'].clear()
            g['cible'] = None
    elif ev == 'ShipTargeted':
        if not e.get('TargetLocked'):
            g['cible'] = None
        else:
            nom = cmb_nom(e.get('PilotName_Localised') or e.get('PilotName'))
            joueur = str(e.get('PilotName') or '').startswith('$cmdr_decorate')
            c = {'s': str(e.get('Ship') or '').lower(), 'sl': live_texte(e.get('Ship_Localised') or e.get('Ship')), 'st': _bn(e.get('ScanStage')), 'p': nom,
                 'rg': live_texte(e.get('PilotRank')), 'f': live_texte(e.get('Faction')), 'l': live_texte(e.get('LegalStatus')), 'j': 1 if joueur else 0,
                 'e': live_texte(e.get('SquadronID')), 'pw': live_texte(e.get('Power')), 'po': 1 if 'police' in str(e.get('PilotName') or '').lower() else 0, 't': int(t)}
            for src, dst in (('ShieldHealth', 'sh'), ('HullHealth', 'hu')):
                if e.get(src) is not None:
                    try:
                        c[dst] = int(round(float(e.get(src))))
                    except Exception:
                        pass
            if e.get('Bounty') is not None:
                c['pm'] = _bn(e.get('Bounty'))
            ss = live_texte(e.get('SubSystem_Localised') or e.get('SubSystem'))
            if ss:
                c['ss'] = ss
            g['cible'] = c
            if joueur and nom and time.time() - t < CROISES_RECENT:
                croises_noter(c, nom, t)
            if joueur and nom and time.time() - t < 20:
                alerte_envoyer(c, nom)
            if nom and c['st'] >= 2:
                dr = cmb_g.setdefault('dern', [])         # derniers vaisseaux scannes (pour rattacher un bon de guerre a un vaisseau : le bon ne donne que la faction)
                if not any(d['s'] == c['s'] and d['p'] == nom and d['f'] == c['f'] and not d['u'] for d in dr) or c['f']:
                    dr[:] = [d for d in dr if not (d['s'] == c['s'] and d['p'] == nom and not d['u'])]
                    dr.append({'s': c['s'], 'p': nom, 'rg': c['rg'], 'f': c['f'], 't': int(t), 'u': False})
                    del dr[:-10]
                cle_v = c['s'] + '|' + nom          # un vaisseau + un pilote (les PNJ d'une meme flotte portent tous le meme nom)
                prec = g['vus'].pop(cle_v, None) or {}      # remis a la fin : le plus recent (escadron et prime gardes si ce ciblage ne les redonne pas)
                g['vus'][cle_v] = {'s': c['s'], 'sl': c['sl'], 'p': nom, 'rg': c['rg'], 'j': c['j'], 'f': c['f'], 'l': c['l'], 'pw': c['pw'], 'po': c['po'], 'e': c['e'] or prec.get('e', ''), 'pm': c.get('pm') or prec.get('pm', 0), 't': int(t)}
                if len(g['vus']) > CMB_VUS_MAX:
                    g['vus'].pop(next(iter(g['vus'])))
            if not cmb_actif():       # toute cible verrouillee (supercruise comprise, armes rentrees) ouvre la vue ; la page ne bascule d'onglet que pour un vrai combat (champ cb)
                cmb_session(t, ts)
            if joueur and cmb_actif():
                cmb['s']['pvp'] = t         # dernier signe de PvP (horodatage) : le mode PvP ne dure que quelques minutes apres
        cmb_marquer()
    elif ev == 'CommitCrime':
        crime = str(e.get('CrimeType') or '').lower()
        if crime in ('assault', 'murder'):         # agression contre un vaisseau : la cible devient hostile pour nous
            ag = g.setdefault('agr', [])
            ag.append([cmb_nom(e.get('Victim')), live_texte(e.get('Faction')), int(t)])
            del ag[:-8]
            cmb_marquer()
        if crime in ('murder', 'onfoot_murder') and e.get('Victim'):         # meurtre d'un innocent (a pied ou en vaisseau) : le jeu n'ecrit ni Bounty ni bon de guerre, donc aucun kill sans cette ligne
            s = cmb_session(t, ts)
            if not s['mode']:
                s['mode'] = 'secu'
            nom = cmb_nom(e.get('Victim_Localised') or e.get('Victim'))
            cmb_kill(s, t, 'civil', nom, live_texte(e.get('Faction')), 0, 'M')
            cmb_journal(s, t, 'x', (nom or 'Civil') + ' tué')
    elif ev == 'ApproachSettlement':
        g['lieu'] = live_texte(e.get('Name'))
        g['lieu_sys'] = live_etat['systeme']
    elif ev == 'SupercruiseDestinationDrop':
        typ = str(e.get('Type') or '').lower()
        if 'warzone' in typ or 'conflict' in typ:
            g['zi'] = 'H' if 'high' in typ else 'M' if 'med' in typ else 'S'
            g['zn'] = live_texte(e.get('Type_Localised'))
            g['zsys'] = live_etat['systeme']
    elif ev == 'Bounty':
        s = cmb_session(t, ts)
        nom = cmb_nom(e.get('PilotName_Localised') or e.get('PilotName'))
        rec = _bn(e.get('TotalReward')) or sum(_bn(r.get('Reward')) for r in (e.get('Rewards') or []) if isinstance(r, dict))
        faction = live_texte(e.get('VictimFaction'))
        s['primes'] += rec
        g['pend_p'] += rec
        s['mode'] = 'secu'
        if faction:
            s['ennemi'] = faction
        cmb_kill(s, t, str(e.get('Target') or '').lower(), nom, faction, rec, 'B')
        cmb_journal(s, t, 'x', (nom or live_texte(e.get('Target_Localised') or e.get('Target'))) + ' éliminé · ' + format(rec, ',').replace(',', ' ') + ' cr')
    elif ev == 'FactionKillBond':
        s = cmb_session(t, ts)
        rec = _bn(e.get('Reward'))
        faction = live_texte(e.get('VictimFaction'))
        s['bons'] += rec
        g['pend_b'] += rec
        s['mode'] = 'guerre'
        s['soutien'] = live_texte(e.get('AwardingFaction')) or s['soutien']
        if faction:
            s['ennemi'] = faction
        cle_g, rg_g = '', ''
        for d in reversed(g.get('dern', [])):         # dernier vaisseau scanne de la faction tuee, pas deja attribue, vu il y a 90 s au plus
            if (not d['u']) and d['f'] and d['f'].lower() == faction.lower() and t - d['t'] <= 90:
                d['u'] = True
                cle_g, rg_g = d['s'], d['rg']
                break
        cmb_kill(s, t, cle_g, rg_g, faction, rec, 'G')
        if s['nb'] % 5 == 1 and not s['pied']:
            cmb_journal(s, t, 'x', 'Ennemi éliminé · ' + faction)
    elif ev == 'PVPKill':
        s = cmb_session(t, ts)
        s['pvp'] = t
        s['dg'] += 1
        nom = live_texte(e.get('Victim'))
        cmb_kill(s, t, 'pvp', nom, '', _bn(e.get('CombatRank')), 'P')
        cmb_journal(s, t, 'x', 'CMD ' + nom + ' éliminé')
    elif ev == 'Died':
        s = cmb_session(t, ts)
        s['deces'] += 1
        tueurs = e.get('Killers') if isinstance(e.get('Killers'), list) and e.get('Killers') else [e]
        texte = []
        for k in tueurs[:2]:
            nom = live_texte(k.get('KillerName') or k.get('Name'))
            if nom.lower().startswith('cmdr'):
                s['pvp'] = t
                s['dp'] += 1
            texte.append((nom or '?') + (' · ' + live_texte(k.get('KillerShip') or k.get('Ship')) if (k.get('KillerShip') or k.get('Ship')) else '') + (' · ' + live_texte(k.get('KillerRank') or k.get('Rank')) if (k.get('KillerRank') or k.get('Rank')) else ''))
        cmb_journal(s, t, 'd', 'Détruit par ' + ' ; '.join(texte))
    elif ev == 'UnderAttack':
        g['attaque'] = t
        cmb_session(t, ts)
    elif ev == 'ShieldState':
        up = bool(e.get('ShieldsUp'))
        avant = g['bo']
        g['bo'] = up
        if cmb_actif() or (t - g['attaque'] < 60):
            s = cmb_session(t, ts)
            if avant and not up:
                s['bt'] += 1
                cmb_journal(s, t, 'b', 'Bouclier tombé')
            elif up and not avant:
                cmb_journal(s, t, 'b', 'Bouclier rétabli')
    elif ev == 'HullDamage':
        if e.get('PlayerPilot') and not e.get('Fighter'):
            try:
                g['co'] = max(0, min(100, int(round(float(e.get('Health')) * 100))))
            except Exception:
                pass
            g['imp'] = int(t)
            cmb_marquer()
    elif ev == 'UseConsumable':
        if cmb_actif() and 'health' in str(e.get('Name') or '').lower():
            cmb['s']['med'] += 1
            cmb['s']['dernier'] = t
            cmb_journal(cmb['s'], t, 'i', 'Médikit utilisé')
            cmb_marquer()
    elif ev == 'RedeemVoucher':
        montant = _bn(e.get('Amount'))
        typ = str(e.get('Type') or '').lower()
        if typ == 'bounty':
            g['pend_p'] = max(0, g['pend_p'] - montant)
        elif 'bond' in typ:
            g['pend_b'] = max(0, g['pend_b'] - montant)
        cmb_marquer()


def cmb_victoire(intensite, connue, ordre):
    """Victoire en zone de conflit comptee par la partie « zone de conflit » du plugin (journal_entry) ; ordre = directive BGS GUERRE rattachee, ou None."""
    try:
        with live_verrou:
            t = time.time()
            ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            s = cmb_session(t, ts)
            s['vic'].append(intensite if connue else '')
            if ordre:
                d = s['dir'] or {'n': 0}
                d.update({'sys': live_texte(ordre.get('systeme_cible')), 'f': live_texte(ordre.get('faction_cible'))})
                d['n'] += 1
                s['dir'] = d
            cmb_journal(s, t, 'v', 'Victoire en zone de conflit' + ({'H': ' (forte intensité)', 'M': ' (intensité moyenne)', 'S': ' (faible intensité)'}.get(intensite, '') if connue else ''))
    except Exception as ex:
        live_trace('combat : victoire ignoree %r' % (ex,))


def cmb_status(js):
    """Status.json : boucliers, armes sorties, groupe de tir, sante a pied. Appelee souvent (toutes les 0,5 s) : ne marque l'etat modifie que si quelque chose change."""
    try:
        flags = int(js.get('Flags') or 0)
    except Exception:
        return
    g = cmb_g
    change = False
    arm = bool(flags & (1 << 6))
    fg = int(js.get('FireGroup') or 0)
    pips = js.get('Pips') if isinstance(js.get('Pips'), list) and len(js.get('Pips')) == 3 else None
    sante = None
    if js.get('Health') is not None:
        try:
            sante = int(round(float(js.get('Health')) * 100))
        except Exception:
            sante = None
    if flags & (1 << 24):                      # aux commandes du vaisseau : Status.json donne l'etat des boucliers
        bo = bool(flags & (1 << 3))
        if bo != g['bo']:
            g['bo'] = bo
            change = True
    if arm != g['arm'] or fg != g['fg'] or sante != g['sante']:
        change = True
    if arm and not g['arm'] and g['cible'] and not g['cible'].get('m') and not cmb_actif():      # armes sorties alors qu'une cible est deja verrouillee
        with live_verrou:
            cmb_session(time.time(), datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
    ls = str(js.get('LegalState') or '')
    if ls != g.get('ls', ''):
        g['ls'] = ls
        change = True
    g['arm'], g['fg'], g['pips'], g['sante'] = arm, fg, pips, sante
    if change:
        cmb_marquer()


def cmb_missions():
    res = []
    for m in live_etat['missions'].values():
        if not m.get('cm'):
            continue
        res.append({'id': m.get('id'), 'ty': m['cm'], 'tf': live_texte(m.get('tf')), 'tg': live_texte(m.get('tg')), 'n': _bn(m.get('kc')),
                    'k': cmb_g['mprog'].get(str(m.get('id')), 0), 'st': live_texte(m.get('st')), 'sys': live_texte(m.get('sys')), 'rec': m.get('rec'), 'exp': m.get('exp'),
                    'nom': live_texte(m.get('nom'))[:60]})
    return res[:3]


def cmb_instantane():
    """Instantane du combat (petit : quelques Ko). Appelee sous live_verrou."""
    s, g = cmb['s'], cmb_g
    pied = bool(live_etat['pied'].get('on'))
    kills = list(s['kills'][-(CMB_KILLS_PIED if s['pied'] else CMB_KILLS_VAISSEAU):])
    if s['pied'] and len(kills) > CMB_NOMS_PIED:
        for k in kills[:-CMB_NOMS_PIED]:
            k[2] = ''
    cible = dict(g['cible']) if g['cible'] else None
    if cible:
        for m in live_etat['missions'].values():
            cm = m.get('cm')
            if (cm == 'M' and cible.get('f') and live_texte(m.get('tf')).lower() == cible['f'].lower()) or (cm == 'A' and cible.get('p') and live_texte(m.get('tg')).lower() == cible['p'].lower()):
                cible['mi'] = 1
    zi = g['zi'] if (g['zsys'] and g['zsys'] == live_etat['systeme']) else ''
    vus = [dict(v) for v in g['vus'].values() if s and int(s['dernier']) - v['t'] < CMB_FIN_SESSION][-6:]
    conf = g.get('conf')
    cf_zone = conf['paires'] if conf and conf.get('sys') == live_etat['systeme'] else []       # factions en guerre dans ce systeme (Conflicts du journal)
    rp = g.get('rep')
    rp_zone = rp['m'] if rp and rp.get('sys') == live_etat['systeme'] else {}                   # reputation aupres des factions du systeme
    ag_recent = [[a[0], a[1]] for a in g.get('agr', []) if time.time() - a[2] < 600]
    cb_reel = cmb_reel(s, g)
    return {
        'v': 1, 'actif': bool(s['actif']), 'debut': s['debut'], 'maj': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'mode': 'pvp' if (s['pvp'] and s['dernier'] - s['pvp'] < CMB_PVP_DUREE) else (s['mode'] or 'secu'), 'pied': bool(s['pied']), 'sys': live_etat['systeme'],
        'lieu': g['lieu'] if g['lieu_sys'] == live_etat['systeme'] else '', 'zi': zi, 'zn': g['zn'] if zi else '',
        'soutien': s['soutien'], 'ennemi': s['ennemi'], 'cf': cf_zone, 'fe': dernier_faction_escadron_envoyee or '', 'pw': pp_etat.get('puissance') or '', 'rp': rp_zone, 'ag': ag_recent, 'ls': g.get('ls', ''), 'cb': 1 if cb_reel else 0, 'nb': s['nb'], 'primes': s['primes'], 'bons': s['bons'],
        'pend': [g['pend_p'], g['pend_b']], 'vic': list(s['vic']), 'dir': dict(s['dir']) if s['dir'] else None, 'bt': s['bt'], 'deces': s['deces'], 'med': s['med'],
        'duels': [s['dg'], s['dp']], 'kills': kills, 'cible': cible, 'vus': vus,
        'moi': {'bo': bool(g['bo']), 'co': g['co'], 'arm': bool(g['arm']), 'fg': g['fg'], 'pips': g['pips'], 'sante': g['sante'], 'imp': g['imp']},
        'miss': cmb_missions(), 'jr': list(s['jr']), 't0': int(s['t0']), 'fin': int(s['dernier'])
    }


def cmb_reel(s, g):
    """Vrai combat (elimination, attaque, bouclier, mort, victoire, armes sorties) par opposition a un simple verrouillage de cible."""
    return bool(s['nb'] or s['deces'] or s['bt'] or s['dg'] or s['dp'] or s['vic'] or g['arm'] or (g['attaque'] and g['attaque'] >= s['t0']))


def cmb_delai(s, g, maintenant):
    """Delai minimal avant le prochain envoi : 4 s en combat, 8 s pour un simple verrouillage, 20 s au-dela de 600 envois par heure."""
    cmb_envois[:] = [x for x in cmb_envois if maintenant - x < 3600]
    base = CMB_ENVOI_MIN if cmb_reel(s, g) else CMB_ENVOI_VERROU
    if len(cmb_envois) >= CMB_PLAFOND_HEURE:
        base = max(base, CMB_ENVOI_LENT)
    return base


def cmb_envoyer():
    """Envoie l'etat du combat si quelque chose a change (au plus un envoi toutes les 4 s) ; un dernier envoi quand la session se termine. Rien ne part hors combat."""
    g = cmb_g
    if g['absente'] or not live_flags['pret']:
        return
    maintenant = time.time()
    with live_verrou:
        s = cmb['s']
        if s and s['actif'] and (maintenant - s['dernier'] > CMB_FIN_SESSION or not live_etat['en_ligne']):
            s['actif'] = False                 # silence de 10 minutes, ou jeu ferme : fin de la session
            g['fin_a_envoyer'] = True
            g['sale'] = True
            live_trace('combat : session terminee (%d morts)' % s['nb'])
        if not s or not (g['sale'] and (s['actif'] or g['fin_a_envoyer'])):
            return
        if maintenant - g['dernier_envoi'] < cmb_delai(s, g, maintenant) * (1 + min(g['echecs'], 8)):
            return
        g['sale'] = False
        fin = g['fin_a_envoyer']
        g['fin_a_envoyer'] = False
        snap = cmb_instantane()
    if not get_user_id():
        g['sale'] = True
        g['dernier_envoi'] = maintenant
        return
    sans_maj = dict(snap)
    sans_maj.pop('maj', None)
    empreinte = hashlib.md5(json.dumps(sans_maj, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    if empreinte == g['empreinte'] and not fin:
        return
    g['dernier_envoi'] = maintenant
    cmb_envois.append(maintenant)
    try:
        res = requests.post(f"{SUPABASE_URL}/rest/v1/rpc/combat_releve", headers=get_headers(), json={'p_etat': snap}, timeout=10)
        live_trace('combat_releve : %d octets -> HTTP %s %s' % (len(json.dumps(snap)), res.status_code, (res.text or '')[:160]))
        if res.status_code in (200, 201, 204):
            g['empreinte'] = empreinte
            g['echecs'] = 0
            return
        if res.status_code == 404:
            g['absente'] = True                # fonction pas encore creee en base : on ne reessaie pas
            return
        raise RuntimeError('HTTP ' + str(res.status_code))
    except Exception as ex:
        live_trace('combat : envoi en echec %r' % (ex,))
        g['echecs'] += 1
        g['sale'] = True
        if fin:
            g['fin_a_envoyer'] = True
# =====================================================================================================
# FIN COMBAT
# =====================================================================================================


def live_envoyer(force=False):
    """Envoie l'instantane si quelque chose a change (au plus un envoi toutes les 30 s, plus espace apres des echecs) ; un envoi de securite toutes les 5 minutes en jeu."""
    if live_flags['absente'] or not live_flags['pret']:
        return
    maintenant = time.time()
    with live_verrou:
        sale = live_flags['sale']
        keepalive = live_etat['en_ligne'] and maintenant - live_flags['dernier_envoi'] >= LIVE_KEEPALIVE
        if not force:
            if not (sale or keepalive):
                return
            base = LIVE_INTERVALLE_ENVOI if live_flags['urgent'] else LIVE_INTERVALLE_MINEUR
            # saut hyperespace avec passagers : on n'attend pas l'intervalle (l'annonce doit partir pendant les ~18 s du saut), sauf si les envois echouent
            # (la base ignore deux envois a moins de 3 s d'ecart : on garde 3,5 s)
            if live_flags['immediat'] and live_flags['echecs'] == 0:
                if maintenant - live_flags['dernier_envoi'] < 3.5:
                    return
            elif maintenant - live_flags['dernier_envoi'] < base * (1 + min(live_flags['echecs'], 8)):
                return
        live_flags['sale'] = False
        live_flags['urgent'] = False
        live_flags['immediat'] = False
    if not get_user_id():
        live_flags['sale'] = True
        live_flags['urgent'] = True
        live_flags['dernier_envoi'] = maintenant
        return
    snap = live_instantane()
    sans_maj = dict(snap)
    sans_maj.pop('maj', None)
    empreinte = hashlib.md5(json.dumps(sans_maj, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    if empreinte == live_flags['empreinte'] and not force and not keepalive:
        return
    live_flags['dernier_envoi'] = maintenant
    try:
        res = requests.post(f"{SUPABASE_URL}/rest/v1/rpc/live_releve", headers=get_headers(), json={'p_etat': snap, 'p_debut': snap.get('debut')}, timeout=10)
        live_trace('live_releve : %d octets -> HTTP %s %s' % (len(json.dumps(snap)), res.status_code, (res.text or '')[:160]))
        if res.status_code in (200, 201, 204):
            live_flags['empreinte'] = empreinte
            live_flags['echecs'] = 0
            return
        if res.status_code == 404:
            live_flags['absente'] = True      # fonction pas encore creee en base : on ne reessaie pas
            return
        raise RuntimeError('HTTP ' + str(res.status_code))
    except Exception as ex:
        live_trace('envoi en echec : %r' % (ex,))
        live_flags['echecs'] += 1
        live_flags['sale'] = True
        live_flags['urgent'] = True


LIVE_FSD_CHARGE = 1 << 17           # Status.json, Flags : FSD en charge (la barre de lancement, avant le saut)
LIVE_SUPERCRUISE = 1 << 4
LIVE_INTERDIT = 1 << 23             # Status.json, Flags : vaisseau en cours d'interception (mini-jeu)


def live_surveiller_fsd(jdir):
    """Debut de la charge du FSD (le journal ne l'ecrit pas, seul Status.json l'indique) : si des passagers sont a bord et que le saut sera un saut hyperespace
    (en supercruise, ou en espace normal avec un systeme vise), la page passe l'annonce tout de suite. Renvoie True si l'annonce vient d'etre declenchee."""
    try:
        with open(os.path.join(jdir, 'Status.json'), 'r', encoding='utf-8') as f:
            js = json.load(f)
        flags = int(js.get('Flags') or 0)
    except Exception:
        return False
    try:
        cmb_status(js)                # onglet Combat : boucliers, armes sorties, groupe de tir, sante a pied
    except Exception as ex:
        live_trace('combat : status ignore %r' % (ex,))
    # interception en cours (Status.json, bit 23 « Being Interdicted ») : le mini-jeu commence, quel que soit l'interdicteur (PNJ ou joueur) ; la page passe l'annonce et les cris
    intercepte = bool(flags & LIVE_INTERDIT)
    avant_i = live_flags.get('interdit', False)
    live_flags['interdit'] = intercepte
    if intercepte and not avant_i:
        with live_verrou:
            et = live_etat
            pax = any(live_est_passagers(m) for m in et['missions'].values())
            live_trace('interception (Status.json) : flags=%d passagers=%s en_ligne=%s' % (flags, pax, et['en_ligne']))
            if et['en_ligne'] and pax:
                et['interception'] = {'t': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'e': 'debut'}
                live_flags['sale'] = True
                live_flags['immediat'] = True
                return True
    charge = bool(flags & LIVE_FSD_CHARGE)
    avant = live_flags.get('fsd_charge', False)
    live_flags['fsd_charge'] = charge
    if not charge or avant:
        return False
    with live_verrou:
        et = live_etat
        pax = any(live_est_passagers(m) for m in et['missions'].values())
        hyper = bool(flags & LIVE_SUPERCRUISE) or bool(et.get('cible'))
        live_trace('charge FSD : flags=%d passagers=%s hyperespace=%s cible=%s' % (flags, pax, hyper, et.get('cible')))
        if not (et['en_ligne'] and pax and hyper):
            return False
        et['saut'] = {'t': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'sys': et.get('cible') or ''}
        live_flags['sale'] = True
        live_flags['immediat'] = True
    return True


def live_boucle():
    while True:
        try:
            live_suivre()
        except Exception as ex:
            live_trace('suivi du journal : %r' % (ex,))
        live_flags['pret'] = True
        try:
            explo_sauver_si_besoin()
        except Exception:
            pass
        try:
            live_envoyer()
        except Exception as ex:
            live_trace('envoi : %r' % (ex,))
        try:
            cmb_envoyer()
        except Exception as ex:
            live_trace('envoi combat : %r' % (ex,))
        try:
            croises_envoyer()
        except Exception as ex:
            live_trace('envoi joueurs croises : %r' % (ex,))
        try:
            hg_envoyer()
        except Exception as ex:
            live_trace('envoi hangar : %r' % (ex,))
        # pendant les 2 s d'attente : surveillance de la charge du FSD (un petit fichier lu 4 fois par passe, seulement en jeu)
        jdir = trouver_journal_dir() if live_etat.get('en_ligne') else None
        for _ in range(4):
            try:
                if jdir and live_surveiller_fsd(jdir):
                    live_envoyer()
                if jdir:
                    cmb_envoyer()         # le combat se met a jour des que Status.json ou le journal a bouge (au plus toutes les 4 s)
            except Exception as ex:
                live_trace('surveillance FSD : %r' % (ex,))
            time.sleep(0.5)


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
        try: ppm_envoyer()  # Powerplay par systeme : etat des systemes et merites en attente (au plus toutes les 2 min)
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

def version_signaler():
    """Dit a la base quelle version du plugin tourne chez ce pilote (une requete au demarrage, puis une toutes les 12 h : le serveur n'ecrit que si besoin)."""
    time.sleep(25)
    essais = 0
    while True:
        attente = 12 * 3600
        try:
            if lire_cle() and not invalid_api_key:
                res = requests.post(f"{SUPABASE_URL}/rest/v1/rpc/plugin_signaler", headers=get_headers(), json={'p_version': str(PLUGIN_VERSION)}, timeout=10)
                if res.status_code == 404:
                    return                                  # fonction pas encore creee en base : on ne reessaie pas
            else:
                essais += 1
                if essais < 20: attente = 120               # pas encore de cle : on reessaie un peu plus tard
        except Exception:
            attente = 600
        time.sleep(attente)

def plugin_start3(plugin_dir):
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=check_for_updates, daemon=True).start()
    threading.Thread(target=version_signaler, daemon=True).start()
    threading.Thread(target=pp_scanner_journaux, daemon=True).start()
    threading.Thread(target=col_scanner_journaux, daemon=True).start()
    threading.Thread(target=ppm_scanner_journaux, daemon=True).start()
    threading.Thread(target=bud_scanner_journaux, daemon=True).start()
    threading.Thread(target=hg_scanner_journaux, daemon=True).start()
    threading.Thread(target=live_boucle, daemon=True).start()
    return "SYS.EDTEAM"

def plugin_stop():
    try:
        explo_etat_init['sauve'] = 0
        explo_etat_init['sale'] = True
        explo_sauver_si_besoin()
    except Exception: pass
    try: pp_envoyer(force=True)
    except: pass
    try: ppm_envoyer(force=True)
    except: pass
    try: col_envoyer(force=True)
    except: pass
    try: bud_envoyer(force=True)
    except: pass
    try: bud_sauver_etat()
    except: pass
    try: croises_envoyer(force=True)
    except: pass
    try: hg_envoyer(force=True)
    except: pass
    try: hg_sauver_etat()
    except: pass
    try:
        with live_verrou:
            live_etat['en_ligne'] = False
        live_envoyer(force=True)
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

# =====================================================================================================
# RELEVE DU SYSTEME POUR LES DIRECTIVES (v2.42) : EDSM n'est mis a jour que si un joueur visite le systeme avec un outil qui envoie ses donnees.
# Quand le pilote ARRIVE (saut ou localisation) dans un systeme cible d'une directive ACTIVE de son escadron, la liste des factions du journal (influence, etats) est envoyee a la fonction serveur
# bgs_releve_pilote. La page affiche alors « mis a jour par CMDR X il y a N h ». Rien n'est envoye pour les autres systemes ; une fonction serveur absente (404) arrete les essais jusqu'au redemarrage.
# =====================================================================================================
bgs_releve_etat = {'absente': False, 'derniers': {}, 'ordres': [], 'ordres_ts': 0.0}

def bgs_ordres_cibles():
    """Systemes des directives actives (liste mise en memoire 5 minutes). Vide si la lecture echoue."""
    try:
        maintenant = time.time()
        if maintenant - bgs_releve_etat['ordres_ts'] > 300:
            res = requests.get(f"{SUPABASE_URL}/rest/v1/ordres_bgs?statut=eq.ACTIF&select=systeme_cible", headers=get_headers(), timeout=5)
            if res.status_code == 200:
                bgs_releve_etat['ordres'] = [str(o.get('systeme_cible') or '').strip().lower() for o in res.json() if o.get('systeme_cible')]
                bgs_releve_etat['ordres_ts'] = maintenant
        return bgs_releve_etat['ordres']
    except Exception:
        return bgs_releve_etat['ordres']

def bgs_noms_etats(liste):
    """['Expansion', 'War'] a partir de [{'State': 'Expansion'}, ...] (journal du jeu)."""
    out = []
    for e in (liste or []):
        n = e.get('State') if isinstance(e, dict) else None
        if n: out.append(str(n))
    return out[:8]

def bgs_releve_charge(entry):
    """Construit la charge utile d'un releve a partir d'un FSDJump / Location : None si rien a envoyer."""
    systeme = str(entry.get('StarSystem') or '').strip()
    factions = entry.get('Factions') or []
    if not systeme or not isinstance(factions, list) or not factions: return None
    liste = []
    for f in factions[:14]:
        if not isinstance(f, dict): continue
        nom, inf = f.get('Name'), f.get('Influence')
        if not nom or inf is None: continue
        try: inf = float(inf)
        except Exception: continue
        if inf < 0 or inf > 1: continue
        liste.append({'n': str(nom), 'i': round(inf, 6), 'e': str(f.get('FactionState') or ''), 'a': bgs_noms_etats(f.get('ActiveStates')),
                      'p': bgs_noms_etats(f.get('PendingStates')), 'r': bgs_noms_etats(f.get('RecoveringStates')), 'h': str(f.get('Happiness_Localised') or '')})
    if not liste: return None
    return {'p_systeme': systeme, 'p_ts': str(entry.get('timestamp') or ''), 'p_factions': liste}

def bgs_releve_systeme(entry):
    try:
        if bgs_releve_etat['absente']: return
        charge = bgs_releve_charge(entry)
        if not charge or not charge['p_ts']: return
        cle = charge['p_systeme'].strip().lower()
        if bgs_releve_etat['derniers'].get(cle) == charge['p_ts']: return
        if not get_user_id(): return
        if cle not in bgs_ordres_cibles(): return
        res = requests.post(f"{SUPABASE_URL}/rest/v1/rpc/bgs_releve_pilote", headers=get_headers(), json=charge, timeout=8)
        nom = charge['p_systeme'].upper()
        if res.status_code == 404:
            bgs_releve_etat['absente'] = True
            mettre_a_jour_interface(">_ BGS : RELEVÉ DU SYSTÈME INDISPONIBLE (fonction serveur absente)", "#FF9A1F")
        elif res.status_code in (200, 201, 204):
            bgs_releve_etat['derniers'][cle] = charge['p_ts']
            try: rep = res.json() if res.content else {}
            except Exception: rep = {}
            if not isinstance(rep, dict): rep = {}
            if rep.get('ok'):
                if rep.get('ignore'): mettre_a_jour_interface(f">_ BGS : {nom} DÉJÀ À JOUR (relevé plus récent connu)", "#00FF66")
                else: mettre_a_jour_interface(f">_ BGS : RELEVÉ DU SYSTÈME ({nom}) ENVOYÉ", "#00FF66")
            else:
                mettre_a_jour_interface(f">_ BGS : RELEVÉ DE {nom} REFUSÉ ({rep.get('raison') or 'réponse vide'})", "#FF9A1F")
        else:
            mettre_a_jour_interface(f">_ BGS : RELEVÉ DE {nom} ÉCHOUÉ (HTTP {res.status_code})", "#FF9A1F")
    except Exception as e:
        logging.error(f"[SYS_EDTEAM] Erreur releve du systeme : {e}")

# =====================================================================================================
# MISSION DE REPERAGE (v2.43) : l'escadron liste les systemes dont la donnee est trop vieille (directives, expansions rivales, places libres, conquetes Powerplay).
# Quand le pilote ARRIVE dans l'un d'eux (saut ou localisation), le plugin envoie ce que voit son jeu : coordonnees, nombre de factions, progression de conquete des puissances.
# Aucun envoi pour les autres systemes. La liste des noms vient de la fonction serveur reperage_cibles (gardee 20 minutes).
# =====================================================================================================
reperage_etat = {'absente': False, 'cibles': set(), 'cibles_ts': 0.0, 'derniers': {}}

def reperage_cibles_liste():
    try:
        maintenant = time.time()
        if maintenant - reperage_etat['cibles_ts'] > 1200:
            res = requests.post(f"{SUPABASE_URL}/rest/v1/rpc/reperage_cibles", headers=get_headers(), json={}, timeout=6)
            if res.status_code == 404:
                reperage_etat['absente'] = True
                return set()
            if res.status_code == 200:
                reperage_etat['cibles'] = {str(x).strip().upper() for x in (res.json() or []) if x}
                reperage_etat['cibles_ts'] = maintenant
        return reperage_etat['cibles']
    except Exception:
        return reperage_etat['cibles']

def reperage_charge(entry):
    """Charge utile d'un releve (FSDJump / Location) : None si le systeme n'a pas de nom."""
    systeme = str(entry.get('StarSystem') or '').strip()
    if not systeme: return None
    charge = {'s': systeme, 'ts': str(entry.get('timestamp') or '')}
    pos = entry.get('StarPos')
    if isinstance(pos, list) and len(pos) == 3:
        try: charge['x'], charge['y'], charge['z'] = float(pos[0]), float(pos[1]), float(pos[2])
        except Exception: pass
    factions = entry.get('Factions')
    if isinstance(factions, list): charge['nb'] = len(factions)
    pp = []
    for c in (entry.get('PowerplayConflictProgress') or [])[:14]:
        try:
            if isinstance(c, dict) and c.get('Power') and c.get('ConflictProgress') is not None:
                v = float(c.get('ConflictProgress'))
                if 0 <= v <= 1: pp.append({'p': str(c.get('Power')), 'v': round(v, 6)})
        except Exception:
            pass
    if pp: charge['pp'] = pp
    return charge

def reperage_releve_systeme(entry):
    try:
        if reperage_etat['absente']: return
        charge = reperage_charge(entry)
        if not charge or not charge['ts']: return
        cle = charge['s'].upper()
        if reperage_etat['derniers'].get(cle) == charge['ts']: return
        if not get_user_id(): return
        if cle not in reperage_cibles_liste(): return
        res = requests.post(f"{SUPABASE_URL}/rest/v1/rpc/reperage_releve", headers=get_headers(), json={'p': charge}, timeout=8)
        if res.status_code == 404:
            reperage_etat['absente'] = True
        elif res.status_code in (200, 201, 204):
            reperage_etat['derniers'][cle] = charge['ts']
            try: rep = res.json() if res.content else {}
            except Exception: rep = {}
            if not isinstance(rep, dict): rep = {}
            if rep.get('ok'): mettre_a_jour_interface(f">_ REPÉRAGE : {cle} RELEVÉ POUR L'ESCADRON" + (" (+1 POINT BGS)" if rep.get('point') else ""), "#00F0FF")
            else: mettre_a_jour_interface(f">_ REPÉRAGE : {cle} REFUSÉ ({rep.get('raison') or 'réponse vide'})", "#FF9A1F")
        else:
            mettre_a_jour_interface(f">_ REPÉRAGE : {cle} ÉCHOUÉ (HTTP {res.status_code})", "#FF9A1F")
    except Exception as e:
        logging.error(f"[SYS_EDTEAM] Erreur repérage : {e}")

# =====================================================================================================
# INSTALLATIONS A SCENARIO (v2.50) : le journal ne donne que le NOM d'une installation (FSSSignalDiscovered, SignalType « Installation »), jamais son proprietaire.
# Le plugin envoie (adresse du systeme, noms) groupes par 4 secondes ; le serveur (installations_signaler, SQL 165) les croise avec les factions en Boom des directives de HAUSSE.
# Un nom n'est envoye qu'une fois par session d'EDMC. Aucun affichage dans l'interface du plugin (rien a signaler au pilote).
# =====================================================================================================
installations_etat = {'absente': False, 'envoyees': set(), 'attente': {}, 'minuteur': None, 'noms': {}}
installations_verrou = threading.Lock()

def installations_retenir_systeme(entry):
    """Retient le nom du systeme par son adresse (debut de saut, saut, localisation) : le systeme courant d'EDMC retarde sur les signaux."""
    adr, nom = entry.get('SystemAddress'), str(entry.get('StarSystem') or '').strip()
    if isinstance(adr, int) and nom:
        if len(installations_etat['noms']) > 500: installations_etat['noms'].clear()
        installations_etat['noms'][adr] = nom

def installations_noter(entry):
    """Note une installation vue (evenement FSSSignalDiscovered) ; l'envoi part 4 secondes apres le dernier nom note."""
    if installations_etat['absente']: return
    nom = str(entry.get('SignalName') or '').strip()
    adr = entry.get('SystemAddress')
    if not nom or not isinstance(adr, int): return
    if nom.startswith('$') or len(nom) > 100: return
    with installations_verrou:
        if (adr, nom) in installations_etat['envoyees']: return
        # a l'arrivee par hyperespace, le journal ecrit les signaux AVANT le saut : le nom du systeme est resolu a l'envoi, pas ici
        lot = installations_etat['attente'].setdefault(adr, {'n': set()})
        lot['n'].add(nom)
        if installations_etat['minuteur'] is None:
            installations_etat['minuteur'] = threading.Timer(4.0, installations_envoyer)
            installations_etat['minuteur'].daemon = True
            installations_etat['minuteur'].start()

def installations_envoyer():
    with installations_verrou:
        lots = installations_etat['attente']
        installations_etat['attente'] = {}
        installations_etat['minuteur'] = None
    try:
        if not lots or installations_etat['absente'] or not get_user_id(): return
        for adr, lot in lots.items():
            systeme = installations_etat['noms'].get(adr)
            if not systeme: continue            # systeme inconnu : rien n'est envoye (plutot qu'un mauvais nom), les noms seront revus au prochain passage
            noms = sorted(lot['n'])[:30]
            res = requests.post(f"{SUPABASE_URL}/rest/v1/rpc/installations_signaler", headers=get_headers(),
                                json={'p': {'s': systeme, 'a': str(adr), 'n': noms}}, timeout=8)
            if res.status_code == 404:
                installations_etat['absente'] = True
                return
            if res.status_code in (200, 201, 204):
                with installations_verrou:
                    for nom in noms: installations_etat['envoyees'].add((adr, nom))
    except Exception as e:
        logging.error(f"[SYS_EDTEAM] Erreur installations : {e}")

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
# =====================================================================================================
# MEMOIRE DES MISSIONS ACCEPTEES (BGS) : qui les a donnees, ou elles ont ete acceptees, ou elles vont.
# Elle sert, a la fin ou a l'abandon d'une mission, a la rattacher a la bonne directive (faction + systeme d'ORIGINE de la mission). Elle etait gardee en memoire seulement :
# chaque redemarrage d'EDMC (mise a jour du plugin, plantage) l'effacait, et une mission terminee ensuite etait rattachee au systeme ou se trouvait le pilote (donc a aucune directive).
# Desormais : gardee sur disque (bgs_missions.json) ET reconstituee au demarrage depuis le journal de la partie en cours.
# =====================================================================================================
BGS_MISSIONS_FICHIER = 'bgs_missions.json'
BGS_MISSIONS_MAX = 300
bgs_re_mission = re.compile('"event":"(Location|FSDJump|CarrierJump|MissionAccepted)"')


def bgs_chemin_missions():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), BGS_MISSIONS_FICHIER)


def bgs_infos_mission(entry, systeme):
    nom = str(entry.get('Name', '')).lower()
    return {
        'faction': entry.get('Faction', ''),
        'system': systeme,
        'dest_system': entry.get('DestinationSystem', systeme),
        'is_combat': any(k in nom for k in ['massacre', 'assassin', 'kill', 'pirat', 'combat', 'destroy', 'skimmer']),
        'is_pirate': any(k in nom for k in ['pirat', 'deserter', 'anarchy']),
        't': entry.get('timestamp', '')
    }


def bgs_sauver_missions(missions):
    try:
        gardes = sorted(missions.items(), key=lambda kv: str((kv[1] or {}).get('t') or ''))[-BGS_MISSIONS_MAX:]
        tmp = bgs_chemin_missions() + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({str(k): v for k, v in gardes}, f)
        os.replace(tmp, bgs_chemin_missions())
    except Exception as e:
        logging.error(f"[SYS_EDTEAM] Sauvegarde des missions impossible : {e}")


def bgs_reconstituer_missions(missions):
    """Relit les journaux de la partie en cours (tous les morceaux, dans l'ordre) : pour chaque mission acceptee qu'on ne connait pas encore, retrouve le systeme ou elle a ete acceptee."""
    jdir = trouver_journal_dir()
    if not jdir or not os.path.isdir(jdir):
        return
    fichiers = lister_journaux(jdir)
    if not fichiers:
        return
    prefixe = fichiers[-1].split('.')[1]
    systeme = ''
    for nom in [f for f in fichiers if f.split('.')[1] == prefixe]:
        with open(os.path.join(jdir, nom), 'r', encoding='utf-8', errors='ignore') as f:
            for ligne in f:
                if not bgs_re_mission.search(ligne):
                    continue
                try:
                    e = json.loads(ligne)
                except Exception:
                    continue
                ev = e.get('event')
                if ev == 'MissionAccepted':
                    m_id = e.get('MissionID')
                    if m_id and m_id not in missions:
                        missions[m_id] = bgs_infos_mission(e, systeme)
                elif e.get('StarSystem'):
                    systeme = e.get('StarSystem')


def bgs_charger_missions():
    missions = {}
    try:
        with open(bgs_chemin_missions(), 'r', encoding='utf-8') as f:
            for k, v in json.load(f).items():
                missions[int(k)] = v
    except Exception:
        pass
    try:
        avant = len(missions)
        bgs_reconstituer_missions(missions)
        if len(missions) != avant:
            bgs_sauver_missions(missions)
    except Exception as e:
        logging.error(f"[SYS_EDTEAM] Reconstitution des missions impossible : {e}")
    return missions


def journal_entry(cmdr, is_beta, system, station, entry, state):
    global systeme_actuel, cmdr_actuel
    cmdr_actuel = cmdr
    if system and system != systeme_actuel: systeme_actuel = system
    event = entry.get('event')

    # Colonisation : releve des chantiers, livraisons et systemes (aucun appel reseau ici)
    try: col_traiter_evenement(entry)
    except Exception as e: col_trace('evenement ignore : %r' % (e,))

    # Installations a scenario vues (nom seulement, groupees avant l'envoi : v2.50)
    if event in ('StartJump', 'FSDJump', 'Location', 'CarrierJump'): installations_retenir_systeme(entry)
    if event == 'FSSSignalDiscovered' and entry.get('SignalType') == 'Installation':
        try: installations_noter(entry)
        except Exception as e: logging.error(f"[SYS_EDTEAM] Installation ignoree : {e}")

    # Powerplay par systeme : etat des systemes et merites rattaches au systeme (aucun appel reseau ici)
    try: ppm_traiter_evenement(entry, ppm_live)
    except Exception as e: ppm_trace('evenement ignore : %r' % (e,))

    # Budget : revenus et charges (aucun appel reseau ici)
    try: bud_traiter_evenement(entry)
    except Exception as e: bud_trace('evenement ignore : %r' % (e,))

    # Hangar : vaisseaux et equipement de chacun (aucun appel reseau ici)
    try: hg_traiter_evenement(entry)
    except Exception as e: hg_trace('evenement ignore : %r' % (e,))

    # Suivi CZ
    if not hasattr(journal_entry, 'cz_cache'):
        journal_entry.cz_cache = {'intensity': 'S', 'points': 1, 'faction': '', 'won': False, 'drop': False}

    if event in ['SupercruiseEntry', 'FSDJump', 'CarrierJump', 'Location']:
        journal_entry.cz_cache = {'intensity': 'S', 'points': 1, 'faction': '', 'won': False, 'drop': False}

    elif event == 'SupercruiseDestinationDrop':
        drop_type = str(entry.get('Type', '')).lower()
        if 'warzone' in drop_type or 'conflict' in drop_type:
            journal_entry.cz_cache['drop'] = True       # l'intensite vient bien du jeu (sinon « faible » par defaut, par exemple au sol)
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
            try:
                cmb_g['conf'] = {'sys': entry.get('StarSystem') or systeme_actuel, 'paires': [[(c.get('Faction1') or {}).get('Name', ''), (c.get('Faction2') or {}).get('Name', '')] for c in conflits if str(c.get('Status', '')).lower() != 'pending' and (c.get('Faction1') or {}).get('Name') and (c.get('Faction2') or {}).get('Name')]}
            except Exception:
                pass
            sys_nom = entry.get('StarSystem') or systeme_actuel
            if conflits and sys_nom:
                threading.Thread(target=sync_conflits_supabase, args=(sys_nom, conflits), daemon=True).start()
            # releve des factions du systeme, seulement s'il est cible d'une directive active (v2.42)
            threading.Thread(target=bgs_releve_systeme, args=(entry,), daemon=True).start()
            # mission de reperage de l'escadron : coordonnees, nombre de factions, progression de conquete (v2.43)
            threading.Thread(target=reperage_releve_systeme, args=(entry,), daemon=True).start()

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

    # (le ciblage d'un joueur n'est plus envoye au site : l'alerte COVAS et le registre tactique ont ete retires ; le combat lit ShipTargeted dans cmb_traiter)

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
        journal_entry.bgs_cache = {'missions': bgs_charger_missions(), 'station_faction': '', 'ordres': [], 'ordres_ts': 0}
    
    if entry.get('event') in ['Docked', 'Location', 'ApproachSettlement']:
        faction_info = entry.get('StationFaction') or entry.get('SystemFaction')
        if isinstance(faction_info, dict):
            journal_entry.bgs_cache['station_faction'] = faction_info.get('Name', '')
        elif isinstance(faction_info, str):
            journal_entry.bgs_cache['station_faction'] = faction_info

    if entry.get('event') == 'MissionAccepted': 
        m_id = entry.get('MissionID')
        if m_id:
            journal_entry.bgs_cache['missions'][m_id] = bgs_infos_mission(entry, system or systeme_actuel)
            bgs_sauver_missions(journal_entry.bgs_cache['missions'])

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
                    if isinstance(m_info, dict):
                        m_info['deja_compte'] = True
                        bgs_sauver_missions(journal_entry.bgs_cache['missions'])
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
                    act_sci = {"faction": f_station, "type": "SCIENCE", "valeur": val, "system": systeme_actuel, "is_combat": False}
                    if evt == 'SellOrganicData':
                        # L'exobiologie ne change PAS l'influence d'une faction (seule la cartographie le fait) : elle compte pour le rang BGS
                        # en soutien libre (station de la faction de l'escadron), mais n'est JAMAIS rattachee a une directive.
                        act_sci["sans_directive"] = True
                        act_sci["details"] = "Exobiologie"
                    actions.append(act_sci)

            # Commerce : une vente au marché compte comme ECONOMIE, pour son BÉNÉFICE seulement (ou CONTREBANDE si volée). Elle n'est JAMAIS une livraison de colonisation :
            # les vraies livraisons à un chantier (ColonisationContribution) sont relevées par le module colonisation, côté serveur.
            elif evt == 'MarketSell':
                val = entry.get('TotalSale', 0)
                if val > 0:
                    illegal = bool(entry.get('Stolen', False) or entry.get('IllegalGoods', False))
                    # Vente a perte : prix paye moyen superieur au prix de vente (marchandise achetee ; un minage ou un butin a un prix paye de 0).
                    # Sous une directive de BAISSE de la faction de la station, elle fait baisser son influence : le serveur recoit la PERTE en credits (voir la boucle d'envoi).
                    perte = 0
                    benefice = val
                    if not illegal:
                        try:
                            benefice = int(float(val) - float(entry.get('AvgPricePaid', 0) or 0) * float(entry.get('Count', 0) or 0))
                        except (TypeError, ValueError):
                            benefice = val
                        perte = -benefice if benefice < 0 else 0
                    action_vente = {
                        "faction": f_station,
                        "type": "CONTREBANDE" if illegal else "ECONOMIE",
                        "valeur": val if illegal else benefice,    # commerce : seul le BENEFICE fait monter l'influence (marchandise minee ou recuperee : prix paye 0, tout est benefice)
                        "system": systeme_actuel,
                        "is_combat": False
                    }
                    if not illegal:
                        action_vente["details"] = "Vente : " + str(int(val)) + " CR"    # montant de la vente, affiche a cote du benefice
                    if perte > 0:
                        action_vente["perte"] = perte
                        action_vente["details_perte"] = "Vente à perte : " + str(entry.get('Type_Localised') or entry.get('Type') or '?') + " x" + str(entry.get('Count', 0))
                    actions.append(action_vente)

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
                    elif act_faction and not action.get('sans_directive'):
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

                        # Vente a perte dans une station de la faction visee par une BAISSE : action offensive (VENTE_PERTE), valeur = perte en credits
                        if ordre_valide and ordre_valide.get('type_ordre') == 'BAISSE' and action['type'] == 'ECONOMIE' and action.get('perte', 0) > 0:
                            action = dict(action, type='VENTE_PERTE', valeur=action['perte'], details=action.get('details_perte'))
                            valeur_finale = action['valeur']

                        # Commerce sans benefice (vente a perte hors BAISSE, ou a prix coutant) : rien a envoyer
                        if action['type'] == 'ECONOMIE' and action['valeur'] <= 0: continue

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
                            elif est_operation_noire:
                                mettre_a_jour_interface(f">_ OPÉRATION NOIRE : {action['type']} (+{int(valeur_finale)})", "#FF3333")
                            else:
                                mettre_a_jour_interface(f">_ SOUTIEN LIBRE : {action['type']} (+{int(valeur_finale)})", "#00FF66")

                            # Brèves QG narratives (seuils sélectifs)
                            if action['type'] == 'CZ_VICTOIRES':
                                cmb_victoire(action.get('intensite', 'S'), journal_entry.cz_cache.get('drop', False), ordre_valide)     # onglet Combat
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