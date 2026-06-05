# Wristband 2501 — BLE Data Tool

Cliente Python para extraer datos de salud de la pulsera **Bluetooth ANT+ Screenless Band (Model 2501)**
de Shenzhen Youhong Technology / J-STYLE.

---

## Requisitos del sistema (Linux)

```bash
# BlueZ (stack Bluetooth de Linux)
sudo apt install bluetooth bluez python3-pip python3-venv

# Asegúrate de que bluetoothd esté corriendo
sudo systemctl enable bluetooth
sudo systemctl start bluetooth
```

---

## Instalación

```bash
# 1. Clonar / copiar el proyecto
cd wristband2501

# 2. Crear entorno virtual
python3 -m venv .venv
source .venv/bin/activate

# 3. Instalar dependencias
pip install -r requirements.txt
```

---

## Primer uso — encontrar la dirección MAC

```bash
# Opción A: usar el scanner del proyecto
python main.py --scan

# Opción B: usar bluetoothctl
bluetoothctl
  > scan on
  # espera ~10s, busca "J-STYLE" o "2501"
  > scan off
  > exit
```

---

## Uso

### Modo interactivo (menú)

```bash
python main.py
# o con dirección explícita:
python main.py --address AA:BB:CC:DD:EE:FF
```

```
╔══════════════════════════════════════╗
║      Wristband 2501 — Data Tool      ║
╚══════════════════════════════════════╝
  1) Scan & connect
  2) Battery level
  3) Sync time
  4) Real-time heart rate  (30s)
  5) Real-time SpO2        (30s)
  6) Real-time steps       (10s)
  7) Read stored heart rate records
  8) Read stored SpO2 records
  9) Read stored sleep records
 10) Read stored step details
 11) Read total activity (30 days)
 12) Read HRV records
 13) ★ Full dump (all data → JSON)
```

### Dump completo directo

```bash
python main.py --dump --address AA:BB:CC:DD:EE:FF
# Guarda en data/dump_YYYYMMDD_HHMMSS.json
```

---

## En VSCode

1. Abre la carpeta `wristband2501` en VSCode
2. Instala la extensión **Python** (ms-python.python)
3. Selecciona el intérprete `.venv/bin/python` (`Ctrl+Shift+P` → "Python: Select Interpreter")
4. En el panel **Run and Debug** (`Ctrl+Shift+D`) verás tres configuraciones:
   - **Run Wristband Tool** — menú interactivo
   - **Scan Only** — solo escanea
   - **Full Dump** — dump automático (edita la MAC en `.vscode/launch.json`)

---

## Estructura del proyecto

```
wristband2501/
├── main.py                  ← Entry point / CLI
├── requirements.txt
├── .vscode/
│   └── launch.json          ← Configuraciones de debug VSCode
├── src/
│   ├── protocol.py          ← Builders de paquetes, CRC, parsers
│   └── ble_client.py        ← Cliente BLE async (bleak)
├── data/                    ← Dumps JSON guardados aquí
└── logs/
    └── wristband.log        ← Log de sesiones
```

---

## Datos extraídos

| Dato | Comando | Descripción |
|------|---------|-------------|
| Frecuencia cardíaca | `0x54` / `0x28` | Histórico por minuto + tiempo real |
| SpO2 (oxígeno) | `0x66` / `0x28` | Histórico + tiempo real |
| Pasos diarios | `0x51` | Totales por día, 30 días |
| Detalle de pasos | `0x52` | Por bloques de 10 minutos |
| Sueño | `0x53` | Calidad minuto a minuto |
| HRV + presión | `0x56` | HRV, fatiga, presión sistólica/diastólica |

---

## Troubleshooting

### "Permission denied" o Bluetooth no funciona

```bash
# Dar permisos al usuario actual
sudo usermod -a -G bluetooth $USER
# O ejecutar con sudo (no recomendado)
sudo python main.py
```

### "No device found"

- Asegúrate de que la pulsera esté encendida y cerca (<2m)
- Comprueba que no esté conectada a otro dispositivo (solo permite 1 conexión BLE)
- Prueba `bluetoothctl scan on` para verificar que el adaptador funcione

### Timeout esperando respuesta

- El dispositivo puede tardar en procesar la primera conexión; reintenta
- Verifica que `RX_UUID (0xFFF7)` tenga notificaciones habilitadas

---

## Zona horaria

Colombia es **UTC-5**, por lo que al sincronizar hora se usa `timezone_minutes=-300`.
Si estás en otra zona, edita la llamada en `main.py`:

```python
await wb.sync_time(timezone_minutes=-300)  # Colombia UTC-5
# UTC+1 (España): timezone_minutes=60
# UTC+8 (China):  timezone_minutes=480
```
