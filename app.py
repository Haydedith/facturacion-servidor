"""
Mesa de Facturación Tándem — servidor (backend)
------------------------------------------------
Recibe las solicitudes del portal, las guarda en Supabase y envía el correo con adjuntos por Brevo.

Configuración: todo se define en Render → Environment (nunca en este archivo).
  Obligatorias : SUPABASE_URL, SUPABASE_KEY (clave service_role), BREVO_API_KEY
  Inicio de sesión (Etapa 3):
    AUTH_SECRET       texto largo y secreto (mínimo 32 caracteres) para firmar las sesiones
    ADMIN_EMAILS      correos de administradores, separados por coma (por defecto hparaguay@tandem.pe)
    AUTH_OBLIGATORIA  "true" = nadie entra sin iniciar sesión; "false" = modo de transición
    SESION_HORAS      duración de la sesión (por defecto 8)
  Opcionales   : SENDER_EMAIL, NOTIFY_EMAIL (puede ser varios, separados por coma), ALLOWED_ORIGINS
"""
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

import requests
from flask import Flask, g, jsonify, request
from flask_cors import CORS
from itsdangerous import BadSignature, URLSafeTimedSerializer
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

# Inicio de sesión por código enviado al correo
AUTH_SECRET = os.getenv('AUTH_SECRET', '')
AUTH_OBLIGATORIA = os.getenv('AUTH_OBLIGATORIA', 'false').strip().lower() in ('1', 'true', 'si', 'sí', 'yes')
ADMIN_EMAILS = {e.strip().lower() for e in os.getenv('ADMIN_EMAILS', 'hparaguay@tandem.pe').split(',') if e.strip()}
try:
    SESION_HORAS = float(os.getenv('SESION_HORAS', '8'))
except ValueError:
    SESION_HORAS = 8.0
CODIGO_MINUTOS = 10        # el código vence a los 10 minutos
MAX_INTENTOS = 5           # intentos por código antes de invalidarlo
MAX_CODIGOS_15MIN = 3      # códigos que se pueden pedir por correo en 15 minutos
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
TIPOS_DOC = ('Factura', 'Nota de Crédito', 'Nota de Débito', 'Boleta')
SOCIEDADES = ('Panorama Outsourcing', 'Sinercorp', 'Panorama Staff', 'Tándem')

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


def a_monto(valor):
    """Convierte a número con 2 decimales; si no es un número razonable devuelve None."""
    try:
        n = float(valor)
    except (TypeError, ValueError):
        return None
    if n != n or n < 0 or n > 1e10:
        return None
    return round(n, 2)


def texto_mat(valor):
    """El MAT puede venir como texto o como número (164, 164.0): siempre se devuelve como texto limpio."""
    if valor is None or isinstance(valor, bool):
        return ''
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    return str(valor).strip()[:60]


def a_moneda(valor):
    t = str(valor or '').strip().upper()
    return t if t in ('PEN', 'USD') else None


def error(mensaje, codigo):
    return jsonify({'ok': False, 'error': mensaje}), codigo


# ─── INICIO DE SESIÓN: códigos por correo y sesiones firmadas ────────────────
def es_si(valor):
    """La base guarda 'Si'/'No' (texto) o true/false: se aceptan ambos."""
    return str(valor).strip().lower() in ('si', 'sí', 'true', '1', 'yes', 'x')


def parse_ts(texto):
    """Convierte fechas de Supabase (con o sin fracciones/zonas) en datetime con zona UTC."""
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}(?::?\d{2})?)?$',
                 str(texto or '').strip())
    if not m:
        raise ValueError('fecha inválida')
    y, mo, d, h, mi, se, frac, zona = m.groups()
    micro = int((frac or '0')[:6].ljust(6, '0'))
    dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(se), micro, tzinfo=timezone.utc)
    if zona and zona != 'Z':
        signo = 1 if zona[0] == '+' else -1
        hh = int(zona[1:3])
        mm = int(zona[4:6] if ':' in zona else zona[3:5] or 0)
        dt = dt - signo * timedelta(hours=hh, minutes=mm)
    return dt


def ip_cliente():
    return (request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or request.remote_addr or 'desconocida')


_limites = {}


def dentro_del_limite(clave, maximo, ventana_s):
    """Límite simple en memoria: máximo N intentos por ventana de tiempo."""
    t = time.time()
    lista = [x for x in _limites.get(clave, []) if t - x < ventana_s]
    if len(lista) >= maximo:
        _limites[clave] = lista
        return False
    lista.append(t)
    _limites[clave] = lista
    if len(_limites) > 5000:  # limpieza para no crecer sin fin
        for k in [k for k, v in _limites.items() if not v or t - v[-1] > 3600]:
            _limites.pop(k, None)
    return True


def auth_configurada():
    return len(AUTH_SECRET) >= 32


def _serializador():
    return URLSafeTimedSerializer(AUTH_SECRET, salt='mesa-facturacion-sesion')


def hash_codigo(correo, codigo):
    return hmac.new(AUTH_SECRET.encode(), f'{correo}:{codigo}'.encode(), hashlib.sha256).hexdigest()


_cache_solicitantes = {'t': 0.0, 'datos': []}


def solicitantes_activos():
    """Lista de usuarios (se guarda 60 s en memoria para no consultar Supabase en cada petición)."""
    if time.time() - _cache_solicitantes['t'] < 60 and _cache_solicitantes['datos']:
        return _cache_solicitantes['datos']
    r = requests.get(f'{SUPABASE_URL}/rest/v1/solicitantes', params={'select': 'nombre,activo,correo', 'limit': 1000},
                     headers=sb_headers(), timeout=15)
    r.raise_for_status()
    datos = [{'nombre': x.get('nombre') or '', 'correo': (x.get('correo') or '').strip().lower(),
              'activo': es_si(x.get('activo'))} for x in r.json()]
    _cache_solicitantes.update({'t': time.time(), 'datos': datos})
    return datos


def buscar_usuario(correo):
    """Usuario activo con ese correo (sin distinguir mayúsculas) o None."""
    correo = (correo or '').strip().lower()
    for u in solicitantes_activos():
        if u['activo'] and u['correo'] == correo:
            return {'nombre': u['nombre'], 'correo': u['correo'], 'admin': correo in ADMIN_EMAILS}
    return None


def protegido(solo_admin=False):
    """Protege una ruta. Con AUTH_OBLIGATORIA=true exige sesión; en modo transición deja pasar sin sesión
    (como antes), pero si llega una sesión la valida y la usa."""
    def deco(fn):
        @wraps(fn)
        def envoltura(*args, **kwargs):
            g.usuario = None
            cab = request.headers.get('Authorization', '')
            token = cab[7:].strip() if cab.lower().startswith('bearer ') else ''
            if token:
                if not auth_configurada():
                    log.error('Llegó una sesión pero falta AUTH_SECRET (mínimo 32 caracteres) en Render → Environment')
                    return error('El inicio de sesión no está configurado en el servidor.', 503)
                try:
                    datos = _serializador().loads(token, max_age=int(SESION_HORAS * 3600))
                except BadSignature:
                    return error('Tu sesión venció. Ingresa de nuevo.', 401)
                try:
                    usuario = buscar_usuario(datos.get('c'))
                except Exception as ex:
                    log.error('No se pudo validar la sesión: %s', ex)
                    return error('No se pudo validar tu sesión. Intenta de nuevo en un momento.', 502)
                if not usuario:
                    return error('Tu sesión venció. Ingresa de nuevo.', 401)
                g.usuario = usuario
            elif AUTH_OBLIGATORIA:
                return error('Debes iniciar sesión.', 401)
            if solo_admin and g.usuario is not None and not g.usuario['admin']:
                return error('No tienes permiso para esta acción.', 403)
            return fn(*args, **kwargs)
        return envoltura
    return deco


def enviar_correo_brevo(destino, asunto, html_cuerpo):
    r = requests.post('https://api.brevo.com/v3/smtp/email',
                      headers={'api-key': BREVO_API_KEY, 'Content-Type': 'application/json', 'Accept': 'application/json'},
                      json={'sender': {'name': SENDER_NAME, 'email': SENDER_EMAIL}, 'to': [{'email': destino}],
                            'subject': asunto, 'htmlContent': html_cuerpo}, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f'Brevo respondió {r.status_code}: {r.text[:200]}')


def tipo_clave_supabase():
    """Dice qué clase de clave hay en SUPABASE_KEY (sin mostrarla): conviene service_role o secret."""
    k = SUPABASE_KEY
    if k.startswith('sb_secret_'):
        return 'secret'
    if k.startswith('sb_publishable_'):
        return 'publishable (anon)'
    partes = k.split('.')
    if len(partes) == 3:
        try:
            carga = partes[1] + '=' * (-len(partes[1]) % 4)
            return json.loads(base64.urlsafe_b64decode(carga)).get('role', 'desconocida')
        except Exception:
            return 'desconocida'
    return 'desconocida'


@app.route('/auth/solicitar', methods=['POST'])
def auth_solicitar():
    """Paso 1: la persona escribe su correo y se le envía un código de 6 dígitos."""
    if not (auth_configurada() and sb_configurado() and BREVO_API_KEY):
        log.error('Inicio de sesión sin configurar: faltan AUTH_SECRET (32+ caracteres), SUPABASE_* o BREVO_API_KEY')
        return error('El inicio de sesión aún no está configurado. Avisa a la administradora.', 503)
    if not dentro_del_limite(f'sol:{ip_cliente()}', 10, 600):
        return error('Demasiados intentos. Espera unos minutos e intenta de nuevo.', 429)
    datos = request.get_json(silent=True) or {}
    correo = limpiar(datos.get('correo'), 120).lower()
    if not EMAIL_RE.match(correo):
        return error('Escribe un correo válido.', 400)
    mensaje = 'Si el correo está registrado, te enviamos un código. Revisa tu bandeja de entrada y el correo no deseado.'

    try:
        usuario = buscar_usuario(correo)
    except Exception as ex:
        log.error('Login: no se pudo leer solicitantes: %s', ex)
        return error('No se pudo conectar con el sistema. Intenta de nuevo en un minuto.', 502)
    if not usuario:
        return jsonify({'ok': True, 'mensaje': mensaje}), 200  # misma respuesta: no se revela quién está registrado

    try:
        desde = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        r = requests.get(f'{SUPABASE_URL}/rest/v1/login_codes',
                         params={'select': 'id', 'correo': f'eq.{correo}', 'created_at': f'gte.{desde}'},
                         headers=sb_headers(), timeout=15)
        r.raise_for_status()
        if len(r.json()) >= MAX_CODIGOS_15MIN:
            return error('Pediste varios códigos seguidos. Espera unos minutos e intenta de nuevo.', 429)
        requests.patch(f'{SUPABASE_URL}/rest/v1/login_codes', params={'correo': f'eq.{correo}', 'used': 'eq.false'},
                       headers=sb_headers({'Prefer': 'return=minimal'}), json={'used': True}, timeout=15)
        codigo = f'{secrets.randbelow(10 ** 6):06d}'
        vence = (datetime.now(timezone.utc) + timedelta(minutes=CODIGO_MINUTOS)).isoformat()
        r = requests.post(f'{SUPABASE_URL}/rest/v1/login_codes', headers=sb_headers({'Prefer': 'return=minimal'}),
                          json={'correo': correo, 'code_hash': hash_codigo(correo, codigo), 'expires_at': vence}, timeout=15)
        r.raise_for_status()
    except Exception as ex:
        log.error('Login: no se pudo guardar el código para %s: %s', correo, ex)
        return error('No se pudo generar el código. Intenta de nuevo en un minuto.', 502)

    cuerpo = (f'<p>Hola {html.escape(usuario["nombre"])},</p>'
              f'<p>Tu código para ingresar a la Mesa de Facturación Tándem es:</p>'
              f'<p style="font-size:28px;font-weight:bold;letter-spacing:6px;">{codigo}</p>'
              f'<p>Vence en {CODIGO_MINUTOS} minutos. Si no lo pediste tú, ignora este mensaje.</p>')
    try:
        enviar_correo_brevo(correo, 'Tu código de acceso a la Mesa de Facturación', cuerpo)
    except Exception as ex:
        log.error('Login: no se pudo enviar el código a %s: %s', correo, ex)
        return error('No se pudo enviar el correo con el código. Intenta de nuevo en un minuto.', 502)
    return jsonify({'ok': True, 'mensaje': mensaje}), 200


@app.route('/auth/verificar', methods=['POST'])
def auth_verificar():
    """Paso 2: la persona escribe el código y recibe una sesión."""
    if not (auth_configurada() and sb_configurado()):
        return error('El inicio de sesión aún no está configurado. Avisa a la administradora.', 503)
    if not dentro_del_limite(f'ver:{ip_cliente()}', 20, 600):
        return error('Demasiados intentos. Espera unos minutos e intenta de nuevo.', 429)
    datos = request.get_json(silent=True) or {}
    correo = limpiar(datos.get('correo'), 120).lower()
    codigo = re.sub(r'\D', '', str(datos.get('codigo') or ''))
    if not EMAIL_RE.match(correo) or len(codigo) != 6:
        return error('Escribe el código de 6 dígitos.', 400)
    incorrecto = 'Código incorrecto o vencido. Pide uno nuevo si hace falta.'

    try:
        usuario = buscar_usuario(correo)
        if not usuario:
            return error(incorrecto, 401)
        r = requests.get(f'{SUPABASE_URL}/rest/v1/login_codes',
                         params={'select': 'id,code_hash,expires_at,attempts', 'correo': f'eq.{correo}',
                                 'used': 'eq.false', 'order': 'created_at.desc', 'limit': 1},
                         headers=sb_headers(), timeout=15)
        r.raise_for_status()
        filas = r.json()
        if not filas:
            return error(incorrecto, 401)
        fila = filas[0]
        if parse_ts(fila['expires_at']) < datetime.now(timezone.utc):
            return error(incorrecto, 401)
        if int(fila.get('attempts') or 0) >= MAX_INTENTOS:
            return error('Demasiados intentos con ese código. Pide uno nuevo.', 401)
        marca = {'Prefer': 'return=minimal'}
        filtro = {'id': f'eq.{fila["id"]}'}
        if not hmac.compare_digest(str(fila['code_hash']), hash_codigo(correo, codigo)):
            requests.patch(f'{SUPABASE_URL}/rest/v1/login_codes', params=filtro, headers=sb_headers(marca),
                           json={'attempts': int(fila.get('attempts') or 0) + 1}, timeout=15)
            return error('Código incorrecto. Revisa el número e intenta de nuevo.', 401)
        requests.patch(f'{SUPABASE_URL}/rest/v1/login_codes', params=filtro, headers=sb_headers(marca),
                       json={'used': True}, timeout=15)
    except Exception as ex:
        log.error('Login: no se pudo verificar el código de %s: %s', correo, ex)
        return error('No se pudo verificar el código. Intenta de nuevo en un minuto.', 502)

    token = _serializador().dumps({'c': usuario['correo']})
    return jsonify({'ok': True, 'token': token, 'horas': SESION_HORAS,
                    'usuario': {'nombre': usuario['nombre'], 'correo': usuario['correo'], 'admin': usuario['admin']}}), 200


@app.route('/auth/yo', methods=['GET'])
@protegido()
def auth_yo():
    """Para que el portal compruebe, al abrirse, que su sesión guardada sigue vigente."""
    if g.usuario is None:
        return error('Debes iniciar sesión.', 401)
    return jsonify({'ok': True, 'usuario': g.usuario}), 200


# ─── DATOS DEL CLIENTE, REGLAS Y AVISOS ──────────────────────────────────────
def cliente_del_usuario(nombre, id_cliente):
    """Fila del cliente asignado a esa persona (nombre, empresa y MAT) o None.
    La columna del MAT puede llamarse mat o MAT; si aún no existe, el MAT queda vacío."""
    url = f'{SUPABASE_URL}/rest/v1/clientes_por_solicitante'
    base = {'solicitante': f'eq.{nombre}', 'id_cliente': f'eq.{id_cliente}', 'limit': 1}
    for columna in ('mat', 'MAT', None):
        r = requests.get(url, params={**base, 'select': 'nombre_cliente,empresa' + (f',{columna}' if columna else '')},
                         headers=sb_headers(), timeout=15)
        if r.status_code == 400 and columna is not None:
            continue  # esa columna no existe: se prueba con la siguiente
        r.raise_for_status()
        filas = r.json()
        if not filas:
            return None
        fila = filas[0]
        fila['mat'] = texto_mat(fila.get('mat', fila.get('MAT')))
        return fila


def regla_cliente(id_cliente):
    """Documentos que exige el cliente: (requiere_oc, requiere_plantilla), o None si el cliente no tiene reglas."""
    r = requests.get(f'{SUPABASE_URL}/rest/v1/reglas_documentos',
                     params={'select': 'oc,plantilla', 'id_cliente': f'eq.{id_cliente}', 'limit': 1},
                     headers=sb_headers(), timeout=15)
    r.raise_for_status()
    filas = r.json()
    if not filas:
        return None
    return es_si(filas[0].get('oc')), es_si(filas[0].get('plantilla'))


_avisos_config = {}


def avisar_cliente_incompleto(id_cliente, cliente, solicitante, faltantes):
    """Avisa por correo a la administradora (una vez cada 6 horas por cliente) que a un cliente le falta configuración."""
    clave = (id_cliente, tuple(sorted(faltantes)))
    t = time.time()
    if t - _avisos_config.get(clave, 0) < 6 * 3600 or not BREVO_API_KEY:
        return
    _avisos_config[clave] = t
    detalle = {'mat': 'no tiene <b>MAT</b> (columna MAT de la tabla <i>clientes_por_solicitante</i>)',
               'reglas': 'no tiene <b>reglas de documentos</b> (una fila con su <i>id_cliente</i> en la tabla <i>reglas_documentos</i>)'}
    lista = ''.join(f'<li>{detalle[x]}</li>' for x in faltantes)
    cuerpo = (f'<p>Un solicitante intentó enviar una solicitud para un cliente que no está completamente configurado en Supabase:</p>'
              f'<p><b>Cliente:</b> {html.escape(cliente or "")} ({html.escape(id_cliente)})<br>'
              f'<b>Solicitante:</b> {html.escape(solicitante or "")}</p><p>Le falta:</p><ul>{lista}</ul>'
              f'<p>Mientras tanto, el portal no le permite enviar solicitudes para ese cliente.</p>')
    for destino in [x.strip() for x in NOTIFY_EMAIL.split(',') if x.strip()]:
        try:
            enviar_correo_brevo(destino, f'Falta configurar el cliente {id_cliente}', cuerpo)
        except Exception as ex:
            log.error('No se pudo avisar a %s de la configuración incompleta de %s: %s', destino, id_cliente, ex)


def insertar_solicitud(fila, extras):
    """Inserta la solicitud. Si Supabase dice que una columna opcional todavía no existe, la quita y reintenta."""
    extras = dict(extras)
    r = None
    for _ in range(len(extras) + 1):
        r = requests.post(f'{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}', headers=sb_headers({'Prefer': 'return=minimal'}),
                          json={**fila, **extras}, timeout=15)
        if r.status_code != 400 or not extras:
            return r
        texto = r.text or ''
        m = re.search(r"'([a-z_]+)' column", texto)
        columna = m.group(1) if m and m.group(1) in extras else next((k for k in extras if k in texto), None)
        if columna is None:
            return r
        log.warning('Falta la columna %s en Supabase; se guarda sin ese dato', columna)
        extras.pop(columna)
    return r


@app.route('/avisos/cliente-incompleto', methods=['POST'])
@protegido()
def aviso_cliente_incompleto():
    """El portal avisa aquí cuando alguien elige un cliente sin MAT o sin reglas; se envía un correo a la administradora."""
    if g.usuario is None:
        return error('Debes iniciar sesión.', 401)
    if not dentro_del_limite(f'cfg:{ip_cliente()}', 20, 3600):
        return error('Demasiados avisos. Intenta más tarde.', 429)
    id_cliente = limpiar((request.get_json(silent=True) or {}).get('id_cliente'), 40)
    try:
        fila = cliente_del_usuario(g.usuario['nombre'], id_cliente)
        if not fila:
            return error('Ese cliente no está asignado a tu usuario.', 403)
        regla = regla_cliente(id_cliente)
    except Exception as ex:
        log.error('Aviso de configuración: no se pudo leer el cliente %s: %s', id_cliente, ex)
        return error('No se pudo validar el cliente.', 502)
    faltantes = [x for x, falta in (('mat', not fila.get('mat')), ('reglas', regla is None)) if falta]
    if faltantes:
        avisar_cliente_incompleto(id_cliente, limpiar(fila.get('nombre_cliente'), 200), g.usuario['nombre'], faltantes)
    return jsonify({'ok': True, 'avisado': bool(faltantes), 'faltan': faltantes}), 200


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
            return jsonify({'status': 'ok', 'db': 'ok', 'clave': tipo_clave_supabase(),
                            'sesion_obligatoria': AUTH_OBLIGATORIA, 'login_listo': auth_configurada()}), 200
        log.error('Health: Supabase respondió %s', r.status_code)
    except Exception as e:
        log.error('Health: no se pudo contactar Supabase: %s', e)
    return jsonify({'status': 'degraded', 'db': 'error'}), 503


# ─── RECIBIR SOLICITUD ───────────────────────────────────────────────────────
@app.route('/solicitud', methods=['POST'])
@protegido()
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
    montos = {k: v for k, v in (('monto_total', a_monto(data.get('monto_total'))),
                                ('monto_sin_igv', a_monto(data.get('monto_sin_igv')))) if v is not None}
    moneda = a_moneda(data.get('moneda'))
    oc_numero = limpiar(data.get('oc_numero'), 120)
    credito = limpiar(data.get('credito'), 20)

    if not solicitante or not cliente or not id_cliente:
        return error('Faltan datos obligatorios (solicitante o cliente).', 400)

    # Con sesión iniciada, la identidad, el cliente y el MAT salen de la base de datos, no del navegador.
    mat = ''
    if g.usuario is not None:
        if tipo_doc not in TIPOS_DOC or sociedad not in SOCIEDADES:
            return error('Tipo de documento o sociedad no válidos.', 400)
        try:
            fila_cli = cliente_del_usuario(g.usuario['nombre'], id_cliente)
            regla = regla_cliente(id_cliente) if fila_cli else None
        except Exception as ex:
            log.error('No se pudo validar el cliente %s de %s: %s', id_cliente, g.usuario['nombre'], ex)
            return error('No se pudo validar el cliente. Intenta de nuevo en un minuto.', 502)
        if not fila_cli:
            return error('Ese cliente no está asignado a tu usuario.', 403)
        solicitante = limpiar(g.usuario['nombre'], 120)
        correo_sol = limpiar(g.usuario['correo'], 120)
        cliente = limpiar(fila_cli.get('nombre_cliente'), 200)
        empresa = limpiar(fila_cli.get('empresa'), 80)
        mat = texto_mat(fila_cli.get('mat'))
        faltantes = [x for x, falta in (('mat', not mat), ('reglas', regla is None)) if falta]
        if faltantes:
            avisar_cliente_incompleto(id_cliente, cliente, solicitante, faltantes)
            if 'mat' in faltantes:
                return error('Este cliente todavía no tiene MAT configurado. Ya se avisó a la administradora; '
                             'no se puede enviar hasta que lo configure.', 422)
            return error('Este cliente todavía no tiene reglas de documentos configuradas. Ya se avisó a la administradora; '
                         'no se puede enviar hasta que lo configure.', 422)
        requiere_oc, requiere_plantilla = regla
        if requiere_oc:
            if not oc_numero or oc_numero.upper() == 'SIN OC':
                return error('No se pudo identificar el N.º de OC. Sube de nuevo una OC con texto legible.', 422)
        else:
            oc_numero = 'SIN OC'
        if requiere_plantilla and not credito:
            return error('No se pudo leer el crédito (celda D8) de la plantilla. Vuelve a subirla.', 422)
        if not requiere_plantilla:
            credito = ''

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
                extras = dict(montos)
                if montos and moneda:
                    extras['moneda'] = moneda
                for clave, valor in (('oc_numero', oc_numero), ('credito', credito), ('mat', mat)):
                    if valor:
                        extras[clave] = valor
                r_db = insertar_solicitud(fila, extras)
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
    <p><b>ID de cliente (ENG):</b> {e(id_cliente)} &nbsp;|&nbsp; <b>N.º de OC:</b> {e(oc_numero) or '—'} &nbsp;|&nbsp; <b>Crédito:</b> {e(credito) or '—'} &nbsp;|&nbsp; <b>MAT:</b> {e(mat) or '—'}</p>
    <p><b>Monto:</b> {e(('%s %s con IGV' % (moneda or 'PEN', format(montos['monto_total'], ',.2f')))) if 'monto_total' in montos else '—'}</p>
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
@protegido()
def obtener_config():
    if not sb_configurado():
        log.error('Falta SUPABASE_URL / SUPABASE_KEY en Render → Environment')
        return error('Base de datos no configurada.', 500)

    def sb_get(tabla, select, tolerar_400=False):
        r = requests.get(f'{SUPABASE_URL}/rest/v1/{tabla}', params={'select': select, 'limit': 1000},
                         headers=sb_headers(), timeout=15)
        if tolerar_400 and r.status_code == 400:
            return None
        r.raise_for_status()
        return r.json()

    try:
        solicitantes = sb_get('solicitantes', 'nombre,activo,correo')
        empresas = sb_get('empresas', 'nombre,activo')
        clientes = None
        for columna in ('mat', 'MAT'):   # la columna puede llamarse mat o MAT
            clientes = sb_get('clientes_por_solicitante', f'solicitante,nombre_cliente,id_cliente,empresa,{columna}', tolerar_400=True)
            if clientes is not None:
                break
        if clientes is None:  # la columna del MAT aún no existe
            clientes = sb_get('clientes_por_solicitante', 'solicitante,nombre_cliente,id_cliente,empresa')
        reglas = sb_get('reglas_documentos', 'id_cliente,nombre_cliente,plantilla,oc,hes,acta_conformidad,'
                                              'excel_liquidacion,aceptacion_servicio,cdr,adicionales,notas')
    except Exception as ex:
        log.error('/config: no se pudo leer Supabase: %s', ex)
        return error('No se pudo leer la configuración. Intenta de nuevo en unos minutos.', 502)

    # Con sesión iniciada, cada persona recibe solo lo suyo: sus clientes y las reglas de esos clientes.
    if g.usuario is not None:
        clientes = [c for c in clientes if c['solicitante'] == g.usuario['nombre']]
        ids = {c['id_cliente'] for c in clientes}
        reglas = [r for r in reglas if r['id_cliente'] in ids]
        solicitantes = [x for x in solicitantes if (x.get('correo') or '').strip().lower() == g.usuario['correo']]

    return jsonify({
        'solicitantes': [[r['nombre'], r['activo'], r['correo']] for r in solicitantes],
        'empresas': [[r['nombre'], r['activo']] for r in empresas],
        'clientes': [[r['solicitante'], r['nombre_cliente'], r['id_cliente'], r['empresa'], texto_mat(r.get('mat', r.get('MAT'))) or None] for r in clientes],
        'reglas': [[r['id_cliente'], r['nombre_cliente'], r['plantilla'], r['oc'], r['hes'],
                    r['acta_conformidad'], r['excel_liquidacion'], r['aceptacion_servicio'],
                    r['cdr'], r['adicionales'], r['notas']] for r in reglas],
    }), 200


# ─── LISTAR SOLICITUDES (Mis solicitudes / Panel) ────────────────────────────
@app.route('/solicitudes', methods=['GET'])
@protegido()
def listar_solicitudes():
    if not sb_configurado():
        log.error('Falta SUPABASE_URL / SUPABASE_KEY en Render → Environment')
        return error('Base de datos no configurada.', 500)
    params = {'select': '*', 'order': 'fecha_solicitud.desc', 'limit': 500}
    if g.usuario is not None and not g.usuario['admin']:
        params['solicitante'] = f'eq.{g.usuario["nombre"]}'  # cada persona ve solo sus solicitudes
    try:
        r = requests.get(f'{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}', params=params,
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
    """Devuelve (fila|None, nivel). El nivel indica qué columnas existen en Supabase:
    'completo' (fechas + actualizado_por), 'fechas' o 'basico' (solo estado y fecha_respuesta)."""
    url = f'{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}'
    niveles = (('completo', 'sol_id,estado,fecha_facturado,fecha_envio,actualizado_por'),
               ('fechas', 'sol_id,estado,fecha_facturado,fecha_envio'),
               ('basico', 'sol_id,estado'))
    for nivel, columnas in niveles:
        r = requests.get(url, params={'select': columnas, 'sol_id': f'eq.{sol_id}', 'limit': 1},
                         headers=sb_headers(), timeout=15)
        if r.status_code == 400 and nivel != 'basico':
            continue  # esas columnas todavía no existen: se prueba con menos
        r.raise_for_status()
        filas = r.json()
        return (filas[0] if filas else None), nivel


@app.route('/solicitudes/<sol_id>/estado', methods=['POST'])
@protegido(solo_admin=True)
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
        fila, nivel = _leer_fila(sol_id)
        if fila is None:
            return error('No se encontró la solicitud.', 404)

        t = ahora()  # la hora la pone el servidor, no el navegador
        if nivel != 'basico':
            if nuevo == 'Pendiente':
                body = {'estado': nuevo, 'fecha_respuesta': None, 'fecha_facturado': None, 'fecha_envio': None}
            elif nuevo == 'Facturado':
                body = {'estado': nuevo, 'fecha_respuesta': t, 'fecha_facturado': t, 'fecha_envio': None}
            else:  # Enviado
                body = {'estado': nuevo, 'fecha_respuesta': t, 'fecha_envio': t,
                        'fecha_facturado': fila.get('fecha_facturado') or t}
            if nivel == 'completo':
                body['actualizado_por'] = g.usuario['nombre'] if g.usuario is not None else None
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
