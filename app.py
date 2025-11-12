#!/usr/bin/env python3
"""
IEEE PDF Agent - Web Interface
A modern web interface for converting documents to IEEE format
"""

import os
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename
import threading
import openai
from dotenv import load_dotenv

from pdf_agent import PDFAgent

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here'
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

socketio = SocketIO(app, cors_allowed_origins="*")

# Ensure upload directory exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs('output', exist_ok=True)

# Global variables for session management
sessions = {}
agent = PDFAgent()

# Initialize OpenAI from agent config
if agent.config.get('openai', {}).get('api_key'):
    openai.api_key = agent.config['openai']['api_key']
else:
    openai.api_key = os.getenv('OPENAI_API_KEY')

# Update notifications system
UPDATE_NOTIFICATIONS = [
    {
        "id": "v1.1.0", 
        "title": "Enhanced IEEE Formatting",
        "message": "Improved IEEE two-column formatting with proper title, author, and section formatting.",
        "type": "success",
        "date": "25-10-2025",
        "dismissible": True
    },
    {
        "id": "v1.0.0",
        "title": "Initial Release",
        "message": "Welcome to the IEEE PDF Agent! Convert LaTeX and Markdown files to IEEE two-column format.",
        "type": "info",
        "date": "13-10-2025",
        "dismissible": False
    }
]

# Allowed file extensions
ALLOWED_EXTENSIONS = {'.md', '.markdown', '.tex', '.latex'}

def allowed_file(filename):
    """Check if file extension is allowed"""
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS

def get_session_id():
    """Generate a unique session ID"""
    return str(uuid.uuid4())

def get_session(session_id):
    """Get or create a session"""
    if session_id not in sessions:
        sessions[session_id] = {
            'id': session_id,
            'created_at': datetime.now(),
            'files': [],
            'status': 'active',
            'dismissed_updates': []
        }
    return sessions[session_id]

@app.route('/')
def index():
    """Main chat interface"""
    return render_template('index.html')

@app.route('/api/upload', methods=['POST'])
def upload_file():
    """Handle file uploads"""
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
        
        file = request.files['file']
        session_id = request.form.get('session_id', get_session_id())
        
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400
        
        if file and file.filename and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{timestamp}_{filename}"
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            
            # Add to session
            session = get_session(session_id)
            session['files'].append({
                'filename': filename,
                'original_name': file.filename,
                'filepath': filepath,
                'uploaded_at': datetime.now().isoformat(),
                'status': 'uploaded'
            })
            
            return jsonify({
                'success': True,
                'filename': filename,
                'session_id': session_id,
                'message': f'File {file.filename} uploaded successfully'
            })
        else:
            return jsonify({'error': 'Invalid file type. Only .md, .markdown, .tex, .latex files are allowed'}), 400
    
    except Exception as e:
        logger.error(f"Upload error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/convert', methods=['POST'])
def convert_file():
    """Convert uploaded file to PDF"""
    try:
        data = request.get_json()
        session_id = data.get('session_id')
        filename = data.get('filename')
        options = data.get('options', {})
        
        if not session_id or not filename:
            return jsonify({'error': 'Session ID and filename required'}), 400
        
        session = get_session(session_id)
        file_info = None
        
        # Find the file in session
        for file in session['files']:
            if file['filename'] == filename:
                file_info = file
                break
        
        if not file_info:
            return jsonify({'error': 'File not found in session'}), 404
        
        # Update file status
        file_info['status'] = 'processing'
        
        # Start conversion in background
        thread = threading.Thread(
            target=process_conversion,
            args=(session_id, file_info, options)
        )
        thread.daemon = True
        thread.start()
        
        return jsonify({
            'success': True,
            'message': 'Conversion started',
            'session_id': session_id
        })
    
    except Exception as e:
        logger.error(f"Conversion error: {e}")
        return jsonify({'error': str(e)}), 500

def process_conversion(session_id, file_info, options):
    """Process file conversion in background"""
    try:
        # Emit status update
        socketio.emit('conversion_status', {
            'session_id': session_id,
            'filename': file_info['filename'],
            'status': 'processing',
            'message': 'Starting conversion...'
        })
        
        # Convert file with IEEE formatting
        send_email = options.get('send_email', True)
        email_recipient = options.get('email_recipient', None)
        
        success = agent.process_file(
            file_info['filepath'],
            send_email=send_email,
            email_recipient=email_recipient
        )
        
        if success:
            # Find the generated PDF
            output_dir = agent.config['output']['directory']
            pdf_files = list(Path(output_dir).glob('*.pdf'))
            latest_pdf = max(pdf_files, key=os.path.getctime) if pdf_files else None
            
            file_info['status'] = 'completed'
            file_info['pdf_path'] = str(latest_pdf) if latest_pdf else None
            file_info['completed_at'] = datetime.now().isoformat()
            
            socketio.emit('conversion_status', {
                'session_id': session_id,
                'filename': file_info['filename'],
                'status': 'completed',
                'message': 'Conversion completed successfully!',
                'pdf_path': str(latest_pdf) if latest_pdf else None
            })
        else:
            file_info['status'] = 'failed'
            file_info['error'] = 'Conversion failed'
            
            socketio.emit('conversion_status', {
                'session_id': session_id,
                'filename': file_info['filename'],
                'status': 'failed',
                'message': 'Conversion failed. Check logs for details.'
            })
    
    except Exception as e:
        logger.error(f"Background conversion error: {e}")
        file_info['status'] = 'failed'
        file_info['error'] = str(e)
        
        socketio.emit('conversion_status', {
            'session_id': session_id,
            'filename': file_info['filename'],
            'status': 'failed',
            'message': f'Conversion error: {str(e)}'
        })

@app.route('/api/download/<path:filename>')
def download_file(filename):
    """Download converted PDF file"""
    try:
        file_path = os.path.join('output', filename)
        if os.path.exists(file_path):
            return send_file(file_path, as_attachment=True)
        else:
            return jsonify({'error': 'File not found'}), 404
    except Exception as e:
        logger.error(f"Download error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/session/<session_id>')
def get_session_info(session_id):
    """Get session information"""
    session = get_session(session_id)
    return jsonify(session)

@app.route('/api/updates')
def get_updates():
    """Get available updates"""
    return jsonify(UPDATE_NOTIFICATIONS)

@app.route('/api/updates/dismiss', methods=['POST'])
def dismiss_update():
    """Dismiss an update notification"""
    try:
        data = request.get_json()
        session_id = data.get('session_id')
        update_id = data.get('update_id')
        
        if not session_id or not update_id:
            return jsonify({'error': 'Session ID and update ID required'}), 400
        
        session = get_session(session_id)
        if update_id not in session['dismissed_updates']:
            session['dismissed_updates'].append(update_id)
        
        return jsonify({'success': True})
    
    except Exception as e:
        logger.error(f"Update dismiss error: {e}")
        return jsonify({'error': str(e)}), 500

@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    logger.info('Client connected')
    emit('connected', {'message': 'Connected to PDF Agent'})

@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection"""
    logger.info('Client disconnected')

if __name__ == '__main__':
    logger.info("Starting PDF Agent Web Interface...")
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)