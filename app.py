from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os
from datetime import datetime

app = Flask(__name__)
CORS(app)

# ─── CONFIGURACIÓN (se define en Render → Environment, no aquí) ──────────────
BREVO_API_KEY = os.getenv('BREVO_API_KEY', '')
SENDER_EMAIL  = os.getenv('SENDER_EMAIL', 'hparaguay@tandem.pe')
SENDER_NAME   = 'Mesa de Facturación Tándem'
NOTIFY_EMAIL  = os.getenv('NOTIFY_EMAIL', 'hparaguay@tandem.pe')

SUPABASE_URL = os.getenv('SUPABASE_URL', '')          # ej. https://xxxxx.supabase.co
SUPABASE_KEY = os.getenv('SUPABASE_KEY', '')           # la clave service_role
SUPABASE_TABLE = 'solicitudes'


def supabase_headers():
    return {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json'
    }


@app.route('/', methods=['GET'])
def home():
    return jsonify({'status': 'ok', 'servicio': 'Mesa de Facturación Tándem - backend'}), 200


@app.route('/solicitud', methods=['POST'])
def recibir_solicitud():
    try:
        data = request.json
        if not data or 'sol_id' not in data:
            return jsonify({'error': 'Datos incompletos'}), 400

        sol_id           = data.get('sol_id', '')
        solicitante      = data.get('solicitante', '')
        correo_sol       = data.get('correo_sol', '')
        cliente          = data.get('cliente', '')
        id_cliente       = data.get('id_cliente', '')
        empresa          = data.get('empresa', '')
        tipo_doc         = data.get('tipo_doc', '')
        sociedad         = data.get('sociedad', '')
        documentos_info  = data.get('documentos_info', '')
        notas            = data.get('notas', '')
        fecha_hora       = data.get('fecha_hora', '') or datetime.utcnow().isoformat()
        archivos         = data.get('archivos', [])

        aviso_bd = None
        # ── 1) Guardar el registro en Supabase (la fuente real para el Panel) ──
        if SUPABASE_URL and SUPABASE_KEY:
            try:
                row = {
                    'sol_id': sol_id,
                    'solicitante': solicitante,
                    'correo_solicitante': correo_sol,
                    'cliente': cliente,
                    'id_cliente': id_cliente,
                    'empresa': empresa,
                    'tipo_doc': tipo_doc,
                    'sociedad': sociedad,
                    'documentos': documentos_info,
                    'notas': notas,
                    'estado': 'Pendiente'
                }
                r_db = requests.post(
                    f'{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}',
                    headers={**supabase_headers(), 'Prefer': 'return=minimal'},
                    json=row, timeout=15
                )
                if r_db.status_code not in (200, 201, 204):
                    aviso_bd = f'No se pudo guardar en la base de datos ({r_db.status_code}): {r_db.text[:150]}'
            except Exception as e:
                aviso_bd = f'No se pudo contactar la base de datos: {e}'
        else:
            aviso_bd = 'Base de datos no configurada (falta SUPABASE_URL / SUPABASE_KEY)'

        # ── 2) Enviar el correo con adjuntos vía Brevo ──────────────────────
        if not BREVO_API_KEY:
            return jsonify({'ok': True, 'sol_id': sol_id, 'aviso': (aviso_bd or '') + ' · Falta BREVO_API_KEY, no se envió correo'}), 200

        html_body = f"""
        <h2>Nueva solicitud de facturación</h2>
        <p><b>Código:</b> {sol_id}</p>
        <p><b>Solicitante:</b> {solicitante} ({correo_sol})</p>
        <p><b>Empresa:</b> {empresa}</p>
        <p><b>Cliente:</b> {cliente} ({id_cliente})</p>
        <p><b>Tipo de documento:</b> {tipo_doc}</p>
        <p><b>Sociedad:</b> {sociedad}</p>
        <p><b>Sustentos incluidos:</b> {documentos_info}</p>
        <p><b>Notas:</b> {notas or '—'}</p>
        <p><b>Fecha y hora de recepción:</b> {fecha_hora}</p>
        """

        attachments = []
        for a in archivos:
            nombre = a.get('nombre', 'documento')
            b64 = a.get('base64', '')
            if ',' in b64:
                b64 = b64.split(',', 1)[1]
            if b64:
                attachments.append({'name': nombre, 'content': b64})

        payload = {
            'sender': {'name': SENDER_NAME, 'email': SENDER_EMAIL},
            'to': [{'email': NOTIFY_EMAIL}],
            'subject': f'Nueva solicitud de facturación: {sol_id} — {cliente}',
            'htmlContent': html_body
        }
        if attachments:
            payload['attachment'] = attachments

        r = requests.post(
            'https://api.brevo.com/v3/smtp/email',
            headers={'api-key': BREVO_API_KEY, 'Content-Type': 'application/json', 'Accept': 'application/json'},
            json=payload, timeout=20
        )

        if r.status_code not in (200, 201):
            return jsonify({'ok': True, 'sol_id': sol_id, 'aviso': (aviso_bd or '') + f' · Correo falló ({r.status_code})'}), 200

        return jsonify({'ok': True, 'sol_id': sol_id, 'aviso': aviso_bd}), 200

    except Exception as e:
        return jsonify({'error': 'Error interno', 'detalle': str(e)}), 500


@app.route('/solicitudes', methods=['GET'])
def listar_solicitudes():
    """El Panel de Facturación llama aquí para ver TODAS las solicitudes."""
    try:
        if not (SUPABASE_URL and SUPABASE_KEY):
            return jsonify({'error': 'Base de datos no configurada'}), 500
        r = requests.get(
            f'{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?select=*&order=fecha_solicitud.desc&limit=500',
            headers=supabase_headers(), timeout=15
        )
        if not r.ok:
            return jsonify({'error': f'Supabase respondió {r.status_code}', 'detalle': r.text[:300]}), 502
        return jsonify(r.json()), 200
    except Exception as e:
        return jsonify({'error': 'Error interno', 'detalle': str(e)}), 500


@app.route('/solicitudes/<sol_id>/estado', methods=['POST'])
def actualizar_estado(sol_id):
    """Para marcar una solicitud como 'En proceso' o 'Facturado' desde el Panel."""
    try:
        if not (SUPABASE_URL and SUPABASE_KEY):
            return jsonify({'error': 'Base de datos no configurada'}), 500
        data = request.json or {}
        nuevo_estado = data.get('estado', '')
        if not nuevo_estado:
            return jsonify({'error': 'Falta el nuevo estado'}), 400

        body = {'estado': nuevo_estado}
        if nuevo_estado.lower() == 'facturado':
            body['fecha_respuesta'] = datetime.utcnow().isoformat()

        r = requests.patch(
            f'{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?sol_id=eq.{sol_id}',
            headers={**supabase_headers(), 'Prefer': 'return=minimal'},
            json=body, timeout=15
        )
        if not r.ok:
            return jsonify({'error': f'Supabase respondió {r.status_code}', 'detalle': r.text[:300]}), 502
        return jsonify({'ok': True}), 200
    except Exception as e:
        return jsonify({'error': 'Error interno', 'detalle': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
