"""Double-click launcher target; reuse only a verified loopback workbench."""
import json
from http.client import HTTPConnection
from pathlib import Path
import sys
from urllib.parse import urlsplit
import webbrowser


def existing_url(root):
    try:
        path = root / 'server.json'
        if path.stat().st_size > 4096:
            return None
        record = json.loads(path.read_text(encoding='utf-8'))
        parsed = urlsplit(record['url'])
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or
                parsed.path != '/' or parsed.query or parsed.fragment or
                parsed.username or parsed.password or not parsed.port):
            return None
        client = HTTPConnection('127.0.0.1', parsed.port, timeout=2)
        try:
            client.request('GET', '/api/bootstrap')
            response = client.getresponse()
            body = response.read(1024 * 1024)
            if response.status == 200 and json.loads(body).get('app_id') == 'local-analysis-workbench-v1':
                return record['url']
        finally:
            client.close()
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


if __name__ == '__main__':
    root = Path(__file__).resolve().parent / 'workspaces' / 'workbench'
    address = existing_url(root)
    if address:
        webbrowser.open(address)
    else:
        sys.argv = [sys.argv[0], '--open-browser']
        from tools.workbench_server import main
        main()
