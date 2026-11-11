# Vacantes BOCM

Lee cada mañana el Boletín Oficial de la Comunidad de Madrid y saca las convocatorias de empleo público de **todas las Consejerías**, hospitales, organismos, universidades y ayuntamientos. Para cada una indica:

- cuerpo, escala o categoría (y especialidad)
- grupo/subgrupo (A1, A2, C1, C2, AP, o grupo laboral I-V) y nivel
- etapa educativa, en el caso de los cuerpos docentes
- plazas, sistema de acceso y plazo de solicitudes
- enlace a la disposición en bocm.es

## Cómo funciona

1. Localiza la disposición nº 1 del día (`BOCM-AAAAMMDD-1`), lee el número del boletín y descarga el sumario.
2. En el sumario se queda con *I.B) Autoridades y Personal* y con las *Ofertas de empleo* de los ayuntamientos (*III*). Descarta listas, nombramientos, adjudicaciones, ceses y tribunales.
3. Descarga cada convocatoria y extrae los datos. Si trae una **tabla de puestos** (libre designación, concurso de méritos), saca cada puesto: código, denominación, unidad, grupo (con su equivalencia A1/A2/C1/C2), nivel CD, complemento específico, cuerpos admitidos, localidad, jornada y perfil.
4. Guarda `resumenes/AAAA-MM-DD.md` y `.json`, lo muestra en el resumen de la ejecución de GitHub y, si lo configuras, lo envía por email.

Si el sumario no está disponible, recorre las disposiciones una a una. Es más lento y el informe lo indica.

## Puesta en marcha

1. Sube estos archivos a un repositorio de GitHub (puede ser privado).
2. En **Actions**, ejecuta *Vacantes BOCM → Run workflow* con una fecha pasada (por ejemplo `2025-03-18`) y compara el resultado con el BOCM de ese día.
3. Opcional: en **Settings → Secrets and variables → Actions**, añade estos secretos:

| Secreto | Para qué |
|---|---|
| `ANTHROPIC_API_KEY` | Extrae los datos con Claude: bastante más preciso que las reglas automáticas. Tiene un coste pequeño por convocatoria. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO` | Envío por email. Con Gmail: `smtp.gmail.com`, `465`, tu dirección y una *contraseña de aplicación*. |

## Uso local

```bash
pip install -r requirements.txt
python bocm_vacantes.py                       # último boletín
python bocm_vacantes.py --fecha 2025-03-18
python bocm_vacantes.py --incluir-tramites
```

## Limitaciones

- Sin API de Claude, los datos de cuerpo, grupo y plazo salen de expresiones regulares. Cuando no encuentra un dato escribe "No consta en el texto" y nunca lo inventa, pero puede fallar con redacciones poco habituales.
- Para los cuerpos docentes, si el texto no indica el grupo, se muestra el que fija la LOE y se avisa de que no aparece en la disposición.
- En los ayuntamientos, el plazo suele empezar a contar con la publicación del extracto en el BOE. El informe lo señala cuando el texto lo dice.
- Si bocm.es cambia el formato del sumario, el script pasa automáticamente al modo de disposición a disposición.
