"""Standard OBD-II DTC descriptions + universal categoriser.

The SAE J2012 *generic* trouble codes (second digit 0, e.g. P0xxx / P2xxx /
P34xx, plus C0/B0/U0 network codes) mean the SAME THING on every make and
model, so a bundled table explains them for ANY vehicle. Manufacturer-specific
codes (second digit 1, e.g. P1xxx) are defined per-make -- for those we can't
give an exact text, so we categorise them clearly instead of guessing.

describe_dtc("P0301") -> "Cylinder 1 Misfire Detected"
describe_dtc("P1abc") -> "Manufacturer-specific powertrain code (see maker service info)"
"""

# --- generic codes that cover the large majority of real-world scans ---------
DTC_DESCRIPTIONS = {
    # Fuel & air metering / MAF / MAP / IAT / ECT
    "P0100": "Mass or Volume Air Flow Circuit Malfunction",
    "P0101": "Mass or Volume Air Flow Circuit Range/Performance",
    "P0102": "Mass or Volume Air Flow Circuit Low Input",
    "P0103": "Mass or Volume Air Flow Circuit High Input",
    "P0105": "Manifold Absolute Pressure/Barometric Pressure Circuit",
    "P0106": "MAP/Barometric Pressure Circuit Range/Performance",
    "P0107": "MAP/Barometric Pressure Circuit Low Input",
    "P0108": "MAP/Barometric Pressure Circuit High Input",
    "P0110": "Intake Air Temperature Circuit Malfunction",
    "P0111": "Intake Air Temperature Circuit Range/Performance",
    "P0112": "Intake Air Temperature Circuit Low Input",
    "P0113": "Intake Air Temperature Circuit High Input",
    "P0115": "Engine Coolant Temperature Circuit Malfunction",
    "P0116": "Engine Coolant Temperature Circuit Range/Performance",
    "P0117": "Engine Coolant Temperature Circuit Low Input",
    "P0118": "Engine Coolant Temperature Circuit High Input",
    "P0120": "Throttle/Pedal Position Sensor A Circuit",
    "P0121": "Throttle/Pedal Position Sensor A Range/Performance",
    "P0122": "Throttle/Pedal Position Sensor A Low Input",
    "P0123": "Throttle/Pedal Position Sensor A High Input",
    "P0125": "Insufficient Coolant Temp for Closed Loop Fuel Control",
    "P0128": "Coolant Thermostat (Below Regulating Temperature)",
    # Oxygen / air-fuel sensors
    "P0130": "O2 Sensor Circuit (Bank 1 Sensor 1)",
    "P0131": "O2 Sensor Circuit Low Voltage (Bank 1 Sensor 1)",
    "P0132": "O2 Sensor Circuit High Voltage (Bank 1 Sensor 1)",
    "P0133": "O2 Sensor Circuit Slow Response (Bank 1 Sensor 1)",
    "P0134": "O2 Sensor Circuit No Activity (Bank 1 Sensor 1)",
    "P0135": "O2 Sensor Heater Circuit (Bank 1 Sensor 1)",
    "P0136": "O2 Sensor Circuit (Bank 1 Sensor 2)",
    "P0137": "O2 Sensor Circuit Low Voltage (Bank 1 Sensor 2)",
    "P0138": "O2 Sensor Circuit High Voltage (Bank 1 Sensor 2)",
    "P0139": "O2 Sensor Circuit Slow Response (Bank 1 Sensor 2)",
    "P0140": "O2 Sensor Circuit No Activity (Bank 1 Sensor 2)",
    "P0141": "O2 Sensor Heater Circuit (Bank 1 Sensor 2)",
    "P0150": "O2 Sensor Circuit (Bank 2 Sensor 1)",
    "P0151": "O2 Sensor Circuit Low Voltage (Bank 2 Sensor 1)",
    "P0152": "O2 Sensor Circuit High Voltage (Bank 2 Sensor 1)",
    "P0155": "O2 Sensor Heater Circuit (Bank 2 Sensor 1)",
    "P0156": "O2 Sensor Circuit (Bank 2 Sensor 2)",
    "P0157": "O2 Sensor Circuit Low Voltage (Bank 2 Sensor 2)",
    "P0158": "O2 Sensor Circuit High Voltage (Bank 2 Sensor 2)",
    "P0160": "O2 Sensor Circuit No Activity (Bank 2 Sensor 2)",
    # Fuel trim / mixture
    "P0170": "Fuel Trim Malfunction (Bank 1)",
    "P0171": "System Too Lean (Bank 1)",
    "P0172": "System Too Rich (Bank 1)",
    "P0173": "Fuel Trim Malfunction (Bank 2)",
    "P0174": "System Too Lean (Bank 2)",
    "P0175": "System Too Rich (Bank 2)",
    # Injectors
    "P0200": "Injector Circuit Malfunction",
    "P0201": "Injector Circuit Malfunction - Cylinder 1",
    "P0202": "Injector Circuit Malfunction - Cylinder 2",
    "P0203": "Injector Circuit Malfunction - Cylinder 3",
    "P0204": "Injector Circuit Malfunction - Cylinder 4",
    "P0205": "Injector Circuit Malfunction - Cylinder 5",
    "P0206": "Injector Circuit Malfunction - Cylinder 6",
    "P0217": "Engine Over Temperature Condition",
    "P0218": "Transmission Over Temperature Condition",
    "P0219": "Engine Overspeed Condition",
    # Ignition / misfire
    "P0300": "Random/Multiple Cylinder Misfire Detected",
    "P0301": "Cylinder 1 Misfire Detected",
    "P0302": "Cylinder 2 Misfire Detected",
    "P0303": "Cylinder 3 Misfire Detected",
    "P0304": "Cylinder 4 Misfire Detected",
    "P0305": "Cylinder 5 Misfire Detected",
    "P0306": "Cylinder 6 Misfire Detected",
    "P0307": "Cylinder 7 Misfire Detected",
    "P0308": "Cylinder 8 Misfire Detected",
    "P0316": "Misfire Detected on Startup (First 1000 Revolutions)",
    "P0320": "Ignition/Distributor Engine Speed Input Circuit",
    "P0325": "Knock Sensor 1 Circuit (Bank 1)",
    "P0330": "Knock Sensor 2 Circuit (Bank 2)",
    "P0335": "Crankshaft Position Sensor A Circuit",
    "P0336": "Crankshaft Position Sensor A Circuit Range/Performance",
    "P0340": "Camshaft Position Sensor A Circuit (Bank 1)",
    "P0341": "Camshaft Position Sensor A Circuit Range/Performance",
    "P0344": "Camshaft Position Sensor A Circuit Intermittent (Bank 1)",
    # Emissions: catalyst / EGR / EVAP / secondary air
    "P0401": "Exhaust Gas Recirculation Flow Insufficient Detected",
    "P0402": "Exhaust Gas Recirculation Flow Excessive Detected",
    "P0404": "Exhaust Gas Recirculation Circuit Range/Performance",
    "P0405": "Exhaust Gas Recirculation Sensor A Circuit Low",
    "P0411": "Secondary Air Injection System Incorrect Flow",
    "P0420": "Catalyst System Efficiency Below Threshold (Bank 1)",
    "P0421": "Warm Up Catalyst Efficiency Below Threshold (Bank 1)",
    "P0430": "Catalyst System Efficiency Below Threshold (Bank 2)",
    "P0440": "Evaporative Emission Control System Malfunction",
    "P0441": "Evaporative Emission System Incorrect Purge Flow",
    "P0442": "Evaporative Emission System Leak Detected (Small Leak)",
    "P0443": "Evaporative Emission System Purge Control Valve Circuit",
    "P0446": "Evaporative Emission System Vent Control Circuit",
    "P0449": "Evaporative Emission System Vent Valve/Solenoid Circuit",
    "P0451": "Evaporative Emission System Pressure Sensor Range/Performance",
    "P0455": "Evaporative Emission System Leak Detected (Large Leak / Gas Cap)",
    "P0456": "Evaporative Emission System Leak Detected (Very Small Leak)",
    "P0457": "Evaporative Emission System Leak Detected (Fuel Cap Loose/Off)",
    # Vehicle speed / idle / aux inputs
    "P0500": "Vehicle Speed Sensor Malfunction",
    "P0501": "Vehicle Speed Sensor Range/Performance",
    "P0505": "Idle Control System Malfunction",
    "P0506": "Idle Control System RPM Lower Than Expected",
    "P0507": "Idle Control System RPM Higher Than Expected",
    "P0520": "Engine Oil Pressure Sensor/Switch Circuit",
    "P0521": "Engine Oil Pressure Sensor/Switch Range/Performance",
    "P0522": "Engine Oil Pressure Sensor/Switch Low Voltage",
    # Computer / outputs / charging
    "P0562": "System Voltage Low",
    "P0563": "System Voltage High",
    "P0600": "Serial Communication Link Malfunction",
    "P0601": "Internal Control Module Memory Check Sum Error",
    "P0606": "ECM/PCM Processor Fault",
    "P0620": "Generator Control Circuit Malfunction",
    "P0700": "Transmission Control System Malfunction",
    "P0705": "Transmission Range Sensor Circuit (PRNDL Input)",
    "P0710": "Transmission Fluid Temperature Sensor Circuit",
    "P0715": "Input/Turbine Speed Sensor Circuit",
    "P0720": "Output Speed Sensor Circuit",
    "P0730": "Incorrect Gear Ratio",
    "P0740": "Torque Converter Clutch Circuit Malfunction",
    "P0741": "Torque Converter Clutch Circuit Performance/Stuck Off",
    "P0748": "Pressure Control Solenoid A Electrical",
    "P0750": "Shift Solenoid A Malfunction",
    "P0755": "Shift Solenoid B Malfunction",
    # Common network / U codes
    "U0100": "Lost Communication With ECM/PCM A",
    "U0101": "Lost Communication With TCM",
    "U0121": "Lost Communication With ABS Control Module",
    "U0140": "Lost Communication With Body Control Module",
    "U0155": "Lost Communication With Instrument Panel Cluster",
    "U0401": "Invalid Data Received From ECM/PCM A",
    # Common body/chassis generics
    "C0035": "Left Front Wheel Speed Sensor Circuit",
    "C0040": "Right Front Wheel Speed Sensor Circuit",
    "C0045": "Left Rear Wheel Speed Sensor Circuit",
    "C0050": "Right Rear Wheel Speed Sensor Circuit",
    "B1318": "Battery Voltage Low",
}

_SYSTEM = {"P": "powertrain", "C": "chassis", "B": "body", "U": "network"}


def describe_dtc(code):
    """Human-readable meaning for a DTC. Exact text for standard SAE generic
    codes (any make); a clear category for manufacturer-specific ones."""
    if not code or len(code) < 5:
        return "Unknown code"
    code = code.upper()
    exact = DTC_DESCRIPTIONS.get(code)
    if exact:
        return exact
    letter = code[0]
    system = _SYSTEM.get(letter, "system")
    # 2nd char: 0/2 = SAE generic, 1/3 = manufacturer-specific (varies by make)
    second = code[1]
    if second in ("1", "3"):
        return "Manufacturer-specific %s code (see maker service info)" % system
    return "Generic %s code (not in local table)" % system
