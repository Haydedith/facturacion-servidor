from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os
from datetime import datetime

app = Flask(__name__)
CORS(app)

# ─── CONFIGURACIÓN (se define en Render → Environment, no aquí) ──────────────
BREVO_API_KEY = os.getenv('BREVO_API_KEY', '')
SENDER_EMAIL  = os.getenv('SENDER_EMAIL', 'hparaguay@tandem.pe')   # debe estar verificado en Brevo
SENDER_NAME   = 'Mesa de Facturación Tándem'
NOTIFY_EMAIL  = os.getenv('NOTIFY_EMAIL', 'hparaguay@tandem.pe')


@app.route('/', methods=['GET'])
def home():
    # Solo para confirmar que el servidor está vivo si alguien abre la URL directamente
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
        fecha_hora       = data.get('fecha_hora', '') or datetime.now().isoformat()
        archivos         = data.get('archivos', [])  # [{nombre, base64}, ...]

        if not BREVO_API_KEY:
            return jsonify({'error': 'Servidor mal configurado: falta BREVO_API_KEY en Render'}), 500

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

        # Los archivos llegan como base64, a veces con el prefijo "data:...;base64,"
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
            headers={
                'api-key': BREVO_API_KEY,
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            },
            json=payload,
            timeout=20
        )

        if r.status_code not in (200, 201):
            return jsonify({'error': f'Brevo respondió {r.status_code}', 'detalle': r.text[:300]}), 502

        return jsonify({'ok': True, 'sol_id': sol_id}), 200

    except Exception as e:
        return jsonify({'error': 'Error interno', 'detalle': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
