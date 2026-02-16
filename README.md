# System ostrzegania przed przymrozkami

Projekt realizowany w ramach przedmiotu komunikacja przewodowa i bezprzewodowa (PBL3)

## Opis projektu
W pierwszej części projektu uruchomiono stację odbiorczą (Bramkę) na Raspberry Pi 4B, która odbiera ramki LoRa, parsuje je i przekazuje do dalszego przetwarzania, z czego agent Telegraf nasłuchujący brokera i zapisuje metryki i wysyła do baza szeregów czasowych InfluxDB v2 na podstawie których są generowane dashbordy Grafana zintegrowane z serwerem WWW. Zostało to jednak potem zmienione, ostatecznie system wykorzystuje oprogramowanie w języku Python, komunikację MQTT oraz WebSocket co pozwoliło na redukcję opóźnień i stworzenie dedykowanej wizualizacji mapowej, zastępując rozwiązania typu InfluxDB i Grafana.

## Architektura systemu

### 1. Stacje pomiarowe (Raspberry Pi Zero)
* **Mikrokontroler:** Raspberry Pi Zero.
* **Komunikacja:** Moduł LoRa SX1262 (868 MHz, interfejs SPI).
* **Czujniki:**
    * **Temperatura (2 m):** DS18B20 (magistrala 1-Wire) – niska bezwładność cieplna, wykorzystywany do wykrywania przymrozków radiacyjnych.
    * **Temperatura i Wilgotność (1 m):** BME280 (magistrala I2C) – pomiar mas powietrza, wykorzystywany przy silniejszym wietrze (przymrozki adwekcyjne).
    * **Wiatr:** Wiatromierz (GPIO) – pomiar prędkości wiatru (sygnał impulsowy).

### 2. Stacja centralna (Raspberry Pi 4B)
Pełni rolę bramki (Gateway), serwera i interfejsu użytkownika:
* **Odbiór danych:** Moduł LoRa w trybie ciągłego nasłuchu.
* **Przetwarzanie:** Parser dekoduje ramki, oblicza punkt rosy, trend zmian temperatury (cooling rate) oraz podejmuje decyzje alarmowe.
* **Logika hybrydowa:** Automatyczny wybór źródła temperatury w zależności od siły wiatru (DS18B20 dla wiatru < 2.0 m/s, BME280 dla wiatru > 2.0 m/s).
* **Komunikacja wewnętrzna:** Broker MQTT (Mosquitto) przekazuje dane do serwera aplikacji.
* **Serwer WWW:** Aplikacja Flask udostępnia interfejs webowy.
* **Aktualizacja danych:** WebSocket zapewnia odświeżanie danych na mapie w czasie rzeczywistym.

## Funkcjonalności

* **Pomiary meteorologiczne:** Temperatura na dwóch wysokościach, wilgotność względna, prędkość wiatru.
* **Analiza ryzyka:** Obliczanie punktu rosy oraz tempa spadku temperatury (stopnie Celsjusza na godzinę).
* **Inteligentne alarmy:** Flaga frost_alert aktywowana w trzech przypadkach:
    * Spadek temperatury poniżej 2.0 stopnia Celsjusza (bezwzględny próg).
    * Punkt rosy < 0 stopnia Celsjusza przy temperaturze < 5.0 stopnia Celsjusza (suche powietrze).
    * Gwałtowny spadek temperatury (trend < -1.5 stopnia Celsjusza/h) przy temperaturze < 3.5 stopnia Celsjusza.
* **Wizualizacja:** Interaktywna mapa sadu ze statusami stacji oraz wykresy historyczne (temperatura, wilgotność, wiatr).

## Specyfikacja techniczna komunikacji

### LoRa (868 MHz)
* **Parametry:** Moc 14 dBm, Spreading Factor SF7, Bandwidth 500 kHz, Coding Rate 4/5.
* **Zasięg:** Potwierdzona stabilna komunikacja w gęstym sadzie na dystansie 450 m (-102 dBm) oraz w otwartej przestrzeni do 1200 m.
* **Ramka danych:** Stała długość 32 bajty (JSON), zawierająca ID stacji, odczyty z czujników, licznik próbek i znacznik czasu.

### Protokoły sieciowe
* **MQTT:** Temat lora/pogoda do przesyłania przetworzonych obiektów JSON wewnątrz stacji centralnej.
* **HTTP/WebSocket:** Serwer Flask (port 5000) obsługuje żądania GET dla API i stron HTML oraz kanał WebSocket dla strumieniowania danych na żywo.

## Instalacja i uruchomienie

Wymagania systemowe dla stacji centralnej:
* Raspberry Pi 4B.
* Python 3.
* Mosquitto MQTT Broker.

Uruchomienie komponentów:
1.  Uruchomienie brokera MQTT: mosquitto -d
2.  Uruchomienie odbiornika LoRa: python3 odbiornik_v7.py
3.  Uruchomienie serwera aplikacji: python3 ff.py

Dostęp do interfejsu WWW odbywa się poprzez przeglądarkę pod adresem IP stacji centralnej na porcie 5000.

