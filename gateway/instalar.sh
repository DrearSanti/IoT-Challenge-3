#!/bin/bash
# Ejecutar en la Raspberry Pi:  bash instalar.sh
set -e
sudo apt update
sudo apt install -y mosquitto mosquitto-clients python3-venv sqlite3

# Mosquitto con usuario y clave, escuchando en la red local
sudo tee /etc/mosquitto/conf.d/aeris.conf >/dev/null <<CONF
listener 1883 0.0.0.0
allow_anonymous false
password_file /etc/mosquitto/passwd
CONF
echo "Cree la clave del usuario MQTT 'aeris' (la misma de MQTT_CLAVE en el .ino):"
sudo mosquitto_passwd -c /etc/mosquitto/passwd aeris
sudo chown mosquitto:mosquitto /etc/mosquitto/passwd 2>/dev/null || true
sudo systemctl enable --now mosquitto
sudo systemctl restart mosquitto

# Python
python3 -m venv venv
./venv/bin/pip install paho-mqtt requests python-dotenv
[ -f .env ] || cp .env.ejemplo .env

echo
echo "Listo. Edite .env y pruebe con: ./venv/bin/python gateway.py"
echo "IP de esta Pi (pongala en el Arduino con /mqtt?h=IP):"
hostname -I
