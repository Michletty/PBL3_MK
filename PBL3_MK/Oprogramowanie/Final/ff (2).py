from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit
import threading
import time
import random
import json
from collections import deque

# Import klienta InfluxDB
from influxdb_client import InfluxDBClient

# Import MQTT client do real-time danych
import paho.mqtt.client as mqtt

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app)

# ================= KONFIGURACJA INFLUXDB (Dla Pi Zero - BACKUP) =================
INFLUX_URL = "http://localhost:8086"
# Token z Twojego sprawozdania [cite: 67]
INFLUX_TOKEN = "fB3HQxYQBvBfTt7U0cSz94mb1Q4AUQU7n2ycuDqEV8IFue-C9Z2uUGr7QBJ1oXnzh19Cw68G_3YweXrsDfogrA=="
INFLUX_ORG = "PBL3_Z8"       # [cite: 68]
INFLUX_BUCKET = "lora_dane"  # [cite: 70]
INFLUX_MEASUREMENT = "mqtt_consumer"

# ================= KONFIGURACJA MQTT (Real-time dla Pi Zero) =================
MQTT_BROKER = "127.0.0.1"    # localhost
MQTT_PORT = 1883
MQTT_TOPIC = "lora/pogoda"    # topic z odbiornik.py

# Mapowanie station_id (LoRa) → indeksy stron (0-7)
STATION_ID_TO_INDEX = {
    "01": 5,  # Pi Zero
    "02": 0,  # Sym1
    "03": 1,  # Sym4
    "04": 2,  # Sym2
    "05": 3,  # Sym3
    "06": 6,  # Sym5
    "07": 7   # Sym6
}

# Zbiór stacji, które wysyłają prawdziwe dane (dynamiczny)
active_real_stations = set()

# Konfiguracja stacji (legacy - na potrzeby kompatybilności)
REAL_STATION_INDEX = 5  # Pi Zero ma index 5 na stronie
REAL_STATION_ID_TAG = "01" # ID wysyłane przez LoRa

# Lista indeksów, które mają być symulowane
# Stacje 2-7 (indeksy 0,1,2,3,6,7) używają WYŁĄCZNIE danych z MQTT - brak symulacji
SIMULATION_INDICES = []  # Pusta lista - wszystkie dane z prawdziwych pomiarów
# =======================================================================

# Przechowywanie danych
latest_values = {}
historical_values = {}

# Inicjalizacja struktur danych
for i in range(8):
    latest_values[str(i)] = []
    historical_values[str(i)] = {
        'T1': deque(maxlen=72), 
        'T2': deque(maxlen=72), 
        'Hu': deque(maxlen=72),
        'Wi': deque(maxlen=72),  # Wiatr
        'Fa': deque(maxlen=72)   # Frost Alert
    }

# Funkcja pomocnicza do aktualizacji danych
def update_data(index, t1, t2, hu, wi=0.0, fa=0):
    """Aktualizuje pamięć i wysyła dane do przeglądarek"""
    key = str(index)
    
    # Aktualizacja wartości
    latest_values[key] = [t1, t2, hu, wi, fa]
    historical_values[key]['T1'].append(t1)
    historical_values[key]['T2'].append(t2)
    historical_values[key]['Hu'].append(hu)
    historical_values[key]['Wi'].append(wi)
    historical_values[key]['Fa'].append(fa)
    
    # Wysłanie tylko zmienionych danych (optymalizacja) lub całości
    # Tutaj wysyłamy całość 'latest_values' tak jak w oryginale
    socketio.emit('values_update', latest_values)

# === MQTT CALLBACK (Real-time data) ===
def on_mqtt_connect(client, userdata, flags, rc):
    """Callback wywoływany po połączeniu z MQTT broker"""
    if rc == 0:
        print(f"MQTT polaczono: subskrybuje {MQTT_TOPIC}")
        client.subscribe(MQTT_TOPIC)
    else:
        print(f"MQTT blad polaczenia: kod {rc}")

def on_mqtt_message(client, userdata, msg):
    """Callback wywoływany przy nowej wiadomości MQTT (REAL-TIME!)"""
    try:
        payload = json.loads(msg.payload.decode('utf-8'))
        station_id = payload.get('station_id')
        
        # Sprawdź czy stacja jest w mapowaniu
        if station_id in STATION_ID_TO_INDEX:
            station_index = STATION_ID_TO_INDEX[station_id]
            
            # Oznacz stację jako aktywną (wyłączy symulację)
            active_real_stations.add(station_index)
            
            # Pobierz dane
            t1 = payload.get('temp_ds18b20')
            t2 = payload.get('temp_bme280')
            hu = payload.get('humidity')
            wi = payload.get('wiatr')
            fa = payload.get('frost_alert', 0)  # Domyślnie 0 (brak alarmu)
            
            # Konwersja None -> 0.0
            if t1 is None: t1 = 0.0
            if t2 is None: t2 = 0.0
            if hu is None: hu = 0.0
            if wi is None: wi = 0.0
            if fa is None: fa = 0
            
            # Konwersja wiatru z m/s na km/h
            wi_kmh = float(wi) * 3.6
            
            # Aktualizacja danych
            station_names = {0:"Stacja 2 (S)", 1:"Stacja 3 (S)", 2:"Stacja 4 (S)", 3:"Stacja 5 (S)", 4:"Pi 4", 5:"Pi Zero", 6:"Stacja 6 (S)", 7:"Stacja 7 (S)"}
            station_name = station_names.get(station_index, f"Stacja {station_index}")
            print(f"MQTT -> {station_name} (ID={station_id}): T1={t1}, T2={t2}, Hu={hu}, Wi={wi_kmh:.1f}km/h, FA={fa}")
            update_data(station_index, round(float(t1), 2), round(float(t2), 2), round(float(hu), 2), round(wi_kmh, 1), int(fa))
        else:
            print(f"Nieznane station_id: {station_id}")
            
    except Exception as e:
        print(f"Blad MQTT message: {e}")

# === WĄTEK 1: SYMULACJA (Dla stacji wirtualnych) ===
def simulation_thread():
    """Generuje losowe dane dla stacji SymX (przywrócona funkcjonalność)"""
    print("Start symulacji dla stacji wirtualnych...")
    while True:
        for idx in SIMULATION_INDICES:
            # Pomiń stacje, które wysyłają prawdziwe dane przez MQTT
            if idx in active_real_stations:
                continue
                
            # Generowanie losowych wartości
            t1 = round(random.uniform(5, 25), 2)
            t2 = round(random.uniform(5, 25), 2)
            hu = round(random.uniform(40, 95), 2)
            wi_ms = random.uniform(0, 15)  # Wiatr 0-15 m/s
            wi_kmh = round(wi_ms * 3.6, 1)  # Konwersja na km/h
            fa = random.choice([0, 0, 0, 1])  # 25% szans na alarm
            
            update_data(idx, t1, t2, hu, wi_kmh, fa)
        
        # Symulacja aktualizuje się co 10 sekund
        time.sleep(10)

# === WĄTEK 2: MQTT SUBSCRIBER (Real-time dla Pi Zero) ===
def mqtt_subscriber_thread():
    """Subskrybuje MQTT i odbiera dane w czasie rzeczywistym"""
    mqtt_client = mqtt.Client()
    mqtt_client.on_connect = on_mqtt_connect
    mqtt_client.on_message = on_mqtt_message
    
    try:
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        print(f"Laczenie z MQTT broker: {MQTT_BROKER}:{MQTT_PORT}")
        mqtt_client.loop_forever()  # Blokujący loop - nasłuchuje wiadomości
    except Exception as e:
        print(f"Blad polaczenia MQTT: {e}")

# === TRASY FLASK (Bez zmian) ===
POINT_MAPPING = {
    "Stacja_2_(S)": 0, "Stacja_3_(S)": 1, "Stacja_4_(S)": 2, "Stacja_5_(S)": 3,
    "Pi_4": 4, "Pi_Zero": 5, "Stacja_6_(S)": 6, "Stacja_7_(S)": 7
}

@app.route("/")
def index():
    return render_template('index.html')

@app.route("/<point_name>", methods=['GET'])
def point_details(point_name):
    if point_name not in POINT_MAPPING: return "Not found", 404
    idx = POINT_MAPPING[point_name]
    return render_template('point.html', point_name=point_name.replace("_", " "), point_index=idx)

@app.route("/api/history/<int:point_index>")
def get_history(point_index):
    key = str(point_index)
    if key not in historical_values: return jsonify({'T1':[], 'T2':[], 'Hu':[], 'Wi':[], 'Fa':[]})
    return jsonify({
        'T1': list(historical_values[key]['T1']),
        'T2': list(historical_values[key]['T2']),
        'Hu': list(historical_values[key]['Hu']),
        'Wi': list(historical_values[key]['Wi']),
        'Fa': list(historical_values[key]['Fa'])
    })

@app.route("/api/values")
def get_values():
    return jsonify(latest_values)

@socketio.on('connect')
def handle_connect():
    emit('values_update', latest_values)

if __name__ == "__main__":
    # Uruchamiamy WĄTEK SYMULACJI (dla Sym1, Sym2...)
    t_sim = threading.Thread(target=simulation_thread, daemon=True)
    t_sim.start()

    # Uruchamiamy WĄTEK MQTT SUBSCRIBER (dla Pi Zero - REAL-TIME)
    t_mqtt = threading.Thread(target=mqtt_subscriber_thread, daemon=True)
    t_mqtt.start()
    
    print("Serwer WWW startuje na porcie 5000...")
    print("Real-time MQTT: Wszystkie stacje ID 01-07 (lora/pogoda)")
    print("Stacje bez danych MQTT: czekaja na pomiary...")
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, allow_unsafe_werkzeug=True)
