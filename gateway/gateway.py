#!/usr/bin/env python3
"""
AERIS - Gateway IoT (Raspberry Pi)
==================================

Flujo de datos::

    Arduino --MQTT--> Mosquitto local --> gateway.py --> SQLite (aeris.db)
                                             |-> procesamiento local (ventana)
                                             |-> IA: prediccion + anomalia + LLM
                                             |-> Telegram (notificaciones)
                                             +-MQTT/TLS-> Ubidots (nube)
    Ubidots (switch "alarma") --MQTT--> gateway.py --MQTT--> Arduino (aeris/<id>/cmd)

Hilos (todos independientes del hilo principal):
    * ``paho-local``   : red MQTT con Mosquitto (loop_start de paho).
    * ``paho-nube``    : red MQTT con Ubidots (loop_start de paho).
    * ``Procesamiento``: cada UBIDOTS_INTERVALO_S agrega la ventana, guarda y sube.
    * ``Reenvio``      : store-and-forward; reenvia lo que no llego a la nube.
    * ``IA``           : prediccion, anomalia y recomendacion con un LLM.

Procesamiento local antes de subir (requisito del reto):
    1. Validacion: descarta lecturas fuera de rango fisico.
    2. Agregacion: mediana por variable de la ventana (robusta a picos).
    3. Derivadas: tasa de descenso del nivel (cm/h) en la ventana.
    4. Solo se sube el agregado (menos trafico y menos "dots" en Ubidots).

Toda la configuracion vive en ``.env`` (ver ``.env.ejemplo``).
"""
import json
import logging
import os
import sqlite3
import statistics
import threading
import time
from datetime import datetime

import paho.mqtt.client as mqtt
import requests
from dotenv import load_dotenv

load_dotenv()


def _env_int(nombre, defecto):
    return int(os.getenv(nombre, str(defecto)))


# ---------------- Configuracion ----------------
LOCAL_HOST = os.getenv("LOCAL_HOST", "localhost")
LOCAL_PORT = _env_int("LOCAL_PORT", 1883)
LOCAL_USER = os.getenv("LOCAL_USER", "")
LOCAL_PASS = os.getenv("LOCAL_PASS", "")

UBIDOTS_TOKEN = os.getenv("UBIDOTS_TOKEN", "")
UBIDOTS_DEVICE = os.getenv("UBIDOTS_DEVICE", "aeris")
UBIDOTS_HOST = os.getenv("UBIDOTS_HOST", "industrial.api.ubidots.com")
UBIDOTS_TLS = os.getenv("UBIDOTS_TLS", "1") == "1"
UBIDOTS_PORT = _env_int("UBIDOTS_PORT", 8883 if UBIDOTS_TLS else 1883)
# Plan Ubidots STEM (verificar en la cuenta): 10 variables por dispositivo,
# ~4000 dots/dia y ~30 dots/min. Con 8 variables cada 300 s -> 2304 dots/dia en
# "aeris" + 4 variables de IA cada 300 s -> 1152 en "aeris-ia" = ~3500/dia.
UBIDOTS_INTERVALO_S = _env_int("UBIDOTS_INTERVALO_S", 300)
UBIDOTS_DEVICE_IA = os.getenv("UBIDOTS_DEVICE_IA", "aeris-ia")
# Variables que se suben (para no pasar el limite del plan). Separadas por coma.
UBIDOTS_VARIABLES = [v.strip() for v in os.getenv(
    "UBIDOTS_VARIABLES",
    "distancia,temperatura,humedad,presion,lux,evaporacion,score_riesgo,"
    "alarma_nivel").split(",") if v.strip()]

IA_MODO = os.getenv("IA_MODO", "groq")          # ollama | groq | gemini | ninguno
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODELO = os.getenv("OLLAMA_MODELO", "llama3.2:1b")
GROQ_KEY = os.getenv("GROQ_KEY", "")
GROQ_MODELO = os.getenv("GROQ_MODELO", "openai/gpt-oss-20b")
GEMINI_KEY = os.getenv("GEMINI_KEY", "")
GEMINI_MODELO = os.getenv("GEMINI_MODELO", "gemini-2.5-flash")
IA_INTERVALO_S = _env_int("IA_INTERVALO_S", 300)
IA_PRIMER_CICLO_S = _env_int("IA_PRIMER_CICLO_S", 60)
IA_VENTANA_S = _env_int("IA_VENTANA_S", 3 * 3600)
IA_MIN_LECTURAS = _env_int("IA_MIN_LECTURAS", 10)
IA_UMBRAL_Z = float(os.getenv("IA_UMBRAL_Z", "3.0"))
# Piso de la desviacion de residuos (cm). Cerca de la resolucion del AJ-SR04M:
# evita z enormes cuando el nivel esta casi quieto y la desviacion tiende a 0.
IA_SD_MIN_CM = float(os.getenv("IA_SD_MIN_CM", "0.5"))
# 1 = mandar a Telegram la recomendacion de CADA ciclo (util para la demo).
# 0 = solo si hay anomalia o el tanque se vacia en menos de 24 h.
IA_NOTIFICAR_SIEMPRE = os.getenv("IA_NOTIFICAR_SIEMPRE", "0") == "1"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT = os.getenv("TELEGRAM_CHAT", "")

DB_PATH = os.getenv("DB_PATH", "aeris.db")
DISTANCIA_VACIO_CM = float(os.getenv("DISTANCIA_VACIO_CM", "100"))
REENVIO_INTERVALO_S = _env_int("REENVIO_INTERVALO_S", 60)
REENVIO_LOTE = _env_int("REENVIO_LOTE", 10)
# Pausa entre reenvios: STEM limita ~30 dots/min y ~1 request/s por token.
# 8 dots por agregado cada 20 s = 24 dots/min, por debajo del limite.
REENVIO_PAUSA_S = _env_int("REENVIO_PAUSA_S", 20)

# Rangos fisicos validos (validacion local). Fuera de rango -> se descarta.
RANGOS = {
    "distancia": (20.0, 400.0),     # zona util del AJ-SR04M
    "temperatura": (-10.0, 60.0),
    "humedad": (0.0, 100.0),
    "presion": (500.0, 1100.0),     # hPa
    "lux": (0.0, 200000.0),
    "evaporacion": (0.0, 100.0),
    "vpd": (0.0, 10.0),
    "scoreRiesgo": (0.0, 100.0),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(threadName)s] %(message)s")
log = logging.getLogger("aeris")

# ---------------- Base de datos ----------------
db_lock = threading.Lock()
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS lecturas (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, dispositivo TEXT,
  temperatura REAL, humedad REAL, presion REAL, distancia REAL,
  luz REAL, lux REAL, vpd REAL, evaporacion REAL, score REAL,
  riesgo TEXT, alarma_nivel INTEGER, alarma_roja INTEGER,
  valida INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS agregados (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, dispositivo TEXT, n INTEGER, payload TEXT,
  enviado_nube INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS alertas (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, dispositivo TEXT, json TEXT
);
CREATE TABLE IF NOT EXISTS ia (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, dispositivo TEXT,
  horas_a_vacio REAL, tasa_cm_h REAL, residuo_z REAL, anomalia INTEGER,
  recomendacion TEXT
);
CREATE INDEX IF NOT EXISTS idx_lect_ts ON lecturas(ts);
CREATE INDEX IF NOT EXISTS idx_agr_env ON agregados(enviado_nube);
""")
db.commit()
# Migracion: marca de anomalia por lectura (la IA no vuelve a usar un pico como historia).
if "anomala" not in [c[1] for c in db.execute("PRAGMA table_info(lecturas)")]:
    db.execute("ALTER TABLE lecturas ADD COLUMN anomala INTEGER DEFAULT 0")
    db.commit()


def es_valida(d):
    """Devuelve True si todas las variables presentes estan en su rango fisico."""
    for clave, (lo, hi) in RANGOS.items():
        v = d.get(clave)
        if v is None:
            continue
        try:
            if not lo <= float(v) <= hi:
                return False
        except (TypeError, ValueError):
            return False
    return True


def guardar_lectura(d, valida):
    """Guarda una lectura cruda del Arduino (validas e invalidas, para trazabilidad)."""
    with db_lock:
        db.execute(
            """INSERT INTO lecturas (ts,dispositivo,temperatura,humedad,presion,distancia,
               luz,lux,vpd,evaporacion,score,riesgo,alarma_nivel,alarma_roja,valida)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (time.time(), d.get("id"), d.get("temperatura"), d.get("humedad"),
             d.get("presion"), d.get("distancia"), d.get("luz"), d.get("lux"),
             d.get("vpd"), d.get("evaporacion"), d.get("scoreRiesgo"), d.get("riesgo"),
             int(bool(d.get("alarmaNivel"))), int(bool(d.get("alarmaRoja"))), int(valida)))
        db.commit()


def lecturas_validas(dispositivo, desde_ts):
    """Lecturas validas de un dispositivo desde ``desde_ts`` (orden cronologico)."""
    with db_lock:
        cur = db.execute(
            "SELECT ts, distancia, temperatura, humedad, presion, lux, evaporacion, vpd, score,"
            " alarma_nivel, alarma_roja, id FROM lecturas"
            " WHERE dispositivo=? AND ts>=? AND valida=1 AND anomala=0 ORDER BY ts",
            (dispositivo, desde_ts))
        return cur.fetchall()


# ---------------- Utilidades numericas ----------------
def regresion(xs, ys):
    """Minimos cuadrados. Devuelve (pendiente, intercepto)."""
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs) or 1e-9
    m = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    return m, my - m * mx


def tasa_cm_h(filas):
    """Pendiente de la distancia (cm/h). Positiva = el agua esta bajando."""
    pts = [(f[0], f[1]) for f in filas if f[1] is not None]
    if len(pts) < 3:
        return None
    t0 = pts[0][0]
    m, _ = regresion([(t - t0) / 3600 for t, _ in pts], [d for _, d in pts])
    return round(m, 3)


# ---------------- Notificaciones ----------------
def notificar(texto):
    """Envia un aviso por Telegram (si esta configurado) y lo deja en el log."""
    log.info("NOTIFICACION: %s", texto)
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT, "text": texto}, timeout=10)
    except requests.RequestException as e:
        log.warning("Telegram fallo: %s", e)


# ---------------- Estado compartido ----------------
estado = {
    "dispositivo": None,
    "ultimo_riesgo": None,
    "alarma_arduino": None,     # ultimo alarmaRoja conocido del Arduino (True/False)
    "alarma_nube": None,        # ultimo valor publicado/recibido del switch en Ubidots
    "ia_ultimo_ts": 0.0,        # hasta donde evaluo anomalias el ciclo anterior
}
estado_lock = threading.Lock()

# ---------------- MQTT Ubidots (nube) ----------------
nube = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"aeris-gw-{int(time.time())}")
nube.username_pw_set(UBIDOTS_TOKEN, "")
if UBIDOTS_TLS:
    nube.tls_set()   # CA del sistema; cifra el enlace Gateway -> nube
TEMA_NUBE = f"/v1.6/devices/{UBIDOTS_DEVICE}"
TEMA_NUBE_ALARMA = f"{TEMA_NUBE}/alarma/lv"
# Segundo dispositivo (STEM permite 3): salidas de la IA, para no pasar de 10 variables.
TEMA_NUBE_IA = f"/v1.6/devices/{UBIDOTS_DEVICE_IA}"
nube_conectada = threading.Event()


def nube_on_connect(c, userdata, flags, rc, props=None):
    """Al conectar a Ubidots se suscribe al switch de alarma."""
    if rc == 0:
        nube_conectada.set()
        c.subscribe(TEMA_NUBE_ALARMA, qos=1)
        log.info("Ubidots conectado (%s:%s TLS=%s)", UBIDOTS_HOST, UBIDOTS_PORT, UBIDOTS_TLS)
    else:
        log.warning("Ubidots rechazo la conexion rc=%s", rc)


def nube_on_disconnect(c, userdata, flags, rc, props=None):
    nube_conectada.clear()
    log.warning("Ubidots desconectado rc=%s", rc)


def nube_on_message(c, userdata, msg):
    """Switch "alarma" de Ubidots -> comando al Arduino, solo si cambia el estado real."""
    try:
        quiere = float(msg.payload.decode().strip()) >= 0.5
    except ValueError:
        return
    with estado_lock:
        disp = estado["dispositivo"]
        estado["alarma_nube"] = quiere
        igual = estado["alarma_arduino"] == quiere
    if not disp or igual:
        return   # evita bucles: el Arduino ya esta en ese estado
    cmd = "alarmon" if quiere else "alarmoff"
    log.info("Ubidots pide %s -> %s", cmd, disp)
    local.publish(f"aeris/{disp}/cmd", cmd, qos=1)


nube.on_connect = nube_on_connect
nube.on_disconnect = nube_on_disconnect
nube.on_message = nube_on_message


def publicar_nube(payload, timeout=10.0, tema=None):
    """Publica en Ubidots (QoS 1) y espera el PUBACK. True si el broker lo confirmo."""
    if not nube_conectada.is_set():
        return False
    info = nube.publish(tema or TEMA_NUBE, json.dumps(payload), qos=1)
    try:
        info.wait_for_publish(timeout)
    except (RuntimeError, ValueError):
        return False
    return info.is_published()


def sincronizar_switch(alarma_roja):
    """Refleja en Ubidots el estado real de la alarma (p. ej. si se silencio en el tablero local)."""
    with estado_lock:
        estado["alarma_arduino"] = alarma_roja
        cambia = estado["alarma_nube"] != alarma_roja
        if cambia:
            estado["alarma_nube"] = alarma_roja
    if cambia and nube_conectada.is_set():
        nube.publish(TEMA_NUBE, json.dumps({"alarma": int(alarma_roja)}), qos=1)


# ---------------- MQTT local (Mosquitto) ----------------
local = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="aeris-gateway")
if LOCAL_USER:
    local.username_pw_set(LOCAL_USER, LOCAL_PASS)


def local_on_connect(c, userdata, flags, rc, props=None):
    log.info("Mosquitto conectado rc=%s", rc)
    c.subscribe([("aeris/+/telemetria", 0), ("aeris/+/alerta", 1), ("aeris/+/estado", 1)])


def local_on_message(c, userdata, msg):
    """Recibe telemetria, alertas y estado (LWT) del Arduino."""
    partes = msg.topic.split("/")
    if len(partes) != 3:
        return
    disp, tipo = partes[1], partes[2]
    texto = msg.payload.decode(errors="replace")
    if tipo == "estado":
        log.info("%s esta %s", disp, texto)
        if texto == "offline":
            notificar(f"AERIS: el dispositivo {disp} se desconecto.")
        return
    try:
        d = json.loads(texto)
    except json.JSONDecodeError:
        log.warning("JSON invalido en %s", msg.topic)
        return
    with estado_lock:
        estado["dispositivo"] = disp
    # El switch "alarma" de Ubidots solo se escribe aqui (no en el agregado),
    # para que un agregado viejo nunca pise una orden reciente de la nube.
    if "alarmaRoja" in d:
        sincronizar_switch(bool(d["alarmaRoja"]))
    if tipo == "telemetria":
        d["id"] = disp
        guardar_lectura(d, es_valida(d))
    elif tipo == "alerta":
        with db_lock:
            db.execute("INSERT INTO alertas (ts,dispositivo,json) VALUES (?,?,?)",
                       (time.time(), disp, texto))
            db.commit()
        riesgo = d.get("riesgo")
        with estado_lock:
            cambio = riesgo != estado["ultimo_riesgo"]
            estado["ultimo_riesgo"] = riesgo
        if d.get("alarmaNivel") or (cambio and d.get("nivelRiesgo", 0) >= 1):
            notificar(f"AERIS {disp}: riesgo {riesgo} (score {d.get('score')}%). "
                      f"Alarma de nivel: {'SI' if d.get('alarmaNivel') else 'no'}.")


local.on_connect = local_on_connect
local.on_message = local_on_message


# ---------------- Procesamiento local + store-and-forward ----------------
def agregar_ventana(disp, desde_ts):
    """Mediana de cada variable en la ventana + tasa de descenso. None si no hay datos."""
    filas = lecturas_validas(disp, desde_ts)
    if not filas:
        return None, 0

    def med(i):
        vals = [f[i] for f in filas if f[i] is not None]
        return round(statistics.median(vals), 2) if vals else None

    completo = {
        "distancia": med(1), "temperatura": med(2), "humedad": med(3),
        "presion": med(4), "lux": med(5), "evaporacion": med(6), "vpd": med(7),
        "score_riesgo": med(8),
        "alarma_nivel": int(any(f[9] for f in filas)),
    }
    payload = {k: v for k, v in completo.items() if v is not None and k in UBIDOTS_VARIABLES}
    return payload, len(filas)


def hilo_procesamiento():
    """Cada UBIDOTS_INTERVALO_S: agrega, guarda en SQLite y sube (marca enviado_nube)."""
    ultimo = time.time()
    while True:
        time.sleep(UBIDOTS_INTERVALO_S)
        ahora = time.time()
        with estado_lock:
            disp = estado["dispositivo"]
        if not disp:
            continue
        payload, n = agregar_ventana(disp, ultimo)
        ultimo = ahora
        if not payload:
            continue
        # timestamp en ms para que Ubidots ubique bien los datos reenviados tarde
        ts_ms = int(ahora * 1000)
        cuerpo = {k: {"value": v, "timestamp": ts_ms} for k, v in payload.items()}
        with db_lock:
            cur = db.execute("INSERT INTO agregados (ts,dispositivo,n,payload) VALUES (?,?,?,?)",
                             (ahora, disp, n, json.dumps(cuerpo)))
            fila_id = cur.lastrowid
            db.commit()
        if publicar_nube(cuerpo):
            with db_lock:
                db.execute("UPDATE agregados SET enviado_nube=1 WHERE id=?", (fila_id,))
                db.commit()
        else:
            log.warning("Sin nube: agregado %s queda pendiente", fila_id)


def hilo_reenvio():
    """Store-and-forward: si se cae internet, reenvia los agregados pendientes al volver."""
    while True:
        time.sleep(REENVIO_INTERVALO_S)
        if not nube_conectada.is_set():
            continue
        with db_lock:
            pend = db.execute(
                "SELECT id, payload FROM agregados WHERE enviado_nube=0 AND ts < ?"
                " ORDER BY id LIMIT ?", (time.time() - UBIDOTS_INTERVALO_S, REENVIO_LOTE)).fetchall()
        for k, (fila_id, payload) in enumerate(pend):
            if k:
                time.sleep(REENVIO_PAUSA_S)   # no saturar la cuota por minuto
            if not publicar_nube(json.loads(payload)):
                break
            with db_lock:
                db.execute("UPDATE agregados SET enviado_nube=1 WHERE id=?", (fila_id,))
                db.commit()
        if pend:
            log.info("Reenviados %d agregados pendientes", len(pend))


# ---------------- IA ----------------
def analizar(disp, desde_nuevas):
    """
    Capa numerica de la IA.

    * Anomalia: se ajusta una recta distancia(t) SOLO con las lecturas anteriores a
      ``desde_nuevas`` (historia). Cada lectura nueva (desde el ciclo anterior) se compara
      contra esa recta; su residuo se normaliza con la desviacion de los residuos de la
      historia, con piso IA_SD_MIN_CM. |z| > IA_UMBRAL_Z => anomalia. Asi se quita la
      tendencia, la lectura evaluada no esta en su propio ajuste y no se pierden picos
      entre ciclos.
    * Prediccion: regresion con todas las lecturas EXCEPTO las anomalas -> tasa (cm/h)
      y horas hasta DISTANCIA_VACIO_CM.
    """
    filas = [f for f in lecturas_validas(disp, time.time() - IA_VENTANA_S) if f[1] is not None]
    if len(filas) < IA_MIN_LECTURAS:
        return None
    t0 = filas[0][0]
    pts = [((f[0] - t0) / 3600, f[1], f) for f in filas]
    hist = [p for p in pts if p[2][0] < desde_nuevas]
    nuevas = [p for p in pts if p[2][0] >= desde_nuevas]
    if len(hist) < max(3, IA_MIN_LECTURAS // 2):     # primer ciclo: evalua la ultima
        hist, nuevas = pts[:-1], pts[-1:]
    m_h, b_h = regresion([p[0] for p in hist], [p[1] for p in hist])
    sd = max(statistics.pstdev([y - (m_h * x + b_h) for x, y, _ in hist]), IA_SD_MIN_CM)
    zs = [(y - (m_h * x + b_h)) / sd for x, y, _ in nuevas]
    z_max = max(zs, key=abs) if zs else 0.0
    anomalas = {p[2][11] for p, z in zip(nuevas, zs) if abs(z) > IA_UMBRAL_Z}
    limpias = [p for p in pts if p[2][11] not in anomalas]
    m, _ = regresion([p[0] for p in limpias], [p[1] for p in limpias])
    actual = limpias[-1][1]
    horas = (DISTANCIA_VACIO_CM - actual) / m if m > 0.01 else None
    u = limpias[-1][2]
    return {
        "tasa_cm_h": round(m, 3),
        "horas_a_vacio": round(horas, 1) if horas and horas > 0 else None,
        "residuo_z": round(z_max, 2),
        "anomalia": int(bool(anomalas)),
        "n_anomalas": len(anomalas),
        "ids_anomalas": sorted(anomalas),
        "n_lecturas": len(filas),
        "distancia": actual, "temperatura": u[2], "humedad": u[3],
        "presion": u[4], "lux": u[5], "evaporacion": u[6], "score": u[8],
    }


def preguntar_llama(a):
    """Capa generativa: el LLM (GPT-OSS en Groq, Gemini u Ollama) convierte los numeros en una recomendacion para la autoridad."""
    # La IA generativa no debe interpretar signos: se le da la tendencia ya resuelta.
    t = a.get("tasa_cm_h") or 0
    if t > 0.5:
        tendencia = f"el nivel del agua esta BAJANDO a {abs(t):.1f} cm/h"
    elif t < -0.5:
        tendencia = f"el nivel del agua esta SUBIENDO a {abs(t):.1f} cm/h (no hay perdida)"
    else:
        tendencia = "el nivel del agua esta estable"
    prompt = (
        f"Hecho principal: {tendencia}. "
        "Eres un asistente de gestion del agua para autoridades locales de Sabana Centro "
        "(Cundinamarca, Colombia) durante el Fenomeno de El Nino. Con estos datos de un tanque "
        "monitoreado, da UNA recomendacion operativa concreta en maximo 3 frases, en espanol, "
        "indicando el nivel de urgencia (bajo, medio o alto).\n"
        f"Datos: {json.dumps(a, ensure_ascii=False)}\n"
        "Usa el hecho principal tal como esta; no reinterpretes el signo de tasa_cm_h. horas_a_vacio es la "
        "prediccion de cuando se vacia el tanque. anomalia=1 indica una lectura fuera de la tendencia."
    )
    try:
        if IA_MODO == "ollama":
            r = requests.post(f"{OLLAMA_URL}/api/generate",
                              json={"model": OLLAMA_MODELO, "prompt": prompt, "stream": False},
                              timeout=180)
            r.raise_for_status()
            return r.json().get("response", "").strip()
        if IA_MODO == "groq" and GROQ_KEY:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                              headers={"Authorization": f"Bearer {GROQ_KEY}"},
                              json={"model": GROQ_MODELO,
                                    "messages": [{"role": "user", "content": prompt}]},
                              timeout=30)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        if IA_MODO == "gemini" and GEMINI_KEY:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODELO}:generateContent",
                headers={"x-goog-api-key": GEMINI_KEY},
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=30)
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (requests.RequestException, KeyError, ValueError) as e:
        log.warning("IA fallo: %s", e)
    return None


def ciclo_ia():
    """Un ciclo completo de IA: analiza, consulta el LLM, guarda y deja el resultado para subir."""
    with estado_lock:
        disp = estado["dispositivo"]
    if not disp:
        return
    with estado_lock:
        desde = estado["ia_ultimo_ts"] or (time.time() - IA_INTERVALO_S)
    a = analizar(disp, desde)
    with estado_lock:
        estado["ia_ultimo_ts"] = time.time()
    if not a:
        log.info("IA: aun no hay %d lecturas validas en la ventana", IA_MIN_LECTURAS)
        return
    ids = a.pop("ids_anomalas")
    texto = preguntar_llama(a) if IA_MODO != "ninguno" else None
    log.info("IA: %s | %s", a, texto)
    with db_lock:
        # Las anomalas se marcan: no se suben en el agregado ni sirven de historia despues.
        db.executemany("UPDATE lecturas SET anomala=1 WHERE id=?", [(i,) for i in ids])
        db.execute("INSERT INTO ia (ts,dispositivo,horas_a_vacio,tasa_cm_h,residuo_z,anomalia,"
                   "recomendacion) VALUES (?,?,?,?,?,?,?)",
                   (time.time(), disp, a["horas_a_vacio"], a["tasa_cm_h"], a["residuo_z"],
                    a["anomalia"], texto))
        db.commit()
    ia_nube = {"anomalia": a["anomalia"], "tasa_descenso_cm_h": a["tasa_cm_h"],
               "residuo_z": a["residuo_z"]}
    if a["horas_a_vacio"] is not None:
        ia_nube["horas_a_vacio"] = a["horas_a_vacio"]
    if texto:
        # Ubidots no guarda texto como valor: va en el "context" de la variable.
        ia_nube["recomendacion_ia"] = {"value": 1, "context": {"texto": texto[:400]}}
    if not publicar_nube(ia_nube, tema=TEMA_NUBE_IA):
        log.warning("IA: no se pudo subir a %s (queda en la tabla ia)", UBIDOTS_DEVICE_IA)
    critico = a["anomalia"] or (a["horas_a_vacio"] and a["horas_a_vacio"] < 24)
    if texto and (critico or IA_NOTIFICAR_SIEMPRE):
        notificar(f"AERIS IA ({datetime.now():%H:%M}): {texto}")


def hilo_ia():
    time.sleep(IA_PRIMER_CICLO_S)
    while True:
        try:
            ciclo_ia()
        except Exception:  # el hilo de IA nunca debe tumbar el gateway
            log.exception("Error en ciclo de IA")
        time.sleep(IA_INTERVALO_S)


def main():
    if not UBIDOTS_TOKEN:
        log.warning("Sin UBIDOTS_TOKEN: los agregados quedan en SQLite como pendientes")
    else:
        nube.connect_async(UBIDOTS_HOST, UBIDOTS_PORT, keepalive=60)
        nube.loop_start()
    local.connect_async(LOCAL_HOST, LOCAL_PORT, keepalive=60)
    local.loop_start()
    for nombre, fn in (("Procesamiento", hilo_procesamiento), ("Reenvio", hilo_reenvio),
                       ("IA", hilo_ia)):
        threading.Thread(target=fn, name=nombre, daemon=True).start()
    log.info("Gateway AERIS en marcha (variables nube: %s)", ", ".join(UBIDOTS_VARIABLES))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        local.loop_stop()
        nube.loop_stop()
        db.close()


if __name__ == "__main__":
    main()
