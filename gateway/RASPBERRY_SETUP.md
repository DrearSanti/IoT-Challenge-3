# AERIS — Configuración de la Raspberry Pi (Gateway IoT)

La Raspberry Pi es el **Gateway IoT** del sistema:

```
Arduino UNO R4 WiFi --MQTT--> Mosquitto (Pi) --> gateway.py --> SQLite (aeris.db)
                                                     |--> IA: predicción + LLM
                                                     |--> Telegram (notificaciones)
                                                     +--MQTT--> Ubidots (nube)
Ubidots (switch "alarma") --MQTT--> gateway.py --MQTT--> Arduino (aeris/<id>/cmd)
```

---

## 0. Requisitos

- Raspberry Pi 4 o 5 (recomendado 4 GB de RAM o más para correr un modelo local con Ollama).
- Raspberry Pi OS Bookworm (64 bits) con acceso a internet.
- La carpeta `aeris-gateway/` (contiene `gateway.py`, `instalar.sh`, `.env.ejemplo`, `aeris-gateway.service`).
- Cuenta en [Ubidots STEM](https://ubidots.com/stem) (gratis).
- (Opcional) Bot de Telegram para notificaciones.

---

## 1. Preparar la Pi

Si la Pi aún no tiene sistema:

1. En su PC instale **Raspberry Pi Imager** (https://www.raspberrypi.com/software/).
2. Elija el modelo de su Pi, **Raspberry Pi OS (64-bit)** y la microSD.
3. En *Editar ajustes*: usuario `pi` y una clave, su red WiFi, y en *Servicios* active **SSH**.
4. Grabe, ponga la microSD en la Pi y enciéndala. Desde el PC (misma red): `ssh pi@raspberrypi.local`.

Con la Pi encendida:

```bash
sudo apt update && sudo apt full-upgrade -y
hostname -I        # anote la IP de la Pi
```

Copie la carpeta del gateway a la Pi (desde su PC):

```bash
scp -r aeris-gateway pi@IP-DE-LA-PI:/home/pi/
```

---

## 2. Instalar Mosquitto, SQLite y Python

```bash
cd ~/aeris-gateway
bash instalar.sh
```

El script:

1. Instala `mosquitto`, `mosquitto-clients`, `sqlite3` y `python3-venv`.
2. Configura Mosquitto en `/etc/mosquitto/conf.d/aeris.conf`:
   ```
   listener 1883 0.0.0.0
   allow_anonymous false
   password_file /etc/mosquitto/passwd
   ```
3. Crea el usuario MQTT `aeris`. **Use la misma clave que `MQTT_CLAVE` en el `.ino`** (por defecto `<CLAVE_MQTT>`).
4. Crea el entorno virtual `venv/` con `paho-mqtt`, `requests` y `python-dotenv`.
5. Copia `.env.ejemplo` a `.env`.

Verifique que Mosquitto corre:

```bash
sudo systemctl status mosquitto
```

---

## 3. Red: conectar el Arduino con la Pi

Ambos deben estar en la **misma red WiFi**. Elija una opción.

### Opción A — Red existente (casa / celular)

1. Conecte la Pi a la red y anote su IP (`hostname -I`).
2. Conecte el Arduino a la misma red: únase a `AERIS-XXXX`, abra `http://192.168.4.1/wifi`, elija la red y guarde. La LCD muestra la IP del Arduino.

### Opción B — La Pi crea la red (recomendado para la sustentación)

Simula la "WLAN de las autoridades" y no depende del WiFi de la universidad:

```bash
sudo nmcli device wifi hotspot ifname wlan0 ssid AERIS-RED password <CLAVE_MQTT>
```

- IP de la Pi en esa red: `10.42.0.1`.
- La Pi necesita internet por **cable Ethernet** (o USB del celular) para llegar a Ubidots.
- Conecte el Arduino a `AERIS-RED` con el portal `AERIS-XXXX`.

### Decirle al Arduino dónde está el broker

Desde un navegador en la misma red (usuario `admin`, clave `<CLAVE_MQTT>`):

```
http://IP-DEL-ARDUINO/mqtt?h=IP-DE-LA-PI&p=1883
```

### Comprobar que llegan los datos

```bash
mosquitto_sub -h localhost -u aeris -P <CLAVE_MQTT> -t 'aeris/#' -v
```

Cada 5 s debe aparecer `aeris/AERIS-XXXX/telemetria {...}`. En `http://IP-DEL-ARDUINO/data` debe verse `"mqtt":true`.

---

## 4. Módulo de IA (estadística + LLM)

### Opción 2 — Local en la Pi con Ollama (edge AI, sin internet)

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.2:1b        # Pi 5 / 8 GB puede usar llama3.2:3b
ollama run llama3.2:1b "hola"  # prueba
```

En `.env`: `IA_MODO=ollama`.

### Opción 1 (en uso) — GPT-OSS 20B por API con Groq (rápido, plan gratuito)

1. Cree una API key en https://console.groq.com.
2. En `.env`: `IA_MODO=groq`, `GROQ_KEY=gsk_...` y `GROQ_MODELO=openai/gpt-oss-20b`.

### Qué hace la IA

Cada `IA_INTERVALO_S` (300 s por defecto), en un hilo independiente:

Sobre la ventana `IA_VENTANA_S` (3 h por defecto, configurable):

1. **Detección de anomalías por residuos**: se ajusta una recta distancia(t) solo con las lecturas *anteriores* al ciclo. Cada lectura nueva desde el ciclo anterior (no solo la última) se compara con esa recta. El residuo se divide por la desviación de los residuos, con un piso de `IA_SD_MIN_CM` (0.5 cm, cerca de la resolución del sensor) para que un nivel quieto no dé z enormes. Si |z| > `IA_UMBRAL_Z` (3), la lectura es anómala.
2. **Predicción**: regresión con todas las lecturas **menos las anómalas** → tasa de descenso (cm/h) y **horas hasta tanque vacío** (`DISTANCIA_VACIO_CM`).
3. **Recomendación**: el LLM recibe la tendencia ya interpretada y esos números y redacta una recomendación operativa (urgencia baja/media/alta).
4. Guarda todo en la tabla `ia`, lo publica en el dispositivo `aeris-ia` de Ubidots y avisa por Telegram si hay anomalía o vaciado en < 24 h.

---

## 5. Ubidots (plataforma IoT en la nube)

1. Cree la cuenta STEM y copie el **Default Token** (*API Credentials*).
2. En `.env`:
   ```
   UBIDOTS_TOKEN=BBUS-xxxxxxxx
   UBIDOTS_DEVICE=aeris
   ```
3. Corra el gateway una vez (paso 7). Se crean **dos dispositivos** (STEM permite 3, con 10 variables cada uno):
   - `aeris` (sensores, cada `UBIDOTS_INTERVALO_S` = 300 s): `distancia`, `temperatura`, `humedad`, `presion`, `lux`, `evaporacion`, `score_riesgo`, `alarma_nivel` + el switch `alarma` = 9 variables.
   - `aeris-ia` (cada `IA_INTERVALO_S` = 300 s): `tasa_descenso_cm_h`, `horas_a_vacio`, `anomalia`, `residuo_z`, `recomendacion_ia` = 5 variables.

   **Cuota STEM** (verifíquela en su cuenta): ~4000 dots/día y ~30 dots/min. Con estos valores: 8 × 288 + 5 × 288 ≈ 3750 dots/día. Si baja los intervalos para la demo, súbalos otra vez después: cada 30 s se gastaría la cuota diaria en pocas horas.
4. En el dispositivo `aeris`, cree una variable **raw** llamada `alarma`.
5. Arme el dashboard:
   - Indicadores / gráficas para cada variable.
   - Widget **Switch** ligado a `alarma` → 0 apaga la alarma física, 1 la activa.
   - Widget **HTML/Text** con el *context* de `recomendacion_ia` para ver el texto de la IA.
   - **Events** de Ubidots (correo/SMS) cuando `score_riesgo` > 60 o `alarma_nivel` = 1.
6. Acceso restringido: el dashboard solo lo ven usuarios con cuenta. Para compartirlo use *Share → link con clave* o cree usuarios de solo lectura.

---

## 6. Notificaciones por Telegram (opcional, recomendado)

1. En Telegram, hable con **@BotFather** → `/newbot` → copie el token.
2. Escriba cualquier mensaje a su bot y abra:
   `https://api.telegram.org/bot<TOKEN>/getUpdates` → copie `chat.id`.
3. En `.env`:
   ```
   TELEGRAM_TOKEN=123456:ABC...
   TELEGRAM_CHAT=123456789
   ```

Se notifica: alerta de riesgo, alarma de nivel, Arduino desconectado (LWT `offline`) y predicciones críticas de la IA.

---

## 7. Configurar y probar el gateway

Edite `.env` (ver `.env.ejemplo`) y ajuste:

| Variable | Qué es |
| --- | --- |
| `LOCAL_USER` / `LOCAL_PASS` | Usuario MQTT de Mosquitto |
| `UBIDOTS_TOKEN` | Token de Ubidots |
| `IA_MODO` | `ollama`, `groq` o `ninguno` |
| `DISTANCIA_VACIO_CM` | Distancia sensor-agua (cm) que significa tanque vacío |
| `TELEGRAM_*` | Bot de notificaciones |

Pruebe en primer plano:

```bash
cd ~/aeris-gateway
./venv/bin/python gateway.py
```

Debe ver: `Mosquitto conectado`, `Ubidots conectado` y, cada 5 min, una línea `IA: ...`.

Consultar la base de datos:

```bash
sqlite3 aeris.db "SELECT datetime(ts,'unixepoch','localtime'), distancia, temperatura, score, riesgo FROM lecturas ORDER BY id DESC LIMIT 10;"
sqlite3 aeris.db "SELECT datetime(ts,'unixepoch','localtime'), horas_a_vacio, anomalia, recomendacion FROM ia ORDER BY id DESC LIMIT 3;"
```

---

## 8. Arranque automático (servicio)

```bash
sudo cp aeris-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aeris-gateway
journalctl -u aeris-gateway -f      # ver logs
```

> El servicio asume usuario `pi` y ruta `/home/pi/aeris-gateway`. Si su usuario es otro, edite `User=` y las rutas en el `.service`.

---

## 9. Prueba de integración (checklist para la sustentación)

| # | Prueba | Cómo se verifica | OK |
| --- | --- | --- | --- |
| 1 | Arduino publica por MQTT | `mosquitto_sub -t 'aeris/#'` muestra telemetría | ☐ |
| 2 | Gateway guarda en SQLite | `SELECT COUNT(*) FROM lecturas;` aumenta | ☐ |
| 3 | Datos llegan a Ubidots | Gráficas se actualizan cada `UBIDOTS_INTERVALO_S` | ☐ |
| 4 | Alarma física se apaga desde Ubidots | Switch `alarma` = 0 → LED rojo se apaga | ☐ |
| 5 | Alarma se apaga desde el tablero local | Botón en `http://IP-DEL-ARDUINO/` | ☐ |
| 6 | Acceso restringido local | Sin clave → 401 | ☐ |
| 7 | Notificación | Bajar el nivel 1 cm → mensaje en Telegram | ☐ |
| 8 | IA | Tabla `ia` con predicción y recomendación | ☐ |
| 9 | Desconexión | Apagar Arduino → `estado = offline` y aviso | ☐ |

Comando útil para probar comandos sin Ubidots:

```bash
mosquitto_pub -h localhost -u aeris -P <CLAVE_MQTT> -t 'aeris/AERIS-XXXX/cmd' -m alarmoff
```

---

## 10. Problemas comunes

| Síntoma | Causa probable |
| --- | --- |
| `MQTT error 5` en el Serial del Arduino | Usuario/clave MQTT no coinciden con Mosquitto |
| `MQTT error -2` | IP del broker mal o Pi/Arduino en redes distintas |
| Nada llega con red de la universidad | La red aísla clientes → use la Opción B (hotspot) |
| Ubidots no conecta | Token mal o la Pi no tiene internet |
| IA muy lenta / falla | Poca RAM → use `llama3.2:1b` o `IA_MODO=groq` |
| `IA: aun no hay ... lecturas validas` | Normal: necesita `IA_MIN_LECTURAS` lecturas válidas en `IA_VENTANA_S` |

---

## 11. Demo de la IA sin esperar horas

En `.env` **solo durante la demo**: `IA_VENTANA_S=900`, `IA_INTERVALO_S=60`, `IA_PRIMER_CICLO_S=20`, `UBIDOTS_INTERVALO_S=60`. Con eso se gastan ~260 dots por cada 10 minutos de demo: vuelva a 300 al terminar.
Precargue datos con el simulador (nivel bajando + un pico anómalo al final):

```bash
./venv/bin/python simular.py --n 40 --cada 2 --anomalia
```

En el log del gateway debe salir `IA: {... "anomalia": 1 ...}` y la recomendación del LLM.

## 12. Seguridad (para la wiki)

- Ninguna clave va en el repo: `.env` está en `.gitignore` y `.env.ejemplo` solo trae `<PLACEHOLDERS>`.
- En el prototipo se usa una sola clave para el tablero, MQTT y el hotspot. En producción se usaría una por servicio (trabajo futuro).
- **Gateway → Ubidots: MQTT sobre TLS (puerto 8883)**, `UBIDOTS_TLS=1`.
- **Arduino → Mosquitto: MQTT con usuario/clave dentro de la WLAN cerrada (WPA2)**, sin anónimos. TLS local queda como trabajo futuro (el UNO R4 tiene `WiFiSSLClient`, pero exige gestionar certificados propios en Mosquitto).

## 13. Procesamiento local en el gateway

Antes de subir a la nube, cada `UBIDOTS_INTERVALO_S`:

1. **Validación** por rango físico (las inválidas se guardan con `valida=0`, no se suben).
2. **Agregación** por mediana de la ventana (robusta a picos del ultrasonido).
3. Solo el agregado sube al dispositivo `aeris` (8 variables cada 300 s, dentro de la cuota STEM).
4. Se guarda en la tabla `agregados` con `enviado_nube`. Si no hay internet, el hilo `Reenvio` sube los pendientes al volver (*store-and-forward*), con su timestamp original.

## 14. Rango de distancia (20 cm)

El AJ-SR04M tiene una **zona ciega de ~20 cm**: por debajo no mide bien. El firmware ya descarta esas lecturas y el gateway hace lo mismo (`RANGOS`). Por eso **el sensor se instala al menos 20 cm por encima del nivel máximo del tanque** (tanque lleno = ~20–25 cm de distancia). Así las lecturas con el tanque lleno siguen siendo válidas.

## 15. Significado del switch `alarma`

En el firmware, `alarmaRoja` es el **permiso** de la alarma física (1 = armada, 0 = silenciada). No es "alarma sonando": eso es `alarma_nivel` / `score_riesgo`. En reposo el switch está en **1 (armada)**.

- **En el dashboard de Ubidots, etiquete el switch como "Alarma armada / silenciada"**, no como "Alarma", y ponga al lado un indicador de `alarma_nivel` ("Descenso detectado"). Así un operador no confunde "armada" con "sonando".
- Silenciar (`alarmoff`) **solo funciona si hay un evento en curso** (descenso de nivel o riesgo alto). Sin evento, el Arduino responde "NO HAY ALARMA ACTIVA" y no se desarma: un toque accidental al switch no deja muda la alarma.
- Cuando el evento silenciado termina, el firmware **se rearma solo** y el switch vuelve a 1.

## 16. Cuidar la cuota de Ubidots en los ensayos

Con los valores por defecto se usa ~94 % de los 4000 dots/día. Para no llegar sin cuota a la sustentación:

- **Ensayos:** deje `UBIDOTS_TOKEN=` vacío. Todo lo demás funciona (SQLite, IA, Telegram) y los agregados quedan pendientes en `agregados`.
- **Prueba 3 y demo:** ponga el token. Revise el consumo en la pestaña **Usage** de Ubidots.
- Si al reactivar el token hay muchos pendientes, el hilo `Reenvio` los sube de a uno cada `REENVIO_PAUSA_S` (20 s ≈ 24 dots/min, debajo del límite de ~30/min). Si no quiere gastar cuota en datos viejos, bórrelos antes: `sqlite3 aeris.db "UPDATE agregados SET enviado_nube=1 WHERE enviado_nube=0;"`.

## 17. Medir el bloqueo con la Pi apagada (para la sustentación)

Con el Arduino conectado y la Pi apagada, abra el Monitor Serie: cada intento imprime `MQTT intento bloqueo ms=...`. Anote ese valor: es la respuesta concreta a "¿qué pasa si la red bloquea el loop?". Los reintentos se espacian 15 s → 30 → 60 → 120 s. Durante el bloqueo, las ISR siguen capturando el eco y el reloj; lo que se puede perder es una pulsación de botón (van por sondeo).
