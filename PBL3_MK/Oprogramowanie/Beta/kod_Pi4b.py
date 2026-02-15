# -*- coding: utf-8 -*-

# kod na Raspberry Pi 4b
# Odbiornik LoRa SX1262 — odbiera 32B ramki wysyłane przez stacje (Pi Zero), Kod:
#inicjalizuje moduł SX126x (GPIO, SPI, parametry LoRa)
#nasłuchuje ramek LoRa, sprawdza CRC, parsuje pola tekstowe
#oblicza punkt rosy na podstawie temperatury/wilgoci z bme280
#publikuje sparsowane dane jako JSON przez MQTT na temat `lora/pogoda`
#Ustawia parametry: CZESTOTLIWOSC, SF, BW, CR oraz piny TX/RX
#Przy wylaczaniu kod czyści stan GPIO

import sys
import time
import json
import math
import RPi.GPIO as GPIO
from LoRaRF import SX126x, LoRaSpi, LoRaGpio
import paho.mqtt.client as mqtt

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
    if temperatura is None or wilgotnosc is None or wilgotnosc <= 0:
        return None
    a = 17.27
    b = 237.7
    alfa = ((a * temperatura) / (b + temperatura)) + math.log(wilgotnosc / 100.0)
    punkt_rosy = (b * alfa) / (a - alfa)
    return round(punkt_rosy, 1)

def parsowanie_ramki(dane):
    """
    Parsowanie ramki 32B o postaci:
    |id_stacji|temp_ds18b20|temp_bme280|wilgotnosc|liczba_probek|czas_wmomenciewyslania|
    np. 01|+22.5|+21.3|045.2|10|143052
    """
    try:
        tekst = dane.decode('utf-8', errors='ignore').strip()
        czesci = tekst.split('|')
        
        if len(czesci) != 6:
            return None
        
        def parsowanie_temperatury(s):
            s = s.strip()
            if 'N/A' in s:
                return None
            return float(s)
        
        def parsowanie_wilgotnosci(s):
            s = s.strip()
            if 'N/A' in s:
                return None
            return float(s)
        
        return {
            'station_id': czesci[0],
            'temp_ds18b20': parsowanie_temperatury(czesci[1]),
            'temp_bme280': parsowanie_temperatury(czesci[2]),
            'humidity': parsowanie_wilgotnosci(czesci[3]),
            'samples': int(czesci[4]),
            'remote_time': f"{czesci[5][:2]}:{czesci[5][2:4]}:{czesci[5][4:6]}"
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
                    print("Blad CRC")
                    lora.setRx(0xFFFFFF)
                    continue


                dlugosc_danych, wskaznik_startu = lora.getRxBufferStatus()
                if dlugosc_danych > 0:
                    dane = lora.readBuffer(wskaznik_startu, dlugosc_danych)
                    dane_bajty = bytes(dane)
                    
                    
                    # Parsowanie ramki
                    sparsowane = parsowanie_ramki(dane_bajty)
                    
                    znacznik_czasu = time.strftime("%Y-%m-%d %H:%M:%S")
                    print(f"\n[{znacznik_czasu}] Ramka: {dane_bajty.decode('utf-8', errors='ignore').strip()}")
                    
                    if sparsowane:                        
                        punkt_rosy = obliczanie_punktu_rosy(sparsowane['temp_bme280'], sparsowane['humidity'])
                        
                        # JSON - do wyslania przez MQTT
                        wyjscie = {
                            'station_id': sparsowane['station_id'],
                            'temp_ds18b20': sparsowane['temp_ds18b20'],
                            'temp_bme280': sparsowane['temp_bme280'],
                            'humidity': sparsowane['humidity'],
                            'dew_point': punkt_rosy,
                            'timestamp': int(time.time())
                        }
                        
                        print(f"         JSON: {json.dumps(wyjscie, ensure_ascii=False)}")
                        
                        # Wysylanie danych przez mqtt na topic lora/pogoda
                        try:                            
                            temat = f"lora/pogoda"
                                                        
                            wiadomosc = json.dumps(wyjscie)
                                                        
                            klient.publish(temat, wiadomosc)
                            
                        except Exception as e:
                            print(f"Blad z MQTT {e}")
                        # ------------------------
                    else:
                        print(" Blad przy parsowaniu")
                
                # Wznowienie odbioru, ustawienie RX
                lora.setRx(0xFFFFFF)
            
            time.sleep(0.01)
            
    except KeyboardInterrupt:
        print("\nZatrzymano program")
    
    lora.setStandby(SX126x.STANDBY_RC)
    GPIO.cleanup()

if __name__ == "__main__":
    main()