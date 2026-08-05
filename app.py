from flask import Flask, request, jsonify
from flask_cors import CORS
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import base64
import os
from datetime import datetime

app = Flask(__name__)
CORS(app)

# Configuración de correo Outlook
OUTLOOK_EMAIL = os.getenv('OUTLOOK_EMAIL', 'hparaguay@tandem.pe')
OUTLOOK_PASSWORD = os.getenv('OUTLOOK_PASSWORD', 'tu_contraseña_aqui')
SMTP_SERVER = 'smtp.office365.com'
SMTP_PORT = 587

@app.route('/solicitud', methods=['POST'])
def recibir_solicitud():
    try:
        data = request.json
        
        # Validar que lleguen los datos requeridos
        if not data or 'sol_id' not in data:
            return jsonify({'error': 'Datos incompletos'}), 400
        
        sol_id = data.get('sol_id')
        solicitante = data.get('solicitante', '')
        correo_sol = data.get('correo_sol', '')
        cliente = data.get('cliente', '')
        id_cliente = data.get('id_cliente', '')
        empresa = data.get('empresa', '')
        tipo_doc = data.get('tipo_doc', '')
        sociedad = data.get('sociedad', '')
        documentos_info = data.get('documentos', '')
        notas = data.get('notas', '')
        fecha_hora = data.get('fecha_hora', '')
        archivos = data.get('archivos', [])  # Lista de {nombre, base64}
        
        # Crear correo
        msg = MIMEMultipart()
        msg['From'] = OUTLOOK_EMAIL
        msg['To'] = OUTLOOK_EMAIL
        msg['Subject'] = f'Nueva Solicitud de Facturación: {sol_id}'
        
        # Cuerpo del correo
        cuerpo = f"""
Solicitud: {sol_id}
Solicitante: {solicitante} ({correo_sol})
Cliente: {cliente} - {id_cliente}
Empresa: {empresa}
Tipo Documento: {tipo_doc}
Sociedad: {sociedad}
Documentos: {documentos_info}
Notas: {notas}
Fecha/Hora de Recepción: {fecha_hora}

---
Documentos adjuntos en este correo.
"""
        
        msg.attach(MIMEText(cuerpo, 'plain'))
        
        # Adjuntar archivos decodificados
        for archivo in archivos:
            try:
                nombre = archivo.get('nombre', 'documento')
                base64_data = archivo.get('base64', '')
                
                if not base64_data:
                    continue
                
                # Decodificar base64
                archivo_bytes = base64.b64decode(base64_data.split(',')[1] if ',' in base64_data else base64_data)
                
                # Crear attachment
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(archivo_bytes)
                encoders.encode_base64(part)
                part.add_header('Content-Disposition', f'attachment; filename= {nombre}')
                msg.attach(part)
            except Exception as e:
                print(f"Error al adjuntar {archivo.get('nombre')}: {e}")
                continue
        
        # Enviar correo
        try:
            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
            server.starttls()
            server.login(OUTLOOK_EMAIL, OUTLOOK_PASSWORD)
            server.send_message(msg)
            server.quit()
            
            return jsonify({'ok': True, 'message': f'Solicitud {sol_id} enviada correctamente', 'id': sol_id}), 200
        except smtplib.SMTPException as e:
            print(f"Error SMTP: {e}")
            return jsonify({'ok': False, 'error': f'Error al enviar correo: {str(e)}'}), 500
            
    except Exception as e:
        print(f"Error general: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'}), 200

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
