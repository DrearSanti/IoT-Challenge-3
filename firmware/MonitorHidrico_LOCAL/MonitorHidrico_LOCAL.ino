/*
 * AERIS Simple - Nodo de monitoreo hidrico (Arduino UNO R4 WiFi)
 * Challenge #3 IoT - Universidad de La Sabana 2026-2
 *
 * Version simplificada con HILOS (FreeRTOS). Tres tareas independientes:
 *
 *   tareaSensores  (prioridad 2, cada 1 s)  -> mide ultrasonido, BME280 y panel solar,
 *                                              calcula VPD, evaporacion, riesgo y alarma.
 *   tareaRed       (prioridad 1)            -> WiFi, MQTT al gateway y tablero web local.
 *   tareaPantalla  (prioridad 1, cada 3 s)  -> LCD rotativa y LEDs.
 *
 * Por que hilos: cada tarea puede "bloquearse" (pulseIn, conectar WiFi, esperar MQTT)
 * sin detener a las demas. La medicion nunca se pierde aunque la red se demore.
 *
 * Datos compartidos: viven en la struct `d` y se protegen con un semaforo usado como candado (mtxDatos).
 * El bus I2C (BME280 y LCD) se protege con otro (mtxI2C).
 * La librería WiFiS3 solo la usa tareaRed (no es segura entre hilos).
 *
 * Sensor de radiacion: PANEL SOLAR pequeno en A0.
 *   + del panel -> A0 y a una resistencia de carga (47-100 ohm) -> GND
 *   - del panel -> GND
 *   La caida en la resistencia es proporcional a la corriente, que es lineal con la
 *   radiacion. Calibre PANEL_RAW_SOL_PLENO con la lectura a pleno sol.
 *
 * Librerias: ArduinoMqttClient, Adafruit BME280, LiquidCrystal I2C, Arduino_FreeRTOS
 * (esta ultima viene con el core Renesas del UNO R4).
 */
#include <Arduino_FreeRTOS.h>
#include <WiFiS3.h>
#include <ArduinoMqttClient.h>
#include <Wire.h>
#include <Adafruit_BME280.h>
#include <LiquidCrystal_I2C.h>

// ======================= CONFIGURACION (editar aqui) =======================
const char WIFI_SSID[]  = "NOMBRE_DE_LA_RED";
const char WIFI_CLAVE[] = "CLAVE_DE_LA_RED";

const char MQTT_BROKER[]  = "IP_DE_LA_PI";   // IP de la Raspberry Pi (hostname -I)
const int  MQTT_PUERTO    = 1883;
const char MQTT_USUARIO[] = "aeris";
const char MQTT_CLAVE[]   = "CLAVE_MQTT";

// Tablero local: "Basic " + base64("usuario:clave")
const char AUTH_BASIC[] = "Basic BASE64_DE_USUARIO_Y_CLAVE";

// Pines
const int TRIG = 2, ECHO = 3;              // AJ-SR04M
const int PANEL = A0;                      // panel solar
const int LED_VERDE = 7, LED_AMARILLO = 8, LED_ROJO = 9;

// Calibracion del panel: lectura analogRead a pleno sol (~1000 W/m2)
const int PANEL_RAW_SOL_PLENO = 800;

// Umbrales del riesgo (iguales a la version completa)
const float W_NIVEL = 0.4, W_TEMP = 0.2, W_HUM = 0.2, W_LUZ = 0.2;
const float U_NIVEL_CM = 1.0, U_TEMP_C = 6.0, U_HUM_PTS = 20.0, U_LUZ_WM2 = 300.0;
const float ALARMA_ON_CM = 1.0, ALARMA_OFF_CM = 0.6;

// ======================= DATOS COMPARTIDOS ================================
struct Datos {
  float temperatura = 0, humedad = 0, presion = 0;
  float distancia = -1;           // cm, -1 = sin lectura
  float irradiancia = 0;          // W/m2 (panel)
  float vpd = 0, evaporacion = 0;
  float score = 0;                // 0..1
  int   nivelRiesgo = -1;         // -1 sin base, 0 bajo, 1 medio, 2 alto
  bool  alarmaNivel = false;      // el nivel bajo >= 1 cm
  bool  alarmaRoja = true;        // permiso de la alarma (false = silenciada)
  bool  silenciadaPorUsuario = false;
  bool  baseOK = false;
  float baseDist = 0, baseTemp = 0, baseHum = 0, baseIrr = 0;
  bool  mqttOK = false;
  char  ip[16] = "sin red";
} d;

SemaphoreHandle_t mtxDatos, mtxI2C;
Adafruit_BME280 bme;
bool bmeOK = false;
LiquidCrystal_I2C lcd(0x27, 16, 2);

WiFiClient netMqtt;
MqttClient mqtt(netMqtt);
WiFiServer server(80);
char idNodo[16] = "AERIS-0000";
char tTelemetria[40], tAlerta[40], tEstado[40], tCmd[40];
volatile bool pedirBase = false;   // comandos que ejecuta tareaSensores

// ======================= UTILIDADES =======================================
const char *textoRiesgo(int n) {
  switch (n) { case 0: return "BAJO"; case 1: return "MEDIO"; case 2: return "ALTO"; default: return "SIN BASE"; }
}

float limitar01(float x) { return x < 0 ? 0 : (x > 1 ? 1 : x); }

// Distancia con el ultrasonido. pulseIn bloquea hasta 25 ms, pero solo bloquea
// ESTE hilo: la red y la pantalla siguen funcionando.
float medirDistanciaUnaVez() {
  digitalWrite(TRIG, LOW);  delayMicroseconds(5);
  digitalWrite(TRIG, HIGH); delayMicroseconds(20);
  digitalWrite(TRIG, LOW);
  unsigned long dur = pulseIn(ECHO, HIGH, 25000UL);   // 25 ms ~ 4 m
  if (dur == 0) return -1;
  float cm = dur * 0.0343f / 2.0f;
  return (cm >= 20 && cm <= 400) ? cm : -1;           // zona util del AJ-SR04M
}

// Mediana de 3 ecos: descarta un rebote aislado.
float medirDistancia() {
  float m[3];
  for (int i = 0; i < 3; i++) { m[i] = medirDistanciaUnaVez(); vTaskDelay(pdMS_TO_TICKS(60)); }
  for (int i = 0; i < 2; i++) for (int j = i + 1; j < 3; j++) if (m[j] < m[i]) { float t = m[i]; m[i] = m[j]; m[j] = t; }
  return m[1];
}

float medirIrradiancia() {
  long suma = 0;
  for (int i = 0; i < 8; i++) suma += analogRead(PANEL);
  float raw = suma / 8.0f;
  float w = raw * 1000.0f / PANEL_RAW_SOL_PLENO;      // aproximacion lineal
  return w < 0 ? 0 : w;
}

// VPD (Magnus): deficit de presion de vapor, en kPa.
float calcularVPD(float t, float hr) {
  float es = 0.6108f * expf(17.27f * t / (t + 237.3f));
  return es * (1.0f - hr / 100.0f);
}

// ======================= HILO 1: SENSORES =================================
void tareaSensores(void *) {
  for (;;) {
    float dist = medirDistancia();
    float irr = medirIrradiancia();
    float t = 0, h = 0, p = 0;
    if (bmeOK && xSemaphoreTake(mtxI2C, pdMS_TO_TICKS(200))) {
      t = bme.readTemperature(); h = bme.readHumidity(); p = bme.readPressure() / 100.0f;
      xSemaphoreGive(mtxI2C);
    }
    float vpd = calcularVPD(t, h);
    // Evaporacion potencial (indice 0-100): aire seco + radiacion.
    float evap = 100.0f * (0.6f * limitar01(vpd / 3.0f) + 0.4f * limitar01(irr / 1000.0f));

    xSemaphoreTake(mtxDatos, portMAX_DELAY);
    d.distancia = dist; d.irradiancia = irr; d.temperatura = t; d.humedad = h; d.presion = p;
    d.vpd = vpd; d.evaporacion = evap;

    // Linea base: la primera lectura valida, o cuando se pide "savebase".
    if ((!d.baseOK || pedirBase) && dist > 0) {
      d.baseDist = dist; d.baseTemp = t; d.baseHum = h; d.baseIrr = irr; d.baseOK = true;
      pedirBase = false;
    }

    // Fusion de alertas: suma ponderada de los cambios contra la base.
    if (d.baseOK && dist > 0) {
      float descenso = dist - d.baseDist;              // + = el agua bajo
      d.score = W_NIVEL * limitar01(descenso / U_NIVEL_CM)
              + W_TEMP  * limitar01((t - d.baseTemp) / U_TEMP_C)
              + W_HUM   * limitar01((d.baseHum - h) / U_HUM_PTS)
              + W_LUZ   * limitar01((irr - d.baseIrr) / U_LUZ_WM2);
      // Alarma de nivel con histeresis (enciende a 1 cm, se apaga bajo 0.6 cm)
      if (!d.alarmaNivel && descenso >= ALARMA_ON_CM) d.alarmaNivel = true;
      if (d.alarmaNivel && descenso < ALARMA_OFF_CM) d.alarmaNivel = false;
      d.nivelRiesgo = (d.alarmaNivel || d.score >= 0.6f) ? 2 : (d.score >= 0.3f ? 1 : 0);
    }
    // Si el usuario silencio un evento y ya termino, se rearma sola.
    if (d.silenciadaPorUsuario && !d.alarmaNivel && d.nivelRiesgo < 2) {
      d.silenciadaPorUsuario = false; d.alarmaRoja = true;
    }
    xSemaphoreGive(mtxDatos);

    vTaskDelay(pdMS_TO_TICKS(1000));
  }
}

// ======================= HILO 2: RED (WiFi, MQTT, web) ====================
// Silenciar solo si hay algo sonando (evita dejar muda la alarma por error).
bool silenciarAlarma() {
  xSemaphoreTake(mtxDatos, portMAX_DELAY);
  bool hayEvento = d.alarmaNivel || d.nivelRiesgo == 2;
  if (hayEvento) { d.alarmaRoja = false; d.silenciadaPorUsuario = true; }
  xSemaphoreGive(mtxDatos);
  return hayEvento;
}
void activarAlarma() {
  xSemaphoreTake(mtxDatos, portMAX_DELAY);
  d.alarmaRoja = true; d.silenciadaPorUsuario = false;
  xSemaphoreGive(mtxDatos);
}

// JSON de telemetria (mismos nombres que espera el gateway).
String jsonTelemetria() {
  xSemaphoreTake(mtxDatos, portMAX_DELAY);
  Datos c = d;
  xSemaphoreGive(mtxDatos);
  String s = "{";
  s += "\"id\":\"" + String(idNodo) + "\",";
  s += "\"ip\":\"" + String(c.ip) + "\",";
  s += "\"temperatura\":" + String(c.temperatura, 2) + ",";
  s += "\"humedad\":" + String(c.humedad, 2) + ",";
  s += "\"presion\":" + String(c.presion, 2) + ",";
  s += "\"distancia\":" + String(c.distancia, 2) + ",";
  s += "\"luz\":" + String(c.irradiancia, 1) + ",";
  s += "\"irradiancia\":" + String(c.irradiancia, 1) + ",";
  s += "\"lux\":" + String(c.irradiancia * 120.0f, 0) + ",";   // ~120 lx por W/m2 de sol
  s += "\"vpd\":" + String(c.vpd, 3) + ",";
  s += "\"evaporacion\":" + String(c.evaporacion, 1) + ",";
  s += "\"scoreRiesgo\":" + String(c.score * 100.0f, 1) + ",";
  s += "\"riesgo\":\"" + String(textoRiesgo(c.nivelRiesgo)) + "\",";
  s += "\"alarmaRoja\":" + String(c.alarmaRoja ? "true" : "false") + ",";
  s += "\"alarmaNivel\":" + String(c.alarmaNivel ? "true" : "false") + ",";
  s += "\"mqtt\":" + String(c.mqttOK ? "true" : "false") + "}";
  return s;
}

void publicarAlerta() {
  xSemaphoreTake(mtxDatos, portMAX_DELAY);
  Datos c = d;
  xSemaphoreGive(mtxDatos);
  String s = "{\"id\":\"" + String(idNodo) + "\",\"riesgo\":\"" + textoRiesgo(c.nivelRiesgo) +
             "\",\"nivelRiesgo\":" + String(c.nivelRiesgo) + ",\"score\":" + String(c.score * 100, 1) +
             ",\"alarmaNivel\":" + (c.alarmaNivel ? "true" : "false") +
             ",\"alarmaRoja\":" + (c.alarmaRoja ? "true" : "false") +
             ",\"distancia\":" + String(c.distancia, 2) + "}";
  mqtt.beginMessage(tAlerta, (unsigned long)s.length(), true, 1);
  mqtt.print(s);
  mqtt.endMessage();
}

void alRecibirComando(int) {
  String cmd;
  while (mqtt.available()) cmd += (char)mqtt.read();
  cmd.trim(); cmd.toLowerCase();
  Serial.println("MQTT cmd: " + cmd);
  if (cmd == "alarmoff") silenciarAlarma();
  else if (cmd == "alarmon") activarAlarma();
  else if (cmd == "savebase") pedirBase = true;
  // "medir": la medicion ya es continua; solo se responde con datos frescos.
}

void conectarWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.print("WiFi -> "); Serial.println(WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_CLAVE);
  for (int i = 0; i < 20 && WiFi.status() != WL_CONNECTED; i++) vTaskDelay(pdMS_TO_TICKS(500));
  if (WiFi.status() == WL_CONNECTED) {
    IPAddress ip = WiFi.localIP();
    xSemaphoreTake(mtxDatos, portMAX_DELAY);
    snprintf(d.ip, sizeof(d.ip), "%d.%d.%d.%d", ip[0], ip[1], ip[2], ip[3]);
    xSemaphoreGive(mtxDatos);
    server.begin();
    Serial.print("IP: "); Serial.println(ip);
  }
}

void conectarMqtt() {
  mqtt.setId(idNodo);
  mqtt.setUsernamePassword(MQTT_USUARIO, MQTT_CLAVE);
  mqtt.setKeepAliveInterval(30000);
  mqtt.beginWill(tEstado, true, 1); mqtt.print("offline"); mqtt.endWill();   // LWT
  if (!mqtt.connect(MQTT_BROKER, MQTT_PUERTO)) {
    Serial.print("MQTT error "); Serial.println(mqtt.connectError());
    return;
  }
  mqtt.onMessage(alRecibirComando);
  mqtt.subscribe(tCmd, 1);
  mqtt.beginMessage(tEstado, true, 1); mqtt.print("online"); mqtt.endMessage();
  Serial.println("MQTT conectado");
}

// Pagina minima del tablero local (refresca /data cada 3 s).
const char PAGINA[] = R"HTML(<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>AERIS</title>
<style>body{font-family:sans-serif;max-width:640px;margin:auto;padding:16px}
.g{display:grid;grid-template-columns:1fr 1fr;gap:8px}.c{background:#f2f2f2;border-radius:8px;padding:10px}
b{font-size:22px}button{padding:10px 14px;margin:6px 6px 0 0}</style></head><body>
<h2>AERIS - Monitor hidrico</h2><div class="g" id="g"></div>
<p><button onclick="f('/alarmoff')">Apagar alarma</button><button onclick="f('/alarmon')">Activar alarma</button>
<button onclick="f('/savebase')">Guardar linea base</button></p><p id="m"></p>
<script>
const n={temperatura:'Temperatura (C)',humedad:'Humedad (%)',presion:'Presion (hPa)',distancia:'Distancia (cm)',
irradiancia:'Radiacion (W/m2)',vpd:'VPD (kPa)',evaporacion:'Evaporacion',scoreRiesgo:'Riesgo (%)',riesgo:'Estado',mqtt:'MQTT'};
async function r(){const d=await (await fetch('/data')).json();
g.innerHTML=Object.keys(n).map(k=>`<div class=c>${n[k]}<br><b>${d[k]}</b></div>`).join('')}
async function f(u){m.textContent=await (await fetch(u)).text();r()}
r();setInterval(r,3000);</script></body></html>)HTML";

void responder(WiFiClient &c, const char *tipo, const String &cuerpo) {
  c.println("HTTP/1.1 200 OK");
  c.print("Content-Type: "); c.println(tipo);
  c.println("Connection: close"); c.println();
  c.print(cuerpo);
}

void atenderWeb() {
  WiFiClient c = server.available();
  if (!c) return;
  String req;
  unsigned long t0 = millis();
  while (c.connected() && millis() - t0 < 1500) {
    if (c.available()) {
      req += (char)c.read();
      if (req.endsWith("\r\n\r\n")) break;
    } else vTaskDelay(1);
  }
  if (req.indexOf(AUTH_BASIC) < 0) {                   // acceso restringido
    c.println("HTTP/1.1 401 Unauthorized");
    c.println("WWW-Authenticate: Basic realm=\"AERIS\"");
    c.println("Connection: close"); c.println();
  } else if (req.startsWith("GET /data")) {
    responder(c, "application/json", jsonTelemetria());
  } else if (req.startsWith("GET /alarmoff")) {
    responder(c, "text/plain", silenciarAlarma() ? "Alarma apagada" : "No hay alarma activa");
  } else if (req.startsWith("GET /alarmon")) {
    activarAlarma(); responder(c, "text/plain", "Alarma activada");
  } else if (req.startsWith("GET /savebase")) {
    pedirBase = true; responder(c, "text/plain", "Linea base guardada");
  } else {
    responder(c, "text/html; charset=utf-8", PAGINA);
  }
  vTaskDelay(pdMS_TO_TICKS(5));
  c.stop();
}

void tareaRed(void *) {
  uint8_t mac[6]; WiFi.macAddress(mac);
  snprintf(idNodo, sizeof(idNodo), "AERIS-%02X%02X", mac[4], mac[5]);
  snprintf(tTelemetria, sizeof(tTelemetria), "aeris/%s/telemetria", idNodo);
  snprintf(tAlerta, sizeof(tAlerta), "aeris/%s/alerta", idNodo);
  snprintf(tEstado, sizeof(tEstado), "aeris/%s/estado", idNodo);
  snprintf(tCmd, sizeof(tCmd), "aeris/%s/cmd", idNodo);

  unsigned long tTele = 0, tReintento = 0;
  int ultimoRiesgo = -99; bool ultimaAlarma = false, ultimoPermiso = true;

  for (;;) {
    conectarWiFi();
    if (WiFi.status() == WL_CONNECTED) {
      if (!mqtt.connected() && millis() - tReintento > 10000) { tReintento = millis(); conectarMqtt(); }
      bool ok = mqtt.connected();
      xSemaphoreTake(mtxDatos, portMAX_DELAY);
      d.mqttOK = ok;
      int riesgo = d.nivelRiesgo; bool alarma = d.alarmaNivel, permiso = d.alarmaRoja;
      xSemaphoreGive(mtxDatos);

      if (ok) {
        mqtt.poll();
        if (millis() - tTele >= 5000) {                 // telemetria cada 5 s
          tTele = millis();
          String s = jsonTelemetria();
          mqtt.beginMessage(tTelemetria, (unsigned long)s.length(), false, 0);
          mqtt.print(s);
          mqtt.endMessage();
        }
        if (riesgo != ultimoRiesgo || alarma != ultimaAlarma || permiso != ultimoPermiso) {
          ultimoRiesgo = riesgo; ultimaAlarma = alarma; ultimoPermiso = permiso;
          publicarAlerta();                              // alerta solo cuando cambia
        }
      }
      atenderWeb();
    }
    vTaskDelay(pdMS_TO_TICKS(20));
  }
}

// ======================= HILO 3: PANTALLA Y LEDS ==========================
void tareaPantalla(void *) {
  int pantalla = 0;
  unsigned long tCambio = 0;
  for (;;) {
    xSemaphoreTake(mtxDatos, portMAX_DELAY);
    Datos c = d;
    xSemaphoreGive(mtxDatos);

    // LEDs segun el riesgo; el rojo solo si la alarma no esta silenciada.
    digitalWrite(LED_VERDE, c.nivelRiesgo <= 0);
    digitalWrite(LED_AMARILLO, c.nivelRiesgo == 1);
    digitalWrite(LED_ROJO, c.nivelRiesgo == 2 && c.alarmaRoja);

    if (millis() - tCambio >= 3000) {
      tCambio = millis();
      if (xSemaphoreTake(mtxI2C, pdMS_TO_TICKS(200))) {
        lcd.clear(); lcd.setCursor(0, 0);
        switch (pantalla) {
          case 0: lcd.print("T:"); lcd.print(c.temperatura, 1); lcd.print("C H:"); lcd.print(c.humedad, 0); lcd.print("%");
                  lcd.setCursor(0, 1); lcd.print("P:"); lcd.print(c.presion, 1); lcd.print(" hPa"); break;
          case 1: lcd.print("Distancia:"); lcd.setCursor(0, 1);
                  if (c.distancia > 0) { lcd.print(c.distancia, 1); lcd.print(" cm"); } else lcd.print("SIN LECTURA"); break;
          case 2: lcd.print("Rad:"); lcd.print(c.irradiancia, 0); lcd.print(" W/m2");
                  lcd.setCursor(0, 1); lcd.print("Evap:"); lcd.print(c.evaporacion, 0); lcd.print("%"); break;
          case 3: lcd.print("Riesgo "); lcd.print(textoRiesgo(c.nivelRiesgo));
                  lcd.setCursor(0, 1); lcd.print("Score:"); lcd.print(c.score * 100, 0); lcd.print("%");
                  if (c.alarmaNivel && !c.alarmaRoja) lcd.print(" SIL"); break;
          case 4: lcd.print("IP "); lcd.print(c.mqttOK ? "MQTT OK" : "MQTT --");
                  lcd.setCursor(0, 1); lcd.print(c.ip); break;
        }
        xSemaphoreGive(mtxI2C);
      }
      pantalla = (pantalla + 1) % 5;
    }
    vTaskDelay(pdMS_TO_TICKS(200));
  }
}

// ======================= ARRANQUE =========================================
void setup() {
  Serial.begin(9600);
  pinMode(TRIG, OUTPUT); pinMode(ECHO, INPUT);
  pinMode(LED_VERDE, OUTPUT); pinMode(LED_AMARILLO, OUTPUT); pinMode(LED_ROJO, OUTPUT);
  Wire.begin();
  lcd.init(); lcd.backlight(); lcd.print("AERIS iniciando");
  bmeOK = bme.begin(0x76) || bme.begin(0x77);
  Serial.println(bmeOK ? "BME280 OK" : "BME280 no encontrado");

  // El FreeRTOS del UNO R4 trae los mutex desactivados (configUSE_MUTEXES = 0).
  // Un semaforo binario que arranca "libre" cumple la misma funcion de candado:
  // Take = entrar a la zona compartida, Give = salir.
  mtxDatos = xSemaphoreCreateBinary();
  mtxI2C = xSemaphoreCreateBinary();
  xSemaphoreGive(mtxDatos);
  xSemaphoreGive(mtxI2C);

  // Tamano de pila en palabras de 4 bytes. La red necesita mas (WiFi + MQTT + JSON).
  xTaskCreate(tareaSensores, "Sensores", 512,  NULL, 2, NULL);
  xTaskCreate(tareaRed,      "Red",      2048, NULL, 1, NULL);
  xTaskCreate(tareaPantalla, "Pantalla", 512,  NULL, 1, NULL);
  vTaskStartScheduler();   // desde aqui mandan los hilos; loop() no se usa
}

void loop() {}
