from __future__ import annotations

import copy
import json
import re
import unicodedata
from typing import Any

from django.db import connection


RESULT_OPTIONS = [
    {"value": "ok", "label": "OK"},
    {"value": "observado", "label": "Observado"},
    {"value": "no_ok", "label": "No OK"},
    {"value": "na", "label": "N/A"},
]

GLOBAL_RESULT_OPTIONS = [
    {"value": "pendiente", "label": "Pendiente"},
    {"value": "apto", "label": "Apto"},
    {"value": "apto_condicional", "label": "Apto condicional"},
    {"value": "no_apto", "label": "No apto"},
]


BASE_TEMPLATES: dict[str, dict[str, Any]] = {
    "aspirador": {
        "type_key": "aspirador",
        "template_key": "aspirador_v1",
        "template_version": "1.0.0",
        "display_name": "Aspirador",
        "default_instrumentos": (
            "Vacuómetro de referencia con certificado: U3556-260115. Última calibración 15/1/2026\n"
            "Flujómetro de referencia: Analizador de flujo de gases Ventmeter de Magnamed. "
            "Última calibración en 2025"
        ),
        "references": [
            {
                "ref_id": "REF-01",
                "tipo": "norma",
                "titulo": "ISO 10079-1:2022",
                "edicion": "2022",
                "anio": 2022,
                "organismo_o_fabricante": "ISO",
                "url": "https://www.iso.org/standard/81532.html",
                "aplica_a": "Aspiradores médicos eléctricos",
            },
        ],
        "sections": [
            {
                "id": "seguridad",
                "title": "Seguridad y verificación inicial",
                "entry_mode": "result_only",
                "items": [
                    {
                        "key": "asp_inspeccion_visual",
                        "label": "Inspección visual y estado general",
                        "target": "Sin daño estructural, cables y conexiones seguros",
                        "unit": "",
                        "ref_ids": ["REF-01"],
                    },
                ],
            },
            {
                "id": "performance",
                "title": "Rendimiento",
                "entry_mode": "measured_only",
                "items": [
                    {
                        "key": "asp_vacio_max",
                        "label": "Vacío máximo",
                        "target": ">= 500 mmHg",
                        "unit": "mmHg",
                        "ref_ids": ["REF-01"],
                    },
                    {
                        "key": "asp_caudal_libre",
                        "label": "Caudal libre",
                        "target": ">= 15 L/min",
                        "unit": "L/min",
                        "ref_ids": ["REF-01"],
                    },
                    {
                        "key": "asp_duracion_bateria",
                        "label": "Duración de batería",
                        "target": "Tensión de batería > 11 V luego de 15 min de prueba continua",
                        "unit": "V",
                        "ref_ids": ["REF-01"],
                    },
                ],
            },
        ],
    },
    "concentrador_oxigeno": {
        "type_key": "concentrador_oxigeno",
        "template_key": "concentrador_oxigeno_v1",
        "template_version": "1.0.0",
        "display_name": "Concentrador de oxígeno",
        "references": [
            {
                "ref_id": "REF-01",
                "tipo": "norma",
                "titulo": "ISO 80601-2-69:2020",
                "edicion": "2020",
                "anio": 2020,
                "organismo_o_fabricante": "ISO",
                "url": "https://www.iso.org/standard/75946.html",
                "aplica_a": "Concentradores de oxígeno para uso médico",
            },
        ],
        "sections": [
            {
                "id": "salida_o2",
                "title": "Salida de oxígeno",
                "items": [
                    {
                        "key": "co2_concentracion",
                        "label": "Concentración de O2",
                        "target": "Dentro de especificación del fabricante por flujo",
                        "unit": "%",
                        "ref_ids": ["REF-01"],
                    },
                    {
                        "key": "co2_flujo_setpoint",
                        "label": "Flujo real vs setpoint",
                        "target": "Desviación dentro de tolerancia del fabricante",
                        "unit": "L/min",
                        "ref_ids": ["REF-01"],
                    },
                ],
            },
            {
                "id": "alarmas",
                "title": "Alarmas y seguridad",
                "items": [
                    {
                        "key": "co2_alarma_baja_concentracion",
                        "label": "Alarma baja concentración de O2",
                        "target": "Activa según especificación",
                        "unit": "",
                        "ref_ids": ["REF-01"],
                    },
                    {
                        "key": "co2_alarma_falla_energia",
                        "label": "Alarma por falla de energía",
                        "target": "Activa y audible/visible según especificación",
                        "unit": "",
                        "ref_ids": ["REF-01"],
                    },
                ],
            },
        ],
    },
    "concentrador_portatil_oxigeno": {
        "type_key": "concentrador_portatil_oxigeno",
        "template_key": "concentrador_portatil_oxigeno_v1",
        "template_version": "1.0.0",
        "display_name": "Concentrador portátil de oxígeno",
        "references": [
            {
                "ref_id": "REF-01",
                "tipo": "norma",
                "titulo": "ISO 80601-2-69:2020",
                "edicion": "2020",
                "anio": 2020,
                "organismo_o_fabricante": "ISO",
                "url": "https://www.iso.org/standard/75946.html",
                "aplica_a": "Concentradores de oxígeno para uso médico",
            },
        ],
        "sections": [
            {
                "id": "modo_pulso",
                "title": "Modo pulso y entrega de O2",
                "items": [
                    {
                        "key": "cpo2_deteccion_inspiracion",
                        "label": "Detección de inspiración",
                        "target": "Detecta ciclo inspiratorio y entrega bolo",
                        "unit": "",
                        "ref_ids": ["REF-01"],
                    },
                    {
                        "key": "cpo2_entrega_bolo",
                        "label": "Entrega de bolo por nivel",
                        "target": "Dentro de especificación del fabricante",
                        "unit": "",
                        "ref_ids": ["REF-01"],
                    },
                ],
            },
            {
                "id": "energia",
                "title": "Energía y alarmas",
                "items": [
                    {
                        "key": "cpo2_bateria_autonomia",
                        "label": "Autonomía de batería",
                        "target": "Dentro de especificación del fabricante",
                        "unit": "min",
                        "ref_ids": ["REF-01"],
                    },
                    {
                        "key": "cpo2_alarmas",
                        "label": "Alarmas (batería baja / fallo)",
                        "target": "Operativas y audibles/visibles",
                        "unit": "",
                        "ref_ids": ["REF-01"],
                    },
                ],
            },
        ],
    },
    "respirador": {
        "type_key": "respirador",
        "template_key": "respirador_v1",
        "template_version": "1.0.0",
        "display_name": "Respirador",
        "references": [
            {
                "ref_id": "REF-01",
                "tipo": "norma",
                "titulo": "ISO 80601-2-12:2020",
                "edicion": "2020",
                "anio": 2020,
                "organismo_o_fabricante": "ISO",
                "url": "https://www.iso.org/cms/render/live/en/sites/isoorg/contents/data/standard/07/20/72069.html",
                "aplica_a": "Ventiladores de cuidados críticos",
            },
        ],
        "sections": [
            {
                "id": "ventilacion",
                "title": "Variables ventilatorias",
                "items": [
                    {
                        "key": "resp_presion_via_aerea",
                        "label": "Presión en vía aérea (PIP/IPAP/EPAP/PEEP)",
                        "target": "Dentro de tolerancia del fabricante",
                        "unit": "cmH2O",
                        "ref_ids": ["REF-01"],
                    },
                    {
                        "key": "resp_volumen_tidal",
                        "label": "Volumen tidal entregado",
                        "target": "Dentro de tolerancia del fabricante",
                        "unit": "mL",
                        "ref_ids": ["REF-01"],
                    },
                ],
            },
            {
                "id": "alarmas",
                "title": "Alarmas de seguridad",
                "items": [
                    {
                        "key": "resp_alarma_apnea",
                        "label": "Alarma de apnea",
                        "target": "Activa conforme configuración",
                        "unit": "",
                        "ref_ids": ["REF-01"],
                    },
                    {
                        "key": "resp_alarma_alta_presion",
                        "label": "Alarma de alta presión",
                        "target": "Activa conforme umbral configurado",
                        "unit": "",
                        "ref_ids": ["REF-01"],
                    },
                ],
            },
        ],
    },
    "cpap_autocpap": {
        "type_key": "cpap_autocpap",
        "template_key": "cpap_autocpap_v1",
        "template_version": "1.0.0",
        "display_name": "CPAP / AutoCPAP",
        "references": [
            {
                "ref_id": "REF-01",
                "tipo": "norma",
                "titulo": "ISO 80601-2-70:2025",
                "edicion": "2025",
                "anio": 2025,
                "organismo_o_fabricante": "ISO",
                "url": "https://www.iso.org/standard/87160.html",
                "aplica_a": "Equipos de terapia de apnea del sueño",
            },
        ],
        "sections": [
            {
                "id": "presion",
                "title": "Presión terapéutica",
                "items": [
                    {
                        "key": "cpap_presion_setpoint",
                        "label": "Presión real vs setpoint",
                        "target": "Dentro de tolerancia del fabricante",
                        "unit": "cmH2O",
                        "ref_ids": ["REF-01"],
                    },
                    {
                        "key": "cpap_rampa",
                        "label": "Función rampa",
                        "target": "Transición progresiva según configuración",
                        "unit": "",
                        "ref_ids": ["REF-01"],
                    },
                ],
            },
            {
                "id": "eventos",
                "title": "Algoritmo y compensaciones",
                "items": [
                    {
                        "key": "cpap_compensacion_fuga",
                        "label": "Compensación de fuga",
                        "target": "Respuesta acorde al diseño del fabricante",
                        "unit": "",
                        "ref_ids": ["REF-01"],
                    },
                    {
                        "key": "cpap_respuesta_auto",
                        "label": "Respuesta en modo Auto",
                        "target": "Ajuste de presión según evento detectado",
                        "unit": "",
                        "ref_ids": ["REF-01"],
                    },
                ],
            },
        ],
    },
    "bpap": {
        "type_key": "bpap",
        "template_key": "bpap_v1",
        "template_version": "1.0.0",
        "display_name": "BPAP",
        "references": [
            {
                "ref_id": "REF-01",
                "tipo": "norma",
                "titulo": "ISO 80601-2-80:2024",
                "edicion": "2024",
                "anio": 2024,
                "organismo_o_fabricante": "ISO",
                "url": "https://www.iso.org/standard/83466.html",
                "aplica_a": "Equipos de soporte ventilatorio para insuficiencia respiratoria",
            },
        ],
        "sections": [
            {
                "id": "bilevel",
                "title": "Parámetros bi-nivel",
                "items": [
                    {
                        "key": "bpap_ipap_epap",
                        "label": "IPAP/EPAP reales vs configuradas",
                        "target": "Dentro de tolerancia del fabricante",
                        "unit": "cmH2O",
                        "ref_ids": ["REF-01"],
                    },
                    {
                        "key": "bpap_ps",
                        "label": "Soporte de presión (PS)",
                        "target": "Consistente con configuración",
                        "unit": "cmH2O",
                        "ref_ids": ["REF-01"],
                    },
                ],
            },
            {
                "id": "temporizacion",
                "title": "Trigger/cycle y respaldo",
                "items": [
                    {
                        "key": "bpap_trigger_cycle",
                        "label": "Sensibilidad trigger y cycle",
                        "target": "Respuesta estable y sin auto-disparo",
                        "unit": "",
                        "ref_ids": ["REF-01"],
                    },
                    {
                        "key": "bpap_backup_rate",
                        "label": "Frecuencia de respaldo",
                        "target": "Dentro de tolerancia del fabricante",
                        "unit": "rpm",
                        "ref_ids": ["REF-01"],
                    },
                ],
            },
        ],
    },
    "alto_flujo": {
        "type_key": "alto_flujo",
        "template_key": "alto_flujo_v1",
        "template_version": "1.0.0",
        "display_name": "Alto flujo",
        "references": [
            {
                "ref_id": "REF-01",
                "tipo": "norma",
                "titulo": "ISO 80601-2-74:2021",
                "edicion": "2021",
                "anio": 2021,
                "organismo_o_fabricante": "ISO",
                "url": "https://www.iso.org/standard/81613.html",
                "aplica_a": "Sistemas de terapia respiratoria de alto flujo con humidificación activa",
            }
        ],
        "sections": [
            {
                "id": "parametros_terapia",
                "title": "Parámetros de terapia",
                "items": [
                    {
                        "key": "af_flujo_setpoint",
                        "label": "Flujo real vs setpoint",
                        "target": "Dentro de tolerancia del fabricante",
                        "unit": "L/min",
                        "ref_ids": ["REF-01"],
                    },
                    {
                        "key": "af_fio2_setpoint",
                        "label": "FiO2 real vs setpoint",
                        "target": "Dentro de tolerancia del fabricante",
                        "unit": "%",
                        "ref_ids": ["REF-01"],
                    },
                    {
                        "key": "af_temperatura_salida",
                        "label": "Temperatura del gas entregado",
                        "target": "Dentro del rango configurado y tolerancia del fabricante",
                        "unit": "°C",
                        "ref_ids": ["REF-01"],
                    },
                ],
            },
            {
                "id": "alarmas_seguridad",
                "title": "Alarmas y seguridad",
                "items": [
                    {
                        "key": "af_alarma_desconexion",
                        "label": "Alarma de desconexión/circuito abierto",
                        "target": "Activa según especificación",
                        "unit": "",
                        "ref_ids": ["REF-01"],
                    },
                    {
                        "key": "af_alarma_fio2_baja",
                        "label": "Alarma de FiO2 fuera de rango",
                        "target": "Activa según especificación",
                        "unit": "",
                        "ref_ids": ["REF-01"],
                    },
                    {
                        "key": "af_alarma_sobretemperatura",
                        "label": "Alarma de sobretemperatura",
                        "target": "Activa según especificación",
                        "unit": "",
                        "ref_ids": ["REF-01"],
                    },
                ],
            },
        ],
    },
    "calentador_humidificador": {
        "type_key": "calentador_humidificador",
        "template_key": "calentador_humidificador_v1",
        "template_version": "1.0.0",
        "display_name": "Calentador humidificador",
        "references": [
            {
                "ref_id": "REF-01",
                "tipo": "norma",
                "titulo": "ISO 80601-2-74:2021",
                "edicion": "2021",
                "anio": 2021,
                "organismo_o_fabricante": "ISO",
                "url": "https://www.iso.org/standard/81613.html",
                "aplica_a": "Equipos de humidificación respiratoria",
            }
        ],
        "sections": [
            {
                "id": "termico",
                "title": "Control térmico",
                "items": [
                    {
                        "key": "hum_temp_salida",
                        "label": "Temperatura de salida",
                        "target": "Dentro de rango configurable y tolerancia del fabricante",
                        "unit": "C",
                        "ref_ids": ["REF-01"],
                    },
                    {
                        "key": "hum_temp_placa",
                        "label": "Temperatura de placa/calefactor",
                        "target": "Estable según setpoint",
                        "unit": "C",
                        "ref_ids": ["REF-01"],
                    },
                ],
            },
            {
                "id": "alarmas",
                "title": "Alarmas y protecciones",
                "items": [
                    {
                        "key": "hum_alarma_sobretemp",
                        "label": "Alarma de sobretemperatura",
                        "target": "Activa según diseño del fabricante",
                        "unit": "",
                        "ref_ids": ["REF-01"],
                    },
                    {
                        "key": "hum_alarma_falta_agua",
                        "label": "Alarma de nivel/cámara",
                        "target": "Activa según diseño del fabricante",
                        "unit": "",
                        "ref_ids": ["REF-01"],
                    },
                ],
            },
        ],
    },
}


TYPE_ALIASES: dict[str, list[str]] = {
    "aspirador": [
        "aspirador",
        "aspirador quirúrgico",
        "aspirador a bateria",
        "bomba de aspiración",
        "suctor",
    ],
    "concentrador_oxigeno": [
        "concentrador de oxígeno",
        "concentrador oxígeno",
        "concentrador fijo",
    ],
    "concentrador_portatil_oxigeno": [
        "concentrador portátil de oxígeno",
        "concentrador portátil",
        "concentrador portable",
        "poc",
    ],
    "respirador": [
        "respirador",
        "ventilador",
        "ventilador mecánico",
    ],
    "cpap_autocpap": [
        "cpap",
        "autocpap",
        "auto cpap",
        "cpap/autocpap",
    ],
    "bpap": [
        "bpap",
        "bi-level",
        "bilevel",
        "bipap",
    ],
    "alto_flujo": [
        "alto flujo",
        "dispositivo de alto flujo",
        "canula nasal de alto flujo",
        "cánula nasal de alto flujo",
        "hfnc",
    ],
    "calentador_humidificador": [
        "calentador humidificador",
        "humidificador",
        "humidificador calentado",
    ],
}


MODEL_OVERRIDES: list[dict[str, Any]] = [
    {
        "name": "covidien_pb560_ch6_performance_verification",
        "type_key": "respirador",
        "match": {
            "marca_contains": "covidien",
            "modelo_contains": "pb 560",
        },
        "set_fields": {
            "template_key": "respirador_pb560_v1",
            "template_version": "1.0.0",
            "display_name": "Respirador Covidien PB 560",
            "default_instrumentos": (
                "VentMeter pneumatic calibration analyzer\n"
                "Circuito dual-limb adulto + valvula exhalatoria\n"
                "Pulmon de prueba 1.0 L y 4 L\n"
                "Tubo patron 22 mm, shell de calibracion, tuberia 1/8, 3/16 y 7/32, acoples y tee\n"
                "Multimetro digital, fuente DC externa >= 2 A, cable de test DC"
            ),
            "sections": [
                {
                    "id": "pb560_precondiciones",
                    "title": "Precondiciones de verificacion (Cap. 6)",
                    "entry_mode": "result_only",
                    "items": [
                        {
                            "key": "pb560_pre_sin_paciente",
                            "label": "Seguridad: ensayos sin paciente conectado",
                            "target": "Cumple durante toda la Performance Verification",
                            "unit": "",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_pre_warmup",
                            "label": "Warm-up de equipo e instrumento",
                            "target": (
                                "Ventilador y VentMeter energizados >=10 min; en Measurements Check, "
                                "blower a maxima con shell previo al test"
                            ),
                            "unit": "",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_pre_setup",
                            "label": "SETUP y preferencias requeridas",
                            "target": (
                                "ENGLISH (US), fecha/hora actual, Key Sound=Accept tone, "
                                "Pressure Unit=cmH2O, relative pressure=YES"
                            ),
                            "unit": "",
                            "ref_ids": ["REF-01"],
                        },
                    ],
                },
                {
                    "id": "pb560_mediciones",
                    "title": "Measurements Check (Table 6-3)",
                    "entry_mode": "measured_only",
                    "items": [
                        {
                            "key": "pb560_24v_check",
                            "label": "24 V check",
                            "target": "23.5 V a 24.5 V",
                            "unit": "V",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_watchdog",
                            "label": "Watchdog",
                            "target": "23.5 V a 24.5 V",
                            "unit": "V",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_baro",
                            "label": "Barometric pressure",
                            "target": "Lectura del ventilador dentro de ±11 mmHg respecto de VentMeter",
                            "unit": "mmHg",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_temp_interna",
                            "label": "Internal temperature",
                            "target": "30 C a 55 C",
                            "unit": "C",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_temp_blower",
                            "label": "Blower temperature",
                            "target": "35 C a 65 C",
                            "unit": "C",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_buzzer",
                            "label": "Buzzer principal",
                            "target": "Beep largo + tension 1.7 V a 2.1 V",
                            "unit": "V",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_backup_buzzer",
                            "label": "Back-up buzzer",
                            "target": "Beep largo audible",
                            "unit": "",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_supplier_bateria",
                            "label": "Supplier bateria interna",
                            "target": "Approved supplier mostrado en Internal battery menu",
                            "unit": "",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_cap_teorica",
                            "label": "Theoretical capacity bateria",
                            "target": "4800 mAh",
                            "unit": "mAh",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_cap_min",
                            "label": "Capacity bateria",
                            "target": ">= 3840 mAh",
                            "unit": "mAh",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_bat_v",
                            "label": "Battery voltage",
                            "target": "23.5 V a 29.7 V",
                            "unit": "V",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_bat_temp",
                            "label": "Battery temperature",
                            "target": "0 C a 40 C",
                            "unit": "C",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_altitude_comp",
                            "label": "Altitude Compensation",
                            "target": "YES (obligatorio para calculo correcto de volumen a toda elevacion)",
                            "unit": "",
                            "ref_ids": ["REF-01"],
                        },
                    ],
                },
                {
                    "id": "pb560_calibraciones",
                    "title": "Calibraciones de sensores",
                    "entry_mode": "measured_only",
                    "items": [
                        {
                            "key": "pb560_pp_zero",
                            "label": "Paciente pressure sensor - cero",
                            "target": "VentMeter low pressure en 0.0 ± 0.1 cmH2O",
                            "unit": "cmH2O",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_pp_cal",
                            "label": "Patient pressure calibration point",
                            "target": "Ajuste en 39.80 a 40.20 cmH2O",
                            "unit": "cmH2O",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_pp_verif",
                            "label": "Patient pressure verificacion post-calibracion",
                            "target": "39.60 a 40.40 cmH2O",
                            "unit": "cmH2O",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_insp_flow_cal",
                            "label": "Inspiratory flow calibration (8 puntos)",
                            "target": (
                                "0±0.10; 4.90-5.10; 11.76-12.24; 19.6-20.4; 36.26-37.74; "
                                "58.8-61.2; 88.2-91.8; 127.4-132.6"
                            ),
                            "unit": "slpm",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_insp_flow_verif",
                            "label": "Inspiratory flow verificacion (8 puntos)",
                            "target": (
                                "0±0.10; 4.50-5.50; 11.1-12.9; 19.0-21.0; 35.1-38.9; "
                                "57.0-63.0; 85.5-94.5; 123.5-136.5"
                            ),
                            "unit": "slpm",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_exh_flow_verif",
                            "label": "Exhalation flow verificacion (8 puntos)",
                            "target": (
                                "0±0.10; 4.50-5.50; 11.1-12.9; 19.0-21.0; 35.1-38.9; "
                                "57.0-63.0; 85.5-94.5; 123.5-136.5"
                            ),
                            "unit": "slpm",
                            "ref_ids": ["REF-01"],
                        },
                    ],
                },
                {
                    "id": "pb560_funcionales",
                    "title": "Pruebas funcionales (6.8)",
                    "entry_mode": "measured_only",
                    "items": [
                        {
                            "key": "pb560_flow_capacity",
                            "label": "Flow sensor capacity",
                            "target": "Inspiratory flow > 145 slpm",
                            "unit": "slpm",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_turbine_peak",
                            "label": "Turbine performance - pico de presion",
                            "target": "> 70 cmH2O con orificio bloqueado (max 3 s)",
                            "unit": "cmH2O",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_turbine_return",
                            "label": "Turbine performance - retorno",
                            "target": "0 ± 0.5 cmH2O luego de desbloquear",
                            "unit": "cmH2O",
                            "ref_ids": ["REF-01"],
                        },
                    ],
                },
                {
                    "id": "pb560_accuracy",
                    "title": "Breath delivery accuracy (6.9)",
                    "entry_mode": "measured_only",
                    "items": [
                        {
                            "key": "pb560_adult_vol",
                            "label": "Adult volume accuracy (Vt 500 mL)",
                            "target": "VTI en menu alarmas: 440 mL a 560 mL",
                            "unit": "mL",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_adult_vol_pts",
                            "label": "Adult volume vs VentMeter",
                            "target": "Dentro de ±(8% + 10 mL) respecto de VentMeter",
                            "unit": "mL",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_adult_press",
                            "label": "Adult pressure accuracy (Pi 20 cmH2O)",
                            "target": "Pi en menu alarmas: 17 cmH2O a 23 cmH2O",
                            "unit": "cmH2O",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_adult_press_pts",
                            "label": "Adult pressure vs VentMeter",
                            "target": "Dentro de ±(8.75% + 2.04 cmH2O) respecto de VentMeter",
                            "unit": "cmH2O",
                            "ref_ids": ["REF-01"],
                        },
                    ],
                },
                {
                    "id": "pb560_alarmas_interfaces",
                    "title": "Alarmas e interfaces",
                    "entry_mode": "result_only",
                    "items": [
                        {
                            "key": "pb560_alarm_ac_disconnect",
                            "label": "AC power disconnection alarm",
                            "target": (
                                "Alarma media + LED amarillo + mensaje AC POWER DISCONNECTION; "
                                "auto-reset al reconectar AC"
                            ),
                            "unit": "",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_alarm_patient_disconnect",
                            "label": "Patient disconnect alarm",
                            "target": (
                                "Alarma alta en <=15 s + LED rojo + mensaje LOW PRESSURE DISCONNECT; "
                                "auto-reset al reconectar"
                            ),
                            "unit": "",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_power_off_while_vent",
                            "label": "Power off while ventilating",
                            "target": "VHP continuo >=120 s y reanudacion inmediata de ventilacion al reencender",
                            "unit": "",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_default_settings",
                            "label": "Default settings (Tables 6-4, 6-5, 6-6)",
                            "target": "Parametros, alarmas y preferencias en valores por defecto; fecha y hora actuales",
                            "unit": "",
                            "ref_ids": ["REF-01"],
                        },
                        {
                            "key": "pb560_reset_hours",
                            "label": "Reset patient hours",
                            "target": "Contador reiniciado a 00000 h y 00 min",
                            "unit": "",
                            "ref_ids": ["REF-01"],
                        },
                    ],
                },
            ],
        },
        "references": [
            {
                "ref_id": "REF-PB560-CH6",
                "tipo": "manual_fabricante",
                "titulo": "Puritan Bennett 560 Ventilator Service Manual - Chapter 6 Performance Verification",
                "edicion": "2017",
                "anio": 2017,
                "organismo_o_fabricante": "Covidien",
                "url": "",
                "aplica_a": "Respirador Covidien PB 560",
            }
        ],
        "append_ref_to_all_items": "REF-PB560-CH6",
    }
]


def _norm(value: str) -> str:
    s = (value or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9\s/+-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _safe_json_doc(value: Any, default: Any):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="ignore")
        except Exception:
            return default
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return default
        try:
            return json.loads(raw)
        except Exception:
            return default
    return default


def _has_protocol_catalog_table() -> bool:
    try:
        with connection.cursor() as cur:
            if connection.vendor == "postgresql":
                cur.execute(
                    """
                    SELECT 1
                      FROM information_schema.tables
                     WHERE table_name='test_protocol_templates'
                       AND table_schema = ANY(current_schemas(true))
                     LIMIT 1
                    """
                )
            elif connection.vendor == "sqlite":
                cur.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='test_protocol_templates' LIMIT 1"
                )
            else:
                cur.execute(
                    """
                    SELECT 1
                      FROM information_schema.tables
                     WHERE table_name='test_protocol_templates'
                     LIMIT 1
                    """
                )
            return cur.fetchone() is not None
    except Exception:
        return False


def _fetch_active_protocol_docs_from_db() -> list[dict[str, Any]]:
    if not _has_protocol_catalog_table():
        return []
    where_sql = "WHERE active = TRUE" if connection.vendor == "postgresql" else "WHERE active = 1"
    try:
        with connection.cursor() as cur:
            cur.execute(
                f"""
                SELECT type_key, template_key, doc
                  FROM test_protocol_templates
                  {where_sql}
                 ORDER BY type_key ASC, id ASC
                """
            )
            rows = cur.fetchall() or []
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for type_key, template_key, raw_doc in rows:
        doc = _safe_json_doc(raw_doc, {})
        if not isinstance(doc, dict):
            continue
        doc = copy.deepcopy(doc)
        if not doc.get("type_key"):
            doc["type_key"] = type_key
        if not doc.get("template_key"):
            doc["template_key"] = template_key
        if not isinstance(doc.get("aliases"), list):
            doc["aliases"] = []
        if not isinstance(doc.get("references"), list):
            doc["references"] = []
        if not isinstance(doc.get("sections"), list):
            doc["sections"] = []
        if not isinstance(doc.get("overrides"), list):
            doc["overrides"] = []
        out.append(doc)
    return out


def _resolve_type_key_from_alias_map(tipo_equipo: str, alias_map: dict[str, list[str]]) -> str:
    raw = _norm(tipo_equipo)
    if not raw:
        return ""
    # Prefer exact alias match first.
    for key, aliases in alias_map.items():
        for alias in aliases:
            if raw == _norm(alias):
                return key
    # Then fallback to "contains" matching, longest aliases first.
    contains_candidates: list[tuple[int, str, str]] = []
    for key, aliases in alias_map.items():
        for alias in aliases:
            a = _norm(alias)
            if not a:
                continue
            if a in raw:
                contains_candidates.append((len(a), key, a))
    if contains_candidates:
        contains_candidates.sort(reverse=True)
        return contains_candidates[0][1]
    return ""


def resolve_type_key(tipo_equipo: str) -> str:
    db_docs = _fetch_active_protocol_docs_from_db()
    if db_docs:
        alias_map: dict[str, list[str]] = {}
        for doc in db_docs:
            type_key = str(doc.get("type_key") or "").strip().lower()
            if not type_key:
                continue
            aliases = doc.get("aliases") if isinstance(doc.get("aliases"), list) else []
            merged = [type_key, doc.get("display_name") or "", *aliases]
            alias_map[type_key] = [str(a or "").strip() for a in merged if str(a or "").strip()]
        resolved = _resolve_type_key_from_alias_map(tipo_equipo, alias_map)
        if resolved:
            return resolved
    return _resolve_type_key_from_alias_map(tipo_equipo, TYPE_ALIASES)


def _add_reference(protocol: dict[str, Any], ref: dict[str, Any]) -> None:
    ref_id = (ref or {}).get("ref_id")
    if not ref_id:
        return
    refs = protocol.setdefault("references", [])
    exists = any((r.get("ref_id") == ref_id) for r in refs)
    if not exists:
        refs.append(copy.deepcopy(ref))


def _append_ref_to_all_items(protocol: dict[str, Any], ref_id: str) -> None:
    if not ref_id:
        return
    for section in protocol.get("sections", []) or []:
        for item in section.get("items", []) or []:
            refs = item.setdefault("ref_ids", [])
            if ref_id not in refs:
                refs.append(ref_id)


def _append_item_refs(protocol: dict[str, Any], item_ref_ids: dict[str, list[str]]) -> None:
    if not item_ref_ids:
        return
    for section in protocol.get("sections", []) or []:
        for item in section.get("items", []) or []:
            key = item.get("key")
            if not key:
                continue
            extra = item_ref_ids.get(key) or []
            refs = item.setdefault("ref_ids", [])
            for ref_id in extra:
                if ref_id and ref_id not in refs:
                    refs.append(ref_id)


def _match_override(override: dict[str, Any], marca: str, modelo: str) -> bool:
    match = override.get("match") or {}
    marca_contains = _norm(match.get("marca_contains") or "")
    modelo_contains = _norm(match.get("modelo_contains") or "")
    marca_n = _norm(marca)
    modelo_n = _norm(modelo)
    marca_compact = re.sub(r"[^a-z0-9]", "", marca_n)
    modelo_compact = re.sub(r"[^a-z0-9]", "", modelo_n)
    marca_contains_compact = re.sub(r"[^a-z0-9]", "", marca_contains)
    modelo_contains_compact = re.sub(r"[^a-z0-9]", "", modelo_contains)
    if marca_contains and marca_contains not in marca_n and marca_contains_compact not in marca_compact:
        return False
    if modelo_contains and modelo_contains not in modelo_n and modelo_contains_compact not in modelo_compact:
        return False
    return True


def _apply_overrides(
    protocol: dict[str, Any],
    marca: str,
    modelo: str,
    overrides: list[dict[str, Any]] | None = None,
) -> None:
    type_key = protocol.get("type_key")
    applied = []
    source = overrides if overrides is not None else MODEL_OVERRIDES
    ordered = sorted(
        [ov for ov in (source or []) if isinstance(ov, dict)],
        key=lambda ov: int(ov.get("priority") if ov.get("priority") is not None else 0),
    )
    for override in ordered:
        if "type_key" in override and override.get("type_key") != type_key:
            continue
        if not bool(override.get("active", True)):
            continue
        if not _match_override(override, marca, modelo):
            continue
        set_fields = override.get("set_fields") or {}
        if isinstance(set_fields, dict):
            for field, value in set_fields.items():
                protocol[field] = copy.deepcopy(value)
        for ref in override.get("references") or []:
            _add_reference(protocol, ref)
        _append_ref_to_all_items(protocol, (override.get("append_ref_to_all_items") or "").strip())
        _append_item_refs(protocol, override.get("item_ref_ids") or {})
        applied.append(override.get("name"))
    protocol["applied_overrides"] = [x for x in applied if x]


def get_protocol_by_type_key(type_key: str, marca: str = "", modelo: str = "") -> dict[str, Any] | None:
    type_key_n = str(type_key or "").strip().lower()
    if not type_key_n:
        return None

    db_docs = _fetch_active_protocol_docs_from_db()
    for doc in db_docs:
        if str(doc.get("type_key") or "").strip().lower() != type_key_n:
            continue
        protocol = copy.deepcopy(doc)
        if not protocol.get("type_key"):
            protocol["type_key"] = type_key_n
        overrides = copy.deepcopy(protocol.pop("overrides", []))
        protocol.pop("aliases", None)
        _apply_overrides(protocol, marca=marca, modelo=modelo, overrides=overrides)
        protocol["result_options"] = copy.deepcopy(RESULT_OPTIONS)
        protocol["global_result_options"] = copy.deepcopy(GLOBAL_RESULT_OPTIONS)
        return protocol

    base = BASE_TEMPLATES.get(type_key_n)
    if not base:
        return None
    protocol = copy.deepcopy(base)
    _apply_overrides(protocol, marca=marca, modelo=modelo)
    protocol["result_options"] = copy.deepcopy(RESULT_OPTIONS)
    protocol["global_result_options"] = copy.deepcopy(GLOBAL_RESULT_OPTIONS)
    return protocol


def get_protocol_by_template_key(template_key: str, marca: str = "", modelo: str = "") -> dict[str, Any] | None:
    needle = (template_key or "").strip().lower()
    if not needle:
        return None

    db_docs = _fetch_active_protocol_docs_from_db()
    if db_docs:
        type_keys = [str(doc.get("type_key") or "").strip().lower() for doc in db_docs if doc.get("type_key")]
        type_keys.extend([str(key).strip().lower() for key in BASE_TEMPLATES.keys()])
    else:
        type_keys = [str(key).strip().lower() for key in BASE_TEMPLATES.keys()]

    seen = set()
    for type_key in type_keys:
        if not type_key or type_key in seen:
            continue
        seen.add(type_key)
        protocol = get_protocol_by_type_key(type_key, marca=marca, modelo=modelo)
        if not protocol:
            continue
        if (protocol.get("template_key") or "").strip().lower() == needle:
            return protocol
    return None


def resolve_protocol_for_equipo(tipo_equipo: str, marca: str = "", modelo: str = "") -> dict[str, Any] | None:
    type_key = resolve_type_key(tipo_equipo)
    if not type_key:
        return None
    return get_protocol_by_type_key(type_key, marca=marca, modelo=modelo)


def flatten_items(protocol: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for section in (protocol or {}).get("sections", []) or []:
        for item in section.get("items", []) or []:
            out.append(item)
    return out


def default_values_for_protocol(protocol: dict[str, Any]) -> dict[str, dict[str, str]]:
    defaults: dict[str, dict[str, str]] = {}
    for item in flatten_items(protocol):
        key = (item.get("key") or "").strip()
        if not key:
            continue
        defaults[key] = {
            "valor_a_medir": "",
            "measured": "",
            "result": "",
            "observaciones": "",
        }
    return defaults


def build_seed_protocol_documents() -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for type_key, base in BASE_TEMPLATES.items():
        doc = copy.deepcopy(base)
        doc["type_key"] = str(doc.get("type_key") or type_key).strip().lower()
        doc["aliases"] = copy.deepcopy(TYPE_ALIASES.get(type_key) or [])
        doc["overrides"] = []
        for idx, override in enumerate(MODEL_OVERRIDES):
            if str(override.get("type_key") or "").strip().lower() != doc["type_key"]:
                continue
            item = copy.deepcopy(override)
            item["active"] = bool(item.get("active", True))
            item["priority"] = int(item.get("priority", idx))
            doc["overrides"].append(item)
        doc["active"] = True
        docs.append(doc)
    return docs


