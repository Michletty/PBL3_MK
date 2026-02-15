# -*- coding: utf-8 -*-

# Odbiornik LoRa Pi4B - LOGIKA PRZYMROZKOWA (Radiacyjna vs Adwekcyjna)
# Format: ID(2) + TDS(6) + TBME(6) + HUM(5) + N(2) + CZAS(6) + WIATR(5) = 32B

import sys
import time
import json
import math
import RPi.GPIO as GPIO
from LoRaRF import SX126x, LoRaSpi, LoRaGpio
import paho.mqtt.client as mqtt

# === KONFIGURACJA LOGIKI ===
# Poniżej 2.0 m/s uznajemy przymrozek za radiacyjny (DS18B20), powyżej za adwekcyjny (BME280).
PROG_WIATRU = 2.0 

# Minimum sekund między pomiarami do liczenia trendu (600s = 10 min).
# Eliminuje to błędy typu "-50.0 st/h" przy częstych ramkach.
MIN_CZAS_DO_TRENDU = 600 

# Słownik do przechowywania poprzednich pomiarów dla każdej stacji
historia_pomiarow = {}

# ustawienie MQTT
BROKER = "127.0.0.1"
klient = mqtt.Client()
try:
    klient.connect(BROKER, 1883, 60)
    klient.loop_start()
    print("MQTT polaczono z brokerem")
except Exception as e:
    print(f"Blad polaczenia MQTT: {e}")

# setup pinow do modułu sx1262
PIN_RESET = 22
PIN_BUSY  = 17
PIN_DIO1  = 25
PIN_RXEN  = 5
PIN_TXEN  = 6
PIN_CS    = 8

# parametry lory
CZESTOTLIWOSC = 868000000
SF = 7
BW = 500000
CR = 5

def obliczanie_punktu_rosy(temperatura, wilgotnosc):
    """
    Oblicza punkt rosy (Dew Point) wg wzoru Magnusa.
    """
    if temperatura is None or wilgotnosc is None or wilgotnosc <= 0:
        return None
    
    a = 17.27
    b = 237.7
    
    gamma = ((a * temperatura) / (b + temperatura)) + math.log(wilgotnosc / 100.0)
    punkt_rosy = (b * gamma) / (a - gamma)
    
    return round(punkt_rosy, 2)

def obliczanie_szybkosci_chlodzenia(station_id, current_temp, current_time):
    """
    Oblicza trend (pochodną temperatury po czasie) w [°C/h].
    ZAWIERA FILTR CZASOWY (naprawia skoki wartości).
    """
    if station_id not in historia_pomiarow:
        # Inicjalizacja: zapisujemy temp, czas i trend=0
        historia_pomiarow[station_id] = {
            'temp': current_temp, 
            'time': current_time,
            'last_trend': 0.0
        }
        return 0.0
    
    prev_data = historia_pomiarow[station_id]
    time_prev = prev_data['time']
    dt_seconds = current_time - time_prev
    
    # === FILTR SZUMU ===
    # Jeśli minęło mniej niż 10 minut, nie liczymy nowego trendu, bo wyjdą bzdury.
    # Zwracamy ostatni zapamiętany trend.
    if dt_seconds < MIN_CZAS_DO_TRENDU:
        return prev_data.get('last_trend', 0.0)

    # Obliczenia właściwe (tylko gdy minęło > 10 min)
    dt_hours = dt_seconds / 3600.0
    t_prev = prev_data['temp']
    dT = current_temp - t_prev
    
    trend = dT / dt_hours
    trend = round(trend, 2)
    
    # Aktualizacja historii (zapisujemy też nowy trend)
    historia_pomiarow[station_id] = {
        'temp': current_temp, 
        'time': current_time, 
        'last_trend': trend
    }
    
    return trend

def wybierz_temperature_do_analizy(ds_temp, bme_temp, wiatr):
    """
    Wybiera czujnik na podstawie siły wiatru.
    """
    wybrana_temp = None
    zrodlo = "BRAK"

    if ds_temp is not None and bme_temp is not None:
        if wiatr is not None and wiatr > PROG_WIATRU:
            wybrana_temp = bme_temp
            zrodlo = "BME280 (Wiatr > Prog)"
        else:
            wybrana_temp = ds_temp
            zrodlo = "DS18B20 (Wiatr <= Prog)"
            
    elif bme_temp is not None:
        wybrana_temp = bme_temp
        zrodlo = "BME280 (Awaria DS)"
    elif ds_temp is not None:
        wybrana_temp = ds_temp
        zrodlo = "DS18B20 (Awaria BME)"
        
    return wybrana_temp, zrodlo

def ocena_ryzyka_przymrozku(temp, punkt_rosy, trend):
    """
    Decyduje czy wysłać ALARM (1) czy OK (0).
    Łączy temperaturę, punkt rosy i szybkość spadku.
    """
    if temp is None:
        return 0
        
    # 1. Próg bezwzględny (już jest zimno)
    if temp <= 2.0:
        return 1
        
    # 2. Analiza Punktu Rosy (predykcja suchego powietrza)
    if punkt_rosy is not None:
        # Jeśli punkt rosy ujemny i temp niska -> brak hamulca termicznego -> ALARM
        if punkt_rosy < 0.0 and temp < 5.0:
            return 1
            
    # 3. Analiza Trendu (gwałtowny spadek)
    # Jeśli spada szybciej niż 1.5 stopnia na godzinę i jest już chłodno (<3.5)
    if trend is not None and temp <= 3.5 and trend <= -1.5:
        return 1
        
    return 0

def parsowanie_ramki(dane):
    try:
        tekst = dane.decode('utf-8', errors='ignore')
        if len(tekst) < 32:
            return None
        
        def parsowanie_float(s):
            s = s.strip()
            if 'N/A' in s: return None
            return float(s)
        
        id_stacji = tekst[0:2]
        temp_ds_str = tekst[2:8]
        temp_bme_str = tekst[8:14]
        wilg_str = tekst[14:19]
        probki_str = tekst[19:21]
        czas_str = tekst[21:27]
        wiatr_str = tekst[27:32]
        
        return {
            'station_id': id_stacji,
            'temp_ds18b20': parsowanie_float(temp_ds_str),
            'temp_bme280': parsowanie_float(temp_bme_str),
            'humidity': parsowanie_float(wilg_str),
            'samples': int(probki_str),
            'remote_time': f"{czas_str[0:2]}:{czas_str[2:4]}:{czas_str[4:6]}",
            'wiatr': parsowanie_float(wiatr_str)
        }
    except:
        return None

def inicjalizacja_lory():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    
    spi = LoRaSpi(0, 0)
    cs = LoRaGpio(PIN_CS, 1)
    reset = LoRaGpio(PIN_RESET, 1)
    busy = LoRaGpio(PIN_BUSY, 0)
    dio1 = LoRaGpio(PIN_DIO1, 0)
    txen = LoRaGpio(PIN_TXEN, 1)
    rxen = LoRaGpio(PIN_RXEN, 1)
    
    lora = SX126x(spi, cs, reset, busy, dio1, txen, rxen)
    
    lora.reset()
    time.sleep(0.1)
    lora.setStandby(SX126x.STANDBY_RC)
    time.sleep(0.01)
    
    if lora.getMode() != 0x20:
        return None, None
    
    lora.setPacketType(SX126x.LORA_MODEM)
    lora.setDio3AsTcxoCtrl(SX126x.DIO3_OUTPUT_1_8, SX126x.TCXO_DELAY_10)
    time.sleep(0.01)
    lora.calibrate(0xFF)
    time.sleep(0.05)
    
    lora.setFrequency(CZESTOTLIWOSC)
    lora.setRxGain(SX126x.RX_GAIN_BOOSTED)
    lora.setLoRaModulation(SF, BW, CR)
    lora.setLoRaPacket(SX126x.HEADER_EXPLICIT, 12, 255, True, False)
    lora.setSyncWord(SX126x.LORA_SYNC_WORD_PRIVATE)
    
    lora.clearIrqStatus(0x03FF)
    lora.setDioIrqParams(
        SX126x.IRQ_RX_DONE | SX126x.IRQ_CRC_ERR,
        SX126x.IRQ_RX_DONE | SX126x.IRQ_CRC_ERR,
        0, 0
    )
    
    return lora, rxen

def main():    
    lora, rxen = inicjalizacja_lory()
    if not lora:
        print("LoRa: inicjalizacja nieudana")
        return
    
    print("LoRa: ustawiono tryb RX")
    
    rxen.output(GPIO.HIGH)
    lora.setBufferBaseAddress(128, 0)
    lora.setRx(0xFFFFFF)  
    
    try:
        while True:
            flagi_irq = lora.getIrqStatus()
            
            if flagi_irq & SX126x.IRQ_RX_DONE:
                lora.clearIrqStatus(0x03FF)
                                
                if flagi_irq & SX126x.IRQ_CRC_ERR:
                    lora.setRx(0xFFFFFF)
                    continue

                dlugosc_danych, wskaznik_startu = lora.getRxBufferStatus()
                if dlugosc_danych > 0:
                    dane = lora.readBuffer(wskaznik_startu, dlugosc_danych)
                    dane_bajty = bytes(dane)
                    
                    sparsowane = parsowanie_ramki(dane_bajty)
                    znacznik_czasu = time.strftime("%Y-%m-%d %H:%M:%S")
                    unix_time = int(time.time())
                    print(f"[{znacznik_czasu}] Ramka: {dane_bajty.decode('utf-8', errors='ignore').strip()}")
                    
                    if sparsowane:
                        # 1. Wybór temperatury (Wiatr)
                        temp_do_analizy, zrodlo_temp = wybierz_temperature_do_analizy(
                            sparsowane['temp_ds18b20'],
                            sparsowane['temp_bme280'],
                            sparsowane['wiatr']
                        )

                        # 2. Obliczenia
                        punkt_rosy = obliczanie_punktu_rosy(temp_do_analizy, sparsowane['humidity'])
                        
                        if temp_do_analizy is not None:
                            cooling_rate = obliczanie_szybkosci_chlodzenia(sparsowane['station_id'], temp_do_analizy, unix_time)
                        else:
                            cooling_rate = 0.0

                        # 3. Decyzja o alarmie (Nowa funkcja!)
                        czy_jest_przymrozek = ocena_ryzyka_przymrozku(temp_do_analizy, punkt_rosy, cooling_rate)

                        wyjscie = {
                            'station_id': sparsowane['station_id'],
                            'temp_ds18b20': sparsowane['temp_ds18b20'],
                            'temp_bme280': sparsowane['temp_bme280'],
                            'selected_temp': temp_do_analizy,
                            'temp_source': zrodlo_temp,
                            'humidity': sparsowane['humidity'],
                            'dew_point': punkt_rosy,
                            'cooling_rate': cooling_rate,
                            'frost_alert': czy_jest_przymrozek, # <--- 0 lub 1
                            'wiatr': sparsowane['wiatr'],
                            'timestamp': unix_time
                        }
                        
                        print(f"         JSON: {json.dumps(wyjscie, ensure_ascii=False)}")
                        
                        try:                            
                            temat = f"lora/pogoda"
                            wiadomosc = json.dumps(wyjscie)
                            klient.publish(temat, wiadomosc)
                        except Exception as e:
                            print(f"Blad z MQTT {e}")
                    else:
                        print(" Blad przy parsowaniu")
                
                lora.setRx(0xFFFFFF)
            
            time.sleep(0.01)
            
    except KeyboardInterrupt:
        print("\nZatrzymano program")
    
    lora.setStandby(SX126x.STANDBY_RC)
    GPIO.cleanup()

if __name__ == "__main__":
    main()
