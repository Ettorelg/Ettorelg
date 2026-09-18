"""Alpha Menu Windows: dedicated WebView2 window and existing ESC/POS bridge."""
import ctypes
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading
import urllib.request

import webview
from webview.menu import Menu, MenuAction, MenuSeparator

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import escpos_bridge as bridge

VERSION = '1.1.7'
BASE = 'https://menu.alphasystemsrl.it'
DATA = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'AlphaMenu'
UPDATE_URL = BASE + '/static/windows/latest.json'
UPDATE_LOCK = threading.Lock()


class LocalApi:
    """Direct WebView-to-Python bridge; avoids browser loopback/CORS restrictions."""
    ALLOWED = {'/health', '/fiscal/probe', '/fiscal/emit', '/fiscal/recover',
               '/fiscal/config/read', '/fiscal/config/write'}

    def fiscal_request(self, path, data=None):
        try:
            if path not in self.ALLOWED:
                raise ValueError('Operazione locale non consentita.')
            if path == '/health':
                result = {'message': 'Programma di stampa pronto.', 'version': bridge.BRIDGE_VERSION,
                          'fiscal': bool(bridge.epson_bridge), 'fiscal_error': bridge.EPSON_IMPORT_ERROR}
            elif not bridge.epson_bridge:
                raise ValueError(bridge.EPSON_IMPORT_ERROR or 'Componente Epson non disponibile.')
            else:
                result = bridge.epson_bridge.dispatch(path, data or {})
            return {'ok': True, 'data': result}
        except Exception as exc:
            logging.exception('Local fiscal request failed')
            return {'ok': False, 'message': str(exc)}


def request(url):
    response = urllib.request.urlopen(urllib.request.Request(url, headers={
        'Origin': BASE, 'User-Agent': 'AlphaMenu-Windows/' + VERSION}), timeout=20)
    if url.startswith(BASE) and not response.url.startswith(BASE + '/'):
        response.close()
        raise ValueError('Indirizzo download non consentito.')
    return response


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise ValueError('Versione aggiornamento non valida.')
    return tuple(map(int, value.split('.')))


def validate_update(data):
    version_tuple(data.get('version'))
    expected = '/static/windows/AlphaMenu-Setup-' + data['version'] + '.exe'
    if data.get('path') != expected or not re.fullmatch(r'[a-f0-9]{64}', data.get('sha256', '')):
        raise ValueError('Dati aggiornamento non validi.')
    return BASE + expected


def bridge_health():
    with request(f'http://127.0.0.1:{bridge.PORT}/health') as response:
        data = json.load(response)
    if data.get('version') != bridge.BRIDGE_VERSION or not data.get('fiscal'):
        raise ValueError('Un vecchio programma di stampa è già aperto. Chiudilo e riavvia Alpha Menu.')


def start_bridge():
    try:
        server = bridge.ThreadingHTTPServer((bridge.HOST, bridge.PORT), bridge.Handler)
    except OSError:
        bridge_health()  # Reuse only a compatible already-running bridge.
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def message(text, error=False):
    ctypes.windll.user32.MessageBoxW(None, text, 'Alpha Menu', 0x10 if error else 0x40)


def updates(window):
    if not UPDATE_LOCK.acquire(blocking=False):
        return
    try:
        with request(UPDATE_URL) as response:
            data = json.loads(response.read(16384))
        url = validate_update(data)
        if version_tuple(data['version']) <= version_tuple(VERSION):
            message('Alpha Menu è aggiornato. Versione ' + VERSION)
            return
        if not window.create_confirmation_dialog('Aggiornamento disponibile',
                'Scaricare Alpha Menu ' + data['version'] + '? Al termine potrai avviare l’installazione.'):
            return
        folder = DATA / 'Downloads'
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / Path(data['path']).name
        temporary = target.with_suffix('.download')
        digest = hashlib.sha256()
        total = 0
        try:
            with request(url) as response, temporary.open('wb') as output:
                while chunk := response.read(256 * 1024):
                    total += len(chunk)
                    if total > 150 * 1024 * 1024:
                        raise ValueError('Download troppo grande.')
                    output.write(chunk)
                    digest.update(chunk)
            if digest.hexdigest() != data['sha256']:
                raise ValueError('Verifica download fallita. Riprova.')
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        if window.create_confirmation_dialog('Download completato',
                'Installare ora? Completa prima eventuali ordini in corso. Alpha Menu verrà chiuso.'):
            os.startfile(target)
            window.destroy()
        else:
            message('Installazione salvata in ' + str(target))
    except Exception as exc:
        logging.exception('Update failed')
        message('Aggiornamento non riuscito: ' + str(exc), True)
    finally:
        UPDATE_LOCK.release()


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=DATA / 'app.log', level=logging.WARNING)
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('AlphaSystem.AlphaMenu')
    ctypes.windll.kernel32.CreateMutexW.restype = ctypes.c_void_p
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, 'Local\\AlphaMenuWindows')
    if ctypes.windll.kernel32.GetLastError() == 183:
        message('Alpha Menu è già aperto. Seleziona la finestra dalla barra delle applicazioni.')
        return
    server = None
    bridge_error = None
    smoke = '--smoke-test' in sys.argv
    if smoke:
        bridge.PORT = 17892
    try:
        server = start_bridge()
    except Exception as exc:
        bridge_error = str(exc)
    window = webview.create_window('Alpha Menu', BASE + '/ordini/evasione', js_api=LocalApi(),
        width=1280, height=850, min_size=(360, 560), maximized=not smoke, hidden=smoke,
        background_color='#0d1727')

    def print_status():
        try:
            bridge_health()
            message('Collegamento di stampa pronto.\nStampanti ESC/POS Ethernet, porta 9100.\n'
                    'Configura gli IP in Stampanti e pagamenti.\nEpson FP-81II RT disponibile dopo configurazione e collaudo.')
        except Exception as exc:
            message(str(exc), True)

    def navigate(path):
        if window.create_confirmation_dialog('Cambia pagina', 'Aprire questa pagina? Le modifiche non salvate potrebbero andare perse.'):
            window.load_url(BASE + path)

    menu = [Menu('Alpha Menu', [
        MenuAction('Banco ordini', lambda: navigate('/ordini/evasione')),
        MenuAction('Stampanti e pagamenti', lambda: navigate('/dashboard_user#stampanti')),
        MenuAction('Stato collegamento stampa', print_status),
        MenuSeparator(),
        MenuAction('Cerca aggiornamenti', lambda: threading.Thread(target=updates, args=(window,), daemon=True).start()),
        MenuAction('Informazioni', lambda: message('Alpha Menu per Windows ' + VERSION + '\nAlpha System srl\nRichiede una connessione Internet.')),
        MenuAction('Esci', window.destroy),
    ])]

    def ready():
        if smoke:
            def complete(result):
                (DATA / 'smoke-test.json').write_text(json.dumps(result), encoding='utf-8')
                window.destroy()
            window.evaluate_js("""(async()=>{
                try {
                    const response=await fetch('http://127.0.0.1:17892/health',{signal:AbortSignal.timeout(8000)});
                    return {title:document.title,url:location.href,bridge:await response.json(),ok:response.ok};
                } catch(error) { return {error:String(error)}; }
            })()""", callback=complete)
        elif bridge_error:
            message('Stampa non disponibile: ' + bridge_error, True)

    window.events.loaded += ready
    try:
        webview.start(gui='edgechromium', private_mode=False, storage_path=str(DATA / ('SmokeProfile' if smoke else 'WebView')), menu=menu)
    except Exception:
        logging.exception('WebView startup failed')
        message('Impossibile aprire Alpha Menu. Installa Microsoft Edge WebView2 Runtime e riprova.\nDettagli: ' + str(DATA / 'app.log'), True)
    finally:
        if server:
            server.shutdown()
            server.server_close()
        ctypes.windll.kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.CloseHandle(mutex)


if __name__ == '__main__':
    main()
