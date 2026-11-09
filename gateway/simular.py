#!/usr/bin/env python3
"""
Simulador de telemetria AERIS (para pruebas y para la demo de la IA).

Publica en Mosquitto mensajes con el mismo formato que el Arduino, con un nivel
que baja a ritmo constante + ruido, y opcionalmente un pico anomalo.

Uso:
    ./venv/bin/python simular.py --n 40 --cada 2 --anomalia
"""
import argparse
import json
import os
import random
import time

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

load_dotenv()

ap = argparse.ArgumentParser()
ap.add_argument("--id", default="AERIS-SIM0")
ap.add_argument("--n", type=int, default=40, help="numero de mensajes")
ap.add_argument("--cada", type=float, default=2.0, help="segundos entre mensajes")
ap.add_argument("--inicio", type=float, default=40.0, help="distancia inicial (cm)")
ap.add_argument("--baja", type=float, default=0.15, help="cm que baja el agua por mensaje")
ap.add_argument("--anomalia", action="store_true", help="el ultimo mensaje es un pico")
a = ap.parse_args()

c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="aeris-simulador")
if os.getenv("LOCAL_USER"):
    c.username_pw_set(os.getenv("LOCAL_USER"), os.getenv("LOCAL_PASS"))
c.connect(os.getenv("LOCAL_HOST", "localhost"), int(os.getenv("LOCAL_PORT", "1883")))
c.loop_start()
c.publish(f"aeris/{a.id}/estado", "online", qos=1, retain=True)
for i in range(a.n):
    dist = a.inicio + a.baja * i + random.gauss(0, 0.05)
    if a.anomalia and i == a.n - 1:
        dist += 8.0
    msg = {"temperatura": 27 + random.gauss(0, 0.3), "humedad": 35 + random.gauss(0, 1),
           "presion": 752 + random.gauss(0, 0.2), "distancia": round(dist, 2),
           "luz": 800, "lux": 1200, "vpd": 2.3, "evaporacion": 70, "scoreRiesgo": 45,
           "riesgo": "ALERTA", "alarmaRoja": True, "alarmaNivel": False}
    c.publish(f"aeris/{a.id}/telemetria", json.dumps(msg))
    print(i, msg["distancia"])
    time.sleep(a.cada)
c.loop_stop()
