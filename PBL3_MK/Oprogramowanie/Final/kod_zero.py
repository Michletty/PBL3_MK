# -*- coding: utf-8 -*-

# Stacja Pi Zero z pomiarem wiatru
# Ramka 32B BEZ SEPARATORÓW - stałe pozycje pól
# Format: ID(2) + TDS(6) + TBME(6) + HUM(5) + N(2) + CZAS(6) + WIATR(5) = 32B
# Przykład: 01+22.5+21.3045.210143052005.2

import time
import glob
import RPi.GPIO as GPIO
from gpiozero import Button
from LoRaRF import SX126x, LoRaSpi, LoRaGpio

# Konfig 
ID_STACJI = "01"
INTERWAL_PROBEK = 30
INTERWAL_WYSYLANIA = 5 * 60

# Piny LORY
PIN_RESET = 17
PIN_BUSY = 4
PIN_RXEN = 5
PIN_TXEN = 6
PIN_CS = 8

# Pin wiatromierza GPIO16
PIN_WIATR = 16

# LoRa Setup
CZESTOTLIWOSC = 868000000
MOC_TX = 14
SF = 7
BW = 500000
CR = 5

# Kalibracja wiatromierza - 1 Hz == 2.4 km/h
WSPOLCZYNNIK_WIATRU = 2.4

#wiatromierz
class LicznikWiatru:
    def __init__(self):
        self.impulsy = 0
        self.ostatni_czas = time.time()
    
    def impuls(self):
        self.impulsy += 1
    
    def odczytaj(self):
        #Odczyt predkosci wiatru od ostatniego wywołania 
        teraz = time.time()
        czas_pomiaru = teraz - self.ostatni_czas
        
        if czas_pomiaru <= 0:
            return 0.0
        
        czestotliwosc = self.impulsy / czas_pomiaru
        predkosc = czestotliwosc * WSPOLCZYNNIK_WIATRU
            
        self.impulsy = 0
        self.ostatni_czas = teraz
        
        return round(predkosc, 1)


licznik_wiatru = LicznikWiatru()
czujnik_wiatru = Button(PIN_WIATR, pull_up=True)
czujnik_wiatru.when_pressed = licznik_wiatru.impuls

#DS18B20
def szukanie_ds18b20():
    urzadzenia = glob.glob('/sys/bus/w1/devices/28*')
    return urzadzenia[0] if urzadzenia else None

def odczyt_ds18b20(urzadzenie):
    if not urzadzenie:
        return None
    try:
        with open(urzadzenie + '/w1_slave', 'r') as f:
            linie = f.readlines()
        if linie[0].strip()[-3:] != 'YES':
            return None
        poz = linie[1].find('t=')
        if poz != -1:
            return float(linie[1][poz+2:]) / 1000.0
    except:
        pass
    return None

#BME280
class BME280:
    def __init__(self):
        self.bus = None
        self.addr = None
        self.kal = None
        
    def inicjalizacja(self):
        try:
            import smbus2
            self.bus = smbus2.SMBus(1)
            for addr in [0x76, 0x77]:
                try:
                    if self.bus.read_byte_data(addr, 0xD0) == 0x60:
                        self.addr = addr
                        self._kalibracja()
                        return True
                except:
                    pass
        except:
            pass
        return False
    
    def _kalibracja(self):
        kal1 = self.bus.read_i2c_block_data(self.addr, 0x88, 26)
        kal2 = self.bus.read_i2c_block_data(self.addr, 0xE1, 7)
        
        def s16(v): return v - 65536 if v > 32767 else v
        def s8(v): return v - 256 if v > 127 else v
        
        self.kal = {
            'T1': kal1[0] | (kal1[1] << 8),
            'T2': s16(kal1[2] | (kal1[3] << 8)),
            'T3': s16(kal1[4] | (kal1[5] << 8)),
            'H1': kal1[25],
            'H2': s16(kal2[0] | (kal2[1] << 8)),
            'H3': kal2[2],
            'H4': s16((kal2[3] << 4) | (kal2[4] & 0x0F)) if (kal2[3] << 4) | (kal2[4] & 0x0F) <= 2047 else (kal2[3] << 4) | (kal2[4] & 0x0F) - 4096,
            'H5': s16((kal2[5] << 4) | ((kal2[4] >> 4) & 0x0F)) if (kal2[5] << 4) | ((kal2[4] >> 4) & 0x0F) <= 2047 else (kal2[5] << 4) | ((kal2[4] >> 4) & 0x0F) - 4096,
            'H6': s8(kal2[6]),
        }
    
    def odczyt(self):
        if not self.addr:
            return None, None
        try:
            self.bus.write_byte_data(self.addr, 0xF2, 0x01)
            self.bus.write_byte_data(self.addr, 0xF4, 0x27)
            time.sleep(0.05)
            
            dane = self.bus.read_i2c_block_data(self.addr, 0xF7, 8)
            adc_t = (dane[3] << 12) | (dane[4] << 4) | (dane[5] >> 4)
            adc_h = (dane[6] << 8) | dane[7]
            
            zm1 = ((adc_t / 16384.0) - (self.kal['T1'] / 1024.0)) * self.kal['T2']
            zm2 = (((adc_t / 131072.0) - (self.kal['T1'] / 8192.0)) ** 2) * self.kal['T3']
            t_fine = zm1 + zm2
            temp = t_fine / 5120.0
            
            h = t_fine - 76800.0
            h = (adc_h - (self.kal['H4'] * 64.0 + (self.kal['H5'] / 16384.0) * h)) * \
                (self.kal['H2'] / 65536.0 * (1.0 + self.kal['H6'] / 67108864.0 * h * \
                (1.0 + self.kal['H3'] / 67108864.0 * h)))
            h = h * (1.0 - self.kal['H1'] * h / 524288.0)
            wilg = max(0.0, min(100.0, h))
            
            return temp, wilg
        except:
            return None, None

#LORA
def inicjalizacja_lory():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
        
    spi = LoRaSpi(0, 0)
    cs = LoRaGpio(PIN_CS, 1)
    reset = LoRaGpio(PIN_RESET, 1)
    busy = LoRaGpio(PIN_BUSY, 0)
    txen = LoRaGpio(PIN_TXEN, 1)
    rxen = LoRaGpio(PIN_RXEN, 1)
    
    lora = SX126x(spi, cs, reset, busy, irq=None, txen=txen, rxen=rxen)
    lora.reset()
    time.sleep(0.1)
    lora.setStandby(SX126x.STANDBY_RC)
    lora.setPacketType(SX126x.LORA_MODEM)
    lora.setDio3AsTcxoCtrl(SX126x.DIO3_OUTPUT_1_8, SX126x.TCXO_DELAY_10)
    lora.calibrate(0xFF)
    time.sleep(0.1)
    lora.setFrequency(CZESTOTLIWOSC)
    lora.setTxPower(MOC_TX, SX126x.TX_POWER_SX1262)
    lora.setLoRaModulation(SF, BW, CR)
    lora.setLoRaPacket(SX126x.HEADER_EXPLICIT, 12, 32, True, False)
    lora.setSyncWord(SX126x.LORA_SYNC_WORD_PRIVATE)
    
    return lora, txen, rxen

def wyslanie_danych(lora, txen, rxen, dane):
    lora.setStandby(SX126x.STANDBY_RC)
    txen.output(GPIO.HIGH)
    rxen.output(GPIO.LOW)
    
    lora.setBufferBaseAddress(0, 128)
    lora.writeBuffer(0, tuple(dane), len(dane))
    lora.setPacketParamsLoRa(12, SX126x.HEADER_EXPLICIT, len(dane), True, False)
    lora.clearIrqStatus(0x03FF)
    lora.setDioIrqParams(SX126x.IRQ_TX_DONE | SX126x.IRQ_TIMEOUT,
                         SX126x.IRQ_TX_DONE | SX126x.IRQ_TIMEOUT, 0, 0)
    lora.setTx(0x000000)
    
    start = time.time()
    sukces = False
    while (time.time() - start) < 5.0:
        irq = lora.getIrqStatus()
        if irq & SX126x.IRQ_TX_DONE:
            sukces = True
            break
        if irq & SX126x.IRQ_TIMEOUT:
            break
        time.sleep(0.001)
    
    lora.clearIrqStatus(0x03FF)
    lora.setStandby(SX126x.STANDBY_RC)
    txen.output(GPIO.LOW)
    
    return sukces


def format_temp(t):    
    if t is None:
        return "  N/A "
    znak = "+" if t >= 0 else "-"
    return f"{znak}{abs(t):05.1f}"

def format_wilg(h):    
    if h is None:
        return " N/A "
    return f"{h:05.1f}"

def format_wiatr(w):
    """Formatuje prędkość wiatru do 5 znaków: XXX.X"""
    if w is None:
        return " N/A "
    # Ograniczenie do 999.9 km/h
    w = min(w, 999.9)
    return f"{w:05.1f}"

def budowanie_ramki(id_stacji, temp_ds, temp_bme, wilg_bme, liczba_probek, wiatr):
    """
    Buduje ramkę 32B BEZ SEPARATORÓW - stałe pozycje pól.
    Format: ID(2) + TDS(6) + TBME(6) + HUM(5) + N(2) + CZAS(6) + WIATR(5) = 32B
    Przykład: 01+22.5+21.3045.210143052005.2
    """
    czas = time.strftime("%H%M%S")
    ramka = (
        f"{id_stacji:2s}"           # 0-1:   ID stacji (2)
        f"{format_temp(temp_ds)}"   # 2-7:   Temp DS18B20 (6)
        f"{format_temp(temp_bme)}"  # 8-13:  Temp BME280 (6)
        f"{format_wilg(wilg_bme)}"  # 14-18: Wilgotność (5)
        f"{liczba_probek:02d}"      # 19-20: Liczba próbek (2)
        f"{czas}"                   # 21-26: Czas HHMMSS (6)
        f"{format_wiatr(wiatr)}"    # 27-31: Wiatr km/h (5)
    )
    return ramka.encode('utf-8')[:32].ljust(32)

# ============ MAIN ============
def main():
    global last_wind_time, pulse_count
    
    print("Stacja przymrozkowa ZERO (z wiatromierzem)")
    print("Format ramki: ID|TDS|TBME|HUM|N|CZAS|WIATR (bez separatorów)")
    
    czujnik_ds = szukanie_ds18b20()
    bme = BME280()
    bme.inicjalizacja()
    lora, txen, rxen = inicjalizacja_lory()
    
    print(f"Wysyłanie ramki co {INTERWAL_WYSYLANIA // 60} min")
    print(f"Próbkowanie co {INTERWAL_PROBEK} s")
    
    probki_ds = []
    probki_bme_t = []
    probki_bme_h = []
    probki_wiatr = []
    ostatnie_wyslanie = time.time()
    
    try:
        while True:
            # Odczyt czujników temperatury/wilgotności
            temp_ds = odczyt_ds18b20(czujnik_ds)
            temp_bme, wilg_bme = bme.odczyt()
            
            # Odczyt wiatru (od ostatniego próbkowania)
            wiatr = licznik_wiatru.odczytaj()
            
            # Zbieranie próbek
            if temp_ds is not None:
                probki_ds.append(temp_ds)
            if temp_bme is not None:
                probki_bme_t.append(temp_bme)
            if wilg_bme is not None:
                probki_bme_h.append(wilg_bme)
            probki_wiatr.append(wiatr)
            
            # Debug - wyświetl aktualne odczyty
            print(f"  DS:{temp_ds} BME:{temp_bme}/{wilg_bme} Wiatr:{wiatr} km/h")
            
            # Czas wysłania?
            if time.time() - ostatnie_wyslanie >= INTERWAL_WYSYLANIA:
                # Oblicz średnie
                sr_ds = round(sum(probki_ds) / len(probki_ds), 1) if probki_ds else None
                sr_bme_t = round(sum(probki_bme_t) / len(probki_bme_t), 1) if probki_bme_t else None
                sr_bme_h = round(sum(probki_bme_h) / len(probki_bme_h), 1) if probki_bme_h else None
                sr_wiatr = round(sum(probki_wiatr) / len(probki_wiatr), 1) if probki_wiatr else 0.0
                
                n = max(min(len(probki_ds), len(probki_bme_t)), 1)
                
                # Buduj i wyślij ramkę
                ramka = budowanie_ramki(ID_STACJI, sr_ds, sr_bme_t, sr_bme_h, n, sr_wiatr)
                czas = time.strftime("%H:%M:%S")
                
                if wyslanie_danych(lora, txen, rxen, ramka):
                    print(f"[{czas}] OK | {ramka.decode().strip()}")
                else:
                    print(f"[{czas}] BŁĄD | {ramka.decode().strip()}")
                
                # Wyczyść bufory
                probki_ds.clear()
                probki_bme_t.clear()
                probki_bme_h.clear()
                probki_wiatr.clear()
                ostatnie_wyslanie = time.time()
            
            time.sleep(INTERWAL_PROBEK)
            
    except KeyboardInterrupt:
        print("\n[STOP]")
    
    lora.setStandby(SX126x.STANDBY_RC)
    GPIO.cleanup()

if __name__ == "__main__":
    main()