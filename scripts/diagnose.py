"""Read-only local diagnostics. Never read API keys or send business documents."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import socket
import sys
from urllib.parse import urlparse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--network', action='store_true', help='Check the configured endpoint without a key or documents')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    print('OS:', platform.system(), platform.machine())
    print('Python:', platform.python_version())
    print('UTF-8 mode:', sys.flags.utf8_mode)
    print('Project files:', 'OK' if (root / 'pyproject.toml').exists() else 'MISSING')
    for package in ['fastapi', 'uvicorn', 'pydantic', 'httpx', 'openpyxl', 'pypdfium2', 'pillow']:
        try:
            print(package + ':', importlib.metadata.version(package))
        except importlib.metadata.PackageNotFoundError:
            print(package + ': MISSING; run uv sync --frozen')
    data = Path(os.environ.get('DATARA_DATA_DIR', root / 'data')).resolve()
    print('Data directory exists:', data.is_dir())
    print('Profile count:', len(list((data / 'profiles').glob('*.json'))))
    for variable in ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'SSL_CERT_FILE', 'SSL_CERT_DIR']:
        value = os.environ.get(variable) or os.environ.get(variable.lower())
        print(variable + ':', 'SET (value hidden)' if value else 'not set')
        if value and variable in {'SSL_CERT_FILE', 'SSL_CERT_DIR'}:
            print(variable + ' path exists:', Path(value).exists())
    with socket.socket() as sock:
        sock.settimeout(1)
        print('Local port 8765 reachable:', sock.connect_ex(('127.0.0.1', 8765)) == 0)
    if not args.network:
        print('Network check skipped. Add --network for a request without credentials/documents.')
        return
    try:
        import httpx
        from datara.provider import Connection, validate_connection
        settings = data / 'connection.json'
        c = Connection.model_validate_json(settings.read_text(encoding='utf-8')) if settings.exists() else Connection()
        validate_connection(c)
        print('Endpoint hostname:', urlparse(c.base_url).hostname)
        with httpx.Client(timeout=20, trust_env=True, follow_redirects=False) as client:
            response = client.get(c.base_url.rstrip('/') + '/models')
        print('HTTP:', response.status_code)
        print('TLS/HTTP response received. 401/403 can be expected without a key; this does not verify model access.')
    except Exception as error:
        # Do not print exception messages: proxy URLs may include credentials.
        chain, seen, cause = [], set(), error
        certificate_error = False
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            chain.append(type(cause).__name__)
            certificate_error |= 'CERTIFICATE_VERIFY_FAILED' in str(cause)
            cause = cause.__cause__ or cause.__context__
        print('Network check failed:', ' -> '.join(chain))
        if certificate_error:
            print('Certificate verification failed. Ask IT for a trusted PEM CA bundle and set SSL_CERT_FILE.')
        else:
            print('Check proxy, DNS, firewall, CA file and endpoint settings using CROSS_PLATFORM.zh-CN.md.')
        sys.exit(1)


if __name__ == '__main__':
    main()
