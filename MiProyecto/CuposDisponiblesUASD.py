# -*- coding: utf-8 -*-
import os
import sys
import json
import time
import random
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter, Retry
import pandas as pd

BASE_URL = "https://app.uasd.edu.do/ProgramacionPorAsignatura"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/json; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://app.uasd.edu.do",
    "Referer": f"{BASE_URL}/",
}

CLAVES_POR_DEFECTO = [
    "IDI0140", "INF3220", "INF5190", "INF5180",
    "MAT3600", "MAT3940", "INF5220", "INF5230",
]

MAPEO_COLUMNAS = {
    "NRC": "nrc",
    "Clave": "clave",
    "Asignatura": "asignatura",
    "Sec": "seccion",
    "Tipo": "tipo",
    "Horario": "horario",
    "Días": "dias",
    "Edificios": "edificio",
    "Aula": "aula",
    "Campus": "campus",
    "Modalidad": "modalidad",
    "Cupos": "cupos",
    "Inscritos": "inscritos",
    "Disponibles": "disponibles",
    "Profesor(a)": "nombre",
}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def crear_sesion():
    session = requests.Session()
    retries = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["POST"],
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers.update(HEADERS)
    return session


def _post_json(session, endpoint, payload=None):
    url = f"{BASE_URL}/Default.aspx/{endpoint}"
    resp = session.post(url, json=payload or {}, timeout=20)
    resp.raise_for_status()
    exterior = resp.json()
    return json.loads(exterior["d"])


def obtener_campus(session):
    return _normalizar_filas(_post_json(session, "getCampus"))


def obtener_data(session, clave, campus):
    """Devuelve siempre una lista de dicts, incluso si la API devuelve null,
    un solo objeto suelto, o un arreglo con elementos null mezclados (esto
    último ocurrió en vivo: alguna sección sin datos completos venía como
    'null' dentro del arreglo y rompía el script)."""
    datos = _post_json(session, "getData", {"campus": campus, "clave": clave})
    return _normalizar_filas(datos)


def _normalizar_filas(datos):
    """Convierte cualquier respuesta de la API a una lista limpia de dicts,
    descartando null, valores sueltos, o elementos corruptos dentro del arreglo."""
    if datos is None:
        return []
    if isinstance(datos, dict):
        return [datos]
    if isinstance(datos, list):
        return [f for f in datos if isinstance(f, dict)]
    return []


# --------------------------------------------------------------------------
# Consulta por clave
# --------------------------------------------------------------------------
def _clave_unica(fila):
    return (fila.get("nrc"), fila.get("seccion"), fila.get("horario"),
            fila.get("dias"), fila.get("aula"))


def consultar_rapido(session, clave):
    """Modo antiguo: una sola petición con campus='000'. Puede venir incompleto."""
    try:
        return obtener_data(session, clave, "000")
    except Exception as e:
        print(f"   (campus='000' falló para {clave}: {e})")
        return []


def consultar_exhaustivo(session, clave, campus_reales, hilos=6, pausa=(0.15, 0.35), verbose=False):
    """Modo garantizado: recorre TODOS los campus reales en paralelo y combina sin duplicados."""
    vistos = set()
    combinado = []
    campus_con_datos = 0

    def _consultar_uno(campus):
        time.sleep(random.uniform(*pausa))  # pequeño jitter para no ráfaguear el servidor
        try:
            return campus["codigo"], obtener_data(session, clave, campus["codigo"]), None
        except Exception as e:
            return campus["codigo"], [], str(e)

    with ThreadPoolExecutor(max_workers=hilos) as executor:
        futuros = [executor.submit(_consultar_uno, c) for c in campus_reales]
        for futuro in as_completed(futuros):
            codigo, filas, error = futuro.result()
            if error:
                print(f"   Error en {clave} / {codigo}: {error}")
                continue
            if filas:
                campus_con_datos += 1
                if verbose:
                    print(f"      {codigo}: {len(filas)} secciones")
            for f in filas:
                if not isinstance(f, dict):
                    continue  # protección extra ante datos corruptos de la API
                cu = _clave_unica(f)
                if cu not in vistos:
                    vistos.add(cu)
                    combinado.append(f)

    if verbose:
        print(f"   ({campus_con_datos} campus con al menos 1 sección)")
    return combinado


# --------------------------------------------------------------------------
# Menú interactivo
# --------------------------------------------------------------------------
COMANDOS_SALIR = {"salir", "exit", "quit", "q", "0"}


def _salir():
    print("\nSaliendo del programa. ¡Hasta luego!")
    sys.exit(0)


def _pedir(prompt, permitir_salir=True):
    """input() con soporte para escribir 'salir'/'0' y cerrar el programa en cualquier momento."""
    try:
        resp = input(prompt)
    except (EOFError, KeyboardInterrupt):
        _salir()
    resp = resp.strip()
    if permitir_salir and resp.lower() in COMANDOS_SALIR:
        _salir()
    return resp


def _leer_lineas_hasta_vacio():
    print("Pega o escribe las claves, una por línea (ej: MAT2330).")
    print("Cuando termines, deja una línea vacía y presiona Enter.")
    print("(Escribe 'salir' en cualquier momento para cerrar el programa)\n")
    lineas = []
    while True:
        linea = _pedir("")
        if linea == "":
            break
        lineas.append(linea)
    return lineas


def _leer_claves_de_archivo(ruta):
    """Lee claves desde un .txt. Devuelve:
        None  -> el archivo no existe o no se pudo leer
        []    -> el archivo existe pero no contiene ninguna clave
        [...] -> lista de claves encontradas
    Tolera BOM de Notepad, distintas codificaciones (utf-8-sig, utf-8, cp1252,
    latin-1) y acepta claves separadas por líneas, comas o punto y coma.
    """
    ruta = os.path.normpath(ruta.strip().strip('"').strip("'"))

    if not os.path.isfile(ruta):
        print(f"   [!] No encontré el archivo en esa ruta: {ruta}")
        return None

    contenido = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            with open(ruta, encoding=enc) as f:
                contenido = f.read()
            break
        except UnicodeDecodeError:
            continue

    if contenido is None:
        print(f"   [!] No pude leer el archivo (problema de codificación de texto): {ruta}")
        return None

    # separar por saltos de línea, comas o punto y coma, lo que se haya usado
    import re
    partes = re.split(r"[\r\n,;]+", contenido)
    claves = [p.strip().upper() for p in partes if p.strip()]

    if not claves:
        print(f"   [!] El archivo se encontró pero está vacío (o no tiene texto reconocible): {ruta}")
        return []

    return claves


def _pedir_archivo_con_reintento():
    """Pide la ruta del .txt y reintenta hasta lograrlo o hasta que el usuario
    escriba 'volver' para regresar al menú principal (o 'salir' para cerrar)."""
    while True:
        ruta = _pedir("Ruta del archivo .txt (o 'volver' para regresar al menú): ")
        if ruta.lower() in ("volver", "atras", "atrás", "back", ""):
            return None
        resultado = _leer_claves_de_archivo(ruta)
        if resultado is None or resultado == []:
            print("   Intenta con otra ruta, o escribe 'volver' para regresar al menú.\n")
            continue
        print(f"   ✅ Se cargaron {len(resultado)} claves del archivo.\n")
        return resultado


def menu_interactivo():
    while True:
        print("=" * 60)
        print(" Consulta de Cupos Disponibles - UASD")
        print("=" * 60)
        print("1) Escribir/pegar la lista de claves ahora")
        print("2) Cargar claves desde un archivo .txt (una por línea)")
        print("3) Usar la lista de ejemplo predefinida (8 claves)")
        print("0) Salir")
        print()
        opcion = _pedir("Selecciona una opción [0-3]: ")

        if opcion == "1":
            claves = _leer_lineas_hasta_vacio()
        elif opcion == "2":
            claves = _pedir_archivo_con_reintento()
            if claves is None:
                print()
                continue  # el usuario pidió volver al menú principal
        elif opcion == "3":
            claves = CLAVES_POR_DEFECTO
        else:
            print("Opción inválida, intenta de nuevo.\n")
            continue

        claves = [c.strip().upper() for c in claves if c.strip()]
        if not claves:
            print("No se ingresó ninguna clave válida. Volvamos a intentar.\n")
            continue

        break

    print()
    print("Modo de búsqueda:")
    print("1) Completo (recomendado) - recorre todos los campus, garantiza traer TODO")
    print("2) Rápido - una sola petición 'todos los campus', más veloz pero puede faltar info")
    modo = _pedir("Selecciona una opción [1-2] (Enter = 1): ")
    rapido = (modo == "2")

    print()
    salida = _pedir("Nombre del archivo de salida (Enter = Cupos_Disponibles_UASD.xlsx): ")
    if not salida:
        salida = "Cupos_Disponibles_UASD.xlsx"
    if not salida.lower().endswith(".xlsx"):
        salida += ".xlsx"

    print()
    return claves, rapido, salida


# --------------------------------------------------------------------------
# Programa principal
# --------------------------------------------------------------------------
def guardar_resultado(todas_las_filas, salida):
    if not todas_las_filas:
        print("\nNo se encontraron resultados para ninguna clave.")
        return

    df = pd.DataFrame(todas_las_filas)

    columnas_finales = {}
    for nombre_final, nombre_api in MAPEO_COLUMNAS.items():
        if nombre_api in df.columns:
            columnas_finales[nombre_final] = df[nombre_api]
    df_final = pd.DataFrame(columnas_finales)

    for col in ("Cupos", "Inscritos", "Disponibles"):
        if col in df_final.columns:
            df_final[col] = pd.to_numeric(df_final[col], errors="coerce")

    df_final = df_final.sort_values(["Clave", "Campus", "Sec"]).reset_index(drop=True)

    ruta_csv = salida.replace(".xlsx", ".csv")
    df_final.to_csv(ruta_csv, index=False, encoding="utf-8-sig")
    print(f"\nGuardado: {ruta_csv}  ({len(df_final)} filas)")

    try:
        df_final.to_excel(salida, index=False)
        print(f"Guardado: {salida}  ({len(df_final)} filas)")
    except ModuleNotFoundError:
        print(f"\n[!] No se pudo generar el .xlsx porque falta 'openpyxl'.")
        print(f"    Instálalo con:  pip install openpyxl")
        print(f"    El CSV ya se guardó correctamente en: {ruta_csv}")

    return df_final


def main():
    parser = argparse.ArgumentParser(description="Consulta de cupos disponibles UASD por clave de asignatura")
    parser.add_argument("--claves", type=str, default="",
                         help="Claves separadas por coma (ej: MAT2330,EST2110)")
    parser.add_argument("--claves-archivo", type=str, default="",
                         help="Ruta a un .txt con una clave por línea")
    parser.add_argument("--rapido", action="store_true",
                         help="Usa el modo antiguo campus='000' (1 petición por clave, puede venir incompleto)")
    parser.add_argument("--diagnosticar", action="store_true",
                         help="Corre ambos modos por cada clave y compara cuántas secciones trae cada uno")
    parser.add_argument("--hilos", type=int, default=6,
                         help="Peticiones en paralelo durante el modo exhaustivo (por defecto 6)")
    parser.add_argument("--salida", type=str, default="Cupos_Disponibles_UASD.xlsx",
                         help="Nombre del archivo Excel de salida")
    parser.add_argument("--verbose", action="store_true",
                         help="Muestra el detalle de secciones encontradas por cada campus")
    args = parser.parse_args()

    interactivo = not (args.claves or args.claves_archivo)

    if interactivo:
        claves, rapido, salida = menu_interactivo()
    else:
        if args.claves_archivo:
            claves = _leer_claves_de_archivo(args.claves_archivo)
            if not claves:
                print("No se pudieron cargar claves desde el archivo indicado. Usando la lista por defecto.")
                claves = CLAVES_POR_DEFECTO
        else:
            claves = [c.strip().upper() for c in args.claves.split(",") if c.strip()]
        claves = claves or CLAVES_POR_DEFECTO
        rapido = args.rapido
        salida = args.salida

    session = crear_sesion()

    print("Obteniendo lista de campus...")
    campus_todos = obtener_campus(session)
    campus_reales = [c for c in campus_todos if c["codigo"] != "000"]
    print(f"   {len(campus_reales)} campus reales encontrados.\n")

    todas_las_filas = []
    for clave in claves:
        print(f"Consultando {clave}...")

        if args.diagnosticar:
            filas_rapido = consultar_rapido(session, clave)
            filas_completo = consultar_exhaustivo(session, clave, campus_reales, hilos=args.hilos, verbose=args.verbose)
            diff = len(filas_completo) - len(filas_rapido)
            print(f"   Rápido (000): {len(filas_rapido)} secciones  |  Completo (por campus): {len(filas_completo)} secciones", end="")
            print(f"   ⚠️  Diferencia: {diff} secciones que 'rápido' NO trajo" if diff else "   ✅ Coinciden")
            filas = filas_completo
        elif rapido:
            filas = consultar_rapido(session, clave)
            print(f"   -> {len(filas)} secciones encontradas (modo rápido)")
        else:
            filas = consultar_exhaustivo(session, clave, campus_reales, hilos=args.hilos, verbose=args.verbose)
            print(f"   -> {len(filas)} secciones encontradas (modo completo)")

        todas_las_filas.extend(filas)

    guardar_resultado(todas_las_filas, salida)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrumpido por el usuario. Saliendo...")
        sys.exit(0)