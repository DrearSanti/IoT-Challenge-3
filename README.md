# IoT-Challenge-3 · AERIS

Sistema IoT de bajo costo para el monitoreo de disponibilidad de agua y alerta temprana por escasez hídrica en la Sabana Centro (Cundinamarca). Un nodo con Arduino UNO R4 WiFi mide nivel de agua y clima, fusiona las señales en un índice de riesgo y alerta en sitio; publica todo por **MQTT** a un **gateway en Raspberry Pi** que guarda en **SQLite**, procesa localmente, ejecuta un **módulo de IA** (predicción, anomalías y recomendación), notifica por **Telegram** y sube el resultado a **Ubidots** por MQTT con TLS.

**Documentación completa:** [Wiki del Challenge #3](https://github.com/DrearSanti/IoT-Challenge-3/wiki)

```
Arduino UNO R4 WiFi ──MQTT (usuario/clave)──▶ Mosquitto en Raspberry Pi ──▶ gateway.py
   sensores, ISR, LCD, LEDs,                                                 ├─ SQLite (aeris.db)
   tablero local con login                                                   ├─ procesamiento local
                                                                              ├─ IA: estadística + LLM
                                                                              ├─ Telegram (alertas)
                                                                              └─MQTT/TLS 8883─▶ Ubidots (nube)
Ubidots (switch "alarma") ──▶ gateway ──▶ aeris/<id>/cmd ──▶ Arduino (apaga/activa la alarma física)
```

## Contenido del repositorio

| Ruta | Contenido |
|---|---|
| `firmware/MonitorHidrico_LOCAL/MonitorHidrico_LOCAL.ino` | Firmware del Arduino UNO R4 WiFi: sensores, interrupciones, fusión, tablero local y cliente MQTT |
| `firmware/MonitorHidrico_LOCAL/dashboard.html` | Fuente del tablero de control local |
| `firmware/MonitorHidrico_LOCAL/dashboard_page.h` | Tablero embebido en la flash |
| `gateway/gateway.py` | Gateway IoT: MQTT local, SQLite, procesamiento, IA, Telegram y MQTT/TLS a Ubidots |
| `gateway/simular.py` | Simulador de telemetría para probar el gateway y la IA sin el nodo |
| `gateway/instalar.sh` | Instalación de Mosquitto, SQLite y el entorno de Python en la Raspberry Pi |
| `gateway/aeris-gateway.service` | Servicio `systemd` para el arranque automático del gateway |
| `gateway/.env.ejemplo` | Plantilla de configuración (sin claves) |
| `gateway/RASPBERRY_SETUP.md` | Guía paso a paso de la Raspberry Pi |
| `docs/` | Imágenes de la wiki, actas de reunión y material complementario |

Para compilar el firmware: abrir `firmware/MonitorHidrico_LOCAL/` en Arduino IDE 2.x con el core *Arduino UNO R4 Boards* y las librerías Adafruit BME280, Adafruit Unified Sensor y LiquidCrystal_I2C, más la librería MQTT indicada en el encabezado del `.ino`.

Para poner en marcha el gateway: seguir `gateway/RASPBERRY_SETUP.md`. Las claves y tokens van solo en `gateway/.env`, que no se versiona.

## Resumen de los Challenges #1 y #2 y su relación con este challenge

AERIS se construyó por capas durante el semestre. Cada reto conservó lo que ya funcionaba y agregó lo que el anterior dejó como trabajo futuro.

El [Challenge #1](https://github.com/DrearSanti/IoT-Challenge-1/wiki) construyó un nodo autónomo con Arduino UNO R3 que medía nivel de agua (AJ-SR04M), temperatura, humedad y presión (BME280) y luz (TCS230), y alertaba solo en sitio, sin ninguna red. Dejó la base que se mantiene hasta hoy: la **fusión de señales** en un índice de riesgo respecto a una línea base, el **Déficit de Presión de Vapor (VPD)** como fundamento físico de la evaporación y el **índice de sequía por periodo**.

El [Challenge #2](https://github.com/DrearSanti/IoT-Challenge-2/wiki) agregó la conectividad como capa: Arduino UNO R4 WiFi, **medición desde rutinas de interrupción**, **tablero web embebido** en la WLAN de la zona, histórico en EEPROM y apagado de la alarma desde el tablero.

El Challenge #3 convierte el nodo en un **ecosistema IoT completo**: el nodo ya no es el final del camino sino el primer eslabón.

| | Challenge #1 | Challenge #2 | Challenge #3 |
|---|---|---|---|
| Microcontrolador | Arduino UNO R3 | Arduino UNO R4 WiFi | Arduino UNO R4 WiFi |
| Medición | Temporizadores en el `loop()` | Interrupciones (ISR) | Interrupciones (ISR) en el nodo + hilos en el gateway |
| Notificación | Solo in situ | In situ + tablero en la WLAN | In situ + tablero local + nube + Telegram |
| Red | Ninguna | Wi-Fi local, HTTP | Wi-Fi, HTTP, MQTT local y MQTT/TLS a la nube |
| Almacenamiento | 4 periodos en EEPROM | 160 registros en EEPROM | SQLite en el gateway + histórico en la nube |
| Acceso al tablero | No aplica | Solo por estar en la WLAN | Usuario y clave (local) y cuenta (nube) |
| Análisis | Fusión por reglas | Fusión por reglas | Fusión + predicción, anomalías y recomendación con IA |

## Equipo

Esteban Sequeda · Santiago Pulido · Santiago Escobar

Universidad de La Sabana · Internet de las Cosas 2026-2
