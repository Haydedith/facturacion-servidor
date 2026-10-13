"""
Mesa de Facturación Tándem — servidor (backend)
------------------------------------------------
Recibe las solicitudes del portal, las guarda en Supabase y envía el correo con adjuntos por Brevo.

Configuración: todo se define en Render → Environment (nunca en este archivo).
  Obligatorias : SUPABASE_URL, SUPABASE_KEY (clave service_role), BREVO_API_KEY
  Opcionales   : SENDER_EMAIL, NOTIFY_EMAIL (puede ser varios, separados por coma), ALLOWED_ORIGINS
"""
import base64
import html
import logging
import os
import re
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

# ─── CONFIGURACIÓN ───────────────────────────────────────────────────────────
BREVO_API_KEY = os.getenv('BREVO_API_KEY', '')
SENDER_EMAIL = os.getenv('SENDER_EMAIL', 'hparaguay@tandem.pe')
SENDER_NAME = 'Mesa de Facturación Tándem'
NOTIFY_EMAIL = os.getenv('NOTIFY_EMAIL', 'hparaguay@tandem.pe')

SUPABASE_URL = os.getenv('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY = os.getenv('SUPABASE_KEY', '')
SUPABASE_TABLE = 'solicitudes'

# Solo este sitio puede usar el servidor desde un navegador (se pueden poner varios separados por coma).
ALLOWED_ORIGINS = [o.strip() for o in os.getenv('ALLOWED_ORIGINS', 'https://haydedith.github.io').split(',') if o.strip()]

ESTADOS_VALIDOS = ('Pendiente', 'Facturado', 'Enviado')
SOL_ID_RE = re.compile(r'^SOL-[A-Z0-9]{4,20}$')
EXT_PERMITIDAS = ('.pdf', '.xlsx', '.xls')
MAX_ARCHIVOS = 10
MAX_BYTES_ARCHIVO = 5 * 1024 * 1024      # el portal ya limita a 1.8 MB; esto es la red de seguridad del servidor
MAX_BYTES_TOTAL = 14 * 1024 * 1024       # Brevo admite 20 MB por correo (incluye el contenido codificado)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024
CORS(app, origins=ALLOWED_ORIGINS)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = app.logger


# ─── UTILIDADES ──────────────────────────────────────────────────────────────
def ahora():
    return datetime.now(timezone.utc).isoformat()


def sb_configurado():
    return bool(SUPABASE_URL and SUPABASE_KEY)


def sb_headers(extra=None):
    h = {'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}', 'Content-Type': 'application/json'}
    if extra:
        h.update(extra)
    return h


def limpiar(valor, maximo=500):
    """Texto simple: sin saltos de línea raros, recortado."""
    s = '' if valor is None else str(valor)
    s = re.sub(r'[\r\n\t]+', ' ', s).strip()
    return s[:maximo]


def nombre_seguro(nombre):
    """Evita rutas, caracteres raros y nombres larguísimos en los adjuntos."""
    n = os.path.basename(str(nombre or 'documento').replace('\\', '/'))
    n = re.sub(r'[^\w.\-() ]', '_', n, flags=re.UNICODE).strip(' .')
    return (n[:120] or 'documento')


def firma_valida(ext, contenido):
    """Comprueba que el archivo realmente sea de ese tipo (no solo que lo diga el nombre)."""
    if ext == '.pdf':
        return contenido[:5] == b'%PDF-'
    if ext in ('.xlsx', '.xls'):
        return contenido[:2] == b'PK' or contenido[:4] == b'\xd0\xcf\x11\xe0'
    return False


def error(mensaje, codigo):
    return jsonify({'ok': False, 'error': mensaje}), codigo


def preparar_adjuntos(archivos):
    """Devuelve (lista_para_brevo, mensaje_de_error). Valida cantidad, tamaño y tipo."""
    if not isinstance(archivos, list):
        return [], 'Formato de archivos inválido.'
    if len(archivos) > MAX_ARCHIVOS:
        return [], f'Demasiados archivos (máximo {MAX_ARCHIVOS}).'
    salida, total = [], 0
    for a in archivos:
        if not isinstance(a, dict):
            return [], 'Formato de archivos inválido.'
        b64 = a.get('base64') or ''
        if not b64:
            continue
        nombre = nombre_seguro(a.get('nombre'))
        ext = os.path.splitext(nombre)[1].lower()
        if ext not in EXT_PERMITIDAS:
            return [], f'Tipo de archivo no permitido: {nombre}. Solo se aceptan PDF y Excel.'
        if ',' in b64:
            b64 = b64.split(',', 1)[1]
        b64 = re.sub(r'\s+', '', b64)
        try:
            contenido = base64.b64decode(b64, validate=True)
        except Exception:
            return [], f'El archivo {nombre} está dañado o incompleto.'
        if len(contenido) > MAX_BYTES_ARCHIVO:
            return [], f'El archivo {nombre} es demasiado grande.'
        if not firma_valida(ext, contenido):
            return [], f'El archivo {nombre} no parece ser un {ext[1:].upper()} válido.'
        total += len(contenido)
        if total > MAX_BYTES_TOTAL:
            return [], 'Los archivos juntos pesan demasiado. Reduce o comprime algunos.'
        salida.append({'name': nombre, 'content': b64})
    return salida, None


# ─── ERRORES: siempre en JSON y sin datos internos ───────────────────────────
@app.errorhandler(413)
def demasiado_grande(_e):
    return error('Los archivos son demasiado grandes. Reduce su tamaño e intenta de nuevo.', 413)


@app.errorhandler(Exception)
def error_general(e):
    if isinstance(e, HTTPException):
        return error(e.name, e.code or 500)
    log.exception('Error no controlado')
    return error('Error interno del servidor.', 500)


# ─── ESTADO DEL SERVIDOR ─────────────────────────────────────────────────────
@app.route('/', methods=['GET'])
def home():
    return jsonify({'status': 'ok', 'servicio': 'Mesa de Facturación Tándem - backend'}), 200


@app.route('/health', methods=['GET'])
def health():
    """/health = el servidor responde. /health?deep=1 = además toca Supabase (evita que se pause)."""
    if not request.args.get('deep'):
        return jsonify({'status': 'ok'}), 200
    if not sb_configurado():
        return jsonify({'status': 'degraded', 'db': 'no configurada'}), 503
    try:
        r = requests.get(f'{SUPABASE_URL}/rest/v1/empresas', params={'select': 'nombre', 'limit': 1},
                         headers=sb_headers(), timeout=15)
        if r.ok:
            return jsonify({'status': 'ok', 'db': 'ok'}), 200
        log.error('Health: Supabase respondió %s', r.status_code)
    except Exception as e:
        log.error('Health: no se pudo contactar Supabase: %s', e)
    return jsonify({'status': 'degraded', 'db': 'error'}), 503


# ─── RECIBIR SOLICITUD ───────────────────────────────────────────────────────
@app.route('/solicitud', methods=['POST'])
def recibir_solicitud():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return error('Datos incompletos.', 400)

    sol_id = limpiar(data.get('sol_id'), 40)
    if not SOL_ID_RE.match(sol_id):
        return error('Código de solicitud inválido.', 400)

    solicitante = limpiar(data.get('solicitante'), 120)
    correo_sol = limpiar(data.get('correo_sol'), 120)
    cliente = limpiar(data.get('cliente'), 200)
    id_cliente = limpiar(data.get('id_cliente'), 40)
    empresa = limpiar(data.get('empresa'), 80)
    tipo_doc = limpiar(data.get('tipo_doc'), 40)
    sociedad = limpiar(data.get('sociedad'), 60)
    documentos_info = limpiar(data.get('documentos_info') or data.get('documentos'), 300)
    notas = limpiar(data.get('notas'), 1500)
    fecha_hora = limpiar(data.get('fecha_hora'), 40) or ahora()

    if not solicitante or not cliente or not id_cliente:
        return error('Faltan datos obligatorios (solicitante o cliente).', 400)

    adjuntos, problema = preparar_adjuntos(data.get('archivos', []))
    if problema:
        return error(problema, 400)

    if not BREVO_API_KEY:
        log.error('Falta BREVO_API_KEY en Render → Environment')
        return error('El servidor no tiene configurado el envío de correo. Avisa a la administradora.', 500)

    # 1) Guardar en Supabase. Si falla, igual se envía el correo y se avisa con un mensaje claro.
    aviso = None
    if sb_configurado():
        try:
            ya_existe = False
            r_chk = requests.get(f'{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}',
                                 params={'select': 'sol_id', 'sol_id': f'eq.{sol_id}', 'limit': 1},
                                 headers=sb_headers(), timeout=15)
            if r_chk.ok and r_chk.json():
                ya_existe = True  # reintento del mismo envío: no se duplica el registro
            if not ya_existe:
                fila = {
                    'sol_id': sol_id, 'solicitante': solicitante, 'correo_solicitante': correo_sol,
                    'cliente': cliente, 'id_cliente': id_cliente, 'empresa': empresa,
                    'tipo_doc': tipo_doc, 'sociedad': sociedad, 'documentos': documentos_info,
                    'notas': notas, 'estado': 'Pendiente',
                }
                r_db = requests.post(f'{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}',
                                     headers=sb_headers({'Prefer': 'return=minimal'}), json=fila, timeout=15)
                if r_db.status_code not in (200, 201, 204, 409):  # 409 = ya existía (carrera entre dos envíos)
                    log.error('Supabase no guardó %s: %s %s', sol_id, r_db.status_code, r_db.text[:300])
                    aviso = 'No se pudo guardar en el sistema de seguimiento.'
        except Exception as e:
            log.error('No se pudo contactar Supabase al guardar %s: %s', sol_id, e)
            aviso = 'No se pudo guardar en el sistema de seguimiento.'
    else:
        log.error('Falta SUPABASE_URL / SUPABASE_KEY en Render → Environment')
        aviso = 'No se pudo guardar en el sistema de seguimiento.'

    # 2) Enviar el correo con adjuntos por Brevo. Es el único medio que lleva los documentos:
    #    si falla se responde con error para que el portal permita reintentar (el reintento no duplica el registro).
    e = html.escape
    cuerpo = f"""
    <h2>Nueva solicitud de facturación</h2>
    <p><b>Código:</b> {e(sol_id)}</p>
    <p><b>Solicitante:</b> {e(solicitante)} ({e(correo_sol)})</p>
    <p><b>Empresa:</b> {e(empresa)}</p>
    <p><b>Cliente:</b> {e(cliente)} ({e(id_cliente)})</p>
    <p><b>Tipo de documento:</b> {e(tipo_doc)}</p>
    <p><b>Sociedad:</b> {e(sociedad)}</p>
    <p><b>Sustentos incluidos:</b> {e(documentos_info)}</p>
    <p><b>Notas:</b> {e(notas) or '—'}</p>
    <p><b>Fecha y hora de recepción:</b> {e(fecha_hora)}</p>
    """
    destinatarios = [{'email': x.strip()} for x in NOTIFY_EMAIL.split(',') if x.strip()]
    payload = {
        'sender': {'name': SENDER_NAME, 'email': SENDER_EMAIL},
        'to': destinatarios,
        'subject': f'Nueva solicitud de facturación: {sol_id} — {cliente}',
        'htmlContent': cuerpo,
    }
    if adjuntos:
        payload['attachment'] = adjuntos

    try:
        r = requests.post('https://api.brevo.com/v3/smtp/email',
                          headers={'api-key': BREVO_API_KEY, 'Content-Type': 'application/json', 'Accept': 'application/json'},
                          json=payload, timeout=40)
    except Exception as ex:
        log.error('No se pudo contactar a Brevo para %s: %s', sol_id, ex)
        return error('No se pudo enviar el correo al equipo de facturación. Intenta de nuevo en un minuto.', 502)

    if r.status_code not in (200, 201):
        log.error('Brevo rechazó %s: %s %s', sol_id, r.status_code, r.text[:300])
        return error('No se pudo enviar el correo al equipo de facturación. Intenta de nuevo en un minuto.', 502)

    return jsonify({'ok': True, 'sol_id': sol_id, 'aviso': aviso}), 200


# ─── CONFIGURACIÓN PARA EL PORTAL (solicitantes, clientes, reglas) ────────────
@app.route('/config', methods=['GET'])
def obtener_config():
    if not sb_configurado():
        log.error('Falta SUPABASE_URL / SUPABASE_KEY en Render → Environment')
        return error('Base de datos no configurada.', 500)

    def sb_get(tabla, select):
        r = requests.get(f'{SUPABASE_URL}/rest/v1/{tabla}', params={'select': select, 'limit': 1000},
                         headers=sb_headers(), timeout=15)
        r.raise_for_status()
        return r.json()

    try:
        solicitantes = sb_get('solicitantes', 'nombre,activo,correo')
        empresas = sb_get('empresas', 'nombre,activo')
        clientes = sb_get('clientes_por_solicitante', 'solicitante,nombre_cliente,id_cliente,empresa')
        reglas = sb_get('reglas_documentos', 'id_cliente,nombre_cliente,plantilla,oc,hes,acta_conformidad,'
                                              'excel_liquidacion,aceptacion_servicio,cdr,adicionales,notas')
    except Exception as ex:
        log.error('/config: no se pudo leer Supabase: %s', ex)
        return error('No se pudo leer la configuración. Intenta de nuevo en unos minutos.', 502)

    return jsonify({
        'solicitantes': [[r['nombre'], r['activo'], r['correo']] for r in solicitantes],
        'empresas': [[r['nombre'], r['activo']] for r in empresas],
        'clientes': [[r['solicitante'], r['nombre_cliente'], r['id_cliente'], r['empresa']] for r in clientes],
        'reglas': [[r['id_cliente'], r['nombre_cliente'], r['plantilla'], r['oc'], r['hes'],
                    r['acta_conformidad'], r['excel_liquidacion'], r['aceptacion_servicio'],
                    r['cdr'], r['adicionales'], r['notas']] for r in reglas],
    }), 200


# ─── LISTAR SOLICITUDES (Mis solicitudes / Panel) ────────────────────────────
@app.route('/solicitudes', methods=['GET'])
def listar_solicitudes():
    if not sb_configurado():
        log.error('Falta SUPABASE_URL / SUPABASE_KEY en Render → Environment')
        return error('Base de datos no configurada.', 500)
    try:
        r = requests.get(f'{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}',
                         params={'select': '*', 'order': 'fecha_solicitud.desc', 'limit': 500},
                         headers=sb_headers(), timeout=15)
    except Exception as ex:
        log.error('/solicitudes: no se pudo contactar Supabase: %s', ex)
        return error('No se pudo leer las solicitudes. Intenta de nuevo en unos minutos.', 502)
    if not r.ok:
        log.error('/solicitudes: Supabase respondió %s %s', r.status_code, r.text[:300])
        return error('No se pudo leer las solicitudes. Intenta de nuevo en unos minutos.', 502)
    return jsonify(r.json()), 200


# ─── CAMBIAR ESTADO (Panel de Facturación) ───────────────────────────────────
def _leer_fila(sol_id):
    """Devuelve (fila|None, tiene_columnas_nuevas). Funciona aunque aún no existan fecha_facturado/fecha_envio."""
    url = f'{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}'
    params = {'select': 'sol_id,estado,fecha_facturado,fecha_envio', 'sol_id': f'eq.{sol_id}', 'limit': 1}
    r = requests.get(url, params=params, headers=sb_headers(), timeout=15)
    nuevas = True
    if r.status_code == 400:  # las columnas nuevas todavía no existen
        nuevas = False
        params['select'] = 'sol_id,estado'
        r = requests.get(url, params=params, headers=sb_headers(), timeout=15)
    r.raise_for_status()
    filas = r.json()
    return (filas[0] if filas else None), nuevas


@app.route('/solicitudes/<sol_id>/estado', methods=['POST'])
def actualizar_estado(sol_id):
    if not sb_configurado():
        log.error('Falta SUPABASE_URL / SUPABASE_KEY en Render → Environment')
        return error('Base de datos no configurada.', 500)
    if not SOL_ID_RE.match(sol_id):
        return error('Código de solicitud inválido.', 400)

    data = request.get_json(silent=True) or {}
    nuevo = limpiar(data.get('estado'), 20)
    if nuevo not in ESTADOS_VALIDOS:
        return error('Estado no válido.', 400)

    try:
        fila, nuevas = _leer_fila(sol_id)
        if fila is None:
            return error('No se encontró la solicitud.', 404)

        t = ahora()  # la hora la pone el servidor, no el navegador
        if nuevas:
            if nuevo == 'Pendiente':
                body = {'estado': nuevo, 'fecha_respuesta': None, 'fecha_facturado': None, 'fecha_envio': None}
            elif nuevo == 'Facturado':
                body = {'estado': nuevo, 'fecha_respuesta': t, 'fecha_facturado': t, 'fecha_envio': None}
            else:  # Enviado
                body = {'estado': nuevo, 'fecha_respuesta': t, 'fecha_envio': t,
                        'fecha_facturado': fila.get('fecha_facturado') or t}
        else:
            body = {'estado': nuevo, 'fecha_respuesta': None if nuevo == 'Pendiente' else t}

        r = requests.patch(f'{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}', params={'sol_id': f'eq.{sol_id}'},
                           headers=sb_headers({'Prefer': 'return=minimal'}), json=body, timeout=15)
    except Exception as ex:
        log.error('Estado de %s: no se pudo contactar Supabase: %s', sol_id, ex)
        return error('No se pudo guardar el cambio. Intenta de nuevo en unos minutos.', 502)

    if not r.ok:
        log.error('Estado de %s: Supabase respondió %s %s', sol_id, r.status_code, r.text[:300])
        return error('No se pudo guardar el cambio. Intenta de nuevo en unos minutos.', 502)
    return jsonify({'ok': True}), 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
