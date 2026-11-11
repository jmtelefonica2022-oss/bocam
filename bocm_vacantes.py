#!/usr/bin/env python3
"""
Vacantes del BOCM (Boletín Oficial de la Comunidad de Madrid).

Lee el boletín del día, localiza las convocatorias de empleo público de TODAS las
Consejerías, organismos, hospitales, universidades y ayuntamientos, y extrae de cada
disposición: cuerpo/escala/categoría, grupo/subgrupo, nivel, plazas, sistema y plazo.

Uso:
    python bocm_vacantes.py                       # último boletín publicado
    python bocm_vacantes.py --fecha 2025-03-18    # boletín de una fecha concreta
    python bocm_vacantes.py --incluir-tramites    # añade listas, nombramientos, etc.

Variables de entorno opcionales:
    ANTHROPIC_API_KEY   -> extrae los datos con Claude (más preciso que las regex)
    BOCM_MODELO         -> modelo de Claude (por defecto claude-haiku-5-5)
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_TO -> envía el resumen por email
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import re
import smtplib
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import pdfplumber
import requests

BASE = "https://www.bocm.es/boletin"
TZ = ZoneInfo("Europe/Madrid")
CVE_RE = re.compile(r"BOCM-(\d{8})-(\d+)")
NO_CONSTA = "No consta en el texto"
PAUSA = 0.4  # segundos entre descargas, para no saturar bocm.es

session = requests.Session()
session.headers["User-Agent"] = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 bocm-vacantes"
)

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


# --------------------------------------------------------------------------- descargas
class ErrorDescarga(Exception):
    pass


def descargar_pdf(url: str, intentos: int = 3) -> bytes | None:
    """Devuelve el PDF, None si no existe, o lanza ErrorDescarga si falla la red."""
    ultimo = ""
    for i in range(intentos):
        try:
            r = session.get(url, timeout=60)
        except requests.RequestException as exc:
            ultimo = str(exc)
            time.sleep(3 * (i + 1))
            continue
        if r.status_code == 404:
            return None
        if r.ok:
            return r.content if r.content[:4] == b"%PDF" else None
        ultimo = f"HTTP {r.status_code}"
        time.sleep(3 * (i + 1))
    raise ErrorDescarga(f"{url}: {ultimo}")


X_TOL = 1.5  # el BOCM usa texto justificado: con el valor por defecto (3) se pierden espacios


def texto_pdf(contenido: bytes, max_paginas: int | None = None) -> str:
    with pdfplumber.open(io.BytesIO(contenido)) as pdf:
        paginas = pdf.pages if max_paginas is None else pdf.pages[:max_paginas]
        return "\n".join((p.extract_text(x_tolerance=X_TOL) or "") for p in paginas)


# --------------------------------------------------------------------------- anexos de puestos
# Equivalencia legal de los grupos antiguos (Ley 1/1986 CM) con los del EBEP (disp. trans. 3ª).
GRUPOS_ANTIGUOS = {"A": "A1", "B": "A2", "C": "C1", "D": "C2", "E": "AP"}


def _lineas(palabras: list[dict], salto: float = 4) -> list[tuple[float, str]]:
    """Agrupa palabras en líneas por su coordenada vertical."""
    lineas: list[list] = []
    for w in sorted(palabras, key=lambda w: (round(w["top"]), w["x0"])):
        if lineas and abs(w["top"] - lineas[-1][0]) <= salto:
            lineas[-1][1].append(w)
        else:
            lineas.append([w["top"], [w]])
    return [(top, " ".join(x["text"] for x in sorted(ws, key=lambda x: x["x0"]))) for top, ws in lineas]


def _bloques(lineas: list[tuple[float, str]], hueco: float = 9) -> list[tuple[float, str]]:
    """Une líneas consecutivas muy próximas (un nombre partido en dos renglones)."""
    res: list[list] = []
    for top, txt in lineas:
        if res and top - res[-1][2] <= hueco:
            res[-1][1] += " " + txt
            res[-1][2] = top
        else:
            res.append([top, txt, top])
    return [(t, s) for t, s, _ in res]


def explicar_grupo(g: str) -> str:
    letras = [x.strip() for x in g.split("/")]
    if letras and all(x in GRUPOS_ANTIGUOS for x in letras):
        return f"{g} (equivale a {'/'.join(GRUPOS_ANTIGUOS[x] for x in letras)})"
    return g


def parsear_anexo_puestos(pdf) -> list[dict]:
    """Lee las tablas 'Puesto/Denominación · Unidad · Grupo · NCD · Específico · Cuerpo…'
    de las convocatorias de libre designación y concurso de méritos."""
    puestos: list[dict] = []
    for page in pdf.pages:
        palabras = page.extract_words(x_tolerance=X_TOL)
        cab = {}
        for w in palabras:
            for clave in ("Grupo", "NCD", "Específico", "Admon", "Unidad", "Cuerpo", "Administración"):
                if w["text"] == clave:
                    cab.setdefault(clave, []).append(w)
        if not ("Grupo" in cab and "NCD" in cab):
            continue
        cabeceras = sorted(cab["NCD"], key=lambda w: w["top"])
        x_uni = cab["Unidad"][0]["x0"] if "Unidad" in cab else 110
        x_gru = cab["Grupo"][0]["x0"]
        x_ncd = cab["NCD"][0]["x0"]
        x_esp = cab["Específico"][0]["x0"] if "Específico" in cab else x_ncd + 20
        x_adm = cab["Admon"][0]["x0"] if "Admon" in cab else page.width
        codigos = [w for w in palabras if w["x0"] < x_uni - 5 and re.fullmatch(r"\d{3,6}", w["text"])
                   and any(w["top"] > h["top"] for h in cabeceras)]
        codigos.sort(key=lambda w: w["top"])
        limites = sorted({h["top"] for h in cabeceras} | {page.height})
        for i, cod in enumerate(codigos):
            fin = min([l for l in limites if l > cod["top"] + 5] +
                      ([codigos[i + 1]["top"] - 3] if i + 1 < len(codigos) else []))
            zona = [w for w in palabras if cod["top"] - 4 <= w["top"] < fin and w is not cod]
            loc = next((w["top"] for w in zona if w["text"].startswith("Localidad")), fin)
            perf = next((w["top"] for w in zona if w["text"] == "PERFIL"), fin)
            tabla = [w for w in zona if w["top"] < loc - 2]
            num = re.compile(r"[\d.]+,\d{2}")
            col = lambda a, b: [w for w in tabla if a <= w["x0"] < b]
            especifico = [w for w in tabla if num.fullmatch(w["text"])]
            x_cuerpo = (max(w["x1"] for w in especifico) + 3) if especifico else x_esp + 35
            cuerpo_ws = [w for w in col(x_cuerpo, x_adm - 5) if w not in especifico]
            fila1 = [w for w in cuerpo_ws if abs(w["top"] - cod["top"]) <= 4]
            resto = [w for w in cuerpo_ws if w not in fila1]
            admon = _lineas(col(x_adm - 5, page.width))
            cuerpos = []
            for top, nombre in _bloques(_lineas(resto)):
                letra = next((t for tp, t in admon if abs(tp - top) <= 4), "")
                cuerpos.append(titulo(nombre) + (f" (Admon {letra})" if letra else ""))
            linea = lambda pref: next((t.split(":", 1)[-1].strip() for tp, t in _lineas(zona)
                                       if t.startswith(pref)), "")
            perfil = []
            for tp, t in _lineas([w for w in zona if w["top"] > perf + 2 and w["text"] != "PERFIL"]):
                if (not es_mayusculas(t) or re.search(r"BOCM|bocm\.es|BOLET[ÍI]N|ISSN|^\(|^\d+-\d+|Administraci[oó]n P[uú]blica$", t)):
                    break  # pie de página o cabecera de la tabla siguiente
                perfil.append(t)
            puestos.append({
                "codigo": cod["text"],
                "denominacion": " ".join(t for _, t in _lineas(col(0, x_uni - 5))).replace(cod["text"], "").strip().capitalize(),
                "unidad": titulo(" ".join(t for _, t in _lineas(col(x_uni - 5, x_gru - 8)))),
                "grupo": explicar_grupo(" ".join(w["text"] for w in col(x_gru - 8, x_ncd - 3))),
                "nivel_cd": " ".join(w["text"] for w in col(x_ncd - 3, x_esp - 3)),
                "especifico": especifico[0]["text"] + " €" if especifico else "",
                "procedencia": " ".join(w["text"] for w in sorted(fila1, key=lambda w: w["x0"])),
                "cuerpos": cuerpos,
                "localidad": re.sub(r"^\.*:?\s*", "", linea("Localidad")),
                "jornada": linea("Turno/Jornada"),
                "perfil": " · ".join(p.capitalize() for p in perfil),
            })
    return puestos


def url_disposicion(fecha: dt.date, n: int) -> str:
    return f"{BASE}/CM_Orden_BOCM/{fecha:%Y/%m/%d}/BOCM-{fecha:%Y%m%d}-{n}.PDF"


def url_sumario(fecha: dt.date, numero: int) -> str:
    return f"{BASE}/CM_Boletin_BOCM/{fecha:%Y/%m/%d}/{numero:03d}00.PDF"


def localizar_boletin(fecha: dt.date | None) -> tuple[dt.date, bytes] | None:
    """Busca la disposición nº 1 del día pedido (o del último día con boletín)."""
    candidatas = [fecha] if fecha else [
        dt.datetime.now(TZ).date() - dt.timedelta(days=d) for d in range(8)
    ]
    for f in candidatas:
        pdf = descargar_pdf(url_disposicion(f, 1))
        if pdf:
            return f, pdf
        time.sleep(PAUSA)
    return None


def numero_boletin(texto: str) -> int | None:
    m = re.search(r"B\.\s*O\.\s*C\.\s*M\.\s*N[úu]m\.\s*(\d+)", texto)
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- sumario
@dataclass
class Entrada:
    cve: str
    seccion: str = ""
    subseccion: str = ""
    organismo: str = ""
    suborganismo: str = ""
    tipo: str = ""
    texto: str = ""
    vacante: bool = False
    detalle: dict = field(default_factory=dict)

    @property
    def url(self) -> str:
        m = CVE_RE.match(self.cve)
        f = dt.datetime.strptime(m.group(1), "%Y%m%d").date()
        return url_disposicion(f, int(m.group(2)))


RUIDO = [
    re.compile(p)
    for p in (
        r"B\.O\.C\.M\.\s*Núm\.",
        r"^(LUNES|MARTES|MIÉRCOLES|JUEVES|VIERNES|SÁBADO|DOMINGO)\s+\d",
        r"^BOLET[ÍI]N OFICIAL",
        r"^DE LA COMUNIDAD DE MADRID$",
        r"^SUMARIO$",
        r"^BOCM$",
        r"^Pág\.\s*\d+",
        r"ISSN|D\. L\.:|www\.bocm\.es",
    )
]
SECCION_RE = re.compile(r"^(I|II|III|IV|V)\.\s+([A-ZÁÉÍÓÚÑ ,]+)$")
SUBSECCION_RE = re.compile(r"^([A-D])\)\s+(.+)$")


def es_ruido(linea: str) -> bool:
    return len(linea) <= 3 or any(p.search(linea) for p in RUIDO)


def es_mayusculas(s: str) -> bool:
    letras = [c for c in s if c.isalpha()]
    return len(letras) >= 4 and sum(c.isupper() for c in letras) / len(letras) > 0.85


def unir(buf: str, linea: str) -> str:
    if buf.endswith("-") and linea[:1].islower():
        return buf[:-1] + linea
    return f"{buf} {linea}".strip()


def limpiar_entrada(texto: str) -> str:
    texto = re.sub(r"(\s*\.){3,}\s*", " ", texto)  # puntos guía ". . . ."
    texto = texto.lstrip("—–- ").strip()
    return re.sub(r"\s+", " ", texto)


def parsear_sumario(texto: str) -> list[Entrada]:
    seccion = subseccion = organismo = suborg = tipo = ""
    pendientes: list[str] = []
    buf = ""
    en_entrada = False
    ultimo_fue_org = False
    entradas: list[Entrada] = []

    for raw in texto.splitlines():
        linea = raw.strip()
        if not linea or es_ruido(linea):
            continue

        m_sec, m_sub = SECCION_RE.match(linea), SUBSECCION_RE.match(linea)
        if m_sec or m_sub:
            en_entrada, buf, pendientes = False, "", []
            organismo = suborg = tipo = ""
            ultimo_fue_org = False
            if m_sec:
                seccion, subseccion = linea, ""
            else:
                subseccion = linea
            continue

        cve = CVE_RE.search(linea)
        en_local = seccion.startswith("III")
        empieza = linea.startswith(("—", "–")) or (en_local and not es_mayusculas(linea))

        if en_entrada or empieza or cve:
            if not en_entrada:
                en_entrada = True
                buf = ""
                if pendientes:
                    tipo = pendientes[-1]
                    if len(pendientes) >= 2:
                        suborg = pendientes[-2]
                    pendientes = []
            buf = unir(buf, linea[: cve.end()] if cve else linea)
            if cve:
                txt = limpiar_entrada(buf[: buf.rfind(cve.group(0))])
                e = Entrada(cve=cve.group(0), seccion=seccion, subseccion=subseccion,
                            organismo=organismo, suborganismo=suborg, tipo=tipo, texto=txt)
                if en_local:  # "Alcobendas. Ofertas de empleo. Convocatoria procesos selectivos"
                    partes = txt.split(". ", 1)
                    e.organismo = f"Ayuntamiento de {partes[0].strip()}"
                    e.tipo = partes[1].strip() if len(partes) > 1 else ""
                entradas.append(e)
                en_entrada, buf = False, ""
            ultimo_fue_org = False
            continue

        if es_mayusculas(linea):
            organismo = f"{organismo} {linea}" if ultimo_fue_org else linea
            suborg = tipo = ""
            pendientes = []
            ultimo_fue_org = True
        else:
            pendientes.append(linea)
            ultimo_fue_org = False

    return entradas


# --------------------------------------------------------------------------- clasificación
EXCLUIR_TIPO = re.compile(
    r"^(aspirantes|nombramiento|adjudicaci|cese|tribunal|lista|aprobad|calificaci|"
    r"toma de posesi|correcci[oó]n|jubilaci)",
    re.I,
)
EXCLUIR_TEXTO = re.compile(
    r"se nombra|nombran? funcionari|admitid|excluid|(relaci[oó]n|lista)[^.]{0,40}aprobad|"
    r"personas aprobadas|aspirantes aprobad|adjudica|resuelve|toma de posesi|\bcese\b|"
    r"tribunal calificador|calificaciones|declara desiert|correcci[oó]n de errores|jubilaci",
    re.I,
)
INCLUIR = re.compile(
    r"convoca|convocatoria|bolsa|oferta de empleo|ofertas de empleo|proceso selectivo|"
    r"procesos selectivos|libre designaci|provisi[oó]n|bases|selecci[oó]n",
    re.I,
)


def es_empleo(e: Entrada) -> bool:
    if e.seccion.startswith("III"):
        return bool(re.search(r"oferta[s]? de empleo|proceso[s]? selectivo|bolsa", e.texto, re.I))
    return "Autoridades y Personal" in e.subseccion


def clasificar(e: Entrada) -> None:
    subtipo = re.sub(r"^ofertas? de empleo\.?\s*", "", e.tipo, flags=re.I)
    excluida = bool(EXCLUIR_TIPO.search(subtipo) or EXCLUIR_TEXTO.search(e.texto))
    e.vacante = es_empleo(e) and bool(INCLUIR.search(f"{e.tipo} {e.texto}")) and not excluida


# --------------------------------------------------------------------------- extracción
NUMEROS = {
    "un": "1", "una": "1", "dos": "2", "tres": "3", "cuatro": "4", "cinco": "5", "seis": "6",
    "siete": "7", "ocho": "8", "nueve": "9", "diez": "10", "once": "11", "doce": "12",
    "quince": "15", "veinte": "20", "treinta": "30",
}
SISTEMAS = [
    (r"concurso[\s-]+oposici[oó]n", "concurso-oposición"),
    (r"(?<!concurso-)(?<!concurso )\boposici[oó]n\b", "oposición"),
    (r"concurso de m[eé]ritos", "concurso de méritos"),
    (r"libre designaci[oó]n", "libre designación"),
    (r"bolsa de (?:empleo|trabajo)", "bolsa de empleo"),
    (r"estabilizaci[oó]n", "estabilización"),
    (r"promoci[oó]n interna", "promoción interna"),
    (r"turno libre|acceso libre", "turno libre"),
    (r"comisi[oó]n de servicios", "comisión de servicios"),
    (r"interin", "interinidad"),
]
# Grupo fijado por la LOE para los cuerpos docentes (se indica que viene de la ley).
CUERPOS_DOCENTES = [
    ("Catedráticos", "A1", "Secundaria / EOI / Música y Artes Escénicas / Artes Plásticas"),
    ("Inspectores de Educación", "A1", "Inspección educativa"),
    ("Profesores de Enseñanza Secundaria", "A1", "ESO, Bachillerato y FP"),
    ("Profesores de Escuelas Oficiales de Idiomas", "A1", "Escuelas Oficiales de Idiomas"),
    ("Profesores de Música y Artes Escénicas", "A1", "Conservatorios / Enseñanzas artísticas"),
    ("Profesores de Artes Plásticas y Diseño", "A1", "Escuelas de Arte"),
    ("Profesores Especialistas en Sectores Singulares", "A2", "Formación Profesional"),
    ("Maestros de Taller de Artes Plásticas", "A2", "Escuelas de Arte"),
    ("Maestros", "A2", "Infantil y Primaria"),
]
CORTES = re.compile(
    r"\s+(?:por el sistema|mediante|de la Comunidad de Madrid|del Ayuntamiento|del Grupo|Grupo|"
    r"Subgrupo|en el|en la|para|con|que|convocad|incluid|correspondiente|perteneciente|y se|"
    r"a cubrir|vacante)\b.*$"
)
PAT_CUERPO = [
    re.compile(r"\b(Cuerpo|Escala|Subescala|Clase|[Cc]ategor[ií]a(?: profesional)?|[Ee]specialidad)\s+"
               r"(?:de\s+|del\s+)?[«“\"]([^»”\"]{3,120})[»”\"]"),
    re.compile(r"\b(Cuerpo|Escala|Subescala|Clase|[Cc]ategor[ií]a(?: profesional)?|[Ee]specialidad)\s+"
               r"((?:de\s+|del\s+)?[A-ZÁÉÍÓÚÑ][^,.;:()«»\"“”]{2,120})"),
]


def normalizar(t: str) -> str:
    t = re.sub(r"-\n(?=[a-záéíóúñ])", "", t)
    return re.sub(r"\s+", " ", t)


def extraer_regex(t: str) -> dict:
    t = normalizar(t)
    d: dict = {"fuente": "regex"}

    # Cuerpo / escala / categoría
    c = Counter()
    for pat in PAT_CUERPO:
        for tipo, nombre in pat.findall(t):
            nombre = CORTES.sub("", nombre).strip(" ,")
            if (3 <= len(nombre) <= 110 and not nombre.lower().startswith(("la ", "el ", "los "))
                    and not re.search(r"Escala Especialidad|Admon|\d{3,}", f"{tipo} {nombre}")):
                c[f"{tipo} {nombre}"] += 1
    d["cuerpo"] = "; ".join(k for k, _ in c.most_common(4)) or NO_CONSTA

    # Grupo / subgrupo
    grupos: list[str] = []
    for g in re.findall(r"[Ss]ubgrupo\s+(A1|A2|C1|C2)\b", t) + re.findall(r"[Gg]rupo\s+(A1|A2|C1|C2|A|B|C|E)\b", t):
        if g not in grupos:
            grupos.append(g)
    if any(len(g) == 2 for g in grupos):  # si hay subgrupo, quita la letra suelta
        grupos = [g for g in grupos if len(g) == 2 or g in ("B", "E")]
    if re.search(r"[Aa]grupaci[oó]n(?:es)? [Pp]rofesional(?:es)?", t):
        grupos.append("Agrupaciones Profesionales (antiguo E)")
    for g, niv, area in re.findall(r"[Gg]rupo\s+(III|II|IV|V|I)\b(?:\s*,?\s*[Nn]ivel\s+(\d+))?(?:\s*,?\s*[ÁA]rea\s+([A-Z])\b)?", t):
        etiqueta = f"Grupo {g} (laboral)" + (f", Nivel {niv}" if niv else "") + (f", Área {area}" if area else "")
        if etiqueta not in grupos:
            grupos.append(etiqueta)

    nivel_educativo = ""
    for nombre, grupo, nivel_ed in CUERPOS_DOCENTES:
        if re.search(rf"Cuerpo\s+de\s+{nombre}", t):
            if not grupos:
                grupos.append(f"{grupo} (fijado por la LOE para este cuerpo; no aparece en el texto)")
            nivel_educativo = nivel_ed
            break
    d["grupo"] = ", ".join(grupos[:4]) or NO_CONSTA
    d["nivel_educativo"] = nivel_educativo

    # Nivel (complemento de destino o nivel laboral)
    niveles = []
    for m in re.finditer(r"[Nn]ivel\s+(de\s+complemento\s+de\s+destino\s+|de\s+C\.?\s?D\.?\s+|C\.?\s?D\.?\s+)?(\d{1,2})\b", t):
        et = f"Nivel CD {m.group(2)}" if m.group(1) else f"Nivel {m.group(2)}"
        if et not in niveles:
            niveles.append(et)
    d["nivel"] = ", ".join(niveles[:3]) or NO_CONSTA

    # Plazas
    plazas = []
    for n in re.findall(r"\b(\d{1,4}|" + "|".join(NUMEROS) + r")\s+(?:\(\d+\)\s+)?(?:plazas?|puestos?)\b", t, re.I):
        n = NUMEROS.get(n.lower(), n)
        if n not in plazas:
            plazas.append(n)
    d["plazas"] = ", ".join(plazas[:3]) or NO_CONSTA

    # Sistema: primero en el título de la disposición; si no aparece, en todo el texto
    sistemas = []
    for patron, nombre in SISTEMAS:
        if re.search(patron, t[:700], re.I):
            sistemas.append(nombre)
    for patron, nombre in ([] if sistemas else SISTEMAS):
        if re.search(patron, t, re.I):
            sistemas.append(nombre)
    d["sistema"] = ", ".join(sistemas) or NO_CONSTA

    # Plazo de solicitudes
    m = re.search(
        r"plazo[^.]{0,80}?(\d+|[a-záéíóú]+(?:\s+y\s+[a-záéíóú]+)?)\s+d[ií]as\s+(h[aá]biles|naturales)",
        t, re.I,
    )
    if m:
        plazo = f"{m.group(1)} días {m.group(2)}"
        contexto = t[m.end(): m.end() + 350]
        desde = re.match(r"\s*,?\s*((?:a contar|contad[oa]s?|computad[oa]s?|desde|a partir)[^.;]{5,160}?)(?=,\s*(?:en|ante|a trav[eé]s|mediante)\b|[.;]|$)", contexto)
        if desde:  # se copia literal la referencia del texto
            plazo += f" {desde.group(1).strip()}"
        elif re.search(r"Bolet[ií]n Oficial del Estado|«BOE»|BOE", contexto):
            plazo += " desde el día siguiente a la publicación en el BOE"
        elif re.search(r"Comunidad de Madrid|BOCM", contexto, re.I):
            plazo += " desde el día siguiente a la publicación en el BOCM"
        d["plazo"] = plazo
    else:
        d["plazo"] = NO_CONSTA
    return d


PROMPT_LLM = """Eres un asistente que extrae datos de convocatorias de empleo público del BOCM.
Devuelve SOLO un objeto JSON con estas claves (valores en español, texto breve):
- "puesto": puesto o plazas que se convocan
- "cuerpo": cuerpo, escala, subescala, clase o categoría profesional (y especialidad si la hay)
- "grupo": grupo/subgrupo de clasificación (A1, A2, C1, C2, AP...) o grupo laboral (I-V) y nivel laboral
- "nivel": nivel de complemento de destino u otro nivel retributivo, si consta
- "nivel_educativo": solo para docentes: etapa educativa (p.ej. ESO y Bachillerato)
- "plazas": número de plazas
- "sistema": oposición, concurso-oposición, concurso, libre designación, bolsa...
- "plazo": plazo de solicitudes y desde cuándo cuenta (p.ej. desde publicación en el BOE)
- "es_convocatoria": true si abre plazas o un plazo para presentarse; false si es una lista, nombramiento, adjudicación, etc.
REGLA ESTRICTA: si un dato no aparece explícitamente en el texto, pon null. No deduzcas ni inventes.

Texto de la disposición:
"""


def extraer_llm(texto: str) -> dict | None:
    clave = os.environ.get("ANTHROPIC_API_KEY")
    if not clave:
        return None
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": clave, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": os.environ.get("BOCM_MODELO", "claude-haiku-5-5"),
                "max_tokens": 800,
                "messages": [{"role": "user", "content": PROMPT_LLM + normalizar(texto)[:25000]}],
            },
            timeout=120,
        )
        r.raise_for_status()
        salida = "".join(b.get("text", "") for b in r.json().get("content", []))
        datos = json.loads(re.search(r"\{.*\}", salida, re.S).group(0))
    except Exception as exc:  # cualquier fallo -> se usa la extracción por regex
        print(f"  Aviso: fallo de la API de Claude ({exc}); uso regex", file=sys.stderr)
        return None
    d = {k: (str(v) if v not in (None, "", []) else NO_CONSTA) for k, v in datos.items() if k != "es_convocatoria"}
    d["es_convocatoria"] = datos.get("es_convocatoria")
    d["fuente"] = "Claude"
    if d.get("nivel_educativo") == NO_CONSTA:
        d["nivel_educativo"] = ""
    return d


def detallar(e: Entrada, errores: list[str], incluir_tramites: bool) -> None:
    try:
        pdf = descargar_pdf(e.url)
    except ErrorDescarga as exc:
        errores.append(str(exc))
        e.detalle = {"error": "No se pudo descargar la disposición"}
        return
    if not pdf:
        errores.append(f"{e.cve}: no encontrado")
        e.detalle = {"error": "Disposición no encontrada"}
        return
    e.detalle = analizar_pdf(pdf)
    if e.detalle.get("es_convocatoria") is False:
        e.vacante = False  # Claude ha visto que no abre plazas


def analizar_pdf(contenido: bytes) -> dict:
    with pdfplumber.open(io.BytesIO(contenido)) as pdf:
        texto = "\n".join((p.extract_text(x_tolerance=X_TOL) or "") for p in pdf.pages[:10])
        try:
            puestos = parsear_anexo_puestos(pdf)
        except Exception as exc:  # tabla con un formato inesperado: seguimos sin ella
            print(f"  Aviso: no se pudo leer la tabla de puestos ({exc})", file=sys.stderr)
            puestos = []
    d = extraer_llm(texto) or extraer_regex(texto)
    if puestos:  # la tabla del anexo manda sobre lo extraído del texto
        d["puestos"] = puestos
        d["plazas"] = f"{len(puestos)} puesto{'s' if len(puestos) > 1 else ''}"
        d["grupo"] = ", ".join(dict.fromkeys(p["grupo"] for p in puestos if p["grupo"])) or NO_CONSTA
        d["nivel"] = ", ".join(dict.fromkeys(f"Nivel CD {p['nivel_cd']}" for p in puestos if p["nivel_cd"])) or NO_CONSTA
        d["cuerpo"] = "ver cada puesto"
    return d


# --------------------------------------------------------------------------- modo alternativo
def recorrer_disposiciones(fecha: dt.date, errores: list[str]) -> list[tuple[Entrada, bytes]]:
    """Si no hay sumario, descarga las disposiciones una a una (más lento)."""
    resultado, fallos, n = [], 0, 1
    while fallos < 8 and n < 800:
        try:
            pdf = descargar_pdf(url_disposicion(fecha, n))
        except ErrorDescarga as exc:
            errores.append(str(exc))
            pdf = None
        if not pdf:
            fallos += 1
        else:
            fallos = 0
            texto = texto_pdf(pdf, max_paginas=10)
            lineas = [l.strip() for l in texto.splitlines() if l.strip() and not es_ruido(l.strip())]
            cab = " ".join(lineas[:15])
            e = Entrada(cve=f"BOCM-{fecha:%Y%m%d}-{n}")
            if "ADMINISTRACIÓN LOCAL" in cab:
                e.seccion = "III. ADMINISTRACIÓN LOCAL"
                m = re.search(r"AYUNTAMIENTO DE\s+([A-ZÁÉÍÓÚÑ \-]+?)\s+(?:OFERTAS|ORGANIZACI|RÉGIMEN|URBANISMO|CONTRATACI|\d)", cab)
                e.organismo = f"Ayuntamiento de {m.group(1).title()}" if m else "Ayuntamiento"
                e.texto = "Ofertas de empleo" if "OFERTAS DE EMPLEO" in cab else ""
            elif "Autoridades y Personal" in cab:
                e.seccion, e.subseccion = "I. COMUNIDAD DE MADRID", "B) Autoridades y Personal"
                orgs = [l for l in lineas[:12] if es_mayusculas(l) and not SECCION_RE.match(l)]
                e.organismo = orgs[0] if orgs else ""
            m = re.search(r"\b\d+\s+((?:RESOLUCIÓN|ORDEN|DECRETO|ANUNCIO|ACUERDO)\b.{20,400}?)(?:\.\s|$)", cab)
            e.texto = (e.texto + " " + (m.group(1) if m else "")).strip()
            resultado.append((e, pdf))
        n += 1
        time.sleep(PAUSA)
    return resultado


# --------------------------------------------------------------------------- informe
def titulo(s: str) -> str:
    menores = {"de", "del", "la", "las", "los", "y", "e", "en", "el", "a"}
    palabras = s.lower().split()
    return " ".join(p if (i and p in menores) else p.capitalize() for i, p in enumerate(palabras))


def orden_bloque(org: str) -> tuple:
    o = org.upper()
    if "EDUCACI" in o:
        return (0, o)
    if o.startswith("CONSEJER"):
        return (1, o)
    if "UNIVERSIDAD" in o:
        return (3, o)
    if o.startswith("AYUNTAMIENTO"):
        return (4, o)
    return (2, o)


def informe(fecha: dt.date, numero: int | None, total: int, vacantes: list[Entrada],
            tramites: list[Entrada], errores: list[str], modo: str) -> str:
    fuente = "Claude (IA)" if os.environ.get("ANTHROPIC_API_KEY") else "reglas automáticas (regex)"
    estado = "COMPLETA" if not errores else f"PARCIAL ({len(errores)} disposiciones no leídas)"
    lineas = [
        f"# Vacantes BOCM{f' nº {numero}' if numero else ''} – {DIAS[fecha.weekday()]} {fecha:%d/%m/%Y}",
        "",
        f"**Revisión:** {estado} · {modo} · {total} disposiciones · **{len(vacantes)} convocatorias**",
        f"_Datos de cuerpo/grupo extraídos con {fuente}. Comprueba siempre la convocatoria original antes de presentarte._",
        "",
    ]
    if not vacantes:
        lineas.append("Hoy no hay convocatorias de empleo en el BOCM.")

    def bloque(entradas: list[Entrada], completo: bool) -> None:
        por_org: dict[str, list[Entrada]] = {}
        for e in entradas:
            org = e.organismo.strip() or "Otros organismos"
            por_org.setdefault(org, []).append(e)
        for org in sorted(por_org, key=orden_bloque):
            lineas.append(f"## {titulo(org) if es_mayusculas(org) else org}")
            for e in por_org[org]:
                d = e.detalle
                cab = " · ".join(x for x in (e.suborganismo, e.tipo) if x)
                desc = e.texto if len(e.texto) < 320 else e.texto[:317] + "…"
                lineas.append(f"**{cab or 'Convocatoria'}** — {desc}")
                if completo:
                    if "error" in d:
                        lineas.append(f"- ⚠️ {d['error']}: revísala a mano")
                    else:
                        if d.get("puesto") and d["puesto"] != NO_CONSTA:
                            lineas.append(f"- **Puesto:** {d['puesto']}")
                        lineas.append(f"- **Cuerpo / escala / categoría:** {d.get('cuerpo', NO_CONSTA)}")
                        nivel_ed = f" · **Etapa:** {d['nivel_educativo']}" if d.get("nivel_educativo") else ""
                        lineas.append(f"- **Grupo / subgrupo:** {d.get('grupo', NO_CONSTA)} · **Nivel:** {d.get('nivel', NO_CONSTA)}{nivel_ed}")
                        lineas.append(f"- **Plazas:** {d.get('plazas', NO_CONSTA)} · **Sistema:** {d.get('sistema', NO_CONSTA)}")
                        lineas.append(f"- **Plazo:** {d.get('plazo', NO_CONSTA)}")
                        for p in d.get("puestos", []):
                            lineas.append(f"- **Puesto {p['codigo']} – {p['denominacion'] or 'sin denominación'}**")
                            if p["unidad"]:
                                lineas.append(f"    - Unidad: {p['unidad']}")
                            lineas.append(f"    - Grupo: {p['grupo'] or NO_CONSTA} · Nivel CD: {p['nivel_cd'] or NO_CONSTA}"
                                          + (f" · C. específico: {p['especifico']}" if p["especifico"] else ""))
                            lineas.append(f"    - Cuerpos: {'; '.join(p['cuerpos']) or NO_CONSTA}")
                            extra = " · ".join(x for x in (
                                f"Procedencia: {p['procedencia']}" if p["procedencia"] else "",
                                f"Localidad: {p['localidad']}" if p["localidad"] else "",
                                f"Jornada: {p['jornada']}" if p["jornada"] else "") if x)
                            if extra:
                                lineas.append(f"    - {extra}")
                            if p["perfil"]:
                                lineas.append(f"    - Perfil: {p['perfil']}")
                lineas.append(f"- [{e.cve}]({e.url})")
                lineas.append("")

    bloque(vacantes, True)
    if tramites:
        lineas += ["---", "# Otros trámites de procesos selectivos", "_Listas, nombramientos, adjudicaciones, tribunales…_", ""]
        bloque(tramites, False)
    if errores:
        lineas += ["---", "### ⚠️ No se pudieron leer", *[f"- {x}" for x in errores]]
    return "\n".join(lineas) + "\n"


def enviar_email(asunto: str, md: str) -> None:
    host, destino = os.environ.get("SMTP_HOST"), os.environ.get("EMAIL_TO")
    if not (host and destino):
        return
    try:
        import markdown
        html = markdown.markdown(md)
    except ImportError:
        html = f"<pre>{md}</pre>"
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = asunto, os.environ.get("SMTP_USER", destino), destino
    msg.attach(MIMEText(md, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    puerto = int(os.environ.get("SMTP_PORT", "465"))
    srv = smtplib.SMTP_SSL(host, puerto) if puerto == 465 else smtplib.SMTP(host, puerto)
    with srv:
        if puerto != 465:
            srv.starttls()
        if os.environ.get("SMTP_USER"):
            srv.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        srv.sendmail(msg["From"], destino.split(","), msg.as_string())
    print(f"Email enviado a {destino}")


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="Extrae las convocatorias de empleo del BOCM")
    ap.add_argument("--fecha", help="AAAA-MM-DD (por defecto, el último boletín)")
    ap.add_argument("--salida", default="resumenes", help="carpeta de salida")
    ap.add_argument("--incluir-tramites", action="store_true", help="lista también listas, nombramientos…")
    args = ap.parse_args()

    fecha = dt.date.fromisoformat(args.fecha) if args.fecha else None
    try:
        hallado = localizar_boletin(fecha)
    except ErrorDescarga as exc:
        print(f"ERROR: no se pudo conectar con bocm.es: {exc}", file=sys.stderr)
        return 1
    if not hallado:
        print(f"No hay BOCM publicado {'el ' + args.fecha if fecha else 'en los últimos 7 días'}.")
        return 0 if fecha else 1
    fecha, pdf1 = hallado
    numero = numero_boletin(texto_pdf(pdf1, max_paginas=1))
    print(f"BOCM nº {numero} del {fecha}")

    errores: list[str] = []
    entradas: list[Entrada] = []
    pdfs: dict[str, bytes] = {}
    sumario = None
    if numero:
        try:
            sumario = descargar_pdf(url_sumario(fecha, numero))
        except ErrorDescarga as exc:
            print(f"Aviso: {exc}", file=sys.stderr)
    if sumario:
        modo = "a partir del sumario"
        entradas = parsear_sumario(texto_pdf(sumario))
        if not entradas:
            sumario = None  # el formato del sumario ha cambiado: modo alternativo
    if not sumario:
        modo = "sin sumario: disposición a disposición"
        print("Sumario no disponible: recorro las disposiciones una a una…")
        for e, pdf in recorrer_disposiciones(fecha, errores):
            entradas.append(e)
            pdfs[e.cve] = pdf

    for e in entradas:
        clasificar(e)
    vacantes = [e for e in entradas if e.vacante]
    tramites = [e for e in entradas if es_empleo(e) and not e.vacante] if args.incluir_tramites else []
    print(f"{len(entradas)} disposiciones, {len(vacantes)} posibles convocatorias")

    for e in vacantes:
        print(f"  · {e.cve} {e.organismo[:60]}")
        if e.cve in pdfs:
            e.detalle = analizar_pdf(pdfs[e.cve])
            if e.detalle.get("es_convocatoria") is False:
                e.vacante = False
        else:
            detallar(e, errores, args.incluir_tramites)
            time.sleep(PAUSA)
    descartadas = [e for e in vacantes if not e.vacante]
    vacantes = [e for e in vacantes if e.vacante]
    if args.incluir_tramites:
        tramites += descartadas

    md = informe(fecha, numero, len(entradas), vacantes, tramites, errores, modo)
    salida = Path(args.salida)
    salida.mkdir(parents=True, exist_ok=True)
    (salida / f"{fecha}.md").write_text(md, encoding="utf-8")
    (salida / f"{fecha}.json").write_text(
        json.dumps([asdict(e) | {"url": e.url} for e in vacantes], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Resumen guardado en {salida / f'{fecha}.md'}")

    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(md)
    enviar_email(f"Vacantes BOCM {fecha:%d/%m/%Y}: {len(vacantes)} convocatorias", md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
